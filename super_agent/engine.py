"""Deterministic fast path. No LLM, no network side effects in observe mode.

One round does: read allow-listed events -> dedupe against persisted cursors ->
triage with fixed rules -> update the local index -> *maybe* dispatch one
action. Dispatch is the single guarded exit: everything it can do is counted
in ``external_actions``, and while ``dispatch_enabled`` is False it does
nothing at all.

Routing a ``[QA]`` verdict::

    comment c on thread T
    | first line [WK:name]  -> delivery (matches the worker's assignment)
    | first line [QA]       -> ALWAYS event.agent.project_pm, never a worker
    |                          a pass with no must-fix ALSO goes to the
    |                          supervisor (``event.agent.supervisor``), but
    |                          not once a PM verdict exists on this thread
    | first line [PM:TASK]  -> never a verdict: ignored, and one ``warn``
    |                          (``pm.task_book_conclusion``) when its own line
    |                          reads as a conclusion - a dispatch must not be
    |                          filed as an unreadable answer (ISS-85)
    |                          an escalation marker is the exception: it takes
    |                          the ``[PM]`` frame path, because a request for a
    |                          person must ring under either prefix (ISS-85 P1-2)
    | first line [PM]       -> three-state verdict, routed by state:
    |                          ready   -> event.agent.supervisor (release)
    |                          blocked -> the same frame, ``exception: true``
    |                          working -> no frame at all (silence)
    |                          unreadable / no coordinate -> treated as blocked
    | first line [AT]       -> three-state verdict of its **own** kind:
    |                          ready     -> event.agent.supervisor
    |                          questions -> the same frame with
    |                                       ``question_url`` (not an
    |                                       exception: a question is
    |                                       a stop, not a failure)
    |                          blocked   -> the same frame, ``exception: true``
    |                          unreadable / no coordinate -> treated as blocked
    + ordinary comment
       | waiting_review and a qa session is known -> send to qa
       | waiting_release / close_reconcile       -> logged, no route
       | a thread held there past ``engine.release_timeout_s`` -> supervisor
       | the thread belongs to a project         -> project_pm (slow path)
       + cross-project or unlisted               -> needs_person

A thread held in ``waiting_release``/``close_reconcile`` past
``engine.pm_stall_rounds`` rounds without a PM verdict gets one
``pm_stall`` exception frame **per hold** - the PM is told QA passed, and a
person is told the PM never answered. The default (15 rounds ≈ 15 min) sits
inside ``engine.release_timeout_s`` (1800s ≈ 30 rounds), so the two watchdogs
never fire on the same round. See :meth:`Engine._check_pm_stall`.

A release rings **once**, and the guard asks whether it has rung, not whether
a PM verdict exists right now: ``index["release_frames"]`` is keyed by the
delivery the frame judges (``release:<thread>:<delivery_cid>``), so the QA pass
and the PM answer to it - two comments about one delivery - produce one frame
across rounds and inside one batch. See :meth:`Engine._release_announced`.

``_dispatch`` refuses ``origin_role=qa`` with ``target_role=worker`` outright.
That gate covers **route decisions the engine makes itself**. It does not cover
a process that calls the session API on its own (``sess.py``, ``direct.send``),
a workbench script writing a comment, or ``_archive_one``, which runs after the
decision was made. Guarding the engine's decisions is not the same as
guarding authority - the boundary is stated here so nobody reads the gate as
more than it is.

A supervisor notice **is** consumed: ``decide_watch.py`` (ISS-55) subscribes to
``event.agent.supervisor`` and delivers ``payload.sid`` the text, with an
at-most-once ledger keyed by event fingerprint. ``event.agent.project_pm`` uses
the same leg and ledger, resolving the project PM before any ring injection.
So a frame sent here is a real delivery attempt, not a log line - an older
version of this docstring said the opposite and it was wrong.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import (at_serve, envelope, flow as flow_mod, github_source, health, issuekey,
               markers, needs, requirement_auth, requirements, session_broker,
               session_events, session_ledger, sessionkeys)
from . import assignments as asg_mod
from .engine_parts import at_routes as at_routes_mod
from .engine_parts import claims as walk_claims
from .engine_parts import fault_report as fault_report_mod
from .engine_parts import health_flag as health_flag_mod
from .engine_parts import frame_text as frame_text_mod
from .engine_parts import project_lookup as project_lookup_mod
from .engine_parts import source_faults as source_faults_mod
from .engine_parts.at_routes import AtRoutes
from .engine_parts import archive as archive_mod
from .engine_parts import at_ledger as at_ledger_mod
from .engine_parts import close as close_mod
from .engine_parts import closed_needs as closed_needs_mod
from .engine_parts import collect as collect_mod
from .engine_parts import consume as consume_mod
from .engine_parts import doctor as doctor_mod
from .engine_parts import delivery as delivery_mod
from .engine_parts import pause as read_pause_mod
from .engine_parts import probes
from .engine_parts import requirement_gate as requirement_gate_mod
from .engine_parts import retry as retry_mod
from .engine_parts import round as round_mod
from .engine_parts.review_routes import ReviewRoutes
from .engine_parts import session_truth as session_truth_mod
from .engine_parts.refusal_ledger import RefusedRoutes
from .engine_parts import session_keys as session_key_mod
from .engine_parts import triage as triage_mod
from .engine_parts import triage_batch as triage_batch_mod
from .engine_parts import watchdog as watchdog_mod
from .engine_parts import issue_state as issue_state_mod
from . import batch as batch_mod
from . import batch_queue as queue_mod
from .allowlist import ProjectAllowlist
from .store import Store, action_id

OBSERVE = "observe"

#: ``_triage`` state → envelope type (``envelope.TYPES``, #92 §A.1).
#: ``ignored``/``needs_person`` have no envelope type of their own: the message
#: that produced them already carried one (a task book is ``pm_task``, a
#: ``[blocked]`` note is ``escalation``), so the map falls back to the first
#: line. ``unknown`` is deliberately absent - ``""`` is what the refusal logs.
_ENVELOPE_TYPES = {"delivery": "delivery", "review": "review",
                   "pm_verdict": "pm_verdict", "pm_task": "pm_task",
                   "at_verdict": "at_verdict", "note": "note"}
# Where a ``[QA]`` verdict may find the assignment it judges. Each extra state
# is an **exit** the verdict has to be able to reach, not a looser reading of
# one:
#
# * ``waiting_release`` - the archive pass may run before the verdict exists, so
#   a delivery fully on record can already sit there when its verdict arrives.
# * ``waiting_person`` - an unreadable conclusion (``可过``) parks the delivery
#   here. Without this state the later, correct ``通过`` found no candidate and
#   became ``review.unmatched``: E59 caused the state, E58 made it terminal.
#   Only a newer, readable verdict leaves it (see
#   ``assignments._verdict_after_person``), so an unreadable replay still
#   parks.
#
# ``close_reconcile`` stays out: that state means the close-out was already
# judged unproven, so a pass arriving there would contradict the evidence chain
# and has to be a person's call. ``completed`` stays out for the same reason,
# with the state machine's "late event is audited, never a restart" on top.
REVIEW_TARGET_STATES = ("working", "waiting_review", "blocked",
                        "waiting_release", "waiting_person")

# A PM task book, dispatch or receipt says so on its first line: ``[PM:TASK]``.
# It is the same bracket and the same author as a verdict, and ``PM_PREFIX_RE``
# deliberately does **not** match it - there the character after ``PM`` is a
# colon, not a closing bracket. That is the whole point: the prefix states
# "this is a dispatch, not a state", so the engine never has to guess from the
# wording (#ISS-85).
#
# The spelling is matched generously - case, half- and full-width brackets, and
# both colons - because it is written by hand, and a prefix that only one
# spelling can use is a prefix half the dispatches will miss.
TASK_PREFIX_RE = triage_mod.TASK_PREFIX_RE

# A full session id: ``session_`` + a UUID. Anything shorter is a *truncated*
# id, and a truncated id can never be delivered to - the server answers 40401
# for it. The one copy lives in :mod:`super_agent.session_ledger`
# (``FULL_SID_RE``); this module reuses it instead of holding a second regex, so
# the shape cannot drift. Import direction is safe: ``session_ledger`` imports
# ``session_events``, and neither imports this module.
SID_RE = session_ledger.FULL_SID_RE



def _now() -> str:
    return probes.now()


def _now_ts() -> float:
    return time.time()


def _current_head() -> str:
    """Forwarder: :func:`engine_parts.probes.current_head`."""
    return probes.current_head()


def _code_version() -> tuple[str, bool | None]:
    """Forwarder: :func:`engine_parts.probes.code_version`."""
    return probes.code_version()


def _tail_events(path: Path, *, limit: int, max_bytes: int = 512 * 1024) -> list:
    """Forwarder: :func:`engine_parts.probes.tail_events`."""
    return probes.tail_events(path, limit=limit, max_bytes=max_bytes)


class Engine(AtRoutes, ReviewRoutes, RefusedRoutes):
    def __init__(self, cfg: dict, store: Store, source, bus=None, *, clock=_now_ts,
                 project_allowlist: ProjectAllowlist | None = None,
                 adapter=None, session_api=None):
        self.cfg = cfg
        self._approval_verifier = requirement_auth.supervisor_verifier(cfg)
        self.store = store
        self.source = source
        # Injected in tests; ``None`` means "use the direct console".
        self.adapter = adapter
        self.session_api = session_api
        self.bus = bus
        self.clock = clock
        self.started_at = _now()
        self.code_commit, self.worktree_dirty = _code_version()
        self._bus_last_error = ""
        self.mode = str(cfg.get("mode") or OBSERVE)
        # Machine-checkable switch. Observe => always False. A test flips this
        # attribute to prove the external-action counter is not hard-wired to 0.
        self.dispatch_enabled = self.mode != OBSERVE
        self.role = str(cfg.get("role") or "")
        engine_cfg = cfg.get("engine") or {}
        # Event-type allowlist. The project/issue/action allowlist is the other
        # dimension and lives in self.projects (see super_agent.allowlist).
        self.allowlist = set(engine_cfg.get("allowlist") or [])
        self.cooldown_s = float(engine_cfg.get("cooldown_s") or 0)
        self.markers = [s.lower() for s in
                        (engine_cfg.get("escalation_markers") or ["[decision]", "[blocked]"])]
        self.creation_mode = str(engine_cfg.get("creation_mode") or "pm_created")
        # Ask the server whether a candidate sid is archived. On by default:
        # an API-side archive writes no ``session.archived`` row, so the stream
        # alone cannot tell that a session is closed. Tests that do not inject a
        # session API turn this off to stay off the network; off means
        # stream-only (the historical behaviour).
        self.archived_crosscheck = engine_cfg.get("archived_crosscheck") is not False
        self.projects = project_allowlist
        if self.projects is None:
            path = str(engine_cfg.get("project_allowlist") or "").strip()
            if path:
                # The last-good cache belongs to run state, not next to the
                # tracked config file.
                self.projects = ProjectAllowlist(
                    path, cache_path=self.store.root / "allowlist.last-good")
        self._recovered = False
        self.last_successful_action_at: str | None = None
        # Set when the session stream cannot be read: no assignment is created
        # and health reports degraded (fail closed, never an empty loop).
        # Round-scoped since #142 (see engine_parts.health_flag).
        self.degraded = False
        self._assignments_error = ""
        # Set when a **declared** session stream has no segment on disk
        # (``_note_stream_state``). Round-scoped: cleared at the top of every
        # round, so a fixed stream does not carry yesterday's reason into
        # today's health snapshot.
        self._stream_error = ""
        # Set when index.json cannot be read (see ``_note_index_state``): the
        # round keeps running so the fault reaches health and the event log.
        self._index_error = ""
        # Last count from a trustworthy index: an unreadable one reads 0, and
        # 0 reported as truth says "nothing in flight".
        self._active_assignments_last = 0
        # One skipped-write event per round, not one per save site.
        self._index_skip_logged = False
        self._archived_cache: dict | None = None
        self._archived_unreadable = False
        self._probe_fault = ""  # why the session probe could not answer
        self._probe_fatal = False
        # Archive refusals already written to ``events.jsonl``, read once on
        # first use (see ``_note_archived_sid``). Round-scoped state would be
        # the bug: the whole point is that round 2 adds no second line.
        self._archived_noted: set | None = None
        # sid -> archived, asked from the server once per round (see
        # ``_live_archived``); reset at the start of every round.
        self._live_check: dict[str, bool] = {}
        # Set the first time the cross-check being off changes an answer (see
        # ``_live_archived``): "off" is otherwise invisible in the event stream.
        self._crosscheck_off_logged = False
        # Keys whose supervisor record carries no decision: logged once each,
        # so a retry loop does not repeat the same warning every round.
        self._undecidable_logged: set[str] = set()
        # ``route.supervisor_undeliverable`` notices already written, keyed by
        # thread+comment (or thread+assignment on the reminder path).
        self._sup_notice_logged: set[str] = set()
        # Threads whose PM verdict is in the batch being read right now. Round
        # scoped: a comment is triaged in id order, so a QA pass is seen before
        # the PM answer to it, and the double-ring guard has to know that the
        # answer is already in hand (see ``_pm_verdict_ahead``).
        self._pm_pending: set[str] = set()
        # ``release.key_unlinked`` notices already written, keyed by
        # thread+comment: the miss is a warning to look at, not one line per
        # round.
        self._release_key_missed: set[str] = set()
        # Replayed lifecycle state, read once per round by the key whitelist
        # (``_session_state``). Round scoped: a release or an archive performed
        # during the round must be visible to the next one.
        self._session_state_cache: dict | None = None
        # ``(stamp, map, problem)`` for ``sessions.json``, shared by the five
        # readers (see ``_session_map``). Round scoped: a bind made during the
        # round must be visible to the next one.
        self._session_map_cache: tuple | None = None
        # The current round's collection state (see ``engine_parts.round``).
        self._round_state = None
        # Closed-only terminal memo (#134 step 5); see engine_parts/issue_state.
        self._closed_states = issue_state_mod.ClosedStates()

    # ------------------------------------------------------------ allowlist
    def allowlist_allows(self, issue: int | None, action: str | None) -> tuple[bool, str]:
        if self.projects is None:
            return True, "no project allowlist configured"
        return self.projects.check(self._project_for(issue), issue, action)

    def _thread_key(self, issue) -> str:
        """Qualified thread key for ``issue`` (falls back to the bare number)."""
        repo = str((self.cfg.get("github") or {}).get("repo") or "")
        if not issuekey.is_repo(repo):
            entry = self.projects.project(self._project_for(issue)) if self.projects else None
            repo = str((entry or {}).get("repo") or "")
        return issuekey.thread_key(repo, issue)

    def _project_for(self, issue) -> str | None:
        """Project that owns ``issue``; see :mod:`.engine_parts.project_lookup`."""
        if self.projects is None or self.projects.data is None:
            return None
        return project_lookup_mod.project_for(
            self.projects.data["projects"], issue,
            str((self.cfg.get("github") or {}).get("repo") or ""))

    def _flow_selection(self, issue) -> tuple[str, str]:
        entry = self.projects.project(self._project_for(issue)) if self.projects else None
        default_at = bool((entry or {}).get("at_mode", False))
        try:
            selected = self.source.flow(issue) if callable(
                getattr(self.source, "flow", None)) else ""
        except flow_mod.FlowError as exc:
            return "invalid", str(exc)
        if selected == flow_mod.AT:
            return flow_mod.AT, ""
        if selected == flow_mod.STANDARD:
            return flow_mod.STANDARD, ""
        return (flow_mod.AT if default_at else flow_mod.STANDARD), ""

    def _at_mode(self, issue) -> bool:
        selected, _error = self._flow_selection(issue)
        return selected == flow_mod.AT

    @staticmethod
    def _requirement_projection(index: dict) -> dict:
        projection = index.get("requirements")
        if not isinstance(projection, dict):
            projection = {"schema_version": requirements.SCHEMA_VERSION,
                          "requirements": {}, "seen_comment_ids": [], "errors": []}
            index["requirements"] = projection
        return projection

    def _apply_requirement_comments(self, index: dict, thread: str,
                                    comments: list[dict],
                                    history_complete: bool = True) -> dict | None:
        """Rebuild the projection for ``thread`` from a **complete** history walk.

        ``history_complete`` defaults to True because the only caller that may
        pass rows here has already checked the walk; it is a parameter so a
        future caller cannot inherit the claim by accident.
        """
        projection = requirements.project_comments(
            comments, self._requirement_projection(index),
            package_verifier=lambda comment: self._at_verified(thread, comment),
            approval_verifier=self._supervisor_verified,
            history_read_complete=history_complete,
            history_scope=thread)
        index["requirements"] = projection
        return requirements.current(projection, thread)

    @staticmethod
    def _requirement_current(index: dict, thread: str) -> dict | None:
        projection = index.get("requirements") or {}
        return requirements.current(projection, thread)

    def _requirement_event_error(self, index: dict, thread: str, issue: int,
                                 comment: dict, assignment: dict | None = None) -> str:
        if not self._at_mode(issue):
            return ""
        reference = requirements.event_reference(str(comment.get("body") or ""))
        if reference is None:
            return "missing or ambiguous requirement revision marker"
        package = self._requirement_current(index, thread)
        if not package or str(package.get("status")) not in ("approved", "dispatched"):
            return "current requirement revision is not approved"
        if not requirements.reference_matches(reference, package):
            return "event belongs to a stale requirement revision"
        if assignment is not None and not requirements.reference_matches(
                reference, assignment):
            return "event and Assignment requirement revisions differ"
        return ""

    def _referenced_assignment(self, index: dict, thread: str, issue: int,
                               comment: dict, name: str = "") -> tuple[dict | None, str]:
        error = self._requirement_event_error(index, thread, issue, comment)
        if error:
            return None, error
        reference = requirements.event_reference(str(comment.get("body") or ""))
        candidates = []
        for rec in (index.get("assignments") or {}).values():
            if str(rec.get("thread") or "") != thread:
                continue
            if str(rec.get("role") or "") != "worker":
                continue
            if str(rec.get("status") or "") in asg_mod.TERMINAL_STATES + (
                    "waiting_requirement",):
                continue
            if name and str(rec.get("session_key") or "") != str(name):
                continue
            if requirements.reference_matches(reference, rec):
                candidates.append(rec)
        if not candidates:
            return None, "no active Assignment matches the requirement reference"
        if name == "" and len(candidates) > 1:
            candidates.sort(key=lambda rec: int(rec.get("delivery_comment_id") or 0),
                            reverse=True)
        return candidates[0], ""

    def _sync_allowlist_issues(self, issue_numbers, summary: dict) -> dict:
        """Keep ``allowlist.issues`` equal to the issues the source returned.

        The source has already dropped held and closed issues, so this list is
        "what is live right now". Only the project that owns the polled
        repository is touched - a second project's numbers are somebody else's
        state and this engine cannot see them.

        A write failure is reported into ``summary["warnings"]`` and the round
        carries on: the allowlist is a guard, and a guard that cannot be
        refreshed must not stop the work it guards.
        """
        repo = str((self.cfg.get("github") or {}).get("repo") or "")
        project = self._project_for_repo(repo)
        if self.projects is None or project is None:
            return {"skipped": "no project owns this repository"}
        result = self.projects.sync_issues(project, issue_numbers)
        if result.get("error"):
            summary.setdefault("warnings", []).append(
                f"allowlist.sync: {result['error']}")
            self.store.log_event({"type": "allowlist.sync_failed", "at": _now(),
                                  "project": project, "error": result["error"],
                                  "level": "warn", "consumers": ["doctor"]})
        elif result.get("changed"):
            self.store.log_event({"type": "allowlist.synced", "at": _now(),
                                  "project": project,
                                  "issues": sorted(int(n) for n in (issue_numbers or []))})
        return result

    # ------------------------------------------------------------------ health
    def _note_index_state(self) -> str:
        """Forwarder: see :func:`engine_parts.health_flag.note_index_state`."""
        return health_flag_mod.note_index_state(self, _now())

    def health_record(self, *, error: str = "", degraded: bool = False,
                      running: bool = True, phase: str = "") -> dict:
        index = self.store.load_index()
        active = len(asg_mod.active_assignments(index))
        if not self._index_error:
            # Only a trustworthy index may move this baseline: an unreadable
            # one reads 0, and 0 reported as truth is "nothing in flight".
            self._active_assignments_last = active
        else:
            active = self._active_assignments_last
        # ``_stream_error`` is part of the record, not just of the round's own
        # error list: the last write of a process (``cli once`` and the stop
        # write of ``cli run``) passes no error, and leaving the stream fault
        # out of here made that write flip a degraded snapshot back to healthy
        # (ISS-79 P1-1). Duplicates are dropped: the round joins its own error
        # list in, and the same reason must not appear twice.
        reasons: list[str] = []
        for reason in (error, self._index_error, self._stream_error,
                       self._bus_last_error):
            if reason and reason not in reasons:
                reasons.append(reason)
        last_error = "; ".join(reasons)
        record = {"pid": os.getpid(), "mode": self.mode,
                  "last_loop_at": _now(),
                  "last_successful_action_at": self.last_successful_action_at,
                  "active_assignments": active,
                  "degraded": bool(degraded or error or self._index_error
                                   or self._stream_error or self._bus_last_error),
                  "last_error": last_error,
                  "running": bool(running),
                  "started_at": self.started_at, "code_commit": self.code_commit,
                  "worktree_dirty": self.worktree_dirty,
                  "bus_last_error": self._bus_last_error}
        if phase:
            record["phase"] = phase
        return record

    def _capture_bus_error(self) -> None:
        self._bus_last_error = ""
        probe = getattr(self.bus, "healthz", None)
        if not callable(probe):
            return
        try:
            snap = probe()
        except Exception as exc:  # noqa: BLE001 - health must retain the fault
            self._bus_last_error = f"bus: {type(exc).__name__}: {exc}"
            return
        error = str((snap or {}).get("error") or "")
        if error or (snap and not snap.get("ok")):
            self._bus_last_error = error or "bus: not ok"

    def write_health(self, *, error: str = "", degraded: bool = False,
                     running: bool = True, phase: str = "") -> dict:
        self._capture_bus_error()
        self.code_commit, self.worktree_dirty = _code_version()
        return health.write(self.store.health_path,
                            self.health_record(error=error, degraded=degraded,
                                               running=running, phase=phase))

    def _skip_write_back(self) -> None:
        """Record that this round wrote no index, once per round.

        The only trace a skipped write leaves: the mtime stops moving, which
        looks the same as a round that changed nothing.
        """
        if self._index_skip_logged:
            return
        self._index_skip_logged = True
        self.store.log_event({"type": "index.write_back_skipped", "at": _now(),
                              "error": self._index_error,
                              "quarantined": self.store.index_quarantined,
                              "level": "warn",
                              "consumers": ["doctor", "health"]})

    # ------------------------------------------------------------------ state
    def _cooldown_ok(self, issue_state: dict, kind: str, now: float) -> bool:
        last = (issue_state.get("cooldowns") or {}).get(kind)
        if not last:
            return True
        try:
            return (now - float(last)) >= self.cooldown_s
        except (TypeError, ValueError):
            return True

    def _mark_cooldown(self, issue_state: dict, kind: str, now: float) -> None:
        issue_state.setdefault("cooldowns", {})[kind] = now

    @staticmethod
    def _action_name(action: dict) -> str:
        """Allowlist action name: explicit ``action``, else the action kind."""
        explicit = action.get("action")
        return str(explicit) if explicit else str(action.get("kind") or "")

    def _triage(self, comment: dict) -> tuple[str, str]:
        """Fixed rules, in one order (see :func:`engine_parts.triage.triage`).

        Forwarder: the classifier lives in :mod:`super_agent.engine_parts.triage`
        so the whole order is readable in one place. The escalation markers are
        configuration, so they are passed in rather than read here.
        """
        return triage_mod.triage(
            comment, role=self.role, escalation=self._escalation_marker,
            log_task_conclusion=lambda c: self.store.log_event({
                "type": "pm.task_book_conclusion", "at": _now(),
                "comment_id": c.get("id"), "level": "warn",
                "consumers": ["doctor"],
                "reason": "a [PM:TASK] line reads as a conclusion; "
                          "use [PM] + verdict marker"}))

    def _envelope(self, thread: str, comment: dict, state: str) -> dict:
        """The message's envelope, built from the whitelist - **not from its body**.

        This is the production entry point for :mod:`super_agent.envelope`
        (#92 §A.4／§D.3). It runs once per triaged comment, inside the round,
        and returns the snapshot that :meth:`_record_envelope` logs.

        ``from_role`` comes from :meth:`_verified_role`, which asks the four
        session-key whitelists. **The body is never read for identity**: a
        comment that writes ``[WK:<somebody else's key>]`` names that key in
        ``claimed_name`` and still gets ``from_role=""``, because the whitelist
        said no (§D.2). That is the failure ``DELIVERY_RE``'s
        ``([^\\]\\n]{1,64})`` admits and this line refuses.

        A ``type`` the engine did not recognise (``logged`` with a bracketed
        first line) maps to ``""``, which :func:`envelope.validate` refuses; the
        caller logs ``envelope.unknown`` instead of pretending it read it.

        Nothing here writes: the envelope is a runtime snapshot, never a second
        source of truth (§C.4).
        """
        body = str(comment.get("body") or "")
        repo, issue = issuekey.split_key(thread)
        bare = issuekey.is_legacy(thread)          # ``"42"``: no repo configured
        env = envelope.legacy_map(
            body, repo=repo, issue=issue if issue is not None else 0,
            cid=int(comment.get("id") or 0),
            thread="" if bare else thread,
            thread_scope="none" if bare else "issue",
            from_role=self._verified_role(thread, comment))
        if bare:
            # A legacy numeric thread names no repository, so it has no
            # ``owner/repo#N`` to validate against. It is still an issue-shaped
            # message, so it carries the coordinate in ``project`` rather than
            # pretending ``thread`` can hold a bare number (§附).
            env["project"] = self._project_for(issue) or ""
        # ``legacy_map`` answers from the first line; the engine's own verdict
        # wins where the two differ, because the engine is what routes. A
        # ``logged`` line that opens with a bracketed marker is the one shape
        # the map must not call "note": it is an unknown marker (§C.3).
        if state == "logged" and envelope.looks_like_marker(
                markers.first_line(body)):
            env["type"] = ""
        elif env.get("type") == "note" and state not in ("logged", ""):
            env["type"] = _ENVELOPE_TYPES.get(state, env["type"])
        return env

    def _verified_role(self, thread: str, comment: dict) -> str:
        """``from_role`` from the whitelists, or ``""`` when unverified.

        Four bindings, and their agreement is the answer: a comment cannot be a
        QA and a PM at once. A delivery is checked against the thread's worker
        binding (``issue_sessions.json[thread]["worker"]``), the same record the
        dispatch wrote; a comment that was never bound is ``""``, which is a
        meaning - "unverified" - and never falls back to the marker letter
        (#92 §D.2), because that fallback would let a forged ``[WK:…]`` certify
        itself.
        """
        claimed = str(comment.get("session_key") or "")
        for verified in (self._pm_verified(thread, comment),
                         self._review_verified(thread, comment),
                         self._at_verified(thread, comment),
                         self._delivery_verified(thread, comment)):
            if verified:
                return envelope.from_role_of(claimed, "", verified=True)
        return ""

    def _delivery_verified(self, thread: str, comment: dict) -> bool:
        """Is this comment from the worker bound to this thread?

        ``issue_sessions.json`` is written by ``gh_issue.py bind --bind-role
        worker``, which names the session - so the comparison is name-to-name
        against the comment's own ``<!-- session: … -->`` line, never against
        the text of its first line. No binding answers ``False``: an unbound
        thread is not a licence to trust whatever marker happens to be there.
        """
        entry = self._issue_session_entry(thread).get("worker")
        if not isinstance(entry, dict):
            return False
        bound = str(entry.get("name") or "")
        claimed = str(comment.get("session_key") or "")
        return bool(bound and claimed and claimed == bound)

    def _record_envelope(self, thread: str, comment: dict, state: str) -> None:
        """Log the envelope, or the refusal that stopped it - one event either way.

        A refusal is **visible**, which is the whole point: before this, a
        malformed message was indistinguishable from a healthy one
        (#92 §A.5 反例 2／3). The event carries no body and no ``claimed_name``
        - a key name in a log is still a credential (RULES §37).
        """
        env = self._envelope(thread, comment, state)
        if not str(env.get("type") or ""):
            marker = env.get("unknown_marker") or {}
            repo, issue = issuekey.split_key(thread)
            self.store.log_event({
                "type": "envelope.unknown", "at": _now(), "repo": repo,
                "issue": issue if issue is not None else 0,
                "comment_id": int(comment.get("id") or 0),
                # Comes from the envelope the map built, so the line is hashed
                # once and the body is never re-read here (#92 P2-1).
                "first_line_sha1": marker.get("first_line_sha1")
                or envelope.first_line_sha1(str(comment.get("body") or "")),
            })
            return
        ok, reason = envelope.validate(env)
        if not ok:
            self.store.log_event(envelope.rejected_event(env, reason))
            return
        self.store.log_event({
            "type": "envelope.built", "at": env.get("created_at"),
            "message_id": env["message_id"], "kind": env["type"],
            "thread": env["thread"], "thread_scope": env["thread_scope"],
            "from_role": env["from_role"], "assignment_id": env["assignment_id"],
            "comment_id": (env.get("payload_ref") or {}).get("cid"),
            "receipt_required": env["receipt_required"],
            "steer_required": env["steer_required"],
        })

    def _escalation_marker(self, body: str, *, first_line_only: bool = True) -> str:
        """The first configured escalation marker, or ``""``.

        ``first_line_only`` defaults to True and is what the ``[PM]`` branch
        uses: the marker must open the comment, the same contract
        ``parse_delivery``, ``parse_review`` and ``parse_pm_verdict`` follow.
        Scanning the whole body was the round-3 P0 - a task book that
        *explains* the markers rang the exception bell every round for 29
        rounds, the exact shape this issue exists to remove. A comment that
        quotes a marker is discussion; one that opens with it is a request.

        The legacy fallthrough passes ``False``: there the whole-body scan is
        the behaviour every other author already had, and narrowing it would
        trade one silence for a different one outside this issue.

        **2026-11-25 fix: the five-part action word counts too.** The charters
        require every role comment to open with
        ``[who]-[do]-[notify]-[project]-[cid]`` (``session_contract.md`` §6),
        and ``告警`` is one of the twelve legal actions. Before this fix the
        PM escalated with ``--do 告警`` and the engine saw nothing: no verdict
        marker (so ``state=""``), no configured marker (so ``escalated=""``),
        therefore no frame and no ``pm.escalation`` row. A request for a
        supervisor sat in GitHub for 82 minutes while the supervisor waited.
        Now a first line whose action is ``告警`` rings the same bell as
        ``[decision]``/``[blocked]``; ``交接要求`` is deliberately excluded
        because it names a peer handoff, not a person's decision.
        """
        text = str(body or "")
        if first_line_only:
            text = markers.first_line(text)
            five = markers.parse_five(text)
            if five and five.get("action_ok") and five.get("do") == "告警":
                return "[告警]"
        lowered = text.lower()
        for marker in self.markers:
            if marker and marker in lowered:
                return marker
        return ""

    # ------------------------------------------------------------ qa routing
    def _session_map_problem(self, where: str, problem: str) -> None:
        """Forwarder: :func:`engine_parts.session_truth.map_problem_row`.

        The three key tables used to return their explicit-binding answer and
        say nothing, which made "the map could not be read" and "nobody has a
        key" the same value (#130 QA P1-3). Addendum 2's rule is that the fault
        must be observable, so it is logged exactly where it is discovered.
        """
        row = session_truth_mod.map_problem_row(where, problem, _now())
        if row is not None:
            self.store.log_event(row)

    def _qa_session_keys(self, thread: str) -> set[str]:
        """Session keys that count as *this thread's* QA.

        Two sources, unioned so a rotation does not lose the check:

        * the ``qa`` entry of ``issue_sessions.json[thread]`` (``name`` and
          ``session``) - an explicit binding, and the only source that can say
          "this thread's QA" without naming a project;
        * a ``qa-*`` key in ``sessions.json`` **that carries a random tail**,
          still live (not archived, not released). The tail is the credential
          (``sessionkeys.has_random_suffix``): the name is written in the
          comment body and every role shares one token, so a guessable name is
          not a check. A short-lived key of this project (``qa-<project>-...``)
          is preferred, but any random-tailed ``qa-*`` key counts - QA sessions
          are one per issue and are named after the issue, not the project.

        What this does fix: a key name that can be **constructed** from public
        information (a project name, a date) no longer verifies. What it does
        not: a leaked random key is still a key. That is the design ceiling -
        the entropy is the gate, not the prefix.

        The verdict's author login cannot be used: every role shares one token,
        so the login is the same for a rejection and for a forgery. This is a
        weak check and it is treated as one: a miss downgrades the verdict, it
        never blocks it.
        """
        keys: set[str] = set()
        ledger = self._load_issue_sessions().get(thread) or {}
        if isinstance(ledger, dict):
            keys |= session_key_mod.ledger_keys(ledger, "qa")
        data, problem = self._session_map()
        if problem:
            # An unreadable map is not an empty one: the explicit binding above
            # still answers, and the fault is reported rather than folded into
            # "this thread has no QA key" (ISS-130 addendum 2).
            self._session_map_problem("_qa_session_keys", problem)
            return keys
        state = self._session_state()
        keys |= session_key_mod.keys_from_map(
            data, "qa", released=state.get("released") or (),
            sessions=state.get("sessions") or {})
        return keys

    def _review_verified(self, thread: str, comment: dict) -> bool:
        """Does this comment come from a QA key of this thread?

        A comment that names its session (``session_key``) is checked against
        the whitelist exactly. Without that field the answer is **False**, and
        that is the whole point of issue #87: "the thread has a QA registered
        at all" was true for every comment on a thread with a QA, so the check
        could not fail and was not a check. It is logged as unverified when the
        answer is False.

        A miss does **not** block the verdict: the caller records the warning
        and still moves the assignment, because the shared login makes a key
        mismatch a reason to look, not proof of a forgery.
        """
        keys = self._qa_session_keys(thread)
        claimed = str(comment.get("session_key") or "")
        if claimed:
            return claimed in keys
        return False

    def _thread_scopes(self, thread: str) -> set[str]:
        """The names a key of ``thread`` may be scoped by (lower case).

        Two spellings of the *same* thread: the allowlist project that lists
        the issue, and the repository's short name. A PM key is named
        ``pm-<scope>`` or ``pm-<scope>-<suffix>``; the QA's reproduction used
        seven real ``pm-*`` keys against ``owner/demo#1``, and none of them is
        scoped by ``demo``, which is the property this function exists for.
        """
        scopes: set[str] = set()
        repo, number = issuekey.split_key(thread)
        if repo:
            scopes.add(repo.rsplit("/", 1)[-1])
        if number is None:
            return scopes
        project = (self._project_for_repo(repo) if repo
                   else self._project_for(number))
        if project:
            scopes.add(str(project).lower())
        return {s for s in scopes if s and not s.isdigit()}

    def _pm_session_keys(self, thread: str) -> set[str]:
        """Session keys that count as *this thread's* PM.

        Two sources, and the second has two conditions (issue #87 P0-1):

        * the ``pm`` entry of ``issue_sessions.json[thread]`` - the explicit
          binding, and the only source that can express "this thread's PM";
        * a rotation fallback: ``pm-*`` keys in ``sessions.json`` whose name
          **carries this thread's repo or project** *and* ends in a **random
          tail** (``sessionkeys.has_random_suffix``), still live (not archived,
          not released).

        Why the tail and not just the scope: the scope is public. Anyone who
        can read the repository name can spell ``pm-<repo>`` - the QA's
        reproduction did exactly that with three published keys on twenty
        threads. The prefix says *which* project; the random tail is what says
        *a key was actually issued*. ``pm-demo-20261116`` is rejected even
        though it looks like a tail, because a date is guessable.

        **What this does not fix.** A key name is still written in a comment
        body, so a *leaked* random key still verifies. That is the ceiling of a
        name-as-credential design: the gate is the entropy, not the prefix. The
        document rule (§37) keeps the name out of issues so the leak does not
        happen by accident.

        The author login cannot be used either: every role shares one token, so
        a forgery and a real verdict carry the same login.
        """
        keys: set[str] = set()
        entry = self._issue_session_entry(thread).get("pm")
        if isinstance(entry, dict):
            for field in ("name", "session"):
                value = str(entry.get(field) or "")
                if value:
                    keys.add(value)
        scopes = self._thread_scopes(thread)
        if not scopes:
            return keys
        data, problem = self._session_map()
        if problem:
            self._session_map_problem("_pm_session_keys", problem)
            return keys
        state = self._session_state()
        keys |= session_key_mod.keys_from_map(
            data, "pm", released=state.get("released") or (),
            sessions=state.get("sessions") or {},
            accept=lambda name, sid: any(
                sessionkeys.scoped_by(name, "pm", scope) for scope in scopes))
        return keys

    def _pm_verified(self, thread: str, comment: dict) -> bool:
        """Does this comment come from a PM key of this thread?

        A comment that names its session (``session_key``) is checked against
        the whitelist exactly. Without that field the answer is **False**:
        "the thread has a PM registered at all" was true for every comment on
        a thread with a PM, so it could never fail and never checked anything
        (issue #87).

        Unlike the QA side, a miss here **does not let the verdict through**:
        a QA verdict that cannot be attributed still moves an assignment a
        person can see, while a PM ``ready`` rings the release bell, which is
        the one action nobody downstream can take back.
        """
        keys = self._pm_session_keys(thread)
        claimed = str(comment.get("session_key") or "")
        if claimed:
            return claimed in keys
        return False

    def _escalation_from_supervisor(self, index: dict | None,
                                    comment: dict) -> bool:
        """Would this escalation frame ring the session that wrote it?

        A supervisor's own ``[告警]`` used to ring itself and leave a
        ``needs_person`` row nobody could resolve (#140). The whole decision -
        key, map, comparison - is in
        :func:`engine_parts.session_keys.escalation_is_self_authored`.
        """
        return session_key_mod.escalation_is_self_authored(
            self._supervisor_sid(index), comment, self.cfg,
            session_map=lambda path: self._session_map(path))

    # ------------------------------------------------------------- at routing
    def _at_session_keys(self, thread: str) -> set[str]:
        """Session keys that count as *this thread's* AT.

        The same two sources as the QA and PM checks, unioned so a rotation
        does not lose the check: the ``at`` entry of
        ``issue_sessions.json[thread]`` (``name`` and ``session``), and every
        ``at-*`` key in ``sessions.json`` **that passes the same two filters
        the other two roles apply** (ISS-88):

        * a **random tail** (``sessionkeys.has_random_suffix``) - the readable
          prefix says which role, the tail is what says a key was issued;
        * **still live** - not released, and its session not archived.

        The prefix is ``at-`` and not ``at``: a bare ``at`` prefix also claims
        keys that merely start with those two letters, and this check decides
        whether a questionnaire link is put in front of a person.

        Why the two filters matter here rather than only on the other two
        routes: this list feeds :meth:`_at_verified`, whose no-``session_key``
        branch answers "the thread has an AT at all". A guessable or retired
        ``at-*`` name kept in ``sessions.json`` would make that answer true for
        a key nobody issued - the same hole #87 closed for PM and QA, left open
        on this route because that issue scoped itself to those two.
        """
        keys: set[str] = set()
        ledger = self._issue_session_entry(thread)
        if isinstance(ledger, dict):
            at_entry = ledger.get("at")
            bound_sid = str((at_entry or {}).get("session") or "") \
                if isinstance(at_entry, dict) else ""
            if not (bound_sid and self._sid_archived(bound_sid)):
                keys |= session_key_mod.ledger_keys({"at": at_entry}, "at")
        data, problem = self._session_map()
        if problem:
            self._session_map_problem("_at_session_keys", problem)
            return keys
        state = self._session_state()
        keys |= session_key_mod.keys_from_map(
            data, "at", released=state.get("released") or (),
            sessions=state.get("sessions") or {},
            accept=lambda name, sid: not (sid and getattr(self, "session_api", None)
                                          is not None and self._live_archived(sid)))
        return keys

    def _at_sid(self, thread: str) -> str:
        """The AT session a questionnaire answer is sent back to, or ``""``.

        **Only this thread's own binding counts** - the ``at`` entry of
        ``issue_sessions.json[thread]``. There is deliberately **no** fallback
        to "the first ``at-*`` key in ``sessions.json``".

        That fallback looked harmless ("there is usually one AT") and is not:
        it answers *an* AT, not *this thread's* AT, so on a machine working two
        projects the answer to project A's question is delivered into project
        B's session. A wrong recipient is the one failure a person cannot see -
        the question was filed, the page said "submitted", the answer went
        somewhere. ``""`` instead makes the questionnaire say "no AT session to
        answer", which is visible, actionable, and true.

        Failing closed also keeps the rule the rest of the engine already
        follows: no coordinate, no verdict; no attribution, no frame.

        The binding is looked up through :meth:`_issue_session_keys`, so a
        record written by ``gh_issue.bind`` under the workbench's
        ``<project>#<n>`` spelling is found by the engine's
        ``<owner>/<repo>#<n>`` thread without either side changing its format.
        """
        entry = self._issue_session_entry(thread).get("at")
        if isinstance(entry, dict) and str(entry.get("session") or ""):
            sid = str(entry["session"])
            return "" if self._sid_archived(sid) else sid
        return ""

    def _at_verified(self, thread: str, comment: dict) -> bool:
        """Does this comment come from an AT key of this thread?

        The same rule as :meth:`_pm_verified`: a named ``session_key`` is
        checked against the whitelist exactly, and **without that field the
        answer is False**.

        It used to answer ``bool(keys)`` - "the thread has an AT at all" - which
        is true for every comment on a thread that has one, so it could not
        fail and was not a check (ISS-88; the same defect #87 fixed for PM and
        QA). A ``questions`` frame puts a link in front of a person and asks
        them to answer it, so an unattributed one is a phishing shape, not just
        a loose end: it must not pass on "some AT exists".
        """
        keys = self._at_session_keys(thread)
        claimed = str(comment.get("session_key") or "")
        if claimed:
            return claimed in keys
        return False

    def _supervisor_verified(self, comment: dict) -> bool:
        return self._approval_verifier(comment)

    def _pm_target(self, thread: str, issue: int) -> dict:
        """Resolve the issue PM, else the current project PM; never a blank OK."""
        ledger = self._issue_session_entry(thread)
        entry = ledger.get("pm") if isinstance(ledger, dict) else None
        sid = str((entry or {}).get("session") or "") if isinstance(entry, dict) else ""
        repo, _number = issuekey.split_key(thread)
        project = self._project_for_repo(repo) or self._project_for(issue) or ""
        if not sid:
            data, problem = self._session_map()
            if problem:
                # No explicit binding **and** an unreadable map: the answer is
                # "no PM", stated as a fault rather than as an empty table.
                self.store.log_event({
                    "type": "session_map.unreadable", "at": _now(),
                    "where": "_pm_target", "detail": problem})
            scope = repo.rsplit("/", 1)[-1].lower() if repo else project
            key = sessionkeys.preferred_key(data, "pm", scope)
            entry = (data.get(key) or {}) if key else {}
            sid = str(entry.get("id") or entry.get("session") or "")
        return {"project": project, "pm_sid": sid, "target_role": "pm"}

    def _review_target(self, index: dict, thread: str, issue: int,
                       comment: dict) -> tuple[dict | None, str]:
        """Worker delivery under review, or the exact QA-only Assignment.

        Worker candidates always win, preserving the normal flow. The fallback
        requires the comment's session key to name the QA Assignment; an
        arbitrary QA comment cannot commandeer another QA's stage.
        """
        assignment = None
        requirement_error = ""
        at_mode = self._at_mode(issue)
        if at_mode:
            assignment, requirement_error = self._referenced_assignment(
                index, thread, issue, comment)
            if assignment is not None:
                return assignment, ""
            if requirement_error != "no active Assignment matches the requirement reference":
                return None, requirement_error
        candidates = [rec for rec in (index.get("assignments") or {}).values()
                      if str(rec.get("thread") or "") == thread
                      and str(rec.get("role") or "") == "worker"
                      and rec.get("delivery_comment_id") is not None
                      and str(rec.get("status") or "") in REVIEW_TARGET_STATES]
        if candidates:
            return max(candidates,
                       key=lambda r: int(r.get("delivery_comment_id") or 0)), ""
        reviewer = str(comment.get("session_key") or "")
        reference = requirements.event_reference(str(comment.get("body") or ""))
        candidates = []
        for rec in (index.get("assignments") or {}).values():
            if str(rec.get("thread") or "") != thread:
                continue
            if str(rec.get("role") or "") != "qa":
                continue
            if str(rec.get("session_key") or "") != reviewer:
                continue
            if str(rec.get("status") or "") not in REVIEW_TARGET_STATES:
                continue
            if at_mode and not requirements.reference_matches(reference, rec):
                continue
            candidates.append(rec)
        if len(candidates) == 1:
            return candidates[0], ""
        if len(candidates) > 1:
            return None, "multiple QA Assignments match the review"
        return None, requirement_error or "no active worker assignment"

    def _route_action(self, thread: str, issue: int, cid, payload: dict,
                      event: str, *, target_role: str, origin_role: str,
                      assignment_id: str) -> dict:
        """A dispatch record whose id is the route key, not a hash.

        Forwarder: :func:`engine_parts.at_ledger.route_action`.
        """
        return at_ledger_mod.route_action(
            thread, issue, cid, payload, event, target_role=target_role,
            origin_role=origin_role, assignment_id=assignment_id)


    def _retry_routes(self, index: dict) -> int:
        """Re-send verdicts a refused/suppressed dispatch never delivered.

        Same rule as the notification retry: an attempt that exists (success or
        ``uncertain``) is never repeated, a refusal writes no attempt so the
        next round may try again.

        Two exceptions, both permanent by nature:

        * a **permanent refusal** (the creation mode forbids the session) is
          recorded once as ``route.refused_permanent`` and never retried again;
          every other refusal keeps retrying and is counted per round
          (:meth:`_count_route_refusal`), so a stuck verdict becomes visible
          without the retry ever giving up on it;
        * a thread whose issue is **closed** is skipped - and only ``closed``,
          never ``unknown``: a broken probe must not silently stop every
          delivery in the engine. Closed is an end state, so the route is
          marked ``permanent`` on the first sighting and stops being a
          candidate; see the note at that branch for what a reopen costs.
        """
        sent = 0
        for key, route in sorted((index.get("routes") or {}).items()):
            # ``routes`` is a plain dict in the index, and the index is a file a
            # person may edit, an older version may have written, or a crash may
            # have truncated. ``run_round`` calls this outside any try, so one
            # malformed record would take down the retry passes, the archiving
            # and both saves of the round. Skipping it keeps the round alive.
            if not retry_mod.route_candidate(route):
                continue
            base = str(route.get("action_id") or "")
            if base and self.store.action_attempted(base) > 0:
                continue
            cid = route.get("comment_id")
            thread = str(route.get("thread") or "")
            issue = retry_mod.issue_of(index, thread)
            if issue is None:
                continue
            if self._issue_state(issue) == "closed":
                retry_mod.note_closed_route(
                    route, self.store.log_event, "route.skipped_closed_issue",
                    thread, issue, cid, _now())
                continue
            review = {"verdict": str(route.get("verdict") or "")}
            result = self._route_review(index, thread, int(issue),
                                        {"id": cid, "body": "", "author": ""},
                                        review, log_unverified=False)
            if result.get("dispatched"):
                sent += 1
                self._supersede_route(index, thread, cid, base)
            elif self.is_permanent_refusal(str(result.get("reason") or "")):
                retry_mod.permanent_route(
                    route, str(result.get("reason") or ""), _now())
                self.store.log_event({
                    "type": "route.refused_permanent", "at": _now(),
                    "thread": thread, "issue": int(issue), "comment_id": cid,
                    "reason": route["permanent_reason"],
                    "level": "warn", "consumers": ["doctor"],
                })
                self._append_needs_person(index, thread, {
                    "comment_id": cid,
                    "reason": "route.refused_permanent: "
                              + route["permanent_reason"],
                    "at": _now()})
        return sent

    def _supersede_route(self, index: dict, thread: str, cid, base: str) -> None:
        """A route that finally landed makes its old dismissals moot.

        Only the rows this route itself appended are counted, and a cid that
        never became ``needs_person`` gets no mark at all. Nothing is rewritten
        or deleted: the entries stay, they are just no longer shown.
        """
        rows = [entry for entry in
                ((index.get("issues") or {}).get(thread) or {}).get("needs_person") or []
                if needs.cid_of(entry) == needs.cid_of(cid)
                and str(entry.get("reason") or "") == "route.undeliverable"]
        if not rows:
            return
        marked = needs.mark(index, thread, cid, state=needs.SUPERSEDED, by="engine",
                            reason="route.undeliverable",
                            superseded_by=str(base or ""), entries=len(rows))
        if not marked.get("changed"):
            return
        self.store.log_event({
            "type": "needs_person.superseded", "at": _now(), "thread": thread,
            "comment_id": needs.cid_of(cid), "reason": "route.undeliverable",
            "by": "engine", "superseded_by": str(base or ""),
            "keys": [thread], "entries": len(rows),
            "already": [], "entries_skipped": 0,
        })

    # ------------------------------------------------------ supervisor routes
    def _sup_eligible(self, review: dict, record: dict | None) -> bool:
        """Does this verdict want the supervisor told?

        Forwarder: :func:`engine_parts.delivery.sup_eligible` - the stored
        ``sup_eligible`` is the decision snapshot a retry must trust.
        """
        return delivery_mod.sup_eligible(review, record)

    def _undecidable(self, thread: str, cid, sup_key: str) -> None:
        """A record without ``sup_eligible`` is not silently "do not send".

        Only reachable when a record was written by something other than
        :meth:`_route_supervisor` (a hand edit) - the normal write always
        carries the snapshot. Logged once per key so a retry loop does not
        repeat it every round.
        """
        if sup_key in self._undecidable_logged:
            return
        self._undecidable_logged.add(sup_key)
        self.store.log_event({
            "type": "route.supervisor_undecidable", "at": _now(),
            "thread": thread, "comment_id": cid, "to": "supervisor",
            "detail": "the supervisor record carries no sup_eligible decision",
        })

    # The one row a malformed supervisor sid produces, once (see
    # ``_note_malformed_supervisor_sid``). It is the doctor's input: without it
    # every frame to the supervisor is refused and nothing says why.
    SUPERVISOR_SID_MALFORMED_ROW = "engine.supervisor_sid_malformed"

    def _note_malformed_supervisor_sid(self, index: dict | None, sid: str) -> None:
        """Write one ``warn`` when the supervisor sid is not a full session id.

        A truncated id (``session_6a5e``) still resolves from the config, so
        ``_supervisor_sid`` returns it and every frame built from it is refused
        with 40401 - silently, because a refusal is a route event about the
        *frame*, not about the sid being unusable. This row is the missing half:
        one ``warn`` naming the shape problem, consumed by the doctor's
        ``supervisor-visibility`` check.

        **Deduplication, stated precisely** (no stronger claim is made):

        * the key lives in ``index["meta"]["warned"]`` - **a new key**, added
          here, not an existing ``index_meta``/``index["warned"]`` field;
        * on the first use of a process the key is seeded from the **current
          segment** of ``events.jsonl`` (the same bounded tail the doctor
          reads). Rotation moves that segment to ``.1`` and the memory goes
          with it, so a rotation costs **one repeated row**, never a wrong
          decision;
        * nothing here is consulted when deciding whether to deliver - the
          refusal path is unchanged.

        The sid **value** is never logged; ``sid_len`` says "it was cut short"
        without copying a possibly-still-valid prefix into the event stream.
        """
        if not retry_mod.malformed_sid(
                index, sid, full_sid=SID_RE.match, row=self.SUPERVISOR_SID_MALFORMED_ROW,
                now=_now(),
                tail=lambda limit: _tail_events(self.store.events_path,
                                                limit=limit)):
            return
        self.store.log_event({
            "type": self.SUPERVISOR_SID_MALFORMED_ROW, "at": _now(),
            "sid_len": len(sid),
            "detail": "engine.supervisor_sid is not a full session UUID; "
                      "every frame to the supervisor will be refused",
            "level": "warn", "consumers": ["doctor"],
        })

    def _supervisor_sid(self, index: dict | None = None) -> str:
        """The supervisor's session id, resolved fresh every round.

        Configured ``supervisor_sid`` first, then the session keys in order -
        reusing the same resolver the rest of the engine uses, so an overlay
        that redirects the workbench moves this with it. Nothing is cached: a
        key that was missing last round may exist now.

        ``index`` is passed in by every caller so a malformed id can be
        reported once (see :meth:`_note_malformed_supervisor_sid`). It does not
        change the value returned - a truncated id is still returned, and the
        refusal that follows is what a person must see.
        """
        engine_cfg = self.cfg.get("engine") or {}
        sid = str(engine_cfg.get("supervisor_sid") or "").strip()
        if sid:
            self._note_malformed_supervisor_sid(index, sid)
            return sid
        keys = engine_cfg.get("supervisor_keys") or ["supervisor"]
        if not isinstance(keys, (list, tuple)):
            keys = [keys]
        try:
            path = session_events.resolve_sessions_path(self.cfg)
        except OSError:
            # No stream declared is the configured state, not a fault: the
            # answer is "no supervisor sid", the same one the inline reader
            # gave before the read moved behind ``_session_map``.
            return ""
        data, problem = self._session_map(path)
        if problem:
            self.store.log_event({
                "type": "session_map.unreadable", "at": _now(),
                "where": "_supervisor_sid", "detail": problem})
            return ""
        for key_name in keys:
            entry = data.get(str(key_name))
            if isinstance(entry, dict):
                found = str(entry.get("id") or entry.get("session") or "")
            else:
                found = str(entry or "")
            if found:
                self._note_malformed_supervisor_sid(index, found)
                return found
        return ""

    def _route_supervisor(self, index: dict, thread: str, issue: int,
                          comment: dict, review: dict, sup_key: str,
                          sup_base: str) -> dict:
        """Tell the supervisor a release is ready; at most once per verdict.

        The record is written **before** the dispatch, the same order the PM
        path uses: a crash between the two loses one send, never the decision,
        and the retry pass reads ``sup_eligible`` back from that record.

        Every way of not delivering also says so: no reachable supervisor lands
        in ``needs_person`` under ``route.supervisor_undeliverable[user]`` - the
        ``[user]`` belongs to the *reason*, not to the event type, because this
        is a local configuration gap that a person has to close, not a decision
        waiting on the supervisor.
        """
        cid = int(comment["id"])
        sup_routes = index.setdefault("supervisor_routes", {})
        if not isinstance(sup_routes, dict):
            sup_routes = index["supervisor_routes"] = {}
        record = at_ledger_mod.supervisor_record(
            sup_routes.get(sup_key), thread=thread, cid=cid, review=review,
            action_id=sup_base, now=_now())
        sup_routes[sup_key] = record
        if self.store.action_attempted(sup_base) > 0:
            return {"dispatched": False, "reason": "already attempted",
                    "action_id": sup_base}
        sid = self._supervisor_sid(index)
        if not sid:
            record["ok"] = False
            record["undeliverable"] = True
            self._note_undeliverable(
                index, thread, cid,
                "no supervisor sid: engine.supervisor_sid is empty and none of "
                "engine.supervisor_keys resolve", issue=issue, to="supervisor",
                once_key=f"sid:{sup_key}")
            return {"dispatched": False, "reason": "no supervisor sid",
                    "action_id": sup_base}
        record["undeliverable"] = False
        payload = {"thread": thread, "issue": int(issue),
                   "review_comment_id": cid,
                   "verdict": str(review.get("verdict") or ""),
                   "fix_needed": bool(review.get("fix_needed")),
                   "sid": sid, "at": _now(),
                   "text": frame_text_mod.release_text(issue, cid)}
        result = self._dispatch(self._route_action(
            thread, issue, cid, payload, "event.agent.supervisor",
            target_role="supervisor", origin_role=markers.QA.lower(),
            assignment_id=sup_key))
        record["ok"] = bool(result.get("ok")) if result.get("dispatched") else False
        record["refused"] = bool(result.get("refused"))
        record["action_id"] = result.get("action_id") or sup_base
        # ISS-74 C(b): the same single place a round learns the outcome, now on
        # this path too. Before this, only the PM route counted refusals, so a
        # supervisor route could be refused every round for hours without one
        # ``route.refused_retryable`` row - and a refusal here is exactly what
        # an allowlist miss or a malformed sid looks like.
        if result.get("dispatched"):
            self._clear_route_refusal(record)
        elif result.get("refused") is True:
            self._count_route_refusal(index, record, thread, cid, result)
        self.store.log_event({
            "type": "route.decided", "at": _now(), "thread": thread,
            "comment_id": cid, "to": "supervisor",
            "verdict": payload["verdict"],
            "dispatched": bool(result.get("dispatched")),
            "refused": bool(result.get("refused")),
            "reason": result.get("reason") or "",
        })
        return result

    def _route_pm_delivery_failure(self, index: dict, thread: str, issue: int,
                                   cid: int, target: dict) -> dict:
        """Tell the supervisor a PM route has no recipient, through the real leg."""
        sid = self._supervisor_sid(index)
        if not sid:
            return {"dispatched": False, "reason": "no supervisor sid"}
        project = str(target.get("project") or "")
        key = f"pm_delivery_failed:{thread}:{cid}"
        base = action_id(key, "bus_inject", str(cid))
        if self.store.action_attempted(base) > 0:
            return {"dispatched": False, "reason": "already attempted",
                    "action_id": base}
        payload = {"thread": thread, "issue": int(issue),
                   "review_comment_id": int(cid), "kind": "pm_delivery_failed",
                   "project": project, "sid": sid, "at": _now(),
                   "text": f"【投递失败】{project}#{int(issue)} 无项目 PM，QA cid {cid}"}
        return self._dispatch(self._route_action(
            thread, issue, cid, payload, "event.agent.supervisor",
            target_role="supervisor", origin_role=markers.QA.lower(),
            assignment_id=key))

    def _note_undeliverable(self, index: dict, thread: str, comment_id,
                            detail: str, *, issue=None, to="supervisor",
                            assignment: str = "", once_key: str = "") -> None:
        """Record a supervisor notice that could not be sent - never silently.

        One event for the machine (``type`` is a key, so it carries no ``[user]``)
        and one ``needs_person`` row for a person, whose **reason** does carry
        ``[user]``: the brief buckets by reason substring, and a local
        configuration gap must not sit in the "waiting for a decision" section.

        Identity is ``(thread, comment, assignment, detail)`` - the same
        comment can fail twice for different reasons (no sid, then a refused
        dispatch) and two different assignments can share a comment id (a
        ``None`` delivery id is not an identity). Keying on the comment alone
        would fold those into one row, which is the loss this method exists to
        prevent. The same failure across rounds still writes one row, because
        the key does not mention the round.
        """
        reason = "route.supervisor_undeliverable[user]"
        key = once_key or (f"{thread}|{needs.cid_of(comment_id)}|{assignment}|"
                           f"{detail}")
        if key not in self._sup_notice_logged:
            self._sup_notice_logged.add(key)
            self.store.log_event({
                "type": "route.supervisor_undeliverable", "at": _now(),
                "thread": thread, "comment_id": comment_id, "to": to,
                "assignment": assignment, "issue": issue, "detail": detail,
            })
        numbers = index.setdefault("issues", {}).setdefault(
            thread, {"state": "idle", "comments_seen": 0, "needs_person": []})
        row = retry_mod.undeliverable_row(
            numbers.get("needs_person") or [], comment_id=comment_id,
            reason=reason, assignment=assignment, detail=detail,
            cid_of=needs.cid_of, now=_now())
        if row is not None:
            numbers.setdefault("needs_person", []).append(row)

    def _note_skipped(self, index: dict, assignment: str, thread: str,
                      comment_id, detail: str) -> None:
        """A held thread with no usable start time: said once, then queued.

        The scan runs every round, so an unconditional ``log_event`` here wrote
        the same line on every round - the same "bury the queue" problem the row
        de-duplication already solves. One event and one row per assignment.
        """
        if f"skipped:{assignment}" not in self._sup_notice_logged:
            self._sup_notice_logged.add(f"skipped:{assignment}")
            self.store.log_event(
                retry_mod.release_skipped_row(thread, assignment, now=_now()))
        self._note_undeliverable(index, thread, comment_id, detail,
                                 assignment=assignment,
                                 once_key=f"notime:{assignment}")

    def _retry_supervisor_routes(self, index: dict) -> int:
        """Re-send supervisor notices a refused/suppressed dispatch never sent.

        Only ``index["supervisor_routes"]`` is read: the PM retry pass owns
        ``index["routes"]`` alone, and the two tables never see each other's
        keys. The stored ``sup_eligible`` is the decision - a retry never
        re-judges the verdict.
        """
        sent = 0
        for key, record in sorted((index.get("supervisor_routes") or {}).items()):
            decision = retry_mod.supervisor_candidate(
                record, attempted=self.store.action_attempted)
            if decision == "undecidable":
                self._undecidable(str(record.get("thread") or ""),
                                  record.get("comment_id"), key)
                continue
            if decision != "retry":
                continue
            base = str(record.get("action_id") or "")
            thread = str(record.get("thread") or "")
            cid = record.get("comment_id")
            issue = retry_mod.issue_of(index, thread)
            if issue is None or cid is None:
                # Dropping this quietly would leave the record ``ok=False``
                # forever with nothing to look at - the same silence this bit
                # exists to remove. ``_note_undeliverable`` writes the event and
                # the person-facing row, once per record.
                self._note_undeliverable(
                    index, thread, cid,
                    "retry cannot resolve the issue for this supervisor record",
                    issue=issue, once_key=f"retry-issue:{key}")
                continue
            # A closed issue ends the wait, as it does for ``routes`` (#84);
            # this copy never had the check, so three stuck records rang 382
            # refusals each -- a refusal writes no attempt, so nothing ended it.
            if self._issue_state(issue) == "closed":
                retry_mod.note_closed_route(
                    record, self.store.log_event,
                    "route.supervisor_skipped_closed",
                    thread, issue, cid, _now())
                continue
            review = {"verdict": str(record.get("verdict") or "")}
            result = self._route_supervisor(index, thread, int(issue),
                                            {"id": cid, "body": "", "author": ""},
                                            review, key, base)
            if result.get("dispatched"):
                sent += 1
        return sent

    # --------------------------------------------------------- pm verdicts
    def _pm_verdict(self, index: dict, thread: str) -> dict | None:
        """The PM verdict recorded for ``thread``, if any."""
        record = (index.get("pm_verdicts") or {}).get(thread)
        return record if isinstance(record, dict) else None

    def _pm_verdict_ahead(self, index: dict, thread: str) -> bool:
        """Is a PM verdict on this thread known, committed *or* in this batch?

        ``pm_verdicts`` is the committed ledger; ``_pm_pending`` is the set of
        threads whose PM verdict is in the batch being read right now. Both
        count, because comments are triaged in id order: a QA pass and the PM
        answer to it usually share a round, and the QA pass is seen first.
        """
        return bool(self._pm_verdict(index, thread)
                    or thread in self._pm_pending)

    def _pm_answered(self, index: dict, thread: str) -> bool:
        """Did the PM speak on this thread after the delivery it judges?

        The stall watchdog asks "has the PM gone quiet", not "has the PM filed
        a signed verdict". Those were one question until #87 made an unsigned
        verdict *not* a verdict: a PM comment without a session key is dropped
        by ``_pm_verified`` before ``_record_pm_verdict`` ever runs, so
        ``pm_verdicts`` stays empty and this watchdog counted rounds for a PM
        that had answered.

        Two sources, in this order, and the first one is not optional:

        1. ``pm_verdicts`` / ``_pm_pending`` - the signed path, which is the
           only path that *still* works after #87. A signed verdict writes
           ``pm_verdicts`` (``_record_pm_verdict``) and does **not** write a
           ``needs_person`` row, so reading only the rows would make every
           correctly signed verdict look like silence and restart the counter
           (QA cid 6952398074). ``_pm_verdict_ahead`` is reused rather than
           reimplemented, because it already carries the in-batch half.
        2. ``needs_person`` rows with a ``pm.*`` reason - the unsigned path.
           The row must be **newer than the delivery**, which is what keeps #71
           counting: it has a pm row but an older one.

        Three of those rows are ``pm.delivery_unknown``: ``#85`` cid
        ``6899552353``, ``#87`` cid ``6920036610``, ``#78`` cid ``6922331512``.
        All three are newer than their own thread's max delivery, and only
        ``#78`` is currently inside a hold, so only it is caught today. When a
        row like that is caught here, the meaning is "the PM spoke and its
        ``delivery:`` coordinate was wrong" - **not** "the PM forgot the key".
        Fixing the coordinate is a different job; silence is not the right
        answer to a PM that talked.

        ``pm.stall`` is excluded by name, not by luck: that reason is written by
        this very watchdog, and reading it back would let the alarm silence
        itself.

        A ``pm.escalation`` row counts too: asking for a person is the PM
        speaking, and it is the louder kind. The catch, stated so the next
        reader is not surprised: one escalation row keeps this hold quiet for
        as long as the row is newer than the delivery, and
        ``_pm_needs_person`` updates a ``(thread, reason)`` row in place rather
        than appending, so a PM that escalates once and then stops talking is
        never reported stalled for that hold. Leaving the hold clears the
        counter (``_check_pm_stall`` pops it), so a new hold starts counting
        from zero and needs a fresh PM row to go quiet again.
        """
        if self._pm_verdict_ahead(index, thread):
            return True
        return watchdog_mod.pm_spoke(
            [rec for rec in (index.get("assignments") or {}).values()
             if isinstance(rec, dict) and str(rec.get("thread") or "") == thread],
            ((index.get("issues") or {}).get(thread) or {}).get("needs_person") or [])

    # --------------------------------------------------------- at verdicts
    def _at_verdict(self, index: dict, thread: str) -> dict | None:
        """The AT verdict recorded for ``thread``, if any."""
        record = (index.get("at_verdicts") or {}).get(thread)
        return record if isinstance(record, dict) else None

    def _record_at_verdict(self, index: dict, thread: str, verdict: dict,
                           cid) -> dict:
        """Commit the verdict to the **AT** ledger, newest comment wins.

        Forwarder: the ledger shape and the watchdog reset live in
        :func:`engine_parts.at_ledger.record`. Two books, never one -
        ``pm_verdicts`` is not read or written there, and ``pm_stall`` is not
        touched: an AT answer that reset the PM watchdog would hide a PM that
        never spoke.
        """
        return at_ledger_mod.record(index, thread, verdict, cid, now=_now())

    @staticmethod
    def _at_frame_key(thread: str, cid) -> str:
        """``at:<thread>:<cid>`` - one namespace inside ``release_frames``.

        The table is reused, the key is not shared: a release key is derived
        from the *delivery* a frame judges, an AT frame is keyed by the comment
        that produced it (a question is about the comment, not a delivery, and
        two questions on one reworked thread are two questions). The prefix is
        what keeps an AT comment from ever being read as "this release rang".
        """
        return f"at:{thread}:{cid}"

    def _at_announced(self, index: dict, thread: str, cid) -> dict | None:
        """The AT frame already sent for this comment, or ``None``."""
        rows = index.get("release_frames")
        if not isinstance(rows, dict):
            return None
        row = rows.get(self._at_frame_key(thread, cid))
        return row if isinstance(row, dict) else None

    def _mark_at_frame(self, index: dict, thread: str, cid, *, kind: str) -> dict:
        """Record that this AT comment's frame landed - after it landed."""
        rows = index.setdefault("release_frames", {})
        if not isinstance(rows, dict):
            rows = index["release_frames"] = {}
        key = self._at_frame_key(thread, cid)
        row = rows.get(key)
        row = dict(row) if isinstance(row, dict) else {"key": key, "at": _now()}
        row.update({"thread": thread, "source": "at",
                    "comment_id": int(cid), "kind": kind})
        rows[key] = row
        return row

    def _at_question_url(self, thread: str, issue: int, cid,
                         verdict: dict) -> str:
        """File the questionnaire and return its loopback link.

        Filing is a file under ``<store root>/atserve/``; it does **not** need
        the service to be up. The frame still carries the link when the service
        is down, because "the question exists and here is where it lives" is
        true either way, and the service answers plainly instead of the reader
        being shown a submission that never happened.

        ``engine.at_question_port`` selects the port the link names; the
        service and the engine must be given the same one.
        """
        engine_cfg = self.cfg.get("engine") or {}
        try:
            port = int(engine_cfg.get("at_question_port")
                       or at_serve.DEFAULT_PORT)
        except (TypeError, ValueError):
            port = at_serve.DEFAULT_PORT
        try:
            question = str(verdict.get("line") or "")
            requirement = verdict.get("_requirement")
            if requirement and requirement.get("open_questions"):
                question = str(requirement["open_questions"][0])
            qid = at_serve.ensure_question(
                root=self.store.root, thread=thread, issue=int(issue),
                comment_id=int(cid), sid=self._at_sid(thread), question=question,
                requirement=requirement,
                binding_keys=self._issue_session_keys(thread))
        except OSError as exc:
            self.store.log_event({
                "type": "at.question_unfiled", "at": _now(), "thread": thread,
                "comment_id": int(cid),
                "detail": f"{type(exc).__name__}: {exc}"})
            return ""
        return at_serve.url(port, qid)

    # ------------------------------------------------------- release identity
    def _release_key(self, index: dict, thread: str, cid,
                     declared: int | None = None) -> str:
        """Identity of the **release** a frame is about: the delivery it judges.

        A release is "this delivery got a clean verdict", so the delivery
        comment is its identity - not the round, not the newest delivery on the
        thread, and not whichever comment happened to arrive first.

        ``cid`` is **the comment being judged** (the QA pass or the PM verdict),
        and it is used only to find the assignment that comment belongs to.
        It is not a fallback number: a key derived from it is a different key
        from the one the other comment about the same release computes.

        A thread with two worker assignments (the rework case) has two
        deliveries. Picking the newest would give the reworked delivery's key
        to a comment that judged the first one - the same release would then be
        counted twice. The assignment that **this** comment belongs to is the
        only thing that answers "which delivery is this about".
        """
        delivery = self._delivery_for(index, thread, cid, declared)
        return f"release:{thread}:{delivery}"

    def _delivery_for(self, index: dict, thread: str, cid,
                      declared: int | None = None) -> int:
        """The delivery comment ``cid`` judges, or ``cid`` itself.

        Forwarder: the five-level ladder lives in
        :func:`engine_parts.delivery.delivery_for`; the one-time
        ``release.key_unlinked`` row is written here because it is a side effect.
        """
        found, unlinked = delivery_mod.delivery_for(
            index, thread, cid, declared, missed=self._release_key_missed)
        if unlinked:
            self.store.log_event({
                "type": "release.key_unlinked", "at": _now(),
                "thread": thread, "comment_id": found,
                "detail": "no worker assignment links this comment to a "
                          "delivery; using the comment as its own release key"})
        return found

    def _release_announced(self, index: dict, thread: str, cid,
                           declared: int | None = None) -> dict | None:
        """The frame already sent for this release, or ``None``.

        This is an **event** ledger, not a state question: it answers "has this
        release already rung?" rather than "is there a PM verdict right now".
        The difference is the whole point - a verdict arrives in a later round
        than the QA pass, so a state question asked at gate time answers "no"
        in both rounds and rings twice.
        """
        rows = index.get("release_frames")
        if not isinstance(rows, dict):
            return None
        row = rows.get(self._release_key(index, thread, cid, declared))
        return row if isinstance(row, dict) else None

    def _mark_release(self, index: dict, thread: str, cid, *,
                      source: str, comment_id, declared: int | None = None) -> dict:
        """Record that this release has rung - **after** the frame landed.

        The ledger means "this has happened", so it is written only when the
        dispatch says it happened. Writing it before the dispatch turns a crash
        in between into a release that is permanently lost with nothing for a
        person to read: the next round sees "already announced" and the frame
        was never sent. Left unwritten, the next round simply retries.
        """
        rows = index.setdefault("release_frames", {})
        if not isinstance(rows, dict):
            rows = index["release_frames"] = {}
        key = self._release_key(index, thread, cid, declared)
        row = rows.get(key)
        row = dict(row) if isinstance(row, dict) else {"key": key, "at": _now()}
        row.update({"thread": thread, "source": source,
                    "comment_id": int(comment_id)})
        rows[key] = row
        return row

    def _record_pm_verdict(self, index: dict, thread: str, verdict: dict,
                           cid) -> dict:
        """Commit the verdict to the thread ledger, newest comment wins.

        Forwarder: :func:`engine_parts.at_ledger.record` with the PM table, so
        the two books share one shape and keep separate names.
        """
        return at_ledger_mod.record(index, thread, verdict, cid, now=_now(),
                                    table="pm_verdicts", stall="pm_stall")

    def _on_pm_verdict(self, index: dict, thread: str, issue: int,
                       comment: dict, verdict: dict) -> tuple[str, str]:
        """Apply one ``[PM]`` verdict and route it.

        Four answers, and only one of them is quiet:

        * ``working`` - the PM says the work is in hand. No frame: waking the
          supervisor to say "nothing new" is the noise that teaches a reader to
          ignore the channel;
        * ``blocked`` - an exception frame;
        * ``ready``   - the release frame (wake the supervisor);
        * unreadable (``""``), or a ``ready`` with no usable coordinate -
          **treated as blocked**. Silence here would be the one failure nobody
          can see: a PM answered and the engine dropped it.
        """
        cid = int(comment["id"])
        state = str(verdict.get("state") or "")
        escalated = self._escalation_marker(str(comment.get("body") or ""))
        if escalated and not state:
            # A ``[PM]`` note that asks for a decision. It rings the exception
            # frame and it also leaves a row: the frame is delivered to a
            # session, and a session that is not listening would otherwise be
            # the only copy of a request for a person (#75 round 2, P0-2).
            #
            # 2026-11-25: the record keeps ``state=""`` (the frame is an
            # exception, not a verdict - the #75 behaviour the stall watchdog
            # depends on), and the frame is sent here instead of falling
            # through. Falling through used to *also* file a
            # ``pm.verdict[user]`` row, so one request produced two rows: the
            # right one and "the PM's answer could not be read". Measured on
            # #130 cid 7157168563. One request, one row, one frame.
            self.store.log_event({
                "type": "pm.escalation", "at": _now(), "thread": thread,
                "comment_id": cid, "marker": escalated})
            if self._escalation_from_supervisor(index, comment):
                # The author is the session this frame would wake. Ringing it
                # is a loop, and the row is one nobody can resolve: the request
                # is already on the issue, written by the person the wake would
                # reach. One event, no row, no frame (#140).
                self.store.log_event({
                    "type": "pm.escalation_self", "at": _now(),
                    "thread": thread, "comment_id": cid,
                    "detail": "escalation authored by the supervisor sid; "
                              "not routed back to itself"})
                return "tracked", "escalation authored by supervisor"
            self._pm_needs_person(index, thread, cid, "pm.escalation[user]",
                                  f"escalation marker {escalated!r}", verdict)
            self._record_pm_verdict(index, thread, verdict, cid)
            result = self._route_pm_frame(
                index, thread, issue, comment, verdict, exception=True,
                reason=f"escalation marker {escalated!r}")
            return self._settle_frame(index, thread, issue, cid, result,
                                      exception=True,
                                      reason=f"escalation marker {escalated!r}",
                                      verdict=verdict, state="")
        if not self._pm_verified(thread, comment):
            # An unverified verdict does not ring. A QA verdict that cannot be
            # attributed still moves an assignment; a PM ``ready`` wakes a
            # person to close an issue and cannot be taken back.
            self.store.log_event({
                "type": "pm.unverified", "at": _now(), "thread": thread,
                "comment_id": cid, "consumers": ["doctor"],
                "detail": "the verdict did not name a pm session key of this "
                          "thread"})
            self._pm_needs_person(
                index, thread, cid, "pm.verdict_unverified[user]",
                "pm verdict from unverified author", verdict)
            return "needs_person", "pm verdict from unverified author"
        delivery_error = str(verdict.get("delivery_error") or "")
        if delivery_error:
            # The PM named a delivery and it did not parse. Guessing instead is
            # the failure the field exists to remove, so this is a look-at.
            self.store.log_event({
                "type": "pm.delivery_bad", "at": _now(), "thread": thread,
                "comment_id": cid, "detail": delivery_error})
            self._pm_needs_person(
                index, thread, cid, "pm.verdict[user]", delivery_error, verdict)
            return "needs_person", f"pm verdict: {delivery_error}"
        declared_delivery = verdict.get("delivery")
        owner = None
        if declared_delivery is not None:
            wanted = int(declared_delivery)
            states = REVIEW_TARGET_STATES + ("close_reconcile",)
            check = delivery_mod.verify_declared(
                index, thread, wanted, states,
                owners_of=asg_mod.owners_of_delivery,
                newest_owner=asg_mod.newest_delivery_owner,
                output_cids=asg_mod.assignment_output_cids)
            if not check["ok"]:
                self.store.log_event({
                    "type": f"pm.delivery_{check['kind']}", "at": _now(),
                    "thread": thread, "comment_id": cid, "delivery": wanted,
                    "owners": check.get("owners") or []})
                self._pm_needs_person(index, thread, cid, "pm.verdict[user]",
                                      check["detail"], verdict)
                return "needs_person", f"pm verdict: {check['detail']}"
            owner = check.get("owner")
        if self._at_mode(issue):
            if declared_delivery is None:
                detail = "AT-mode PM verdict requires an exact delivery coordinate"
                self._pm_needs_person(index, thread, cid, "pm.verdict[user]",
                                      detail, verdict)
                return "needs_person", detail
            requirement_error = self._requirement_event_error(
                index, thread, issue, comment, owner)
            if requirement_error:
                self.store.log_event({"type": "pm.requirement_refused", "at": _now(),
                                      "thread": thread, "comment_id": cid,
                                      "reason": requirement_error})
                self._pm_needs_person(index, thread, cid, "pm.verdict[user]",
                                      requirement_error, verdict)
                return "needs_person", requirement_error
        named = verdict.get("issue")
        if named is not None and int(named) != int(issue):
            # The verdict speaks about a different issue. Filing it against
            # this thread would close the wrong one.
            self.store.log_event({
                "type": "pm.verdict_wrong_issue", "at": _now(),
                "thread": thread, "comment_id": cid, "named_issue": int(named),
                "thread_issue": int(issue)})
            self._pm_needs_person(
                index, thread, cid, "pm.verdict_wrong_issue[user]",
                f"verdict names issue #{int(named)} but thread is #{int(issue)}",
                verdict)
            return "needs_person", (f"verdict names issue #{int(named)} but "
                                    f"thread is #{int(issue)}")
        record = self._record_pm_verdict(index, thread, verdict, cid)
        if state == "working":
            self.store.log_event({
                "type": "pm.verdict", "at": _now(), "thread": thread,
                "comment_id": cid, "state": state,
                "matched": record["matched"], "frame": "none"})
            return "pm_verdict", ""
        exception = state != "ready"
        reason = ""
        if not state:
            reason = (verdict.get("coords_error")
                      or "no unmarked pm verdict; treated as blocked")
        elif exception:
            reason = "pm reports blocked"
        # A release rings once. ``ready`` is the release frame, so it reads the
        # same ledger the QA pass reads; an exception frame is about the state
        # of the work, not about a release, and is keyed by its verdict.
        if not exception:
            announced = self._release_announced(index, thread, cid,
                                                verdict.get("delivery"))
            if announced is not None:
                self.store.log_event({
                    "type": "route.supervisor_skipped", "at": _now(),
                    "thread": thread, "comment_id": cid, "to": "supervisor",
                    "reason": "this release already rang",
                    "announced_by": str(announced.get("source") or "")})
                return "pm_verdict", ""
        result = self._route_pm_frame(index, thread, issue, comment, verdict,
                                      exception=exception, reason=reason)
        return self._settle_frame(index, thread, issue, cid, result,
                                  exception=exception, reason=reason,
                                  verdict=verdict, state=state)

    def _settle_frame(self, index: dict, thread: str, issue, cid,
                      result: dict, *, exception: bool, reason: str,
                      verdict: dict | None, state: str,
                      key: str = "") -> tuple[str, str]:
        """Close out one dispatch: ledger, or a row a person can read.

        Three outcomes and no fourth::

            dispatched and ok        -> record the release (ready frames only)
            dispatched, not ok       -> the frame may have landed; a person looks
            not dispatched           -> nothing happened; the next round retries

        The third outcome needs no row of its own: it is "not yet", and the
        attempt counter plus the retry pass own it. The first two are the ones
        that must leave a trace.

        The middle outcome is the reason this exists as one function. It used
        to fall between two ``if``s on the inline path - ``dispatched`` true,
        ``ok`` false (the bus raised after the send) - so the frame was sent,
        the ledger was not written, and nothing was recorded: a release that
        rang and left no trace, or a notice that vanished. Collecting the three
        branch here means a new call site cannot re-open the gap by forgetting
        one of them.
        """
        dispatched = bool(result.get("dispatched"))
        ok = bool(result.get("ok")) if dispatched else False
        if not dispatched:
            if exception:
                # The PM asked for a look and the look could not be sent.
                self._note_undeliverable(
                    index, thread, cid,
                    "pm verdict could not be delivered: "
                    f"{result.get('reason') or 'unknown'}", issue=issue,
                    to="supervisor", once_key=f"pm:{thread}:{cid}")
                self._pm_needs_person(
                    index, thread, cid, "pm.verdict[user]",
                    (verdict or {}).get("coords_error") or reason, verdict)
                return "needs_person", f"pm verdict: {reason or state}"
            return "pm_verdict", ""
        if not ok:
            # Landed but unconfirmed, or the send raised after counting the
            # attempt: the counter stops a blind repeat, so a person has to
            # decide - silence here is a lost bell.
            self._note_undeliverable(
                index, thread, cid,
                "frame was dispatched but the send was not confirmed; it may "
                "or may not have arrived - check before resending",
                issue=issue, to="supervisor",
                once_key=f"uncertain:{key or thread + ':' + str(cid)}")
            return "pm_verdict", ""
        if not exception:
            self._mark_release(index, thread, cid, source="pm",
                               comment_id=cid,
                               declared=(verdict or {}).get("delivery"))
        return "pm_verdict", ""

    @staticmethod
    def _walk_covers_cursor(walked, cursor: int) -> bool:
        """Did this walk prove every row above ``cursor`` is present?

        Forwarder: the rule lives in :mod:`super_agent.engine_parts.claims`
        (ISS-128 L1). The name stays so no test or caller changes.
        """
        return walk_claims.walk_covers_cursor(walked, cursor)

    def _pause_read(self, index: dict, key: str, issue: int, cursor: int,
                    rows: int, max_pages: int, summary: dict) -> None:
        """Mark the issue as read-paused until a person lifts it (ISS-128 L1).

        Forwarder: the record shape and its dedupe live in
        :func:`engine_parts.pause.pause_read`; the two side effects this class
        owns - ``needs_person`` dedupe and the event log - are passed in.
        """
        read_pause_mod.pause_read(index, key, issue, cursor, rows, max_pages, summary,
                          append=self._append_needs_person,
                          log=self.store.log_event)

    def rebuild_issue(self, thread, reason: str, *, collected=None,
                      index: dict | None = None,
                      cursor: dict | None = None) -> dict:
        """Rebuild one thread from the full listing, **without moving the cursor**.

        The counter-example this exists for: a rebuild that advances the cursor
        before it has agreed that it read everything. A rebuild is a repair, and
        the one state a repair must never leave behind is "the cursor moved, the
        rows did not arrive" - that silently drops comments and nothing later
        notices.

        So the cursor is written **only** on the success path, and only after the
        rebuilt prefix is complete:

        * read the whole listing (``since_id=0``), not the unread prefix;
        * if the walk cannot claim it reached page 1, write one ``needs_person``
          row, pause the read, return ``{"ok": False}`` - and leave the cursor
          byte for byte as it was;
        * if it can, adopt the rebuilt rows as consumed and move the cursor to
          what was actually read.

        ``collected`` lets the caller pass the batch it already has, so a gap
        found mid-round costs no second walk of the same listing.

        ``index``/``cursor`` are the round's own copies. Passing them is not an
        optimisation: a round holds its index in memory and writes it once at
        the end, so a rebuild that loaded its own copy had every row it wrote
        (``needs_person``, ``read_paused``) overwritten by that final write -
        the pause existed in the event log and nowhere a reader looks, and the
        next round read the issue again (#130 QA P0-2). Without them this is a
        standalone repair that writes through, which is what the tests and the
        CLI use.
        """
        thread = str(thread or "")
        repo, number = issuekey.split_key(thread)
        out = {"thread": thread, "issue": number, "ok": False, "reason": str(reason),
               "rows": 0, "cursor_before": None, "cursor_after": None}
        if number is None:
            out["reason"] = f"{reason}: no issue number in {thread!r}"
            return out
        in_round = index is not None
        index = index if index is not None else self.store.load_index()
        cursor = cursor if cursor is not None else self.store.load_cursor()
        legacy = str(number)
        cur = int(issuekey.read_both(cursor.get("issues") or {}, thread, legacy) or 0)
        out["cursor_before"] = cur
        if collected is None:
            budget = int((self.cfg.get("github") or {}).get("max_comment_pages") or 10)
            walk = github_source.paged_batch(self.source, number, cursor_before=0,
                                             max_pages=budget, whole_listing=True)
            collected = batch_mod.CollectedBatch(
                thread, walk.rows, cursor_before=0,
                pages=list(getattr(walk, "pages", []) or []),
                complete=bool(getattr(walk, "pagination_complete", False)),
                contiguous=bool(getattr(walk, "contiguous", False)),
                reason=str(getattr(walk, "stop_reason", "") or ""))
        out["rows"] = len(collected.rows)
        # Two facts, both required: the walk reached page 1, **and** the page
        # intervals it read form one unbroken run from page 1. The second one is
        # ``contiguous_with`` - the issue names it as the real interval check,
        # and reading only the first let a rebuild adopt a listing whose middle
        # page came back empty (#130 QA P1-2).
        complete = (collected.pagination_complete
                    and queue_mod.contiguous_with(collected.observed_spans(),
                                                  expect_from=1))
        if not complete:
            # One row, one pause, no cursor movement. The reason names the fault
            # so a person can see *why* the history is untrusted.
            self._pause_read(index, thread, number, cur, len(collected.rows),
                             int((self.cfg.get("github") or {}).get(
                                 "max_comment_pages") or 10),
                             {"needs_person": 0, "read_paused": 0})
            self._append_needs_person(index, thread, {
                "comment_id": 0, "reason": "rebuild.incomplete[user]",
                "at": _now(),
                "detail": (f"full rebuild could not read the listing "
                           f"(rows={len(collected.rows)}, "
                           f"missing_page={collected.missing_page()}, "
                           f"reason={collected.reason}); the cursor was not "
                           f"moved")})
            if in_round:
                # The round's write-back persists this index; saving here would
                # just be overwritten by it (#130 QA P0-2).
                self._block_cursor(thread)
            else:
                self.store.save_index(index)
            self.store.log_event({"type": "rebuild.incomplete", "at": _now(),
                                  "thread": thread, "issue": number,
                                  "rows": len(collected.rows),
                                  "cursor": cur})
            out["reason"] = f"{reason}: the whole listing could not be read"
            return out
        cursor.setdefault("issues", {})[thread] = round_mod.consumed_prefix(
            cur, [int(row["id"]) for row in collected.rows])
        out["cursor_after"] = cursor["issues"][thread]
        if not in_round:
            self.store.save_cursor(cursor)
        self.store.log_event({"type": "rebuild.done", "at": _now(),
                              "thread": thread, "issue": number,
                              "rows": len(collected.rows),
                              "cursor": out["cursor_after"]})
        out["ok"] = True
        return out

    def _block_cursor(self, thread: str) -> None:
        """Refuse to advance this thread's cursor for the rest of the round.

        The cursor write lives in ``_round_issue`` while the fault is found in
        ``rebuild_issue``, so the two must agree explicitly. A rebuild that
        could not prove its rows makes the rows just read untrustworthy, and
        advancing to them would put the missing middle below the cursor - the
        loss is permanent and nothing later notices (#130 QA P0-3).
        """
        state = getattr(self, "_round_state", None)
        if state is not None:
            state.block_cursor(str(thread))

    def resume_read(self, issue, *, by: str = "", max_pages: int | None = None,
                    detail: str = "") -> dict:
        """Lift a read pause for one issue - the manual way out of the hard cap.

        Forwarder: the state machine lives in :func:`engine_parts.pause.lift`,
        which clears the record, the ``state`` and the pause rows together, so
        an orchestrator stops seeing "waiting for a person" the moment a person
        actually answered.

        ``max_pages`` is the page budget for the **next** read of this issue and
        is persisted with the lift; the round then uses it for that issue
        (never for others, never as a new default).

        Returns ``{"changed": bool, "reason": str, "number": int|None,
        "max_pages": int|None}`` and never reads the source.
        """
        number = read_pause_mod.issue_number(issue)
        out = {"changed": False, "reason": "", "number": number,
               "max_pages": max_pages}
        if number is None:
            out["reason"] = f"cannot read an issue number from {issue!r}"
            return out
        index = self.store.load_index()
        keys = [key for key in (index.get("issues") or {})
                if read_pause_mod.issue_number(key) == number]
        if not keys:
            out["reason"] = f"issue #{number} is not in the index"
            return out
        lifted = read_pause_mod.lift(index, keys, by=str(by or ""), max_pages=max_pages,
                             mark=needs.mark)
        if lifted["changed"]:
            for key in keys:
                if not read_pause_mod.is_paused((index.get("issues") or {}).get(key)):
                    self.store.log_event({
                        "type": "read.resumed", "at": _now(),
                        "issue": number, "thread": str(key),
                        "by": str(by or ""), "was": lifted["reason"],
                        "detail": str(detail or ""), "max_pages": max_pages})
            self.store.save_index(index)
            out["changed"] = True
            out["reason"] = lifted["reason"]
        else:
            out["reason"] = f"#{number} has no read pause"
        return out

    @staticmethod
    def _clear_read_pause_surface(index: dict, key: str, *, by: str = "") -> None:
        """Put the issue back in the queue state the pause took it out of.

        Forwarder: :func:`engine_parts.pause.clear_surface`.
        """
        read_pause_mod.clear_surface(index, key, by=by, mark=needs.mark)

    def _append_needs_person(self, index: dict, thread: str, row: dict) -> bool:
        """Append one person-facing row unless ``(thread, cid, reason)`` exists.

        The scan runs every round and the same comment is re-judged whenever a
        dispatch stays refused, so an unconditional ``append`` grew a row per
        round: ``#69`` held 138 copies of one ``route.undeliverable`` (#75 B).
        A repeat updates the row it repeats instead - the failure is still
        current, the queue is not.

        The identity is the triple the issue names. Rows that carry an
        ``assignment`` or a ``detail`` are **not** folded by this rule; those
        live in :meth:`_note_undeliverable`, where two assignments sharing one
        comment id are two distinct failures and collapsing them loses one.
        """
        numbers = index.setdefault("issues", {}).setdefault(
            thread, {"state": "idle", "comments_seen": 0, "needs_person": []})
        rows = numbers.setdefault("needs_person", [])
        reason = str(row.get("reason") or "")
        cid = needs.cid_of(row)
        existing = next((entry for entry in rows
                         if cid is not None and needs.cid_of(entry) == cid
                         and str(entry.get("reason") or "") == reason), None)
        if existing is not None:
            existing["at"] = row.get("at") or _now()
            if row.get("detail") is not None:
                existing["detail"] = row["detail"]
            return False
        rows.append(row)
        return True

    def _file_needs_person(self, index: dict, key: str, issue: int, cid,
                           reason: str, rec: dict, summary: dict) -> bool:
        """File one person-facing row and ring at most one bell.

        Returns ``True`` when the caller should stop for this comment. Two rules
        make the queue readable: a row the router already filed is not filed
        again (its own ``reason`` is actionable prose, and a second copy is the
        shape that put 179 rows under one comment in #68), and the bus bell keeps
        its cooldown so a stuck comment cannot ring every round.
        """
        if self._pm_row_exists(index, key, cid):
            return True
        if not self._append_needs_person(index, key, {
                "comment_id": cid, "reason": reason, "at": _now()}):
            return True
        summary["needs_person"] += 1
        now = self.clock()
        if self._cooldown_ok(rec, "needs_person", now):
            self._mark_cooldown(rec, "needs_person", now)
            self._dispatch({"kind": "bus_inject",
                            "event": "event.agent.needs_person",
                            "issue": issue,
                            "payload": {"issue": issue, "comment_id": cid,
                                        "reason": reason}})
        return False

    def _pm_row_exists(self, index: dict, thread: str, cid) -> bool:
        """Is there already a row for this comment under this thread?

        The loop's fallback path asks this before appending. Its ``reason`` is
        the prose the router produced, so the same comment can arrive here
        after the router has already filed a row with a name a reader can act
        on - two rows for one failure, and one of them a duplicate of the
        other's detail.
        """
        rows = ((index.get("issues") or {}).get(thread) or {}).get(
            "needs_person") or []
        want = needs.cid_of(cid)
        return want is not None and any(needs.cid_of(entry) == want
                                        for entry in rows)

    def _pm_needs_person(self, index: dict, thread: str, cid, reason: str,
                         detail: str, verdict: dict | None = None) -> None:
        """One person-facing row per ``(thread, reason)``, never a pile.

        The stall watchdog re-fires across holds and the seam paths can repeat,
        so an unconditional append would grow a row per round and bury the
        queue. The row is updated in place; its identity is the reason plus the
        thread, not the comment id, because the latest comment is only the one
        that happened to be current when the condition was noticed.
        """
        numbers = index.setdefault("issues", {}).setdefault(
            thread, {"state": "idle", "comments_seen": 0, "needs_person": []})
        rows = numbers.setdefault("needs_person", [])
        existing = next((row for row in rows
                         if str(row.get("reason") or "") == reason), None)
        if existing is not None:
            existing["at"] = _now()
            existing["detail"] = detail
            existing["comment_id"] = cid
            return
        rows.append({"comment_id": cid, "at": _now(), "reason": reason,
                     "detail": detail})

    def _route_pm_frame(self, index: dict, thread: str, issue: int,
                        comment: dict, verdict: dict, *, exception: bool,
                        reason: str = "", action_key: str = "",
                        body_text: str = "") -> dict:
        """Send one PM frame on ``event.agent.supervisor``; at most once.

        The record is written **before** the dispatch and carries the decision
        snapshot (``exception``), the same order and the same reason as
        :meth:`_route_supervisor`: a crash between the two loses one send,
        never the decision, and :meth:`_retry_pm_frames` reads the snapshot
        back instead of re-judging a body it no longer has.
        """
        cid = int(comment["id"]) if comment.get("id") is not None else 0
        key = action_key or f"pm:{thread}:{cid}"
        base = action_id(key, "bus_inject", str(cid))
        frames = index.setdefault("pm_frames", {})
        if not isinstance(frames, dict):
            frames = index["pm_frames"] = {}
        record = at_ledger_mod.frame_record(
            frames.get(key), thread=thread, cid=cid, issue=issue,
            kind="pm_verdict", exception=exception, question_url="",
            text=(body_text or self._pm_frame_text(
                issue, cid, verdict, exception=exception)),
            action_id=base, now=_now(), delivery=verdict.get("delivery"))
        frames[key] = record
        if self.store.action_attempted(base) > 0:
            return {"dispatched": False, "reason": "already attempted",
                    "action_id": base}
        sid = self._supervisor_sid(index)
        if not sid:
            record["ok"] = False
            record["undeliverable"] = True
            self._note_undeliverable(
                index, thread, cid,
                "no supervisor sid: engine.supervisor_sid is empty and none of "
                "engine.supervisor_keys resolve", issue=issue, to="supervisor",
                once_key=f"pmsid:{key}")
            return {"dispatched": False, "reason": "no supervisor sid",
                    "action_id": base}
        record["undeliverable"] = False
        payload = {"thread": thread, "issue": int(issue),
                   "review_comment_id": cid,
                   "kind": "qa_arbitration" if action_key.startswith("qa_arbitration:")
                           else ("pm_stall" if action_key.startswith("pm_stall:")
                                 else "pm_verdict"),
                   "state": str(verdict.get("state") or ""),
                   "exception": bool(exception),
                   "reason": reason,
                   "sid": sid, "at": _now(), "text": record["text"]}
        result = self._dispatch(self._route_action(
            thread, issue, cid, payload, "event.agent.supervisor",
            target_role="supervisor", origin_role="pm",
            assignment_id=key))
        record["ok"] = bool(result.get("ok")) if result.get("dispatched") else False
        record["refused"] = bool(result.get("refused"))
        record["action_id"] = result.get("action_id") or base
        # ISS-74 C(b): this frame follows the same rule as the PM route and the
        # release route - one increment per round while it keeps being refused.
        if result.get("dispatched"):
            self._clear_route_refusal(record)
        elif result.get("refused") is True:
            self._count_route_refusal(index, record, thread, cid, result)
        self.store.log_event({
            "type": "route.decided", "at": _now(), "thread": thread,
            "comment_id": cid, "to": "supervisor", "origin": "pm",
            "state": str(verdict.get("state") or ""),
            "exception": bool(exception),
            "dispatched": bool(result.get("dispatched")),
            "refused": bool(result.get("refused")),
            "reason": result.get("reason") or "",
        })
        return result

    @staticmethod
    def _pm_frame_text(issue, cid, verdict: dict, *, exception: bool) -> str:
        """Forwarder: the wording lives in ``engine_parts.frame_text``."""
        return frame_text_mod.pm_frame_text(issue, cid, verdict,
                                            exception=exception)

    def _retry_pm_frames(self, index: dict) -> int:
        """Re-send PM frames a refused/suppressed dispatch never sent.

        Same rule as :meth:`_retry_supervisor_routes`: the snapshot is the
        decision, an attempt that exists is never repeated, and a record with
        no snapshot is skipped rather than guessed at.

        A successful re-send is a **first** delivery as far as the release
        ledger is concerned, so it writes the ledger here - the same
        ``dispatched and ok`` rule as the two inline gates. Leaving it out made
        the retry path the one place a release could ring twice: the frame went
        out, the ledger stayed empty, and the next PM verdict rang again. The
        QA side never had the hole because its retry re-enters
        :meth:`_route_review`, which writes the ledger itself.

        ``exception`` frames are not releases, so they write nothing - the same
        distinction the inline path draws.
        """
        sent = 0
        for key, record in sorted((index.get("pm_frames") or {}).items()):
            plan = retry_mod.pm_frame_candidate(
                key, record, attempted=self.store.action_attempted)
            if plan is None:
                continue
            thread, issue = plan["thread"], plan["issue"]
            if issue is None:
                for rec in (index.get("assignments") or {}).values():
                    if str(rec.get("thread") or "") == thread:
                        issue = rec.get("issue")
                        break
            if issue is None:
                continue
            cid = plan["cid"]
            verdict = {"state": "blocked" if plan["exception"] else "ready",
                       "matched": ""}
            result = self._route_pm_frame(
                index, thread, int(issue), {"id": cid}, verdict,
                exception=plan["exception"], action_key=key,
                body_text=plan["text"])
            self._settle_frame(index, thread, issue, cid, result,
                               exception=plan["exception"],
                               reason=plan["reason"],
                               verdict={"delivery": plan["delivery"]}, state="",
                               key=key)
            # Write the outcome back **through the table**. ``_route_pm_frame``
            # stores a fresh ``dict`` under ``key``, so the loop's ``record``
            # is a stale reference: setting a field on it writes to a copy and
            # the next round retries a frame that already landed.
            written = index.get("pm_frames") or {}
            current = written.get(key) if isinstance(written, dict) else None
            if isinstance(current, dict):
                dispatched = bool(result.get("dispatched"))
                current["ok"] = bool(result.get("ok")) if dispatched else False
                if dispatched:
                    sent += 1
        return sent

    # ------------------------------------------------------- pm stall watchdog
    def _check_pm_stall(self, index: dict) -> int:
        """Ask once about a thread where QA passed and the PM never answered.

        A release held in ``waiting_release``/``close_reconcile`` is waiting for
        a person to close it. The PM verdict is the new gate in front of that
        person, so a PM that stops answering turns into a **silent** hold - the
        exact failure mode this engine is built to make visible.

        Counting is by **round**, not by wall clock: a round is the unit the
        engine actually acts in, and ``pm_stall_rounds`` rounds at the default
        60s poll interval is roughly that many minutes. The counter is per
        thread, reset by any PM verdict (see :meth:`_record_pm_verdict`) and by
        leaving the held states.

        **Each hold is its own notice.** The action id carries a per-thread
        ``hold_seq`` that only moves when the thread leaves the held states and
        comes back, so a second stuck release is reported instead of being
        swallowed by the first one's attempt counter. Inside one hold the
        ``fired`` flag keeps it to one frame and one ``needs_person`` row.

        The default is **15 rounds** (about 15 minutes at the 60s interval).
        ``engine.release_timeout_s`` is 1800s = 30 rounds, so 15 sits **inside**
        it: the PM is asked first, and only a thread that is still held after
        that gets the release-timeout reminder. Equal defaults would put two
        frames on the same round for the same thread.
        """
        engine_cfg = self.cfg.get("engine") or {}
        try:
            limit = int(engine_cfg.get("pm_stall_rounds") or 15)
        except (TypeError, ValueError):
            limit = 15
        if limit <= 0:
            return 0
        stall = watchdog_mod.table(index, "pm_stall")
        held = watchdog_mod.pm_held(index, issue_state=self._issue_state)
        for thread in list(stall):
            if thread not in held:
                # Left the hold: the next hold is a new notice, so the seq
                # moves and the old attempt counter no longer swallows it.
                watchdog_mod.bump_hold_seq(index, "pm_stall_seq", thread)
                stall.pop(thread, None)
        fired = 0
        for thread, (aid, rec) in sorted(held.items()):
            if self._pm_answered(index, thread):
                stall.pop(thread, None)
                continue
            entry = watchdog_mod.round_count(stall.get(thread), thread, _now())
            stall[thread] = entry
            if not watchdog_mod.due(entry, limit):
                continue
            issue = rec.get("issue")
            comment_id = rec.get("delivery_comment_id")
            seq = watchdog_mod.hold_seq(index, "pm_stall_seq", thread)
            stall[thread] = self._fire_stall(
                index, thread, issue=issue, comment_id=comment_id, seq=seq,
                table="pm_stall_seq", entry=entry, kind="pm",
                assignment=aid,
                body_text=self._pm_stall_text(thread, issue, entry["rounds"]),
                row_reason="pm.stall[user]",
                row_detail=f"QA 通过后 {entry['rounds']} 轮无 PM 结论")
            fired += 1
        return fired

    @staticmethod
    def _pm_stall_text(thread: str, issue, rounds: int) -> str:
        return f"【PM 停摆】#{int(issue or 0)} QA 已通过，但 {rounds} 轮无 PM 结论"

    def _question_answered(self, thread: str, comment_id) -> bool:
        for path in (self.store.root / "atserve" / "questions").glob("*.json"):
            record = at_serve.load(self.store.root, path.stem) or {}
            if (str(record.get("thread") or "") == thread
                    and record.get("comment_id") == comment_id
                    and record.get("submitted") is True):
                return True
        return False

    def _at_needs_answer(self, index: dict, thread: str, at_entry: dict) -> bool:
        """Is this AT registration still waiting for an answer?

        Forwarder: the six refusals live in
        :func:`engine_parts.watchdog.at_needs_answer`, so "what counts as an AT
        silence" is readable in one place instead of inside a 30-line predicate.
        """
        def flow_of(issue):
            try:
                return (self.source.flow(issue)
                        if callable(getattr(self.source, "flow", None)) else "")
            except flow_mod.FlowError:
                return flow_mod.STANDARD

        return watchdog_mod.at_needs_answer(
            issue=at_entry.get("issue"), thread=thread, at_entry=at_entry,
            flow_of=flow_of, standard=flow_mod.STANDARD,
            live=getattr(self, "_live_issues", set()),
            issue_state=self._issue_state, at_sid=self._at_sid,
            current=self._requirement_current(index, thread),
            verdict=self._at_verdict(index, thread),
            question_answered=self._question_answered,
            split_key=issuekey.split_key)

    # ------------------------------------------------------- at stall watchdog
    # -------------------------------------------------- release-time reminders
    def _fire_stall(self, index: dict, thread: str, *, issue, comment_id,
                    seq: int, table: str, entry: dict, kind: str,
                    assignment: str, body_text: str, row_reason: str,
                    row_detail: str) -> dict:
        """Send one stall notice and record it; PM and AT share this shape.

        ``kind`` chooses the frame (``pm`` or ``at``) and the event name. The
        record is written whether or not the frame landed, so one hold produces
        one notice: a failed send leaves ``needs_person`` as the copy a person
        can act on, and a retry loop cannot turn it into a bell every round.
        """
        action_key = f"{kind}_stall:{thread}:{seq}"
        verdict = {"state": "blocked", "matched": f"{kind}_stall"}
        if kind == "pm":
            result = self._route_pm_frame(
                index, thread, int(issue or 0), {"id": comment_id}, verdict,
                exception=True,
                reason=f"no pm verdict in {entry['rounds']} rounds",
                action_key=action_key, body_text=body_text)
        else:
            result = self._route_at_frame(
                index, thread, int(issue or 0), {"id": 0}, verdict,
                exception=True, kind="at_stall",
                reason=f"no at verdict in {entry['rounds']} rounds",
                action_key=action_key, body_text=body_text)
        self.store.log_event({
            "type": f"{kind}.stall", "at": _now(), "thread": thread,
            "assignment": assignment or None, "rounds": entry["rounds"],
            "hold_seq": seq, "dispatched": bool(result.get("dispatched")),
            "reason": result.get("reason") or ""})
        if not result.get("dispatched"):
            self._note_undeliverable(
                index, thread, comment_id,
                f"{kind} stall notice could not be delivered: "
                f"{result.get('reason') or 'unknown'}", issue=issue,
                assignment=assignment or None,
                once_key=f"{kind}stall:{thread}:{seq}")
        self._pm_needs_person(index, thread, comment_id, row_reason, row_detail)
        return watchdog_mod.remember(result, entry, seq)

    def _release_start(self, index: dict, thread: str, assignment: dict) -> str:
        """Forwarder: :func:`engine_parts.watchdog.release_start`."""
        return watchdog_mod.release_start(index, thread, assignment)

    def _remind_release_timeouts(self, index: dict) -> int:
        """Remind the supervisor once about a thread held too long.

        The scan covers **both** held states: an open Issue turns every
        ``waiting_release`` into ``close_reconcile`` in the same round, so
        scanning only the former would watch a set that is empty by design.

        A reminder dispatches, logs, and - when it cannot be delivered - appends
        ``needs_person``. It never touches ``status``, ``qa_verdict``, the
        ``close_reconcile`` record or the archive outbox: reconciliation and
        reminding stay independent.
        """
        engine_cfg = self.cfg.get("engine") or {}
        try:
            timeout = float(engine_cfg.get("release_timeout_s") or 1800)
        except (TypeError, ValueError):
            timeout = 1800.0
        now = datetime.now(timezone.utc)
        sent = 0
        for aid, rec in sorted((index.get("assignments") or {}).items()):
            if not isinstance(rec, dict):
                continue
            if str(rec.get("status") or "") not in ("waiting_release",
                                                    "close_reconcile"):
                continue
            thread = str(rec.get("thread") or "")
            issue = rec.get("issue")
            # A closed issue has nothing to release. Reconciling a closed issue
            # is the end state, not a stuck state; reading it as "overdue" is
            # the false alarm (#80). The attempt de-dup below stays: it is what
            # ended the 57-round re-send of #68, and removing it to "make the
            # alarm show up" would trade one noise for another.
            if issue is not None and self._issue_state(issue) == "closed":
                continue
            start = self._release_start(index, thread, rec)
            due, seconds, problem = watchdog_mod.overdue(start, timeout)
            if problem:
                self._note_skipped(
                    index, aid, thread, rec.get("delivery_comment_id"), problem)
                continue
            if not due:
                continue
            base = action_id(f"release_timeout:{aid}", "bus_inject", start)
            if self.store.action_attempted(base) > 0:
                continue
            sid = self._supervisor_sid(index)
            issue = rec.get("issue")
            if not sid:
                self._note_undeliverable(
                    index, thread, rec.get("delivery_comment_id"),
                    "release timeout with no resolvable supervisor sid",
                    issue=issue, assignment=aid,
                    once_key=f"nosid:{aid}:{start}")
                continue
            result = self._dispatch({
                "kind": "bus_inject", "event": "event.agent.supervisor",
                "issue": issue, "thread": thread, "input_ref": start,
                "assignment_id": f"release_timeout:{aid}",
                "origin_role": "engine", "target_role": "supervisor",
                "payload": {"thread": thread, "issue": issue,
                            "assignment": aid, "kind": "release_timeout",
                            "status": str(rec.get("status") or ""),
                            "started_at": start, "sid": sid, "at": _now(),
                            "text": f"【超时未关】#{issue} 已滞留 "
                                    f"{int(seconds)}s"
                                    f"（起点 {start}）"},
            })
            if not result.get("dispatched"):
                self._note_undeliverable(
                    index, thread, rec.get("delivery_comment_id"),
                    f"release timeout dispatch refused: "
                    f"{result.get('reason') or 'unknown'}", issue=issue,
                    assignment=aid, once_key=f"timeout:{aid}:{start}")
                continue
            sent += 1
            self.store.log_event({
                "type": "release_timeout.reminded", "at": _now(),
                "thread": thread, "assignment": aid, "status":
                str(rec.get("status") or ""), "started_at": start,
                "ok": bool(result.get("ok"))})
        return sent

    # --------------------------------------------------------------- dispatch
    def _dispatch(self, action: dict, *, reuse_action_id: str | None = None) -> dict:
        """The only exit to the outside world.

        Order is load-bearing: the project allowlist is checked first (an
        unlisted issue/action never becomes an external action), then
        ``prepared`` is persisted, then the counter moves, then the call runs.
        A call that raises is left ``in_flight`` -> ``uncertain`` so it is
        reconciled, never blindly resent.

        Counting happens *before* the attempt: an action that was enabled and
        attempted is an external action even if the remote end refuses it.

        ``reuse_action_id`` replays an existing ``prepared`` record (startup
        recovery) instead of opening a new attempt.
        """
        # A verdict never goes to the worker it judges. This covers only the
        # engine's own routing path (``_dispatch``); a process that talks to the
        # session API directly is outside this gate - see the module docstring
        # and the ``_archive_one`` note below.
        gate = delivery_mod.dispatch_refusal(
            action, dispatch_enabled=self.dispatch_enabled, mode=self.mode,
            sid_archived=self._sid_archived)
        if gate is not None:
            if gate["gate"] == "qa_to_worker":
                self.store.log_event({
                    "type": "route.refused", "at": _now(),
                    "action": action.get("kind"), "issue": action.get("issue"),
                    "thread": action.get("thread"),
                    "origin_role": action.get("origin_role"),
                    "target_role": action.get("target_role"),
                    "reason": gate["reason"]})
                return {"dispatched": False, "refused": True,
                        "reason": gate["reason"]}
            if gate["gate"] == "disabled":
                self.store.log_event({
                    "type": "dispatch.suppressed", "at": _now(),
                    "mode": gate.get("mode"), "action": action.get("kind"),
                    "issue": action.get("issue")})
                return {"dispatched": False, "reason": gate["reason"]}
            # Refused every round, but **logged once**: archive is a reversible
            # operational act, so this is not a permanent refusal.
            self._note_archived_sid(gate["sid"], action)
            return {"dispatched": False, "refused": True,
                    "reason": gate["reason"]}

        verb = str(action.get("kind") or "")
        issue = action.get("issue")
        allowed, reason = self.allowlist_allows(issue, self._action_name(action))
        if not allowed:
            self.store.log_event({
                "type": "dispatch.refused", "at": _now(), "action": verb,
                "issue": issue, "reason": reason,
            })
            return {"dispatched": False, "reason": reason, "refused": True}

        aid = reuse_action_id or action_id(
            action.get("assignment_id"), verb,
            str(action.get("input_ref") or issue or ""))
        prepared = self.store.begin_action(
            aid, verb=verb, kind=verb,
            assignment_id=action.get("assignment_id"),
            input_ref=str(action.get("input_ref") or issue or ""),
            payload=action.get("payload") or {},
            issue=issue,
            reuse=bool(reuse_action_id))
        # A second attempt of the same action carries a ``#<n>`` suffix; every
        # later transition must use that key or it would resurrect the base id
        # and overwrite the earlier (possibly ``uncertain``) record.
        aid = str(prepared["action_id"])
        total = self.store.bump_external_actions()
        self.store.mark_in_flight(aid)
        ok, err = False, ""
        try:
            if action.get("kind") == "bus_inject":
                if self.bus is None:
                    raise RuntimeError("no bus client configured")
                reply = self.bus.inject(str(action.get("event")),
                                        action.get("payload") or {})
                if isinstance(reply, dict) and reply.get("ok") is False:
                    raise RuntimeError(str(reply.get("error") or "bus refused the event"))
                ok = True
            else:
                raise RuntimeError(f"unknown action kind {action.get('kind')!r}")
        except Exception as exc:  # noqa: BLE001  a failed action must not abort the round
            err = f"{type(exc).__name__}: {exc}"
        if ok:
            self.last_successful_action_at = _now()
            self.store.mark_done(aid)
        else:
            self.store.mark_uncertain(aid, err)
        self.store.log_outbox({
            "at": _now(), "kind": action.get("kind"), "issue": issue,
            "event": action.get("event"), "ok": ok, "error": err,
            "action_id": aid, "external_actions_total": total,
        })
        return {"dispatched": True, "ok": ok, "error": err,
                "external_actions_total": total}

    # ------------------------------------------------------- assignments (ISS-20)
    def _load_issue_sessions(self) -> dict:
        """Read the map through the broker's one reader (#129), not a second chain.

        An undeclared root is ``{}``, the same answer the inline reader gave:
        ``SessionStreamNotConfigured`` is a ``FileNotFoundError``, and the map is
        a cross-check, not the source of truth.
        """
        try:
            return session_broker.read_issue_sessions(self.cfg)
        except OSError:
            return {}

    def _issue_session_keys(self, thread: str) -> list[str]:
        """Candidate ``issue_sessions.json`` keys for ``thread``, strongest first.

        **The two sides do not share one key space, and pretending they do is
        how a binding becomes unreadable.** ``gh_issue.bind`` writes
        ``<project>#<n>`` - the workbench's short project name from its
        ``config/gh_projects.json`` - while this engine keys a thread by
        ``issuekey.thread_key`` -> ``<owner>/<repo>#<n>``. Reading only the
        qualified key makes every ``at`` binding written by the CLI invisible,
        which is the "green test, nothing in production" shape this lookup
        exists to close.

        So the qualified key stays first (it is the canonical form and the one
        a future writer should use), then the two spellings the workbench
        actually produces, then - **only for a thread that is itself a bare
        number** - the legacy numeric key. A qualified thread never falls back
        to a bare number: two repositories share issue numbers, and a silent
        bare-number match would hand one repository's answer to the other.
        """
        text = str(thread or "").strip()
        if not text:
            return []
        keys = [text]
        repo, number = issuekey.split_key(text)
        if number is None:
            return keys

        def add(candidate: str) -> None:
            if candidate and candidate not in keys:
                keys.append(candidate)

        # The project name is only a spelling of **this repo's** binding. Taken
        # from the issue number alone it would claim whichever project happens
        # to list that number - and two repositories share numbers, so
        # ``other/repo#64`` would read ``this_project#64``'s AT.
        project = self._project_for_repo(repo) if repo else self._project_for(number)
        if not repo:
            # Bare-number thread: the numeric key is this thread's own spelling.
            if project:
                add(f"{project}#{number}")
            return keys
        add(f"{repo.rsplit('/', 1)[-1]}#{number}")
        if project:
            add(f"{project}#{number}")
        return keys

    def _project_for_repo(self, repo: str) -> str | None:
        """Project name whose ``repo`` is ``repo``; ``None`` when none matches."""
        wanted = issuekey.repo_full(repo)
        if not wanted or self.projects is None or self.projects.data is None:
            return None
        for name, entry in self.projects.data["projects"].items():
            if issuekey.repo_full((entry or {}).get("repo")) == wanted:
                return name
        return None

    def _issue_session_entry(self, thread: str) -> dict:
        """The ``issue_sessions.json[thread]`` record, found across key spaces.

        Returns ``{}`` when no spelling matches - never the first record that
        happens to exist, because a wrong recipient is the failure nobody sees.
        """
        ledger = self._load_issue_sessions()
        for key in self._issue_session_keys(thread):
            entry = ledger.get(key)
            if isinstance(entry, dict):
                return entry
        return {}

    def _sync_assignments(self, index: dict, thread: str, issue: int) -> dict:
        """Create/refresh assignments for ``thread`` from the session stream.

        The stream is the source of truth; ``issue_sessions.json`` only
        cross-checks it. When the stream cannot be read this refuses to create
        anything and marks the engine degraded - an empty loop that looks
        healthy is the one outcome this must never produce.
        """
        try:
            events_path, _sessions = self._session_stream()
            events = session_events.load_events(events_path)
            state = session_events.replay(events)
        except Exception as exc:  # noqa: BLE001  fail closed, report why
            self.degraded = True
            self._assignments_error = fault_report_mod.describe(exc)
            self.store.log_event({"type": "assignment.stream_unreadable", "at": _now(),
                                  "thread": thread, "error": self._assignments_error})
            return {"created": [], "updated": [], "conflict": [],
                    "unreadable": self._assignments_error}
        live = asg_mod.live_bindings(state, thread, issue)
        # The lifecycle stream is the source of truth *except* for archiving:
        # an archive done through the session API writes no ``session.archived``
        # row, so a stream-only view keeps treating a closed session as a live
        # binding and the next round forks ``asg_<issue>_<role>_2`` on the same
        # sid. Ask the server about the *candidates only* (one cached answer per
        # sid per round) and drop the confirmed-archived ones.
        offstream = sorted({b["sid"] for b in live if self._sid_archived(b["sid"])})
        if offstream:
            self.store.log_event({"type": "assignment.archived_offstream",
                                  "at": _now(), "thread": thread,
                                  "issue": int(issue), "sids": offstream,
                                  "source": "session_info"})
            live = asg_mod.live_bindings(state, thread, issue,
                                         archived=set(offstream))
        crosscheck = set()
        ledger = self._load_issue_sessions().get(thread) or {}
        if isinstance(ledger, dict):
            for entry in ledger.values():
                sid = str((entry or {}).get("session") or "")
                if sid:
                    crosscheck.add(sid)
        requirement = None
        if self._at_mode(issue):
            requirement = self._requirement_current(index, thread)
            held = asg_mod.supersede(index, thread, requirement)
            if held:
                self.store.log_event({"type": "assignment.waiting_requirement",
                                      "at": _now(), "thread": thread,
                                      "assignments": held,
                                      "revision": (requirement or {}).get("revision")})
            if not self._at_sid(thread):
                self._append_needs_person(index, thread, {
                    "comment_id": 0, "reason": "at.session_missing[user]",
                    "at": _now()})
            if not requirement or str(requirement.get("status")) not in (
                    "approved", "dispatched"):
                if live:
                    self.store.log_event({"type": "assignment.requirement_refused",
                                          "at": _now(), "thread": thread,
                                          "status": (requirement or {}).get("status"),
                                          "bindings": len(live)})
                    self._append_needs_person(index, thread, {
                        "comment_id": 0,
                        "reason": "requirement.not_approved[user]", "at": _now()})
                return {"created": [], "updated": [], "conflict": [],
                        "crosscheck": [], "resumed": [],
                        "requirement_refused": True}
        out = asg_mod.sync(index, thread, issue, live, crosscheck=crosscheck,
                           requirement=requirement)
        if out["conflict"]:
            self.store.log_event({"type": "assignment.source_conflict", "at": _now(),
                                  "thread": thread, "conflict": out["conflict"]})
        return out

    def _record_waiting_review(self, assignment: dict) -> dict:
        """Log one ``assignment.waiting_review`` row, at most once per delivery.

        This deliberately sends **nothing**: #143 established that the
        waiting-review bus event had no consumer anywhere in the repo
        (``decide_watch.NOTICE_EVENT_TYPES`` never listed it), so the bus
        write was cost with no reader. A PM learns about a delivery through
        the delivery leg, which reads the ledger - not through this row.

        What survives is the audit row: it is the only local evidence that a
        delivery reached ``waiting_review`` and which requirement version it
        carried, so the AT revision/hash binding stays checkable after the
        fact. Dropping it would leave no trace at all - a silent success,
        which ``fail_loud.md`` forbids as much as a silent failure.
        """
        delivery = assignment.get("delivery_comment_id")
        if delivery is None:
            return {"recorded": False, "reason": "no delivery recorded"}
        key = action_id(assignment["id"], "bus_inject", str(delivery))
        if assignment.get("waiting_review_logged") == key:
            return {"recorded": False, "reason": "already recorded",
                    "action_id": key}
        assignment["waiting_review_logged"] = key
        self.store.log_event({
            "type": "assignment.waiting_review", "at": _now(),
            "assignment": assignment["id"],
            "delivery_comment_id": delivery,
            "action_id": key,
            "requirement_id": assignment.get("requirement_id"),
            "revision": assignment.get("revision"),
            "requirement_hash": assignment.get("requirement_hash"),
            "approval_ref": assignment.get("approval_ref"),
        })
        return {"recorded": True, "action_id": key}

    def _on_delivery(self, index: dict, thread: str, issue: int, comment: dict,
                     delivery: dict, summary: dict) -> str:
        """Match a delivery marker to an assignment and notify once.

        Returns the resulting triage state so the caller can count it: a
        delivery that cannot be matched becomes ``needs_person`` (never silently
        dropped), which is the one place this differs from the other outcomes.
        """
        cid = int(comment["id"])
        if self._at_mode(issue):
            assignment, matched_by = self._referenced_assignment(
                index, thread, issue, comment, delivery["name"])
        else:
            assignment, matched_by = asg_mod.match_delivery(
                index, thread, delivery["name"], delivery["marker"])
        if assignment is None:
            self.store.log_event({"type": "assignment.unmatched", "at": _now(),
                                  "thread": thread, "comment_id": cid,
                                  "marker": delivery["name"],
                                  "reason": matched_by})
            return "needs_person", f"unmatched delivery {delivery['name']!r}: {matched_by}"
        moved = asg_mod.on_delivery(assignment, cid, delivery["name"], matched_by)
        if moved["changed"]:
            self.store.log_event({"type": "assignment.delivery", "at": _now(),
                                  "assignment": assignment["id"], "thread": thread,
                                  "comment_id": cid, "matched_by": matched_by,
                                  "requirement_id": assignment.get("requirement_id"),
                                  "revision": assignment.get("revision"),
                                  "requirement_hash": assignment.get("requirement_hash")})
        elif moved.get("refused"):
            self.store.log_event({"type": "assignment.delivery_refused", "at": _now(),
                                  "assignment": assignment["id"], "thread": thread,
                                  "comment_id": cid, "reason": moved.get("reason")})
            return "needs_person", str(moved.get("reason") or "delivery refused")
        # One audit row per delivery (#143): the bus event this used to send had
        # no consumer, so only the local record survives.
        self._record_waiting_review(assignment)
        return "delivery", ""

    # ------------------------------------------------------------- archive (C7)
    def _archiver(self):
        """Archive callable: the configured adapter, else the direct console.

        Kept behind one method so tests inject a fake and the production path
        goes through the same code. Archive is *not* guessed: the returned
        session info is read back and ``archived is True`` is the only success
        signal (the workbench's own archive path does exactly this read-back).
        """
        adapter = getattr(self, "adapter", None)
        if adapter is not None:
            return adapter
        return None

    def _sid_archived(self, sid: str) -> bool:
        """Is this sid archived? Unreadable state counts as archived (closed).

        Two sources, in this order, and the answer is cached for the round:

        1. the lifecycle stream (``rec["archived"]``) - cheap, but incomplete,
           because an archive made through ``POST /api/v1/sessions:archive``
           writes no ``session.archived`` row (only the workbench's own
           ``tools/sess_events.py:emit`` writes that event);
        2. the server's ``session_info(sid)["archived"]``, asked **once per sid
           per round** and only for a sid the stream still calls live - see
           :meth:`_live_archived`.

        Fail **closed** on purpose: if the session service cannot be asked, a
        dispatch to that sid might hit a closed session, and the safe answer is
        "assume closed, refuse the send". The reverse choice would keep pushing
        events into a session that no longer exists.
        """
        if not sid:
            return False
        snapshot = self._archived_cache
        if snapshot is None:
            snapshot = self._refresh_archived_cache()
        if self._archived_unreadable:
            return True
        if snapshot.get(sid):
            # The stream is authoritative when it *did* record the archive.
            return True
        # ``False`` only means "no archive event was written", which is the
        # normal state after an API-side archive. Ask the server.
        return self._live_archived(sid)

    def _archived_noted_key(self, sid: str, action: dict) -> tuple:
        """Forwarder: :func:`engine_parts.archive.refusal_key`."""
        return archive_mod.refusal_key(sid, action)

    def _note_archived_sid(self, sid: str, action: dict) -> None:
        """Write ``dispatch.archived_sid`` at most once per refusal identity.

        The refusal itself is unchanged and still happens every round: a route
        that comes back from ``restore`` must be able to deliver, so nothing is
        marked permanent here (RULES §33 - archive is reversible, unlike a
        closed issue). What is bounded is the *event*: the check is cheap, the
        identical log line every round was not (#84: 229 rows for one sid).

        The memory of "already written" is the event stream itself, read once
        per process and then kept in :attr:`_archived_noted` (see
        :func:`engine_parts.archive.load_noted`).
        """
        key = self._archived_noted_key(sid, action)
        if self._archived_noted is None:
            self._archived_noted = self._load_archived_noted()
        if key in self._archived_noted:
            return
        self._archived_noted.add(key)
        self.store.log_event({"type": "dispatch.archived_sid", "at": _now(),
                              "sid": sid,
                              "action": action.get("kind"),
                              "assignment": action.get("assignment_id")})

    def _load_archived_noted(self) -> set:
        """Forwarder: :func:`engine_parts.archive.load_noted`."""
        return archive_mod.load_noted(self.store.events_path,
                                      self._archived_noted_key)

    def _live_archived(self, sid: str) -> bool:
        """Ask the server about one sid; cached for the round, fail-closed.

        The cache holds a per-round answer *per sid*, so a round with ten
        candidates makes at most ten ``session_info`` calls and a dispatch
        check on the same sid later in that round makes none.
        """
        if not self.archived_crosscheck:
            # Stream-only mode: no server call, so ``False`` means "the stream
            # has no archive event", not "the session is live". That answer is
            # silently optimistic, so leave one trace the first time it is
            # given: otherwise a production config that turned the check off
            # looks exactly like a healthy stream.
            if not self._crosscheck_off_logged:
                self._crosscheck_off_logged = True
                self.store.log_event({
                    "type": "engine.crosscheck_disabled", "at": _now(), "sid": sid,
                    "detail": "engine.archived_crosscheck is off: an archived sid "
                              "that wrote no session.archived row is treated as live",
                })
            return False
        cache = self._live_check
        if sid in cache:
            return cache[sid]
        try:
            info = self._session_api().session_info(sid)
        except Exception as exc:  # noqa: BLE001  unreachable means "closed"
            cache[sid] = True
            # "Unknown" is not "archived": do not fork a second assignment, but
            # tell the round, with the cause, instead of passing as success.
            self._probe_fault = fault_report_mod.one_line(exc)
            self._probe_fatal = source_faults_mod.is_fatal(exc)
            self.store.log_event({"type": "assignment.archived_unreachable",
                                  "at": _now(), "sid": sid,
                                  "error": f"{type(exc).__name__}: {exc}",
                                  "trace": fault_report_mod.describe(exc)})
            return True
        if not isinstance(info, dict) or info.get("_err"):
            cache[sid] = True
            self._probe_fault = str((info or {}).get("_err") or "no session info")
            self.store.log_event({"type": "assignment.archived_unreachable",
                                  "at": _now(), "sid": sid,
                                  "error": self._probe_fault})
            return True
        cache[sid] = bool(info.get("archived"))
        return cache[sid]

    def _refresh_archived_cache(self) -> dict:
        """One refresh per round of ``sid -> archived`` from the session stream.

        Every sid the stream mentions is cached, archived **and** live. A sid
        that is absent from the cache is unknown to the stream and therefore
        needs the server check in :meth:`_live_archived`.

        "Unavailable" means *no segment exists*, decided by
        :func:`session_events.read_stream` - not ``path.is_file()``. The writer
        rotates the current file to ``.1``, and the old ``is_file()`` test read
        that instant as an unreadable stream: every sid then became "unknown"
        and every binding fell to the server check (ISS-79, same family as the
        two silent points in the reader).
        """
        try:
            events_path, _sessions = self._session_stream()
            events, missing = session_events.read_stream(events_path)
        except Exception:  # noqa: BLE001  unreadable means "assume archived"
            events, missing = [], True
        cache, unreadable = archive_mod.from_stream(events, missing)
        self._archived_cache = cache
        self._archived_unreadable = unreadable
        return cache

    def archive_thread(self, thread: str, index: dict | None = None) -> dict:
        """Archive a worker whose review passed, idempotently.

        The archive is gated by :func:`assignments.qa_gate` and the gate sits
        **between the idempotency check and ``begin_action``**: ``begin_action``
        is the first side effect of the pass (it persists a ``prepared`` record
        and moves the try counter), so a refusal placed after it would already
        have written state. Refused assignments are skipped with
        ``assignment.qa_gate_refused`` and stay ``waiting_review`` - a thread
        that is still waiting for QA is not a fault, and inventing a ``blocked``
        state for it would make every in-flight delivery look broken.

        Success moves the assignment to ``waiting_release``, **not**
        ``completed``: the work is done and archived, but the Issue is not
        proved closed yet. ``completed`` is written by the close reconciliation
        (four conditions, two rounds), never here.

        Nothing here waits on a notification any more: #143 removed the bus
        event this pass used to require, so ``eligible`` no longer asks whether
        one was attempted. The tombstone still records ``notified: false`` -
        that is now the honest, constant answer rather than a failure case.
        """
        out = {"archived": [], "skipped": [], "blocked": [], "errors": []}
        save = index is None
        index = self.store.load_index() if index is None else index
        for aid, rec in sorted((index.get("assignments") or {}).items()):
            if str(rec.get("thread") or "") != thread:
                continue
            decision, detail = archive_mod.eligible(
                aid, rec,
                archive_attempted=lambda a, d: self.store.action_attempted(
                    action_id(a, "archive", str(d))))
            if decision == "skip":
                out["skipped"].append(aid)
                continue
            delivery = rec.get("delivery_comment_id")
            # The gate, and its position: after the idempotency checks in
            # ``eligible``, before ``begin_action`` below - which is the first
            # side effect (a persisted ``prepared`` record and a bumped try
            # counter). A gate placed after it would have written state it then
            # refuses.
            gate_ok, gate_why = asg_mod.qa_gate(rec)
            if not gate_ok:
                # First sighting + a 30-round re-alert; see engine_parts.archive.
                archive_mod.note_qa_refusal(self, index, aid, rec,
                                            gate_why, _now())
                out["skipped"].append(aid)
                continue
            sid = str(rec.get("sid") or "")
            if not sid:
                self._mark_blocked(rec, asg_mod.NO_SID_REASON)
                out["blocked"].append(aid)
                continue
            base = action_id(aid, "archive", str(delivery))
            self.store.begin_action(base, verb="archive", kind="archive",
                                    assignment_id=aid, input_ref=str(delivery),
                                    payload={"sid": sid},
                                    issue=rec.get("issue"))
            ok, error = self._archive_one(sid)
            archived = self._read_back_archived(sid)
            if ok and archived:
                self.store.mark_done(base, sid=sid)
                archive_mod.record_archived(rec, delivery=delivery, now=_now())
                # Only the archive counter is prunable: it is terminal.
                self.store.prune_attempts(base)
                out["archived"].append(aid)
            else:
                self._mark_blocked(rec, error or "archive not confirmed by read-back",
                                   cid=delivery)
                self.store.mark_uncertain(base, error or "archive unconfirmed")
                out["blocked"].append(aid)
        if save:
            self.store.save_index(index)
        return out

    def _session_api(self):
        if self.session_api is not None:
            return self.session_api
        from . import api
        return api

    def _archive_one(self, sid: str) -> tuple[bool, str]:
        adapter = self._archiver()
        try:
            if adapter is not None and hasattr(adapter, "archive"):
                result = adapter.archive({"id": "", "sid": sid}, "thread completed")
                return bool(result.get("ok")), str(result.get("error") or "")
            ok, _payload = self._session_api().archive_session(sid)
            return bool(ok), ""
        except Exception as exc:  # noqa: BLE001  reported as a blocked archive
            return False, f"{type(exc).__name__}: {exc}"

    def _read_back_archived(self, sid: str) -> bool:
        """``archived is True`` for ``sid``; False on any doubt."""
        try:
            info = self._session_api().session_info(sid)
        except Exception:  # noqa: BLE001
            return False
        return bool((info or {}).get("archived"))

    def _mark_blocked(self, rec: dict, reason: str, *, cid=None) -> None:
        """Block one assignment through the shared tombstone writer.

        ``cid`` follows the rule the state machine documents: ``None`` for a
        failure that happened before a delivery was acted on (no sid), the
        delivery id once the archive started. The attempt counter is monotonic,
        so the same delivery failing twice is attempt 2 rather than a reset key.
        """
        asg_mod.mark_blocked(rec, reason, cid=cid, at=_now())

    def _issue_state(self, issue) -> str:
        """``source.issue_state(issue)``, failures folded to ``unknown``.

        Fail closed: an unreadable probe must never look like a closed Issue.
        """
        number = int(issue)
        return self._closed_states.state(
            number, lambda: issue_state_mod.read_state(
                self.source, number, self.store.log_event, _now))

    # ------------------------------------------------- close reconciliation
    def _binding_roles(self, thread: str) -> list[str]:
        """Roles still registered for ``thread``, per :mod:`engine_parts.close`."""
        return close_mod.binding_roles(self._load_issue_sessions().get(thread))

    def _running_tasks(self, sid: str):
        """``(running, skipped)`` for one sid; the judgement is in the module.

        The probe itself stays here because it is the only place that decides
        which client answers (``direct.tasks``), and an unreadable table is
        reported as ``unreadable:`` rather than as idle.
        """
        if not sid:
            return [], "no sid"
        try:
            from . import direct
            rows = direct.tasks(sid)
        except Exception as exc:  # noqa: BLE001  an unreadable table is not "idle"
            return [], f"unreadable: {type(exc).__name__}"
        return close_mod.running_tasks(rows)

    def close_conditions(self, thread: str, index: dict) -> dict:
        """The four conditions that make a closed thread provable.

        Every answer is a live read: ``_read_back_archived`` asks the server
        and is **not** cached, so two rounds that both say "archived" are two
        independent observations rather than one repeated from memory.

        Forwarder: the decision lives in :func:`engine_parts.close.conditions`,
        which takes the readers as arguments and never touches the store.
        """
        recs = [rec for rec in (index.get("assignments") or {}).values()
                if str(rec.get("thread") or "") == thread]
        issue = None
        for rec in recs:
            if rec.get("issue") is not None:
                issue = int(rec.get("issue"))
                break
        return close_mod.conditions(
            recs,
            issue_state=(self._issue_state(issue) if issue is not None
                         else "unknown"),
            binding_roles=self._binding_roles(thread),
            archived_of=self._read_back_archived,
            running_of=self._running_tasks)

    def _close_blocked_reason(self, snapshot: dict) -> str:
        """Forwarder: :func:`engine_parts.close.blocked_reason`."""
        return close_mod.blocked_reason(snapshot)

    def _cycle_start(self, rec: dict) -> str:
        """Forwarder: :func:`engine_parts.close.cycle_start` - the current cycle."""
        return close_mod.cycle_start(rec, _now())

    def _reconcile_thread(self, index: dict, thread: str) -> dict:
        """One observation of one held thread; the record lives in the index.

        Forwarder: the state machine (four conditions, two consecutive rounds,
        a refusal that keeps the record ``pending``) lives in
        :func:`engine_parts.close.reconcile`. The engine supplies only the two
        readers and the side effects it owns, so a loop inside one round still
        cannot confirm itself - ``run_count`` is read from the index, not held
        here.
        """
        return close_mod.reconcile(
            index, thread,
            conditions=lambda: self.close_conditions(thread, index),
            enter_reconcile=asg_mod.enter_close_reconcile,
            complete=asg_mod.complete,
            clear_refused=self._clear_refused,
            log=self.store.log_event, now=_now())

    def _round_closed_threads(self, index: dict) -> list[dict]:
        """Check held releases only; never batch-close unfinished assignments."""
        engine_cfg = self.cfg.get("engine") or {}
        try:
            limit = max(1, int(engine_cfg.get("max_closed_thread_checks") or 10))
        except (TypeError, ValueError):
            limit = 10
        threads = sorted({str(rec.get("thread") or "")
                          for rec in (index.get("assignments") or {}).values()
                          if str(rec.get("status") or "") in
                          ("waiting_release", "close_reconcile")})
        out = []
        for thread in [t for t in threads if t][:limit]:
            try:
                out.append(self._reconcile_thread(index, thread))
            except Exception as exc:  # noqa: BLE001  one bad thread is isolated
                self.store.log_event({"type": "close_reconcile.blocked",
                                      "at": _now(), "thread": thread,
                                      "reason": f"{type(exc).__name__}: {exc}"})
                out.append({"thread": thread, "status": "pending",
                            "completed": [], "reason": str(exc)})
        return out

    def reconcile_close(self, thread: str, index: dict | None = None) -> dict:
        """Close a thread: every assignment completed, then reconcile the stream."""
        index = index if index is not None else self.store.load_index()
        recs = [rec for rec in (index.get("assignments") or {}).values()
                if str(rec.get("thread") or "") == thread]
        unfinished = [rec["id"] for rec in recs
                      if str(rec.get("status") or "") not in asg_mod.TERMINAL_STATES]
        if unfinished:
            return {"ok": False, "reason": "assignments not completed",
                    "unfinished": sorted(unfinished)}
        try:
            events_path, sessions_path = self._session_stream()
            report = session_events.reconcile(events_path, sessions_path, None)
            server_supplied = bool(report.get("server_supplied"))
            self.store.log_event({"type": "thread.reconciled", "at": _now(),
                                  "thread": thread, "ok": bool(report.get("ok")),
                                  "server_supplied": server_supplied,
                                  "diff": report.get("diff_non_empty") or []})
            return {"ok": bool(report.get("ok")),
                    "server_supplied": server_supplied,
                    "diff": report.get("diff_non_empty") or []}
        except Exception as exc:  # noqa: BLE001  a failed reconcile is reported
            self.store.log_event({"type": "thread.reconciled", "at": _now(),
                                  "thread": thread, "ok": False,
                                  "error": f"{type(exc).__name__}: {exc}"})
            return {"ok": False, "server_supplied": False,
                    "error": f"{type(exc).__name__}: {exc}"}

    # ------------------------------------------------------------------- round
    def run_once(self) -> dict:
        """One round. ``cli once`` and every iteration of ``cli run`` use this."""
        return self.run_round()

    def recover(self) -> dict:
        """Startup recovery: poll the allowlist, then reconcile the outbox.

        Only ``prepared`` actions are replayed (they are certain not to have
        run). ``in_flight`` becomes ``uncertain`` and waits for reconciliation.
        Nothing here resends a create/send/comment.
        """
        if self.projects is not None:
            try:
                self.projects.poll()
            except Exception as exc:  # noqa: BLE001  hot reload never crashes a round
                self.store.log_event({"type": "allowlist.error", "at": _now(),
                                      "error": f"{type(exc).__name__}: {exc}"})
        result = self.store.recover_outbox()
        self._recovered = True
        self.store.log_event({"type": "outbox.recovered", "at": _now(),
                              "replayable": len(result["replayable"]),
                              "reconcile": len(result["reconcile"]),
                              "moved_to_uncertain": len(result["moved_to_uncertain"])})
        return result

    def startup(self, probe=None) -> dict:
        """The production startup entry: recover -> replay -> reconcile.

        ``cli`` calls this once before the first round. The three steps are
        ordered and each is the *only* place that touches its state:
        ``store.recover_outbox()`` moves ``in_flight`` to ``uncertain``,
        :meth:`replay_prepared` re-issues only the records that are certain not
        to have run, and :meth:`reconcile_uncertain` resolves the rest against
        the remote side - with no probe every one of them lands in
        ``waiting_person``, never silently dropped and never re-sent.

        A failure here is fatal for the startup: the caller must not start a
        round with unresolved actions, and the health snapshot records
        ``degraded``, ``last_error`` and ``phase="startup-failed"`` so a
        watchdog sees why - and sees that this was not a crash of a running
        loop.
        """
        out = {"ok": True, "error": "", "recovered": {}, "replayed": [],
               "reconciled": []}
        try:
            out["recovered"] = self.recover()
            out["replayed"] = self.replay_prepared()
            out["reconciled"] = self.reconcile_uncertain(probe)
        except Exception as exc:  # noqa: BLE001  a failed recovery is a reportable state
            out["ok"] = False
            out["error"] = f"{type(exc).__name__}: {exc}"
            self.store.log_event({"type": "outbox.recovery_failed", "at": _now(),
                                  "error": out["error"]})
            self.write_health(error=f"outbox recovery failed: {out['error']}",
                              degraded=True, running=False,
                              phase="startup-failed")
            return out
        self.store.log_event({
            "type": "outbox.startup", "at": _now(),
            "replayed": len(out["replayed"]),
            "reconciled": [r["outcome"] for r in out["reconciled"]],
            "waiting_person": sum(1 for r in out["reconciled"]
                                  if r["outcome"] == "waiting_person"),
        })
        return out

    def _prepared_requirement_error(self, rec: dict) -> str:
        issue = rec.get("issue")
        selected, flow_error = self._flow_selection(issue)
        if flow_error:
            return f"invalid Issue flow: {flow_error}"
        index = self.store.load_index()
        assignment = (index.get("assignments") or {}).get(str(rec.get("assignment_id") or ""))
        if assignment is not None and str(assignment.get("status") or "") == "blocked" \
                and assignment.get("flow_hold_reason"):
            return "prepared action Assignment is paused by a flow change"
        if selected != flow_mod.AT:
            return ""
        outer = rec.get("payload") or {}
        payload = outer.get("payload") if isinstance(outer.get("payload"), dict) else outer
        reference = {key: payload.get(key) for key in
                     ("requirement_id", "revision", "requirement_hash")}
        if not all(reference.values()):
            return "AT-mode prepared action has no requirement version"
        thread = str(payload.get("thread") or self._thread_key(issue))
        package = self._requirement_current(index, thread)
        if not package or str(package.get("status")) not in ("approved", "dispatched"):
            return "prepared action requirement is no longer approved"
        if not requirements.reference_matches(reference, package):
            return "prepared action belongs to a superseded requirement"
        if assignment is not None:
            if str(assignment.get("status") or "") == "waiting_requirement":
                return "prepared action Assignment is waiting for a requirement"
            if not requirements.reference_matches(reference, assignment):
                return "prepared action and Assignment versions differ"
        return ""

    def replay_prepared(self) -> list[dict]:
        """Replay only actions recorded as ``prepared`` (certain not to have run).

        ``in_flight`` and ``uncertain`` records are deliberately excluded: they
        may have landed, so they go through :meth:`reconcile_uncertain` instead.
        Each replay reuses the exact record key (``reuse_action_id``): the
        attempt already exists, so it must not fork a second attempt id.
        """
        out = []
        for rec in self.store.recover_outbox()["replayable"]:
            payload = rec.get("payload") or {}
            action = {"kind": rec.get("kind") or rec.get("verb"),
                      "assignment_id": rec.get("assignment_id"),
                      "input_ref": rec.get("input_ref") or rec.get("comment_id"),
                      "issue": rec.get("issue") if rec.get("issue") is not None
                      else payload.get("issue"),
                      "event": payload.get("event"),
                      "payload": payload.get("payload") if isinstance(
                          payload.get("payload"), dict) else payload}
            action.setdefault("event", payload.get("event"))
            requirement_error = self._prepared_requirement_error(rec)
            if requirement_error:
                aid = str(rec.get("action_id"))
                self.store.mark_reconciled(aid, "done",
                                           f"superseded requirement: {requirement_error}")
                self.store.log_event({"type": "outbox.requirement_superseded",
                                      "at": _now(), "action_id": aid,
                                      "reason": requirement_error})
                result = {"dispatched": False, "refused": True,
                          "reason": requirement_error, "action_id": aid}
                out.append(result)
                continue
            result = self._dispatch(action, reuse_action_id=rec.get("action_id"))
            result["action_id"] = rec.get("action_id")
            out.append(result)
        return out

    def reconcile_uncertain(self, probe=None) -> list[dict]:
        """Check each uncertain action against the remote side. Never resends.

        ``probe(record) -> dict`` returns ``{"found": bool, "sid"/"prompt_id"/
        "comment_id": ...}``. ``found`` marks it ``done``; not found (or no
        probe) marks it ``waiting_person`` so a human decides. The action is
        never re-issued from here.
        """
        results = []
        for rec in self.store.reconcile_queue():
            aid = str(rec.get("action_id") or "")
            self.store.mark_reconciling(aid)
            verdict = {"found": False, "detail": "no probe configured"}
            if probe is not None:
                try:
                    verdict = probe(dict(rec)) or verdict
                except Exception as exc:  # noqa: BLE001  a failed probe waits for a person
                    verdict = {"found": False,
                               "detail": f"probe failed: {type(exc).__name__}: {exc}"}
            if verdict.get("found"):
                self.store.mark_done(aid, sid=verdict.get("sid"),
                                     prompt_id=verdict.get("prompt_id"),
                                     comment_id=verdict.get("comment_id"))
                outcome = "done"
            else:
                self.store.mark_reconciled(
                    aid, "waiting_person",
                    verdict.get("detail") or "remote side did not confirm the action")
                outcome = "waiting_person"
            results.append({"action_id": aid, "outcome": outcome,
                            "detail": verdict.get("detail") or ""})
        return results

    def _resolve_closed_needs(self, index: dict) -> int:
        """Mark needs_person rows resolved once their Issue is confirmed closed.

        Forwarder: which rows qualify is decided in
        :mod:`engine_parts.closed_needs` (a pure read of the index). What stays
        here is the per-process state - the "already reported" set for foreign
        repos, the live-issue set, and the event log.
        """
        try:
            limit = max(1, int((self.cfg.get("engine") or {}).get(
                "max_closed_thread_checks") or 10))
        except (TypeError, ValueError):
            limit = 10
        current_repo = issuekey.repo_full((self.cfg.get("github") or {}).get("repo"))
        skipped = getattr(self, "_closed_resolution_skipped", None)
        if skipped is None:
            skipped = self._closed_resolution_skipped = set()
        for thread, repo in closed_needs_mod.foreign_threads(
                index, current_repo=current_repo):
            if thread in skipped:
                continue
            skipped.add(thread)
            self.store.log_event({"type": "needs_person.resolution_skipped",
                                  "at": _now(), "thread": thread,
                                  "repo": repo, "current_repo": current_repo})
        plan = closed_needs_mod.pending_rows(
            index, current_repo=current_repo, limit=limit)
        return closed_needs_mod.resolve(
            index, plan,
            live=getattr(self, "_live_issues", set()),
            issue_state=self._issue_state,
            log_row=lambda row: self.store.log_event({"at": _now(), **row}))

    def run_round(self) -> dict:
        """One round = ``begin_round`` → ``run_issue_phase`` → ``run_global_phase``.

        Kept as a wrapper so every caller (``cli once``, ``cli run``, the tests
        that monkeypatch the round) keeps one entry point. The three phases are
        separated because they answer different questions and fail differently:
        the first decides whether the round may start at all, the second can lose
        one issue without losing the round, and only the third decides whether
        the round counts.
        """
        index, cursor, state, prep = self.begin_round()
        self.run_issue_phase(index, cursor, state, prep)
        return self.run_global_phase(index, cursor, state, prep)

    def begin_round(self) -> tuple[dict, dict, round_mod.RoundState, dict]:
        """Everything that must succeed before a single comment is read.

        A refused cursor raises here, and that is the point: the refusal is not
        a per-issue fault that the issue loop could swallow.
        """
        index = self.store.load_index()
        # Checked before anything else so the health snapshot and the
        # ``index.unreadable`` event exist even when the write-back is refused
        # at the end of the round (see the two save sites below).
        self._index_skip_logged = False
        # Round-scoped: a stream fault from the previous round must not linger
        # in this round's health line after the stream came back.
        self._stream_error = ""
        health_flag_mod.clear(self)   # round-scoped; see engine_parts.health_flag
        self._note_index_state()
        # ISS-79 F: a declared-but-absent session stream must not turn into a
        # quiet round. Checked here, before any issue is read, so the fault is
        # in health and in the event log even when the round finds no comments.
        self._note_stream_state()
        # ISS-74 C(a): resolve the supervisor sid once per round even when no
        # frame needs it. A malformed id otherwise stayed invisible until it was
        # used - and then the symptom was a refused *frame*, not the id. The
        # value is discarded on purpose: this call exists for its side effect
        # (one ``engine.supervisor_sid_malformed`` row, see the docstring).
        self._supervisor_sid(index)
        # A cursor that cannot be trusted is refused **before** anything is
        # read: with a verdict requiring a session key (issue #87), re-reading
        # an issue's whole history turns every older verdict into an
        # ``unverified`` row. ``engine.allow_empty_cursor`` is the explicit
        # "re-scan everything deliberately" answer, never a default.
        allow_empty = bool((self.cfg.get("engine") or {}).get(
            "allow_empty_cursor"))
        cursor = self.store.load_cursor(strict=not allow_empty)
        # Both caches are round-scoped: the stream snapshot is re-read lazily
        # and each candidate sid is asked of the server at most once per round.
        self._archived_cache = None
        self._archived_unreadable = False
        self._probe_fault = ""  # round-scoped: reported by _read_and_consume
        self._live_check = {}
        self._session_state_cache = None
        self._session_map_cache = None
        gh_cfg = self.cfg.get("github") or {}
        max_issues = int(gh_cfg.get("max_issues") or 20)
        max_comments = int(gh_cfg.get("max_comments_per_issue") or 50)
        max_pages = int(gh_cfg.get("max_comment_pages") or 10)

        # Legacy numeric keys are copied forward once, before anything reads
        # them: from here on every write uses the qualified form.
        repo = str(gh_cfg.get("repo") or "")
        if issuekey.is_repo(repo):
            issuekey.migrate(index, cursor, repo, log_event=self.store.log_event)

        # Hot reload before reading anything: a valid new file applies to this
        # round onwards, a broken one leaves the previous copy in force.
        allowlist_reload = ""
        if self.projects is not None:
            try:
                polled = self.projects.poll()
                if polled.get("error"):
                    allowlist_reload = polled["error"]
            except Exception as exc:  # noqa: BLE001  never abort a round
                allowlist_reload = f"{type(exc).__name__}: {exc}"

        summary = {"mode": self.mode, "dispatch_enabled": self.dispatch_enabled,
                   "issues": 0, "comments_new": 0, "processed": 0,
                   "needs_person": 0, "deliveries": 0, "assignments": 0,
                   "read_paused": 0, "errors": []}
        if allowlist_reload:
            summary["allowlist_reload"] = allowlist_reload
        # ``round_seq`` moves here, unconditionally, before anything is read: a
        # round that later fails still has a number, so a stall can be dated and
        # ``round.start``/``round.end`` stay countable. ``run_count`` is the
        # other counter and only advances for a complete round (see
        # ``engine_parts.round``).
        policy = round_mod.round_policy(gh_cfg)
        state = round_mod.RoundState.begin(index, cursor, policy)
        state.summary = summary
        self._round_state = state
        self.store.log_event({"type": "run.started", "at": _now(),
                              "mode": self.mode, "round_seq": state.seq})
        prep = {"max_issues": max_issues, "max_comments": max_comments,
                "max_pages": max_pages, "allowlist_reload": allowlist_reload}
        return index, cursor, state, prep

    def run_issue_phase(self, index: dict, cursor: dict,
                        state: round_mod.RoundState, prep: dict) -> None:
        """Read every live issue; one bad issue never ends the round.

        ``source_ok`` is recorded here because the allowlist sync below is gated
        on it: an issue list that came from a failed request is not "the live
        set", and syncing it would empty the overlay - after which every frame
        for every issue is refused until the source comes back.
        """
        summary = state.summary
        try:
            issue_numbers = self.source.issues(limit=int(prep["max_issues"]))
        except Exception as exc:  # noqa: BLE001  a dead source is a reportable state
            summary["errors"].append(f"issues: {type(exc).__name__}: {exc}")
            # No fallback page (2026-11-28): a failed round reads nothing, so
            # nothing is consumed and nothing is lost; the next good round reads
            # it after creating the assignment it needs (sync precedes triage).
            kind = source_faults_mod.classify(f"{type(exc).__name__}: {exc}")
            summary["source_fatal"] = summary.get("source_fatal") or kind == "fatal"
            issue_numbers = []
            state.source_ok = False
            self.store.log_event({"type": "source.failed", "at": _now(),
                                  "round_seq": state.seq, "kind": kind,
                                  "error": f"{type(exc).__name__}: {exc}",
                                  "trace": fault_report_mod.describe(exc)})
        self._live_issues = {int(issue) for issue in issue_numbers}
        state.issues = [int(issue) for issue in issue_numbers]
        if issue_numbers and len(issue_numbers) >= int(prep["max_issues"]):
            summary["issues_truncated"] = int(prep["max_issues"])  # newest-first
        # The issue itself now says whether it is live (open, not on hold), so
        # the allowlist is kept in step with that instead of being a hand-kept
        # list. A failed sync is a warning: a read-only checkout must still run.
        # A failed **source** is not a sync: leaving the overlay alone is what
        # keeps the in-flight set from being emptied by a network blip.
        if state.source_ok:
            self._sync_allowlist_issues(issue_numbers, summary)
        else:
            summary["allowlist_kept"] = "source failed this round"

        # Whoever the bound refused last round is served first this round. A
        # fixed order plus a fixed cap is not backpressure, it is starvation:
        # the same first ``max_batches`` issues take the slots every round and
        # the tail's cursor never moves (#130 QA P0-4).
        for issue in issue_numbers:
            thread = self._thread_key(issue)
            try:
                self._round_issue(issue, index, cursor, prep["max_comments"],
                                  summary, prep["max_pages"])
            except Exception as exc:  # noqa: BLE001  isolate one bad issue
                summary["errors"].append(f"issue {issue}: {type(exc).__name__}: {exc}")
                state.fail(thread)
                self.store.log_event({"type": "issue.error", "at": _now(),
                                      "issue": int(issue),
                                      "error": f"{type(exc).__name__}: {exc}",
                                      "trace": fault_report_mod.describe(exc)})
        state.round_result.carry = round_mod.carry_remaining(state.round_result)

    def run_global_phase(self, index: dict, cursor: dict,
                         state: round_mod.RoundState, prep: dict) -> dict:
        """Retries, archives and the write-back; decides whether the round counts.

        The archive pass **skips a thread whose read failed this round**: its
        state is partly read, so "nothing is waiting" is not a fact about it. A
        thread that failed is not archived, not closed, and not counted.
        """
        summary = state.summary
        round_result = state.round_result
        summary["round_seq"] = state.seq
        summary["collection_complete"] = round_result.collection_complete
        summary["consumption_complete"] = round_result.consumption_complete
        if round_result.carry:
            summary["carry"] = round_mod.carry_remaining(round_result)
        if state.queue.refused:
            summary["backpressure"] = list(state.queue.refused)

        self._retry_routes(index)
        self._retry_supervisor_routes(index)
        self._retry_pm_frames(index)
        # Archive last: a review that arrived this round is archived this
        # round, and the archived-sid guard applies from the next dispatch on.
        self._archived_cache = None
        for thread in sorted({str(rec.get("thread") or "")
                              for rec in (index.get("assignments") or {}).values()
                              if str(rec.get("status") or "") in
                              ("waiting_review", "waiting_release")}):
            if thread and thread not in round_result.failed:
                result = self.archive_thread(thread, index)
                summary.setdefault("archived", []).extend(result["archived"])
                summary.setdefault("archive_blocked", []).extend(result["blocked"])
        summary["needs_resolved_closed"] = self._resolve_closed_needs(index)

        # Index is written once per round, atomically: a crash leaves the old one.
        # The dismissal side table is monotonic, so unioning whatever is on disk
        # now keeps a ``resolve`` run during this round from being overwritten.
        #
        # Skipped, not merely refused, when the index could not be read: the
        # raise would skip ``write_health`` below - the one snapshot an operator
        # reads during this exact fault.
        if not self.store.index_writable:
            self._skip_write_back()
        else:
            needs.merge_into(index, self.store.load_index())
            index["meta"].update({
                "updated_at": _now(), "mode": self.mode, "role": self.role,
                "dispatch_enabled": self.dispatch_enabled,
            })
            # ``run_count`` moves only for a round that both collected and
            # consumed everything. The close-reconcile streak and the stall
            # watchdogs read this number, so a partial round that bumped it
            # would advance both of them on evidence that does not exist.
            round_mod.complete_round(index, round_result)
            self.store.save_index(index)
            self.store.save_cursor(cursor)
        # Close reconciliation last: the round's own archives are visible to it,
        # and ``run_count`` is already incremented, so the two-round streak can
        # only be advanced by a genuine next round.
        self._round_closed_threads(index)
        self._remind_release_timeouts(index)
        self._check_pm_stall(index)
        self._check_at_stall(index)
        if self.store.index_writable:
            needs.merge_into(index, self.store.load_index())
            self.store.save_index(index)
        self.store.log_event({"type": "run.completed", "at": _now(),
                              "round_seq": state.seq,
                              "round_complete": round_result.round_complete,
                              "processed": summary["processed"],
                              "needs_person": summary["needs_person"],
                              "external_actions": self.store.load_counters()
                              .get("external_actions", 0)})
        summary["external_actions"] = self.store.load_counters().get("external_actions", 0)
        summary["round_complete"] = round_result.round_complete
        summary["run_count"] = round_mod.run_count(index)
        health_errors = list(summary["errors"])
        allowlist_reload = prep.get("allowlist_reload") or ""
        if allowlist_reload:
            health_errors.append(f"allowlist kept previous copy: {allowlist_reload}")
        # ISS-79 F: the stream fault is a round-level reason, so it belongs in
        # the same health line as the other ones instead of living only on a
        # private attribute where a reader would never see it.
        if self._stream_error:
            health_errors.append(self._stream_error)
        # Snapshot first, then date a recovery across the two published ones;
        # see ``engine_parts.health_flag.publish_round_health``.
        health_flag_mod.publish_round_health(
            self, "; ".join(health_errors), now=_now(), round_seq=state.seq)
        return summary

    def _note_collected(self, thread: str, batch, cursor_before: int,
                        fresh, summary: dict, index: dict | None = None,
                        cursor: dict | None = None) -> bool:
        """Forwarder: the six decisions live in :mod:`engine_parts.consume`."""
        return consume_mod.note_collected(self, thread, batch, cursor_before,
                                          fresh, summary, index=index,
                                          cursor=cursor)

    def _note_consumed(self, thread: str, consumed) -> None:
        """Forwarder: batch and cursor prefix move together (see ``consume``)."""
        consume_mod.note_consumed(self, thread, consumed)

    def _save_requirement_projection(self, index: dict, key: str) -> None:
        """Persist the projection; a write failure is logged, never raised."""
        try:
            requirements.save_projection(self.store.root, index["requirements"])
        except OSError as exc:
            self.store.log_event({
                "type": "requirement.projection_write_failed",
                "at": _now(), "thread": str(key),
                "error": f"{type(exc).__name__}: {exc}"})

    def _requirement_index(self) -> dict:
        """The live ``requirements`` block of the index (loaded, never cached)."""
        return self.store.load_index().get("requirements") or {}

    def _flow_gate(self, issue: int, index: dict, key: str, rec: dict,
                   summary: dict) -> bool:
        """Does this issue pass the flow gate? ``False`` means "stop this issue".

        A conflicting tag pair stops it (nobody may guess which branch applies),
        and a **changed** flow pauses the unfinished assignments from the old
        branch: an Assignment bound to one branch is not carried into the other.
        The switch is reported once, with the paused ids, so a person can see
        what stopped.
        """
        selected_flow, flow_error = self._flow_selection(issue)
        previous_flow = str(rec.get("flow") or "")
        rec["flow"] = selected_flow
        if flow_error:
            paused = asg_mod.pause_for_flow(index, key, "flow.conflict[user]")
            rec["state"] = "needs_person"
            self._append_needs_person(index, key, {
                "comment_id": 0, "reason": "flow.conflict[user]", "at": _now()})
            summary["needs_person"] += 1
            self.store.log_event({"type": "flow.rejected", "at": _now(),
                                  "issue": issue, "reason": flow_error,
                                  "assignments_paused": paused})
            return False
        if previous_flow in (flow_mod.AT, flow_mod.STANDARD) \
                and previous_flow != selected_flow:
            reason = f"flow.changed:{previous_flow}->{selected_flow}"
            paused = asg_mod.pause_for_flow(index, key, reason)
            self.store.log_event({"type": "flow.changed", "at": _now(),
                                  "issue": issue, "previous": previous_flow,
                                  "current": selected_flow,
                                  "assignments_paused": paused})
            self._append_needs_person(index, key, {
                "comment_id": 0, "reason": "flow.switch_pause[user]", "at": _now()})
        return True

    def _round_issue(self, issue: int, index: dict, cursor: dict,
                     max_comments: int, summary: dict,
                     max_pages: int = 10) -> None:
        """One issue: flow gate, read, requirement gate, consumption.

        The flow gate comes first because a conflicting or switched flow must
        stop this issue before a single comment is triaged: the branch decides
        what the comments mean, so reading them under the wrong one would attach
        them to the wrong state machine.
        """
        issue = int(issue)
        key = self._thread_key(issue)
        legacy = str(issue)
        summary["issues"] += 1
        issues = index.setdefault("issues", {})
        rec = issues.setdefault(key, {"state": "idle", "comments_seen": 0,
                                      "needs_person": []})
        try:
            self._read_and_consume(issue, index, cursor, max_comments, summary,
                                   max_pages, key, legacy, rec)
        finally:
            # The queue is drained per issue, always - including on the path
            # where this issue produced nothing. The first delivery only ever
            # ``put``, so the bound filled with the first ``max_batches`` issues
            # and every later one was refused for ever; that is starvation, not
            # backpressure (#130 QA P0-4).
            consume_mod.drain_queue(self, key, summary)

    def _read_and_consume(self, issue: int, index: dict, cursor: dict,
                          max_comments: int, summary: dict, max_pages: int,
                          key: str, legacy: str, rec: dict) -> None:
        """The read → gate → triage → cursor chain for one issue."""
        if not self._flow_gate(issue, index, key, rec, summary):
            return
        rec["title"] = rec.get("title") or self.source.title(issue)
        # Qualified key first, legacy second: a mixed file converges instead of
        # re-reading comments already consumed under the old form.
        cur = int(issuekey.read_both(cursor.get("issues") or {}, key, legacy) or 0)
        every = int((self.cfg.get("github") or {}).get("delivery_recovery_every") or 3)
        seq = int((index.get("meta") or {}).get("round_seq") or 0)
        qa_recovery, delivery_recovery = collect_mod.recovery_flags(
            index.get("assignments") or {}, key,
            terminal=asg_mod.TERMINAL_STATES,
            output_cids=asg_mod.assignment_cids,
            due=every <= 1 or seq % every == 0)
        if delivery_recovery:
            self.store.log_event({"type": "delivery.recovery_read", "at": _now(),
                                  "thread": key, "issue": int(issue),
                                  "reason": "worker live, no delivery recorded"})
        at_mode = self._at_mode(issue)
        read = collect_mod.read_issue(
            self.source, issue, key, rec, cur, at_mode=at_mode,
            qa_recovery=qa_recovery, delivery_recovery=delivery_recovery,
            max_comments=max_comments,
            max_pages=max_pages, summary=summary,
            pause_read=lambda rows, budget: self._pause_read(
                index, key, issue, cur, rows, budget, summary),
            log=self.store.log_event)
        fresh = read.fresh
        history_rows = read.history_rows
        history_done = read.history_done
        rows_read = read.rows_read
        paused_now = read.paused_now
        read_paused = read.skipped
        accepted = True
        if not read.skipped:
            if not paused_now and read.batch is not None:
                accepted = self._note_collected(key, read.batch, cur, fresh,
                                                summary, index=index,
                                                cursor=cursor)
            # A refused batch was not collected, so it may not be consumed: the
            # rows stay above the cursor and the next round reads them again.
            # Consuming here and advancing the cursor is exactly the loss the
            # bound exists to prevent.
            if accepted:
                summary["comments_new"] += len(fresh)
        if at_mode and not read_paused:
            # Rebuild from the Issue, not from the cursor: approval may predate
            # at_mode being enabled. The projection is only a disposable cache.
            # The gate (and why a prefix must never be written as if it were
            # the listing) lives in ``engine_parts.requirement_gate``.
            requirement_gate_mod.apply(
                index, key, issue, history_done=history_done,
                history_rows=history_rows, fresh=fresh, rows_read=rows_read,
                paused_now=paused_now,
                pause_history=lambda rows: read_pause_mod.pause_history(
                    index, key, issue, rows, summary,
                    reason="the requirement history walk did not reach the "
                           "last page",
                    append=self._append_needs_person, log=self.store.log_event),
                apply_rows=lambda rows: self._apply_requirement_comments(
                    index, key, rows),
                save_projection=lambda: self._save_requirement_projection(
                    index, key),
                log=self.store.log_event)
        # Requirement projection precedes session discovery: a live binding is
        # not permission to work before the exact revision has been approved.
        synced = self._sync_assignments(index, key, issue)
        summary["assignments"] += len(synced.get("created") or [])
        if self._probe_fault:
            summary["errors"].append(f"session probe: {self._probe_fault}")
            if self._probe_fatal:
                summary["source_fatal"] = True  # same rule as the source: stop now
            self._probe_fault, self._probe_fatal = "", False

        triage_batch_mod.triage_batch(self, index, issue, key, rec, fresh,
                                      accepted=accepted, summary=summary)

        if fresh and accepted:
            self._advance_cursor(key, cursor, cur, fresh)
        else:
            # Never move the cursor on a round that consumed nothing: a paused
            # issue (no read at all) must keep its position byte for byte, and a
            # batch the queue **refused** was not consumed either - advancing
            # here would put the refused rows below the cursor, and no later
            # round would ever read them again.
            cursor.setdefault("issues", {}).setdefault(key, cur)

    def _advance_cursor(self, key: str, cursor: dict, cur: int, fresh) -> None:
        """Forwarder: the prefix rule lives in :meth:`consume.advance_cursor`."""
        consume_mod.advance_cursor(self, key, cursor, cur, fresh)

    # ------------------------------------------------------------- status/doctor
    def status(self, *, outbox: bool = False, all: bool = False) -> dict:
        """Items that need a person. ``outbox=True`` adds the outbox layer.

        ``index.json`` holds triage results; ``outbox_state.json`` holds actions
        that are still unfinished. ``waiting_person``/``uncertain`` live in the
        outbox and were invisible here, so a brief that pointed at ``status`` for
        them named a command that returned nothing. ``outbox`` defaults to False
        so the printed shape of plain ``status`` does not change.

        ``all=True`` drops the dismissal filter and tags each index item with
        the dismissal audit (``state``/``entry_at``/``resolved_at``/...). The
        extra keys only exist on that path, and only in the returned dict -
        nothing is written back to the index.

        Two timestamps, never one: ``entry_at`` belongs to the triage row and
        ``replay`` rewrites it; ``resolved_at`` is the side-table time and is
        what any report about the dismissal must use.
        """
        index = self.store.load_index()
        counters = self.store.load_counters()
        items = []
        for issue_key, rec in sorted((index.get("issues") or {}).items(),
                                     key=lambda kv: issuekey.issue_number(kv[0]) or 0):
            for entry in rec.get("needs_person") or []:
                if not all and not needs.is_open(index, issue_key, entry):
                    continue
                item = {"issue": issue_key, "comment_id": entry.get("comment_id"),
                        "reason": entry.get("reason"), "at": entry.get("at"),
                        "source": "index", "action_id": None}
                if all:
                    row = needs.stored_row(index, issue_key, entry)
                    if row is not None:
                        item.update({
                            "state": row.get("state"),
                            "entry_at": entry.get("at"),
                            "resolved_at": row.get("at"),
                            "resolved_by": row.get("by"),
                            "resolved_by_comment": row.get("by_comment"),
                            "superseded_by": row.get("superseded_by"),
                            "entries": row.get("entries")})
                items.append(item)
        if outbox:
            for aid, rec in sorted(self.store.unfinished_outbox().items()):
                status = str(rec.get("status") or "")
                if status not in ("waiting_person", "uncertain"):
                    continue
                items.append({"issue": rec.get("issue"), "comment_id": None,
                              "reason": rec.get("reason"), "at": rec.get("updated_at"),
                              "source": "outbox", "action_id": aid,
                              "status": status})
        return {"external_actions": int(counters.get("external_actions") or 0),
                "mode": self.mode, "items": items}

    def doctor(self) -> dict:
        """Distinguishable faults, one readable label each.

        ``process-dead`` / ``startup-failed`` / ``bus-down`` / ``github-down`` /
        ``config-bad`` / ``outbox-stuck``. A fault is only ever reported once,
        with the detail that makes it actionable.
        """
        checks = []

        def add(name: str, ok: bool, detail: str, level: str = "fail",
                label: str = "") -> None:
            checks.append({"name": name, "ok": ok, "detail": detail,
                           "level": "ok" if ok else level,
                           "label": "ok" if ok else (label or name)})

        add(*self._check_process())

        source_name = (self.cfg.get("github") or {}).get("source")

        add(*doctor_mod.bus_check(self.bus))

        if source_name == "none":
            add("github", True, "source=none (skipped)")
        else:
            add(*doctor_mod.github_check(self.source, str(source_name)))

        add(*doctor_mod.config(self.projects))

        add(*doctor_mod.outbox_check(self.store))
        add(*doctor_mod.waiting_person_check(self.store))

        cursor = self.store.load_cursor()
        add(*doctor_mod.cursor_check(cursor, self.store.cursor_problem(),
                                     issuekey.issue_number))

        warn_rows = [r for r in _tail_events(self.store.events_path, limit=500)
                     if r.get("type") == "pm.task_book_conclusion"]
        add(*doctor_mod.task_book_check(warn_rows))

        index = self.store.load_index()
        add(*doctor_mod.assignments_check(asg_mod.active_assignments(index)))

        add(*doctor_mod.supervisor_visibility(
            self.cfg, self.store.events_path, self.SUPERVISOR_SID_MALFORMED_ROW,
            _tail_events))

        add(*doctor_mod.charters())

        counters = self.store.load_counters()
        return {"checks": checks, "external_actions":
                int(counters.get("external_actions") or 0),
                "mode": self.mode}

    # ------------------------------------------------------ doctor sub-checks
    def _check_process(self) -> tuple:
        """Forwarder: :func:`engine_parts.doctor.process`.

        Nothing is injected that the module can read itself; ``current_head`` is
        passed because a test patches the engine-level name.
        """
        return doctor_mod.process(self.store, current_head=_current_head)

    # ------------------------------------------------------ session truth (ISS-27)
    def _session_stream(self) -> tuple[Path, Path]:
        """Forwarder: :func:`engine_parts.session_truth.resolve_paths`.

        Raises :class:`session_events.SessionStreamNotConfigured` when the config
        declares no stream: an undeclared stream is a configured state, not a
        fault. Never guessed from the working directory (ISS-79).
        """
        return session_truth_mod.resolve_paths(self.cfg)

    def _note_stream_state(self) -> str:
        """Apply :func:`engine_parts.session_truth.stream_fault` to this round.

        The decision (missing vs undecodable, and why they differ) lives in the
        module; this method only applies it, because the effect - one degraded
        flag, one error string, one event row - is host state.
        """
        try:
            events, _sessions = self._session_stream()
        except session_events.SessionStreamNotConfigured:
            return ""
        fault = session_truth_mod.stream_fault(events, _now())
        if fault is None:
            return ""
        self.degraded = True
        self._stream_error = fault["error"]
        self.store.log_event(fault["row"])
        return self._stream_error

    def _session_map(self, path: Path | None = None) -> tuple[dict, str]:
        """``(map, problem)`` for ``sessions.json``, cached per round.

        Forwarder: the read and its "problem is returned, never swallowed" rule
        live in :func:`engine_parts.session_truth.read_session_map`. Caching by
        ``(path, mtime_ns, size)`` stays here because it is per-round host state.
        """
        if path is None:
            try:
                path = self._session_stream()[1]
            except Exception:  # noqa: BLE001  undeclared stream = no session truth
                # Nothing declared is the configured state, not a fault: the
                # same answer the inline readers gave (#79). A *declared* path
                # that cannot be read is the fault, reported by the reader.
                return {}, ""
        path = Path(path)
        try:
            stat = path.stat()
            stamp = (str(path), stat.st_mtime_ns, stat.st_size)
        except OSError:
            stamp = (str(path), 0, 0)
        cache = getattr(self, "_session_map_cache", None)
        if cache is not None and cache[0] == stamp:
            return dict(cache[1]), cache[2]
        data, problem = session_truth_mod.read_session_map(path)
        self._session_map_cache = (stamp, dict(data), problem)
        return data, problem

    def _session_state(self) -> dict:
        """Replayed lifecycle state, cached for the round.

        Forwarder: :func:`engine_parts.session_truth.replay_state`, which answers
        ``{}`` on a broken stream so "cannot tell" never becomes a new way to
        fail closed on a healthy thread.
        """
        if self._session_state_cache is None:
            try:
                path, _sessions = self._session_stream()
            except Exception:  # noqa: BLE001  undeclared stream = no session truth
                self._session_state_cache = {}
                return self._session_state_cache
            self._session_state_cache = session_truth_mod.replay_state(path)
        return self._session_state_cache

    def replay_sessions(self, at: str | None = None) -> dict:
        """Rebuild ``sid -> (role, issue, assignment, profile)`` as of ``at``.

        Forwarder: :func:`engine_parts.session_truth.replay_as_of`. Read-only -
        the engine's own ``events.jsonl`` is a different stream.
        """
        events_path, _sessions_path = self._session_stream()
        return session_truth_mod.replay_as_of(events_path, at)

    def reconcile_sessions(self, at: str | None = None,
                           server_sids: set[str] | None = None) -> dict:
        """Three-way: replay x ``sessions.json`` x live server. Read-only."""
        events_path, sessions_path = self._session_stream()
        return session_truth_mod.reconcile(events_path, sessions_path,
                                           server_sids, at)

    # -------------------------------------------------------------------- replay
    def replay(self, issue: int) -> dict:
        """Rebuild one issue's index entry from the source. Read-only."""
        issue = int(issue)
        index = self.store.load_index()
        cursor = self.store.load_cursor()
        key = self._thread_key(issue)
        previous = (index.get("issues") or {}).get(key) or {}
        kept = [dict(row) for row in (previous.get("needs_person") or [])
                if isinstance(row, dict)]
        rec = dict(previous)
        rec.update({"state": "needs_person" if kept else "idle", "comments_seen": 0,
                    "needs_person": kept, "title": self.source.title(issue),
                    "rebuilt_at": _now()})
        # ``paged()`` is never cached (#134 P1-2).
        comments = collect_mod.replay_rows(self.source, issue,
                                           github_source.paged_batch)
        for comment in comments:
            cid = int(comment["id"])
            rec["comments_seen"] += 1
            state, reason = self._triage(comment)
            if state == "needs_person":
                rec["state"] = "needs_person"
                if not any(row.get("comment_id") == cid for row in rec["needs_person"]):
                    rec["needs_person"].append({"comment_id": cid, "reason": reason,
                                                "at": _now()})
            elif rec["state"] == "idle":
                rec["state"] = "tracked"
        rec["last_comment_id"] = max((int(c["id"]) for c in comments), default=0)
        index.setdefault("issues", {})[key] = rec
        index.setdefault("meta", {})["updated_at"] = _now()
        index["meta"]["rebuilt_issue"] = issue
        self.store.save_index(index)
        cursor.setdefault("issues", {})[key] = rec["last_comment_id"]
        self.store.save_cursor(cursor)
        # Read the gap *after* the save: the answer is about what a round would
        # see now, and one rebuilt thread must not hide the others (#87 P0-2).
        gap = self.store.cursor_gaps(cursor.get("issues") or {})
        self.store.log_event({"type": "replay.rebuilt", "at": _now(), "issue": issue,
                              "comments": rec["comments_seen"],
                              "cursor_gap": len(gap),
                              "external_actions":
                                  self.store.load_counters().get("external_actions", 0)})
        return {"issue": issue, "comments": rec["comments_seen"],
                "state": rec["state"], "cursor_gap": gap,
                "external_actions": self.store.load_counters()
                .get("external_actions", 0)}


def build_engine(cfg: dict, store: Store, bus=None, *, source=None) -> Engine:
    """``source`` overrides the configured backend (``status`` needs none)."""
    if source is None:
        try:
            source = github_source.build(cfg)
        except Exception as exc:  # noqa: BLE001  report as a failed check, never crash
            source = github_source.BrokenSource(f"{type(exc).__name__}: {exc}")
    return Engine(cfg, store, source, bus)
