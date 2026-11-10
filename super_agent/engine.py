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
    |                          supervisor (``event.agent.supervisor``)
    | first line [PM]/[supervisor] -> audit only
    + ordinary comment
       | waiting_review and a qa session is known -> send to qa
       | waiting_release / close_reconcile       -> audit only
       | a thread held there past ``engine.release_timeout_s`` -> supervisor
       | the thread belongs to a project         -> project_pm (slow path)
       + cross-project or unlisted               -> needs_person

``_dispatch`` refuses ``origin_role=qa`` with ``target_role=worker`` outright.
That gate covers **route decisions the engine makes itself**. It does not cover
a process that calls the session API on its own (``sess.py``, ``direct.send``),
a workbench script writing a comment, or ``_archive_one``, which runs after the
decision was made. Guarding the engine's decisions is not the same as
guarding authority - the boundary is stated here so nobody reads the gate as
more than it is.

A supervisor notice is a **protocol**, not a wake-up: nothing in this repository
consumes ``event.agent.*``. Until the workbench subscribes to
``event.agent.supervisor``, the delivery is recorded and visible in the event
stream and nowhere else.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from . import github_source, health, issuekey, markers, needs, session_events
from . import assignments as asg_mod
from .allowlist import ProjectAllowlist
from .store import Store, action_id

OBSERVE = "observe"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_ts() -> float:
    return time.time()


def _parse_ts(value: str) -> datetime | None:
    """Parse an ISO timestamp written by ``_now()``; ``None`` when unusable."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _default_allowlist_path() -> Path:
    """``config/allowlist.json`` next to the package, i.e. inside the repo."""
    return Path(__file__).resolve().parent.parent / "config" / "allowlist.json"


class Engine:
    def __init__(self, cfg: dict, store: Store, source, bus=None, *, clock=_now_ts,
                 project_allowlist: ProjectAllowlist | None = None,
                 adapter=None, session_api=None):
        self.cfg = cfg
        self.store = store
        self.source = source
        # Injected in tests; ``None`` means "use the direct console".
        self.adapter = adapter
        self.session_api = session_api
        self.bus = bus
        self.clock = clock
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
        self.degraded = False
        self._assignments_error = ""
        self._archived_cache: dict | None = None
        self._archived_unreadable = False
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
        """Project name that lists ``issue``; None when nothing claims it."""
        if self.projects is None or self.projects.data is None:
            return None
        try:
            issue = int(issue)
        except (TypeError, ValueError):
            return None
        for name, entry in self.projects.data["projects"].items():
            if issue in entry["issues"]:
                return name
        return None

    # ------------------------------------------------------------------ health
    def health_record(self, *, error: str = "", degraded: bool = False,
                      running: bool = True, phase: str = "") -> dict:
        index = self.store.load_index()
        record = {"pid": os.getpid(), "mode": self.mode,
                  "last_loop_at": _now(),
                  "last_successful_action_at": self.last_successful_action_at,
                  "active_assignments": len(asg_mod.active_assignments(index)),
                  "degraded": bool(degraded or error), "last_error": error,
                  "running": bool(running)}
        if phase:
            record["phase"] = phase
        return record

    def write_health(self, *, error: str = "", degraded: bool = False,
                     running: bool = True, phase: str = "") -> dict:
        return health.write(self.store.health_path,
                            self.health_record(error=error, degraded=degraded,
                                               running=running, phase=phase))

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
        """Fixed rules, in this order:

        1. a delivery marker on the first line wins over everything - a worker
           whose role is configured would otherwise have its own delivery
           ignored as role-authored noise;
        2. a ``[QA]`` review is the second marker: its verdict is a transition,
           not discussion, and it must be read before role noise too;
        3. role-authored noise is ignored;
        4. escalation markers ask for a person;
        5. everything else is logged (the historical behaviour, unchanged).

        ``parse_delivery`` and ``parse_review`` only read the first non-empty
        line, so a comment that quotes a delivery or a verdict in its body stays
        ordinary discussion.
        """
        body = str(comment.get("body") or "")
        delivery = markers.parse_delivery(body)
        if delivery:
            return "delivery", f"marker {delivery['name']!r}"
        review = markers.parse_review(body)
        if review:
            return "review", f"qa verdict {review['verdict'] or 'unreadable'!r}"
        author = str(comment.get("author") or "").strip().lower()
        if self.role and author == self.role.lower():
            return "ignored", "authored by configured role"
        lowered = body.lower()
        for marker in self.markers:
            if marker and marker in lowered:
                return "needs_person", f"marker {marker!r}"
        return "logged", ""

    # ------------------------------------------------------------ qa routing
    def _qa_session_keys(self, thread: str) -> set[str]:
        """Session keys that count as *this thread's* QA.

        Two sources, unioned so a rotation does not lose the check:

        * the ``qa`` entry of ``issue_sessions.json[thread]`` (``name`` and
          ``session``);
        * every ``qa-*`` key in ``sessions.json`` - the one-shot naming rule.

        The verdict's author login cannot be used: every role shares one token,
        so the login is the same for a rejection and for a forgery. This is a
        weak check and it is treated as one: a miss downgrades the verdict, it
        never blocks it.
        """
        keys: set[str] = set()
        ledger = self._load_issue_sessions().get(thread) or {}
        if isinstance(ledger, dict):
            for role, entry in ledger.items():
                if str(role).lower() != "qa" or not isinstance(entry, dict):
                    continue
                for field in ("name", "session"):
                    value = str(entry.get(field) or "")
                    if value:
                        keys.add(value)
        try:
            path = self._session_stream()[1]
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if isinstance(data, dict):
            keys.update(str(k) for k in data if str(k).startswith("qa"))
        return keys

    def _review_verified(self, thread: str, comment: dict) -> bool:
        """Does this comment come from a QA key of this thread?

        A comment that names its session (``session_key``) is checked against
        the whitelist exactly. Without that field the answer is "the thread has
        a QA registered at all", which is the strongest statement the shared
        login allows - and it is logged as unverified when it is false.
        """
        keys = self._qa_session_keys(thread)
        claimed = str(comment.get("session_key") or "")
        if claimed:
            return claimed in keys
        return bool(keys)

    def _pm_target(self, thread: str, issue: int) -> dict:
        """Where a verdict goes: the project's PM, never a worker."""
        ledger = self._load_issue_sessions().get(thread) or {}
        pm_sid = ""
        if isinstance(ledger, dict):
            entry = ledger.get("pm")
            if isinstance(entry, dict):
                pm_sid = str(entry.get("session") or "")
        project = self._project_for(issue) or ""
        return {"project": project, "pm_sid": pm_sid, "target_role": "pm"}

    def _review_target(self, index: dict, thread: str) -> dict | None:
        """The worker assignment a verdict on ``thread`` applies to.

        A verdict judges a **delivery**, so an assignment that never recorded
        one is not a candidate: without a delivery there is nothing to accept or
        send back, and moving it would invent state out of a stray comment. The
        newest delivery wins - an older assignment on the same thread is stale.
        """
        candidates = [rec for rec in (index.get("assignments") or {}).values()
                      if str(rec.get("thread") or "") == thread
                      and str(rec.get("role") or "") == "worker"
                      and rec.get("delivery_comment_id") is not None
                      and str(rec.get("status") or "") in
                      ("working", "waiting_review", "blocked")]
        if not candidates:
            return None
        return max(candidates, key=lambda r: int(r.get("delivery_comment_id") or 0))

    def _route_action(self, thread: str, issue: int, cid, payload: dict,
                      event: str, *, target_role: str, origin_role: str,
                      assignment_id: str) -> dict:
        """A dispatch record whose id is the route key, not a hash.

        ``_dispatch`` derives the action id from ``assignment + verb +
        input_ref``; naming the route as the "assignment" keeps the route's
        attempt counter findable from ``index["routes"]``.
        """
        return {"kind": "bus_inject", "event": event, "issue": issue,
                "thread": thread, "input_ref": str(cid),
                "assignment_id": assignment_id,
                "origin_role": origin_role, "target_role": target_role,
                "payload": payload}

    def _route_review(self, index: dict, thread: str, issue: int, comment: dict,
                      review: dict, *, log_unverified: bool = True) -> dict:
        """Send one verdict to the project PM and record the attempt.

        The route record lives in ``index["routes"]`` keyed by thread and
        comment, so a refused or suppressed dispatch can be retried next round
        under the **same** action id - at-most-once is the attempt counter, not
        the record's presence.

        A shippable verdict also goes to the supervisor. That decision is taken
        **before** the PM early return below, under its own action id and in its
        own table: the PM attempt counter must never be the reason the
        supervisor never hears about a release.
        """
        cid = int(comment["id"])
        key = f"route:{thread}:{cid}"
        target = self._pm_target(thread, issue)
        verified = self._review_verified(thread, comment)
        if not verified and log_unverified:
            # Doctor is the only consumer on purpose: a key miss is a warning
            # to look at, not a reason to wake the PM twice for one verdict.
            self.store.log_event({
                "type": "review.unverified", "at": _now(), "thread": thread,
                "comment_id": cid, "level": "warn", "consumers": ["doctor"],
                "detail": "the verdict did not name a qa session key of this thread",
            })
        sup_key = f"sup:{thread}:{cid}"
        sup_routes = index.setdefault("supervisor_routes", {})
        if not isinstance(sup_routes, dict):
            sup_routes = index["supervisor_routes"] = {}
        sup_rec = sup_routes.get(sup_key)
        sup_rec = sup_rec if isinstance(sup_rec, dict) else None
        if (sup_rec is not None and "sup_eligible" not in sup_rec
                and "fix_needed" not in review):
            self._undecidable(thread, cid, sup_key)
        if self._sup_eligible(review, sup_rec) and not (
                sup_rec is not None and sup_rec.get("ok")):
            self._route_supervisor(index, thread, issue, comment, review,
                                   sup_key, action_id(sup_key, "bus_inject",
                                                      str(cid)))
        payload = {"thread": thread, "issue": int(issue),
                   "review_comment_id": cid,
                   "verdict": str(review.get("verdict") or ""),
                   "project": target["project"], "pm_sid": target["pm_sid"],
                   "unverified": not verified}
        base = action_id(key, "bus_inject", str(cid))
        routes = index.setdefault("routes", {})
        if routes is None or not isinstance(routes, dict):
            routes = index["routes"] = {}
        if key not in routes:
            routes[key] = {"thread": thread, "comment_id": cid, "at": _now(),
                           "verdict": payload["verdict"], "ok": None,
                           "action_id": base}
        route = routes[key]
        if self.store.action_attempted(base) > 0:
            return {"dispatched": False, "reason": "already attempted",
                    "action_id": base}
        result = self._dispatch(self._route_action(
            thread, issue, cid, payload, "event.agent.project_pm",
            target_role=target["target_role"], origin_role=markers.QA.lower(),
            assignment_id=key))
        route["ok"] = bool(result.get("ok")) if result.get("dispatched") else False
        route["refused"] = bool(result.get("refused"))
        route["action_id"] = result.get("action_id") or base
        self.store.log_event({
            "type": "route.decided", "at": _now(), "thread": thread,
            "comment_id": cid, "to": "project_pm", "project": target["project"],
            "verdict": payload["verdict"],
            "dispatched": bool(result.get("dispatched")),
            "refused": bool(result.get("refused")),
            "reason": result.get("reason") or "",
        })
        if not result.get("dispatched"):
            numbers = index.setdefault("issues", {}).setdefault(
                thread, {"state": "idle", "comments_seen": 0, "needs_person": []})
            numbers.setdefault("needs_person", []).append({
                "comment_id": cid, "reason": "route.undeliverable", "at": _now()})
            self.store.log_event({
                "type": "route.undeliverable", "at": _now(), "thread": thread,
                "comment_id": cid, "reason": result.get("reason") or "",
            })
        return result

    def _on_review(self, index: dict, thread: str, issue: int, comment: dict,
                   review: dict, summary: dict) -> str:
        """Apply a verdict, then route it. Returns the triage state."""
        cid = int(comment["id"])
        assignment = self._review_target(index, thread)
        if assignment is None:
            self.store.log_event({"type": "review.unmatched", "at": _now(),
                                  "thread": thread, "comment_id": cid,
                                  "reason": "no active worker assignment"})
            return "needs_person", "review without an active assignment"
        verified = self._review_verified(thread, comment)
        moved = asg_mod.on_review(assignment, review, review_cid=cid,
                                  verified=verified)
        if moved["changed"]:
            self.store.log_event({
                "type": "assignment.qa_review", "at": _now(),
                "assignment": assignment["id"], "thread": thread,
                "comment_id": cid, "status": moved.get("status"),
                "verdict": str(review.get("verdict") or ""),
                "verified": verified,
            })
        elif moved.get("reason") == "review.stale":
            self.store.log_event({"type": "review.stale", "at": _now(),
                                  "assignment": assignment["id"],
                                  "thread": thread, "comment_id": cid})
        self._route_review(index, thread, issue, comment, review)
        return "review", ""

    def _retry_routes(self, index: dict) -> int:
        """Re-send verdicts a refused/suppressed dispatch never delivered.

        Same rule as the notification retry: an attempt that exists (success or
        ``uncertain``) is never repeated, a refusal writes no attempt so the
        next round may try again.
        """
        sent = 0
        for key, route in sorted((index.get("routes") or {}).items()):
            if route.get("ok"):
                continue
            base = str(route.get("action_id") or "")
            if base and self.store.action_attempted(base) > 0:
                continue
            cid = route.get("comment_id")
            thread = str(route.get("thread") or "")
            issue = None
            for rec in (index.get("assignments") or {}).values():
                if str(rec.get("thread") or "") == thread:
                    issue = rec.get("issue")
                    break
            if issue is None:
                continue
            review = {"verdict": str(route.get("verdict") or "")}
            result = self._route_review(index, thread, int(issue),
                                        {"id": cid, "body": "", "author": ""},
                                        review, log_unverified=False)
            if result.get("dispatched"):
                sent += 1
                self._supersede_route(index, thread, cid, base)
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

        ``fix_needed`` is the parser's answer and is recomputed every time;
        ``sup_eligible`` on the stored record is the **decision snapshot**, and
        it is the only thing a retry may trust: a retry re-enters here with a
        stub review that carries the verdict and nothing else, and re-deriving
        the answer would turn ``有条件通过（1 条 P1 必须修）`` into a pass.

        Neither answer available means "do not send" - guessing here would
        either withhold a release or send a false one.
        """
        if "fix_needed" in (review or {}):
            return (str(review.get("verdict") or "") == "pass"
                    and review.get("fix_needed") is False)
        if isinstance(record, dict) and "sup_eligible" in record:
            return bool(record["sup_eligible"])
        return False

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

    def _supervisor_sid(self) -> str:
        """The supervisor's session id, resolved fresh every round.

        Configured ``supervisor_sid`` first, then the session keys in order -
        reusing the same resolver the rest of the engine uses, so an overlay
        that redirects the workbench moves this with it. Nothing is cached: a
        key that was missing last round may exist now.
        """
        engine_cfg = self.cfg.get("engine") or {}
        sid = str(engine_cfg.get("supervisor_sid") or "").strip()
        if sid:
            return sid
        keys = engine_cfg.get("supervisor_keys") or ["supervisor"]
        if not isinstance(keys, (list, tuple)):
            keys = [keys]
        try:
            path = session_events.resolve_sessions_path(self.cfg)
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        if not isinstance(data, dict):
            return ""
        for key_name in keys:
            entry = data.get(str(key_name))
            if isinstance(entry, dict):
                found = str(entry.get("id") or entry.get("session") or "")
            else:
                found = str(entry or "")
            if found:
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
        record = sup_routes.get(sup_key)
        record = dict(record) if isinstance(record, dict) else {
            "thread": thread, "comment_id": cid, "at": _now(),
            "verdict": str(review.get("verdict") or ""),
            "fix_needed": review.get("fix_needed"),
        }
        record["sup_eligible"] = True
        record["action_id"] = sup_base
        sup_routes[sup_key] = record
        if self.store.action_attempted(sup_base) > 0:
            return {"dispatched": False, "reason": "already attempted",
                    "action_id": sup_base}
        sid = self._supervisor_sid()
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
                   "text": self._release_text(issue, cid)}
        result = self._dispatch(self._route_action(
            thread, issue, cid, payload, "event.agent.supervisor",
            target_role="supervisor", origin_role=markers.QA.lower(),
            assignment_id=sup_key))
        record["ok"] = bool(result.get("ok")) if result.get("dispatched") else False
        record["refused"] = bool(result.get("refused"))
        record["action_id"] = result.get("action_id") or sup_base
        self.store.log_event({
            "type": "route.decided", "at": _now(), "thread": thread,
            "comment_id": cid, "to": "supervisor",
            "verdict": payload["verdict"],
            "dispatched": bool(result.get("dispatched")),
            "refused": bool(result.get("refused")),
            "reason": result.get("reason") or "",
        })
        return result

    @staticmethod
    def _release_text(issue, cid) -> str:
        """The one line a person reads: the issue and the verdict that closed it."""
        return f"【可关单】#{int(issue)}，QA cid {cid}（P0=0，无必修项）"

    def _note_undeliverable(self, index: dict, thread: str, comment_id,
                            detail: str, *, issue=None, to="supervisor",
                            assignment: str = "", once_key: str = "") -> None:
        """Record a supervisor notice that could not be sent - never silently.

        One event for the machine (``type`` is a key, so it carries no ``[user]``)
        and one ``needs_person`` row for a person, whose **reason** does carry
        ``[user]``: the brief buckets by reason substring, and a local
        configuration gap must not sit in the "waiting for a decision" section.

        ``once_key`` makes both idempotent: the retry and reminder passes run
        every round, and a repeated line per round would bury the queue it is
        writing to. The row is keyed by ``(thread, comment)`` either way.
        """
        reason = "route.supervisor_undeliverable[user]"
        key = once_key or f"{thread}|{needs.cid_of(comment_id)}"
        if key not in self._sup_notice_logged:
            self._sup_notice_logged.add(key)
            self.store.log_event({
                "type": "route.supervisor_undeliverable", "at": _now(),
                "thread": thread, "comment_id": comment_id, "to": to,
                "assignment": assignment, "issue": issue, "detail": detail,
            })
        numbers = index.setdefault("issues", {}).setdefault(
            thread, {"state": "idle", "comments_seen": 0, "needs_person": []})
        existing = [entry for entry in numbers.get("needs_person") or []
                    if needs.cid_of(entry) == needs.cid_of(comment_id)
                    and str(entry.get("reason") or "") == reason]
        if existing:
            return
        numbers.setdefault("needs_person", []).append({
            "comment_id": comment_id, "reason": reason, "at": _now()})

    def _retry_supervisor_routes(self, index: dict) -> int:
        """Re-send supervisor notices a refused/suppressed dispatch never sent.

        Only ``index["supervisor_routes"]`` is read: the PM retry pass owns
        ``index["routes"]`` alone, and the two tables never see each other's
        keys. The stored ``sup_eligible`` is the decision - a retry never
        re-judges the verdict.
        """
        sent = 0
        for key, record in sorted((index.get("supervisor_routes") or {}).items()):
            if not isinstance(record, dict) or record.get("ok"):
                continue
            if "sup_eligible" not in record:
                self._undecidable(str(record.get("thread") or ""),
                                  record.get("comment_id"), key)
                continue
            if record.get("sup_eligible") is not True:
                continue
            base = str(record.get("action_id") or "")
            if not base or self.store.action_attempted(base) > 0:
                continue
            thread = str(record.get("thread") or "")
            cid = record.get("comment_id")
            issue = None
            for rec in (index.get("assignments") or {}).values():
                if str(rec.get("thread") or "") == thread:
                    issue = rec.get("issue")
                    break
            if issue is None or cid is None:
                continue
            review = {"verdict": str(record.get("verdict") or "")}
            result = self._route_supervisor(index, thread, int(issue),
                                            {"id": cid, "body": "", "author": ""},
                                            review, key, base)
            if result.get("dispatched"):
                sent += 1
        return sent

    # -------------------------------------------------- release-time reminders
    def _release_start(self, index: dict, thread: str, assignment: dict) -> str:
        """The stable start time of a held thread, or ``""`` when there is none.

        ``first_checked_at`` is written once by ``_reconcile_thread`` and kept;
        ``updated_at`` is rewritten by ``enter_close_reconcile`` every round and
        would keep pushing the deadline out forever. The fallbacks are the
        archive record, and nothing else is guessed.
        """
        rec = (index.get("close_reconcile") or {}).get(thread)
        if isinstance(rec, dict) and str(rec.get("first_checked_at") or ""):
            return str(rec["first_checked_at"])
        tombstone = assignment.get("tombstone")
        if isinstance(tombstone, dict) and str(tombstone.get("at") or ""):
            return str(tombstone["at"])
        return str(assignment.get("archived_at") or "")

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
            start = self._release_start(index, thread, rec)
            parsed = _parse_ts(start)
            if parsed is None:
                self.store.log_event({
                    "type": "release_timeout.skipped", "at": _now(),
                    "thread": thread, "assignment": aid,
                    "reason": "no usable start time"})
                self._note_undeliverable(
                    index, thread, rec.get("delivery_comment_id"),
                    "release timeout start time is unusable", issue=rec.get("issue"),
                    assignment=aid, once_key=f"notime:{aid}")
                continue
            if (now - parsed).total_seconds() < timeout:
                continue
            base = action_id(f"release_timeout:{aid}", "bus_inject", start)
            if self.store.action_attempted(base) > 0:
                continue
            sid = self._supervisor_sid()
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
                                    f"{int((now - parsed).total_seconds())}s"
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
        origin = str(action.get("origin_role") or "")
        target = str(action.get("target_role") or "")
        if origin == markers.QA.lower() and target == "worker":
            self.store.log_event({
                "type": "route.refused", "at": _now(), "action": action.get("kind"),
                "issue": action.get("issue"), "thread": action.get("thread"),
                "origin_role": origin, "target_role": target,
                "reason": "a qa verdict is never dispatched to a worker",
            })
            return {"dispatched": False, "refused": True,
                    "reason": "qa verdict routed to a worker"}
        if not self.dispatch_enabled:
            self.store.log_event({
                "type": "dispatch.suppressed", "at": _now(),
                "mode": self.mode, "action": action.get("kind"),
                "issue": action.get("issue"),
            })
            return {"dispatched": False, "reason": "dispatch disabled"}

        # Never deliver into a session that is already closed. Checked before
        # the allowlist and before any counter moves, so a refusal here is not
        # an external action and leaves no attempt behind.
        payload_sid = str((action.get("payload") or {}).get("sid") or "")
        if payload_sid and self._sid_archived(payload_sid):
            self.store.log_event({"type": "dispatch.archived_sid", "at": _now(),
                                  "sid": payload_sid,
                                  "action": action.get("kind"),
                                  "assignment": action.get("assignment_id")})
            return {"dispatched": False, "refused": True,
                    "reason": "target session is archived"}

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
    def _issue_sessions_path(self) -> Path:
        """``issue_sessions.json`` beside ``sessions.json`` (cross-check only)."""
        _events, sessions = self._session_stream()
        return sessions.parent / "issue_sessions.json"

    def _load_issue_sessions(self) -> dict:
        try:
            data = json.loads(self._issue_sessions_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

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
            self._assignments_error = f"{type(exc).__name__}: {exc}"
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
        out = asg_mod.sync(index, thread, issue, live, crosscheck=crosscheck)
        if out["conflict"]:
            self.store.log_event({"type": "assignment.source_conflict", "at": _now(),
                                  "thread": thread, "conflict": out["conflict"]})
        return out

    def _notify_waiting_review(self, assignment: dict) -> dict:
        """Send ``event.agent.waiting_review`` at most once per delivery."""
        delivery = assignment.get("delivery_comment_id")
        if delivery is None:
            return {"dispatched": False, "reason": "no delivery recorded"}
        base = action_id(assignment["id"], "bus_inject", str(delivery))
        if self.store.action_attempted(base) > 0:
            assignment["notification_action_id"] = base
            return {"dispatched": False, "reason": "already attempted",
                    "action_id": base}
        assignment["notification_action_id"] = base
        assignment["attempts"] = int(assignment.get("attempts") or 0) + 1
        result = self._dispatch({
            "kind": "bus_inject",
            "event": "event.agent.waiting_review",
            "assignment_id": assignment["id"],
            "input_ref": str(delivery),
            "issue": assignment.get("issue"),
            "payload": {"thread": assignment.get("thread"),
                        "assignment": assignment["id"],
                        "sid": assignment.get("sid"),
                        "delivery_comment_id": delivery},
        })
        if result.get("dispatched"):
            # ``ok`` is the only confirmation: ``mark_done`` runs only when the
            # bus accepted the event. A dispatch that was attempted and failed
            # (`uncertain`) is recorded in the outbox but is *not* a delivery,
            # and recording ``notified_at`` for it is how the archive pass used
            # to mark a never-sent notification as sent.
            if result.get("ok"):
                assignment["notified_at"] = _now()
            else:
                assignment["notified_at"] = ""
            self.store.log_event({
                "type": "assignment.waiting_review", "at": _now(),
                "assignment": assignment["id"],
                "delivery_comment_id": delivery,
                "action_id": result.get("action_id") or base,
                "ok": bool(result.get("ok")),
            })
        return result

    def _retry_waiting_review(self, index: dict) -> int:
        """Re-attempt notifications a refused/suppressed dispatch never sent.

        The delivery comment is already behind the cursor by then, so this is
        the only path that can retry: the attempt counter is what keeps it
        at-most-once (a real attempt - success or ``uncertain`` - is never
        repeated).
        """
        sent = 0
        for rec in (index.get("assignments") or {}).values():
            if str(rec.get("status") or "") != "waiting_review":
                continue
            if rec.get("delivery_comment_id") is None:
                continue
            base = action_id(rec["id"], "bus_inject",
                             str(rec["delivery_comment_id"]))
            if self.store.action_attempted(base) > 0:
                continue
            result = self._notify_waiting_review(rec)
            if result.get("dispatched"):
                sent += 1
        return sent

    def _on_delivery(self, index: dict, thread: str, issue: int, comment: dict,
                     delivery: dict, summary: dict) -> str:
        """Match a delivery marker to an assignment and notify once.

        Returns the resulting triage state so the caller can count it: a
        delivery that cannot be matched becomes ``needs_person`` (never silently
        dropped), which is the one place this differs from the other outcomes.
        """
        cid = int(comment["id"])
        assignment, matched_by = asg_mod.match_delivery(index, thread,
                                                        delivery["name"])
        if assignment is None:
            self.store.log_event({"type": "assignment.unmatched", "at": _now(),
                                  "thread": thread, "comment_id": cid,
                                  "marker": delivery["name"],
                                  "reason": matched_by})
            return "needs_person", f"unmatched delivery {delivery['name']!r}"
        moved = asg_mod.on_delivery(assignment, cid, delivery["name"], matched_by)
        if moved["changed"]:
            self.store.log_event({"type": "assignment.delivery", "at": _now(),
                                  "assignment": assignment["id"], "thread": thread,
                                  "comment_id": cid, "matched_by": matched_by})
        # Retried on a later round when the previous attempt was refused or
        # suppressed: those paths write no attempt counter on purpose.
        self._notify_waiting_review(assignment)
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
           because an archive made through ``POST /api/v2/sessions:archive``
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
            self.store.log_event({"type": "assignment.archived_unreachable",
                                  "at": _now(), "sid": sid,
                                  "error": f"{type(exc).__name__}: {exc}"})
            return True
        if not isinstance(info, dict) or info.get("_err"):
            cache[sid] = True
            self.store.log_event({"type": "assignment.archived_unreachable",
                                  "at": _now(), "sid": sid,
                                  "error": str((info or {}).get("_err")
                                               or "no session info")})
            return True
        cache[sid] = bool(info.get("archived"))
        return cache[sid]

    def _refresh_archived_cache(self) -> dict:
        """One refresh per round of ``sid -> archived`` from the session stream.

        Every sid the stream mentions is cached, archived **and** live. A sid
        that is absent from the cache is unknown to the stream and therefore
        needs the server check in :meth:`_live_archived`.
        """
        try:
            events_path, _sessions = self._session_stream()
            events = session_events.load_events(events_path)
            if not events_path.is_file() or not events:
                raise RuntimeError(f"session stream unavailable: {events_path.name}")
            state = session_events.replay(events)
        except Exception:  # noqa: BLE001  unreadable means "assume archived"
            self._archived_cache = {}
            self._archived_unreadable = True
            return {}
        sessions = state.get("sessions") or {}
        self._archived_cache = {sid: bool(rec.get("archived"))
                                for sid, rec in sessions.items()}
        self._archived_unreadable = False
        return self._archived_cache

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

        The notification gate is ``notified_at``, not the attempt counter alone:
        an attempt that came back ``uncertain`` never reached the bus, so the
        tombstone records ``notified: false`` rather than claiming a delivery
        that did not happen.
        """
        out = {"archived": [], "skipped": [], "blocked": [], "errors": []}
        save = index is None
        index = self.store.load_index() if index is None else index
        for aid, rec in sorted((index.get("assignments") or {}).items()):
            if str(rec.get("thread") or "") != thread:
                continue
            if str(rec.get("status") or "") == "waiting_release" and \
                    (rec.get("tombstone") or {}).get("archived"):
                # Already archived (possibly in this same round): nothing left
                # to do for this record here.
                out["skipped"].append(aid)
                continue
            if str(rec.get("status") or "") not in ("waiting_review",
                                                    "waiting_release"):
                continue
            delivery = rec.get("delivery_comment_id")
            if delivery is None:
                out["skipped"].append(aid)
                continue
            notify_base = action_id(aid, "bus_inject", str(delivery))
            if self.store.action_attempted(notify_base) < 1:
                # The review notification has not been sent (refused, or
                # dispatch disabled). Archiving now would drop the only chance
                # to tell the supervisor, so it waits for the retry pass.
                out["skipped"].append(aid)
                continue
            # An *attempt* is not a delivery: ``uncertain`` means the bus call
            # raised, so the supervisor was never told. Archiving on the
            # counter alone recorded those as "notified + archived" while the
            # event never left the process. The archive still proceeds (a
            # worker that delivered is done, and leaving it open keeps the
            # thread permanently non-terminal), but the tombstone records
            # ``notified: false`` and the loss is logged - the unsettled
            # outbox entry is what makes it visible to a person.
            notified = bool(str(rec.get("notified_at") or ""))
            base = action_id(aid, "archive", str(delivery))
            if self.store.action_attempted(base) > 0:
                out["skipped"].append(aid)
                continue
            if not notified:
                self.store.log_event({"type": "assignment.notify_unconfirmed",
                                      "at": _now(), "assignment": aid,
                                      "delivery_comment_id": delivery,
                                      "action_id": notify_base})
            # The gate, and its position: after the idempotency checks above,
            # before ``begin_action`` below - which is the first side effect
            # (a persisted ``prepared`` record and a bumped try counter). A gate
            # placed after it would have written state it then refuses.
            gate_ok, gate_why = asg_mod.qa_gate(rec)
            if not gate_ok:
                self.store.log_event({
                    "type": "assignment.qa_gate_refused", "at": _now(),
                    "assignment": aid, "thread": thread,
                    "delivery_comment_id": delivery, "reason": gate_why,
                    "level": "info", "consumers": ["doctor"],
                })
                out["skipped"].append(aid)
                continue
            sid = str(rec.get("sid") or "")
            if not sid:
                self._mark_blocked(rec, asg_mod.NO_SID_REASON)
                out["blocked"].append(aid)
                continue
            self.store.begin_action(base, verb="archive", kind="archive",
                                    assignment_id=aid, input_ref=str(delivery),
                                    payload={"sid": sid},
                                    issue=rec.get("issue"))
            ok, error = self._archive_one(sid)
            archived = self._read_back_archived(sid)
            if ok and archived:
                self.store.mark_done(base, sid=sid)
                rec["status"] = "waiting_release"
                rec["archived_at"] = _now()
                rec["tombstone"] = {"archived": True, "read_back": True,
                                    "notified": notified, "at": _now(),
                                    "cid": delivery, "attempt": 1}
                # Only the archive counter is prunable here: the notification
                # counter must survive so a late retry cannot re-send it.
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
        """``source.issue_state(issue)`` with every failure folded to unknown.

        Fail closed for the caller: an unreadable probe must never look like a
        closed Issue, or a broken network would complete threads by itself.
        """
        try:
            state = self.source.issue_state(int(issue))
        except Exception as exc:  # noqa: BLE001  reported as unknown, never closed
            self.store.log_event({"type": "issue.state_unreadable", "at": _now(),
                                  "issue": int(issue),
                                  "error": f"{type(exc).__name__}: {exc}"})
            return "unknown"
        return str(state or "unknown")

    # ------------------------------------------------- close reconciliation
    def _binding_roles(self, thread: str) -> list[str]:
        """Roles still registered for ``thread`` in ``issue_sessions.json``."""
        ledger = self._load_issue_sessions().get(thread)
        if not isinstance(ledger, dict):
            return []
        return sorted(str(role) for role, entry in ledger.items()
                      if isinstance(entry, dict) and entry)

    def _running_tasks(self, sid: str):
        """``(running, skipped)`` for one sid.

        An archived session returns ``[]`` from ``tasks()``, which is neither
        "idle" nor evidence: the concurrency question is answered by the
        un-cached archive read-back, so a skipped table is reported as skipped
        and the condition does not depend on it.
        """
        if not sid:
            return [], "no sid"
        try:
            from . import direct
            rows = direct.tasks(sid)
        except Exception as exc:  # noqa: BLE001  an unreadable table is not "idle"
            return [], f"unreadable: {type(exc).__name__}"
        running = [str(t.get("id") or "") for t in (rows or [])
                   if str(t.get("status") or "") == "running"]
        return running, ""

    def close_conditions(self, thread: str, index: dict) -> dict:
        """The four conditions that make a closed thread provable.

        Every answer is a live read: ``_read_back_archived`` asks the server
        and is **not** cached, so two rounds that both say "archived" are two
        independent observations rather than one repeated from memory.
        """
        recs = [rec for rec in (index.get("assignments") or {}).values()
                if str(rec.get("thread") or "") == thread]
        issue = None
        for rec in recs:
            if rec.get("issue") is not None:
                issue = int(rec.get("issue"))
                break
        issue_state = self._issue_state(issue) if issue is not None else "unknown"
        binding_roles = self._binding_roles(thread)
        unarchived, running, skipped = [], [], []
        for rec in recs:
            sid = str(rec.get("sid") or "")
            if not sid:
                continue
            if not self._read_back_archived(sid):
                unarchived.append(sid)
                continue
            tasks_running, why = self._running_tasks(sid)
            if why:
                skipped.append(f"{sid}:{why}")
            running.extend(tasks_running)
        return {
            "conditions": {
                "issue_closed": issue_state == "closed",
                "binding_cleared": not binding_roles,
                "sid_archived": not unarchived,
                # Archived sids cannot be judged; the archive read-back is what
                # carries concurrency, so an unjudgeable table is not a "false".
                "no_running_task": not running,
            },
            "detail": {"issue_state": issue_state,
                       "binding_roles": binding_roles,
                       "unarchived_sids": sorted(unarchived),
                       "running_tasks": sorted(running),
                       "tasks_skipped": "; ".join(skipped)},
        }

    def _close_blocked_reason(self, snapshot: dict) -> str:
        cond, detail = snapshot["conditions"], snapshot["detail"]
        if not cond["issue_closed"]:
            return f"issue_state={detail['issue_state']}"
        if not cond["binding_cleared"]:
            return f"binding still registered: {detail['binding_roles']}"
        if not cond["sid_archived"]:
            return f"unarchived sids: {detail['unarchived_sids']}"
        if not cond["no_running_task"]:
            return f"running tasks: {detail['running_tasks']}"
        return ""

    def _reconcile_thread(self, index: dict, thread: str) -> dict:
        """One observation of one held thread; the record lives in the index.

        ``completed`` needs the **same** four conditions in two consecutive
        rounds, and the second round must be a later ``run_round`` - the counter
        is only ever advanced by a call from a new round, so a loop inside one
        round cannot confirm itself. Any failed observation resets the streak
        to zero and moves a release into ``close_reconcile``.

        The record says ``completed`` only when the gate let every held
        assignment through; a refusal keeps it ``pending`` with the refusing
        id in ``blocked_reason``, so the next round checks the thread again.
        """
        records = index.setdefault("close_reconcile", {})
        if not isinstance(records, dict):
            records = index["close_reconcile"] = {}
        snap = self.close_conditions(thread, index)
        rec = records.get(thread) if isinstance(records.get(thread), dict) else {}
        previous_ok = int(rec.get("consecutive_ok") or 0)
        ok_now = all(snap["conditions"].values())
        round_no = int((index.get("meta") or {}).get("run_count") or 0)
        last_round = rec.get("last_checked_round")
        streak = previous_ok + 1 if ok_now and (
            previous_ok == 0 or last_round != round_no) else (1 if ok_now else 0)
        record = {
            "status": "pending", "attempts": int(rec.get("attempts") or 0) + 1,
            "consecutive_ok": streak,
            "first_checked_at": rec.get("first_checked_at") or _now(),
            "checked_at": _now(), "last_checked_round": round_no,
            "conditions": snap["conditions"], "detail": snap["detail"],
            "blocked_reason": self._close_blocked_reason(snap),
        }
        records[thread] = record
        if not ok_now:
            for assignment in (index.get("assignments") or {}).values():
                if str(assignment.get("thread") or "") != thread:
                    continue
                if str(assignment.get("status") or "") != "waiting_release":
                    continue
                moved = asg_mod.enter_close_reconcile(
                    assignment, record["blocked_reason"])
                self.store.log_event({
                    "type": ("assignment.reopened" if moved.get("changed")
                             else "close_reconcile.refused"),
                    "at": _now(), "thread": thread,
                    "assignment": assignment.get("id"),
                    "reason": moved.get("reason") or "",
                })
            self.store.log_event({"type": "close_reconcile.blocked", "at": _now(),
                                  "thread": thread,
                                  "reason": record["blocked_reason"],
                                  "streak": streak})
            return {"thread": thread, "status": "pending", "completed": [],
                    "reason": record["blocked_reason"], "conditions":
                    snap["conditions"], "consecutive_ok": streak}
        completed, refused = [], []
        if streak >= 2:
            for assignment in (index.get("assignments") or {}).values():
                if str(assignment.get("thread") or "") != thread:
                    continue
                if str(assignment.get("status") or "") not in (
                        "waiting_release", "close_reconcile"):
                    continue
                moved = asg_mod.complete(assignment)
                if not moved.get("changed"):
                    refused.append(str(assignment.get("id") or ""))
                    self.store.log_event({
                        "type": "assignment.qa_gate_refused", "at": _now(),
                        "thread": thread, "assignment": assignment.get("id"),
                        "reason": moved.get("reason") or "",
                        "level": "info", "consumers": ["doctor"]})
                    continue
                completed.append(assignment["id"])
                # A late event after this finds a terminal assignment, so it is
                # audited by on_delivery/late_event and never restarts work.
                self.store.log_event({
                    "type": "assignment.completed", "at": _now(),
                    "thread": thread, "assignment": assignment.get("id"),
                    "reason": moved.get("reason") or ""})
            # The thread record may only claim ``completed`` when every held
            # assignment actually moved: a gate refusal leaves the assignments
            # held, and a ``completed`` record would drop the thread out of
            # ``_round_closed_threads`` - no later round would ever look again.
            if completed and not refused:
                record["status"] = "completed"
                records[thread] = record
                self.store.log_event({"type": "thread.completed", "at": _now(),
                                      "thread": thread, "assignments": completed,
                                      "conditions": snap["conditions"]})
                return {"thread": thread, "status": "completed",
                        "completed": completed, "reason": "",
                        "conditions": snap["conditions"], "consecutive_ok": streak}
            record["status"] = "pending"
            record["blocked_reason"] = (
                "qa_gate_refused: " + ", ".join(sorted(refused)) if refused
                else "no held assignment to complete")
            records[thread] = record
            return {"thread": thread, "status": "pending",
                    "completed": completed,
                    "reason": record["blocked_reason"],
                    "conditions": snap["conditions"], "consecutive_ok": streak}
        self.store.log_event({"type": "close_reconcile.passed", "at": _now(),
                              "thread": thread, "streak": streak,
                              "conditions": snap["conditions"]})
        return {"thread": thread, "status": "pending", "completed": [],
                "reason": "waiting for a second round", "conditions":
                snap["conditions"], "consecutive_ok": streak}

    def _round_closed_threads(self, index: dict) -> list[dict]:
        """Check the held threads of this round, up to the configured cap.

        Only ``waiting_release`` and ``close_reconcile`` qualify: every other
        active assignment is still being worked on, and a terminal one has
        nothing left to prove.
        """
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
                      if str(rec.get("status") or "") != "completed"]
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

    def run_round(self) -> dict:
        index = self.store.load_index()
        cursor = self.store.load_cursor()
        # Both caches are round-scoped: the stream snapshot is re-read lazily
        # and each candidate sid is asked of the server at most once per round.
        self._archived_cache = None
        self._archived_unreadable = False
        self._live_check = {}
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
                   "errors": []}
        if allowlist_reload:
            summary["allowlist_reload"] = allowlist_reload
        self.store.log_event({"type": "run.started", "at": _now(), "mode": self.mode})

        try:
            issue_numbers = self.source.issues(limit=max_issues)
        except Exception as exc:  # noqa: BLE001  a dead source is a reportable state
            summary["errors"].append(f"issues: {type(exc).__name__}: {exc}")
            issue_numbers = []

        for issue in issue_numbers:
            try:
                self._round_issue(issue, index, cursor, max_comments, summary,
                                  max_pages)
            except Exception as exc:  # noqa: BLE001  isolate one bad issue
                summary["errors"].append(f"issue {issue}: {type(exc).__name__}: {exc}")
                self.store.log_event({"type": "issue.error", "at": _now(),
                                      "issue": int(issue),
                                      "error": f"{type(exc).__name__}: {exc}"})

        summary["notifications_retried"] = self._retry_waiting_review(index)
        self._retry_routes(index)
        self._retry_supervisor_routes(index)
        # Archive last: a review that arrived this round is archived this
        # round, and the archived-sid guard applies from the next dispatch on.
        self._archived_cache = None
        for thread in sorted({str(rec.get("thread") or "")
                              for rec in (index.get("assignments") or {}).values()
                              if str(rec.get("status") or "") in
                              ("waiting_review", "waiting_release")}):
            if thread:
                result = self.archive_thread(thread, index)
                summary.setdefault("archived", []).extend(result["archived"])
                summary.setdefault("archive_blocked", []).extend(result["blocked"])

        # Index is written once per round, atomically: a crash leaves the old one.
        # The dismissal side table is monotonic, so unioning whatever is on disk
        # now keeps a ``resolve`` run during this round from being overwritten.
        needs.merge_into(index, self.store.load_index())
        index["meta"].update({
            "updated_at": _now(), "mode": self.mode, "role": self.role,
            "dispatch_enabled": self.dispatch_enabled,
            "run_count": int(index["meta"].get("run_count") or 0) + 1,
        })
        self.store.save_index(index)
        self.store.save_cursor(cursor)
        # Close reconciliation last: the round's own archives are visible to it,
        # and ``run_count`` is already incremented, so the two-round streak can
        # only be advanced by a genuine next round.
        self._round_closed_threads(index)
        self._remind_release_timeouts(index)
        needs.merge_into(index, self.store.load_index())
        self.store.save_index(index)
        self.store.log_event({"type": "run.completed", "at": _now(),
                              "processed": summary["processed"],
                              "needs_person": summary["needs_person"],
                              "external_actions": self.store.load_counters()
                              .get("external_actions", 0)})
        summary["external_actions"] = self.store.load_counters().get("external_actions", 0)
        health_errors = list(summary["errors"])
        if allowlist_reload:
            health_errors.append(f"allowlist kept previous copy: {allowlist_reload}")
        self.write_health(error="; ".join(health_errors), degraded=self.degraded)
        return summary

    def _round_issue(self, issue: int, index: dict, cursor: dict,
                     max_comments: int, summary: dict,
                     max_pages: int = 10) -> None:
        issue = int(issue)
        key = self._thread_key(issue)
        legacy = str(issue)
        summary["issues"] += 1
        issues = index.setdefault("issues", {})
        rec = issues.setdefault(key, {"state": "idle", "comments_seen": 0,
                                      "needs_person": []})
        rec["title"] = rec.get("title") or self.source.title(issue)
        # Assignments come from the session stream, once per issue per round,
        # before any comment is triaged: a delivery needs its assignment to
        # exist already.
        synced = self._sync_assignments(index, key, issue)
        summary["assignments"] += len(synced.get("created") or [])
        # Qualified key first, legacy second: a mixed file converges instead of
        # re-reading comments already consumed under the old form.
        cur = int(issuekey.read_both(cursor.get("issues") or {}, key, legacy) or 0)
        # Paged read from the cursor, then take the **oldest** N new comments:
        # taking the newest N (the old slice) silently skipped the middle when
        # an issue had more new comments than the cap.
        comments = github_source.comments_from(self.source, issue, since_id=cur,
                                               max_pages=max_pages)
        fresh = [c for c in comments if int(c["id"]) > cur][:max_comments]
        summary["comments_new"] += len(fresh)

        seen_this_round: set[int] = set()
        for comment in fresh:
            event_type = "issue_comment.created"
            if event_type not in self.allowlist:
                continue
            cid = int(comment["id"])
            if cid in seen_this_round:          # in-run dedupe only
                continue
            seen_this_round.add(cid)
            state, reason = self._triage(comment)
            delivery = markers.parse_delivery(str(comment.get("body") or ""))
            review = markers.parse_review(str(comment.get("body") or ""))
            summary["processed"] += 1
            rec["comments_seen"] = int(rec.get("comments_seen") or 0) + 1
            rec["last_comment_id"] = cid
            rec["last_event_ts"] = _now()
            self.store.log_event({"type": "comment.processed", "at": _now(),
                                  "issue": issue, "comment_id": cid, "state": state,
                                  "reason": reason})
            if state == "delivery":
                state, reason = self._on_delivery(index, key, issue, comment,
                                                  delivery, summary)
                summary["deliveries"] += 1
                if state != "needs_person":
                    summary["processed"] += 0
                    if rec.get("state") == "idle":
                        rec["state"] = "tracked"
                    continue
            if state == "review":
                state, reason = self._on_review(index, key, issue, comment,
                                                review, summary)
                summary["reviews"] = int(summary.get("reviews") or 0) + 1
                if state != "needs_person":
                    if rec.get("state") == "idle":
                        rec["state"] = "tracked"
                    continue
            if state == "needs_person":
                rec["state"] = "needs_person"
                rec.setdefault("needs_person", []).append({
                    "comment_id": cid, "reason": reason, "at": _now(),
                })
                summary["needs_person"] += 1
                now = self.clock()
                if self._cooldown_ok(rec, "needs_person", now):
                    self._mark_cooldown(rec, "needs_person", now)
                    self._dispatch({"kind": "bus_inject",
                                    "event": "event.agent.needs_person",
                                    "issue": issue,
                                    "payload": {"issue": issue, "comment_id": cid,
                                                "reason": reason}})
            elif rec.get("state") == "idle":
                rec["state"] = "tracked"

        if fresh:
            cursor.setdefault("issues", {})[key] = max(int(c["id"]) for c in fresh)
        else:
            cursor.setdefault("issues", {}).setdefault(key, cur)

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

        health = {}
        if self.bus is not None:
            health = self.bus.healthz()
            connected = bool(health.get("connected"))
            ready = bool(health.get("ready"))
            add("bus", bool(health.get("ok")),
                f"ok={health.get('ok')} ready={ready} connected={connected} "
                f"boot_id={health.get('boot_id') or '-'} "
                f"seq={health.get('seq')} "
                f"{health.get('error') or ''}".strip(), label="bus-down")
        else:
            add("bus", False, "no bus client configured", level="warn",
                label="bus-down")

        source_name = (self.cfg.get("github") or {}).get("source")
        if source_name == "none":
            add("github", True, "source=none (skipped)")
        else:
            try:
                issues = self.source.issues(limit=1)
                add("github", True, f"source={source_name} reachable, sample={issues}")
            except Exception as exc:  # noqa: BLE001
                add("github", False, f"source={source_name} error: "
                                     f"{type(exc).__name__}: {exc}",
                    label="github-down")

        add(*self._check_config())

        stuck = self.store.stuck_outbox()
        if stuck:
            detail = "; ".join(f"{r.get('action_id')} {r.get('status')} "
                               f"age={r.get('age_s')}s" for r in stuck[:5])
            add("outbox", False,
                f"{len(stuck)} action(s) past {self.store.outbox_stale_s:g}s: {detail}",
                label="outbox-stuck")
        else:
            add("outbox", True, "no prepared/in_flight/uncertain action is overdue")

        # Reported separately on purpose: ``outbox-stuck`` means "nobody is
        # looking", ``waiting-person`` means "a person has to decide". Merging
        # the two would make a resolved-looking alarm hide a real queue.
        waiting = self.store.waiting_person()
        add("waiting-person", not waiting,
            "none" if not waiting else
            f"{len(waiting)} action(s) need a person: " + "; ".join(
                f"{r.get('action_id')} ({r.get('reason') or 'no remote confirmation'})"
                for r in waiting[:5]),
            label="waiting-person")

        cursor = self.store.load_cursor()
        bad_cursor = [k for k, v in (cursor.get("issues") or {}).items()
                      if not isinstance(v, int) or v < 0
                      or issuekey.issue_number(k) is None]
        add("cursor", not bad_cursor,
            "cursor ok" if not bad_cursor else f"invalid cursors: {bad_cursor}")

        index = self.store.load_index()
        residual = asg_mod.active_assignments(index)
        add("assignments", not residual,
            "none" if not residual else f"non-terminal: {sorted(residual)}",
            level="warn")

        counters = self.store.load_counters()
        return {"checks": checks, "external_actions":
                int(counters.get("external_actions") or 0),
                "mode": self.mode}

    # ------------------------------------------------------ doctor sub-checks
    def _check_process(self) -> tuple:
        """Fault 1: a dead pid lock, or a health record with no live engine."""
        from .pidlock import process_alive
        # Read the snapshot first: the real sequence for a failed startup is
        # "startup failed -> process exits (rc=1) -> the lock was not cleaned
        # up", so the stale pid lock and the startup-failed snapshot arrive
        # together. Judging the lock first would answer "the next start takes
        # it over" and never mention why the last start gave up.
        snap = health.read(self.store.health_path)
        startup_failed = (snap.get("phase") == "startup-failed"
                          and not snap.get("running"))
        try:
            info = json.loads(self.store.pid_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            info = {}
        if info.get("pid"):
            pid = info.get("pid")
            if process_alive(pid):
                return ("process", True,
                        f"engine.pid pid={pid} alive since={info.get('started_at')}",
                        "fail", "process-dead")
            if startup_failed:
                return ("process", False,
                        f"startup-failed with a stale engine.pid pid={pid}: "
                        f"last_error={snap.get('last_error') or 'unknown'}; "
                        f"the last start failed during startup and left the "
                        f"lock behind", "fail", "startup-failed")
            return ("process", False,
                    f"engine.pid pid={pid} is not alive (stale lock; the next "
                    f"start takes it over)", "fail", "process-dead")
        if startup_failed:
            # The process wrote its snapshot, failed during startup and exited
            # (rc=1) before any round ran. It did not crash mid-loop, so the
            # label says so and the detail carries the reason it gave up.
            return ("process", False,
                    f"startup-failed: the engine exited while starting up "
                    f"(last_error={snap.get('last_error') or 'unknown'}, "
                    f"pid={snap.get('pid')}); no round ran, so this is not a "
                    f"crash of a running loop", "fail", "startup-failed")
        if snap.get("running"):
            return ("process", False,
                    f"crashed: no pid lock but health.json is still running=true "
                    f"(last_loop_at={snap.get('last_loop_at')}, pid={snap.get('pid')}); "
                    f"the process died without a clean exit", "fail",
                    "process-dead")
        if snap.get("last_loop_at"):
            return ("process", True,
                    f"engine stopped cleanly (last_loop_at={snap.get('last_loop_at')}, "
                    f"pid={snap.get('pid')})", "warn", "process-dead")
        return ("process", True,
                "engine not running (no pid lock, no health record)", "warn",
                "process-dead")

    def _check_config(self) -> tuple:
        """Fault 4: allowlist rejected; the previous copy is still in force."""
        if self.projects is None:
            return ("config", True, "no project allowlist configured", "fail",
                    "config-bad")
        snap = self.projects.summary()
        if snap["valid"] and not snap["error"]:
            return ("config", True,
                    f"allowlist ok projects={snap['projects']} "
                    f"allowlist={self.projects.source_label()} "
                    f"generation={snap['generation']}", "fail", "config-bad")
        if snap["valid"]:
            return ("config", False,
                    f"config-bad: {snap['error']} - still using the previous copy "
                    f"(last-good cache, generation={snap['generation']})",
                    "fail", "config-bad")
        return ("config", False,
                f"config-bad: {snap['error'] or 'no valid copy'}; nothing is allowed",
                "fail", "config-bad")

    # -------------------------------------------------------------------- replay
    def replay(self, issue: int) -> dict:
        """Rebuild one issue's index entry from the source. Read-only."""
        issue = int(issue)
        index = self.store.load_index()
        cursor = self.store.load_cursor()
        rec = {"state": "idle", "comments_seen": 0, "needs_person": [],
               "title": self.source.title(issue), "rebuilt_at": _now()}
        comments = self.source.comments(issue)
        for comment in comments:
            cid = int(comment["id"])
            rec["comments_seen"] += 1
            state, reason = self._triage(comment)
            if state == "needs_person":
                rec["state"] = "needs_person"
                rec["needs_person"].append({"comment_id": cid, "reason": reason,
                                            "at": _now()})
            elif rec["state"] == "idle":
                rec["state"] = "tracked"
        rec["last_comment_id"] = max((int(c["id"]) for c in comments), default=0)
        key = self._thread_key(issue)
        index.setdefault("issues", {})[key] = rec
        index.setdefault("meta", {})["updated_at"] = _now()
        index["meta"]["rebuilt_issue"] = issue
        self.store.save_index(index)
        cursor.setdefault("issues", {})[key] = rec["last_comment_id"]
        self.store.save_cursor(cursor)
        self.store.log_event({"type": "replay.rebuilt", "at": _now(), "issue": issue,
                              "comments": rec["comments_seen"],
                              "external_actions":
                                  self.store.load_counters().get("external_actions", 0)})
        return {"issue": issue, "comments": rec["comments_seen"],
                "state": rec["state"],
                "external_actions": self.store.load_counters()
                .get("external_actions", 0)}

    # ------------------------------------------------------ session truth (ISS-27)
    def _session_stream(self) -> tuple[Path, Path]:
        engine_cfg = self.cfg.get("engine") or {}
        events = session_events.resolve_events_path(
            self.cfg, engine_cfg.get("session_events_path") or None)
        sessions = session_events.resolve_sessions_path(self.cfg)
        return events, sessions

    def replay_sessions(self, at: str | None = None) -> dict:
        """Rebuild ``sid -> (role, issue, assignment, profile)`` as of ``at``.

        Reads the workbench lifecycle stream (``tools/sess_events.py:emit`` wrote
        it). Strictly read-only and it never writes into ``store/``: the engine's
        own ``events.jsonl`` is a different stream and stays untouched.
        """
        events_path, _sessions_path = self._session_stream()
        events = session_events.load_events(events_path)
        state = session_events.replay(events, at)
        state["events_path"] = str(events_path)
        state["keys_active"] = session_events.active_keys(state)
        return state

    def reconcile_sessions(self, at: str | None = None,
                           server_sids: set[str] | None = None) -> dict:
        """Three-way: replay × ``sessions.json`` × live server. Read-only."""
        events_path, sessions_path = self._session_stream()
        return session_events.reconcile(events_path, sessions_path, server_sids, at)


def build_engine(cfg: dict, store: Store, bus=None, *, source=None) -> Engine:
    """``source`` overrides the configured backend (``status`` needs none)."""
    if source is None:
        try:
            source = github_source.build(cfg)
        except Exception as exc:  # noqa: BLE001  report as a failed check, never crash
            source = github_source.BrokenSource(f"{type(exc).__name__}: {exc}")
    return Engine(cfg, store, source, bus)
