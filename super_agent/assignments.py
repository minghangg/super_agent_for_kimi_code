"""Thread × Assignment: the single-issue loop's state machine.

An **assignment** is one role's claim to work one issue: ``asg_<issue>_<role>_<n>``.
The index holds them under ``assignments``, keyed by that id, and the record
carries the session it was bound to, the comment that triggered it, and the
delivery that closed it.

State machine::

    queued -> working -> waiting_review -> waiting_release -> completed
                  |            |                 |
                  |            +-> blocked       +-> close_reconcile -> completed
                  +-> blocked  +-> waiting_person      |
                        |                             +-> working (reopen)
                        +-> working (unblock)

``waiting_release`` and ``close_reconcile`` are *hold* states: the work is
done and a QA pass is recorded, but the engine has not proved the Issue is
closed and the sessions are archived. They are active on purpose - the engine
must keep looking at a thread in either state, which is what ``doctor``'s
non-terminal warning is for.

The transitions, in full (a row is a test)::

    #   from                              trigger                     to
    1   queued|working|waiting_review     [WK:name] delivery          waiting_review
    2   waiting_release|close_reconcile   late [WK:]                  unchanged (audit)
    3   waiting_review                    [QA] pass                   waiting_release
    4   waiting_review                    [QA] reject                 working
    5   working                           [QA] pass                   waiting_release
                                                                      (stale if the
                                                                      delivery is newer)
    6   waiting_review                    [QA] with no verdict        waiting_person
    7   waiting_review                    verdict key unverified      still 3/4
    8   waiting_review                    pass + archived read-back   waiting_release
    9   waiting_release                   four conditions, two rounds completed
    10  waiting_release                   any condition false         close_reconcile
    11  close_reconcile                   four conditions, two rounds completed
    12  close_reconcile                   reject + newer delivery     working
                                                                      + live session
    13  close_reconcile                   only some of the three      unchanged
    14  blocked                           [WK:] newer than tombstone  waiting_review
    15  completed                         any late event              unchanged (audit)
    16  blocked                           cli unblock                 working


Rules this module enforces (each one is a test):

* **The source of truth is the session stream**, not a guess. A live binding
  comes from ``session_events.active_keys()`` filtered to ``role in {worker,qa}``
  with an issue and a non-archived session. If the stream cannot be read, no
  assignment is created and the engine is marked ``degraded`` - fail closed,
  never an empty loop that looks healthy.
* **Archiving is the one thing the stream cannot be trusted for.** An archive
  made through ``POST /api/v2/sessions:archive`` writes no ``session.archived``
  row; only the workbench's own ``tools/sess_events.py:emit`` writes that
  event. So the stream's ``archived=False`` means "nobody emitted an archive",
  not "the session is live". The engine asks the server about the *candidate*
  sids (one cached answer per sid per round) and passes the confirmed ones as
  ``archived=`` to :func:`live_bindings`. Without that, a closed worker stays
  a live binding and the next round forks ``asg_<issue>_<role>_2`` on the same
  sid. An unreachable server counts as archived (fail closed) and is logged.
* ``issue_sessions.json`` is only a **cross-check**: it confirms a thread has a
  binding, it never creates one (it covers a subset of live bindings).
* A delivery is matched to an assignment by marker name first, then by "the
  only live worker on this thread". Anything else is ``unmatched`` and escalates
  to a person - a delivery is never dropped silently.
* The ``waiting_review`` notification is **at-most-once**. The decision is
  ``Store.action_attempted(base) == 0``, and the attempt counter is persisted
  before the external call, so a crash either replays the same record or waits
  for reconciliation. A refused/disabled dispatch writes no counter, which is
  what makes "refused -> retry next round" safe.
* **An attempt is not a delivery.** ``uncertain`` means the call raised, so the
  supervisor was never told; it must never be recorded as ``notified_at``. The
  archive pass may still close the assignment (a worker that delivered is
  done), but its tombstone says ``notified: false`` and the loss is logged.
"""
from __future__ import annotations

from datetime import datetime, timezone

ACTIVE_STATES = ("queued", "working", "waiting_review", "waiting_release",
                 "close_reconcile", "blocked", "waiting_person")
TERMINAL_STATES = ("completed",)
PARTICIPANT_ROLES = ("worker", "qa")

# A hold state waits for the supervisor to close the Issue; a delivery that
# arrives there is late and is only audited (a new ``[WK:]`` must not restart
# work the QA already accepted).
HOLD_STATES = ("waiting_release", "close_reconcile")
NO_SID_REASON = "no sid recorded"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def assignment_id(issue, role: str, seq: int) -> str:
    return f"asg_{int(issue)}_{str(role)}_{int(seq)}"


def next_id(assignments: dict, issue, role: str) -> str:
    """Highest existing sequence for ``(issue, role)`` plus one."""
    prefix = f"asg_{int(issue)}_{str(role)}_"
    seq = 0
    for aid in assignments or {}:
        if str(aid).startswith(prefix):
            try:
                seq = max(seq, int(str(aid)[len(prefix):]))
            except ValueError:
                continue
    return assignment_id(issue, role, seq + 1)


def is_active(rec: dict | None) -> bool:
    return bool(rec) and str((rec or {}).get("status") or "") in ACTIVE_STATES


def live_bindings(state: dict, thread: str, issue: int | None = None,
                  archived: set[str] | None = None) -> list[dict]:
    """Sessions currently bound to ``thread`` as a worker/qa participant.

    ``state`` is a ``session_events.replay()`` result. The filter is the one QA
    asked for: a session with no role or no issue takes no part in the thread
    view (its ``key`` is just its name).

    ``archived`` is an extra exclusion set supplied by the caller. The stream
    only knows about archives written by the workbench's own tooling; an
    archive performed through the session API leaves no ``session.archived``
    row, so ``rec["archived"]`` alone keeps a closed session looking live. The
    engine asks the server (one cached answer per sid per round) and passes the
    confirmed sids here. This function stays pure: it never calls out.

    Matching uses the **issue number**, not the event's ``key``: ``bind`` puts
    the worker's *name* (``wk-probe-1``) in ``key`` and the issue number in
    ``issue``, so comparing keys would never match a real binding.
    """
    sessions = state.get("sessions") or {}
    keys = session_events_active(state)
    by_key = {sid: key for key, sid in keys.items()}
    known_archived = set(archived or ())
    out = []
    for sid, rec in sessions.items():
        if rec.get("archived") or sid in known_archived:
            continue
        role = str(rec.get("role") or "")
        if role not in PARTICIPANT_ROLES:
            continue
        bound_issue = rec.get("issue")
        if bound_issue is None:
            continue
        if issue is not None and int(bound_issue) != int(issue):
            continue
        name = str(rec.get("name") or by_key.get(sid) or rec.get("key") or "")
        out.append({"key": by_key.get(sid) or thread, "sid": sid, "role": role,
                    "issue": bound_issue, "name": name})
    return out


def session_events_active(state: dict) -> dict:
    """``active_keys(state)`` without importing the module at import time."""
    from .session_events import active_keys
    return active_keys(state)


def sync(index: dict, thread: str, issue, live: list[dict], *,
         crosscheck: set[str] | None = None) -> dict:
    """Create/refresh assignments for ``live`` bindings.

    Returns ``{"created": [...], "updated": [...], "conflict": [...]}``. A
    session already carried by an active assignment is not re-created; the
    earliest assignment for the same ``(thread, role)`` wins, so a rotation
    updates ``sid`` instead of forking a second worker.
    """
    assignments = index.setdefault("assignments", {})
    out = {"created": [], "updated": [], "conflict": [], "crosscheck": []}
    by_role: dict[str, list[str]] = {}
    for aid, rec in assignments.items():
        if str(rec.get("thread") or "") != thread:
            continue
        if str(rec.get("status") or "") in TERMINAL_STATES:
            continue
        by_role.setdefault(str(rec.get("role") or ""), []).append(aid)

    for bind in live:
        role = bind["role"]
        key = bind.get("name") or bind["key"]
        existing = sorted(by_role.get(role) or [])
        # A role may be held by more than one session at a time, so the match
        # key is the *session* (sid or session name), not the role alone.
        # Matching on the role merged a second worker into the first record and
        # lost it.
        match = None
        for aid in existing:
            rec = assignments[aid]
            if rec.get("sid") == bind["sid"] or (
                    rec.get("session_key") and rec.get("session_key") == key):
                match = aid
                break
        if match is not None:
            rec = assignments[match]
            if rec.get("sid") != bind["sid"]:
                rec["sid"] = bind["sid"]
                rec["session_key"] = key
                rec["updated_at"] = now()
                out["updated"].append(match)
            continue
        aid = next_id(assignments, issue, role)
        assignments[aid] = {
            "id": aid, "thread": thread, "issue": int(issue), "role": role,
            "sid": bind["sid"], "session_key": key,
            "input_comment_id": None, "status": "working",
            "attempts": 0, "notification_action_id": "", "notified_at": "",
            "delivery_comment_id": None, "delivery_marker": "",
            "matched_by": "", "created_at": now(), "updated_at": now(),
            "archived_at": "", "tombstone": {},
        }
        by_role.setdefault(role, []).append(aid)
        out["created"].append(aid)

    if crosscheck is not None:
        stray = sorted(crosscheck - {b["sid"] for b in live})
        out["crosscheck"] = stray
    return out


def match_delivery(index: dict, thread: str, name: str) -> tuple[dict | None, str]:
    """Find the assignment a ``[WK:<name>]`` delivery belongs to.

    Returns ``(assignment, matched_by)`` or ``(None, reason)``. Matching order:

    1. ``session_key`` equals the marker name (exact, the normal path);
    2. the thread has exactly **one** live worker (``matched_by="sole"``);
    3. otherwise nothing matches and the caller must escalate.
    """
    assignments = index.get("assignments") or {}
    candidates = [rec for rec in assignments.values()
                  if str(rec.get("thread") or "") == thread
                  and str(rec.get("status") or "") in ACTIVE_STATES]
    for rec in candidates:
        if str(rec.get("session_key") or "") == name:
            return rec, "marker"
    workers = [rec for rec in candidates if str(rec.get("role") or "") == "worker"]
    if len(workers) == 1:
        return workers[0], "sole"
    return None, "no live worker matches the marker"


def on_delivery(assignment: dict, delivery_cid, name: str,
                matched_by: str, *, verdict: str = "") -> dict:
    """Record a delivery; move to ``waiting_review``.

    Four outcomes, each its own answer so the caller can log one event:

    * **terminal** - nothing changes (``late_event``);
    * **hold state** (``waiting_release``/``close_reconcile``) - a late
      delivery is audited, never a restart;
    * **blocked** - the exit is *not* "a newer comment"; it is "a newer comment
      than the blocked one, or a block that a missing sid caused". The tombstone
      carries ``cid`` **and** ``attempt``: the same delivery failing twice bumps
      the attempt and keeps the cid, so the guard cannot be reset by re-reading
      one comment. A ``no sid`` tombstone has no cid at all (the archive never
      started), so completing the sid is enough to retry;
    * **everything else** - ``waiting_review`` (idempotent on the delivery id).
    """
    status = str(assignment.get("status") or "")
    if status in TERMINAL_STATES:
        return {"changed": False, "reason": "assignment already completed"}
    if status in HOLD_STATES:
        return {"changed": False, "reason": "late delivery after a QA pass"}
    if status == "blocked":
        gate = blocked_exit(assignment, delivery_cid)
        if not gate["ok"]:
            return {"changed": False, "reason": gate["reason"]}
    if assignment.get("delivery_comment_id") == delivery_cid:
        return {"changed": False, "reason": "same delivery already recorded"}
    assignment["delivery_comment_id"] = delivery_cid
    assignment["delivery_marker"] = name
    assignment["matched_by"] = matched_by
    assignment["status"] = "waiting_review"
    assignment["updated_at"] = now()
    if verdict:
        assignment["qa_verdict"] = verdict
    return {"changed": True, "reason": "waiting_review"}


def blocked_exit(assignment: dict, delivery_cid) -> dict:
    """May a ``blocked`` assignment take this delivery?

    ``cid`` comparison is skipped when the block was "no sid recorded": that
    failure happened *before* the archive, so no delivery had been consumed and
    re-reading the same comment is exactly the retry that should work. Any
    other tombstone is an archive failure for a known delivery, and only a
    strictly newer comment may retry it.
    """
    tomb = assignment.get("tombstone") or {}
    if str(tomb.get("reason") or "") == NO_SID_REASON:
        return {"ok": True, "reason": "no sid recorded: a completed sid may retry"}
    if not assignment.get("sid"):
        return {"ok": True, "reason": "no sid on the record: a completed sid may retry"}
    blocked_cid = tomb.get("cid")
    try:
        blocked_cid = int(blocked_cid)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "blocked without a delivery id: a person decides"}
    if int(delivery_cid) > blocked_cid:
        return {"ok": True, "reason": "newer than the blocked delivery"}
    return {"ok": False,
            "reason": f"delivery {delivery_cid} is not newer than blocked {blocked_cid}"}


def mark_blocked(assignment: dict, reason: str, *, cid=None, at: str = "") -> None:
    """Block an assignment with a tombstone a later round can reason about.

    ``cid`` is ``None`` for a failure that happened before a delivery was acted
    on (``no sid recorded``) and the delivery id once an archive started. The
    attempt counter is monotonic: a second failure for the **same** cid bumps
    it instead of overwriting the cid, so "blocked -> unblock -> same failure
    again" is still recorded as attempt 2.
    """
    previous = assignment.get("tombstone") or {}
    attempt = int(previous.get("attempt") or 0)
    if previous.get("cid") == cid and str(previous.get("reason") or "") == reason:
        attempt += 1 if previous.get("at") else 0
    else:
        attempt = 1 if not previous.get("at") else max(attempt, 1) + 1
    assignment["status"] = "blocked"
    assignment["updated_at"] = at or now()
    assignment["tombstone"] = {"archived": False, "reason": reason,
                               "at": at or now(), "cid": cid,
                               "attempt": attempt}


def qa_gate(assignment: dict) -> tuple[bool, str]:
    """The single gate in front of *every* write that ends a thread.

    A thread may only be archived or completed when the QA verdict on record is
    ``pass``. This is called from the archive pass (before ``begin_action``, the
    first side effect) and from :func:`complete`, so a manual status flip or a
    missing review can never reach ``completed``.
    """
    if str(assignment.get("status") or "") in TERMINAL_STATES:
        return False, "assignment already completed"
    verdict = str(assignment.get("qa_verdict") or "")
    if verdict == "pass":
        return True, "qa pass"
    if verdict == "reject":
        return False, "qa rejected: work must return to the worker first"
    return False, "no qa pass recorded"


def on_review(assignment: dict, review: dict, *, review_cid, verified: bool = True,
              delivery_cid=None) -> dict:
    """Apply one parsed ``[QA]`` verdict.

    ``review["verdict"]`` is empty when the parser read no conclusion; that is
    ``waiting_person`` (never a guess). An unverified source - the comment did
    not come from this thread's QA session key - is downgraded to a recorded
    warning and the verdict **still applies**: the author login is shared by
    every role, so a key mismatch cannot be proof of a forgery, and refusing to
    move would leave every rotated QA unable to reject anything.
    """
    status = str(assignment.get("status") or "")
    if status in TERMINAL_STATES or status in HOLD_STATES:
        return {"changed": False, "reason": "review after the hold state"}
    if assignment.get("last_review_comment_id") == review_cid:
        return {"changed": False, "reason": "same review already recorded"}
    verdict = str(review.get("verdict") or "")
    assignment["last_review_comment_id"] = review_cid
    assignment["review_verified"] = bool(verified)
    assignment["updated_at"] = now()
    if not verdict:
        assignment["status"] = "waiting_person"
        return {"changed": True, "status": "waiting_person",
                "reason": "no readable verdict"}
    if verdict == "pass":
        # A pass re-read after a newer delivery is stale: the worker delivered
        # again, so the review no longer describes the code on the table.
        delivery = (assignment.get("delivery_comment_id") if delivery_cid is None
                    else delivery_cid)
        try:
            delivery = int(delivery)
        except (TypeError, ValueError):
            delivery = None
        if delivery is not None and delivery > int(review_cid):
            return {"changed": False, "reason": "review.stale"}
        assignment["status"] = "waiting_release"
        assignment["qa_verdict"] = "pass"
        assignment["review_comment_id"] = review_cid
        return {"changed": True, "status": "waiting_release", "reason": "qa pass"}
    assignment["status"] = "working"
    assignment["qa_verdict"] = "reject"
    assignment["review_comment_id"] = review_cid
    return {"changed": True, "status": "working", "reason": "qa reject"}


def enter_close_reconcile(assignment: dict, reason: str = "") -> dict:
    """Hold a release whose close-out did not check out.

    Refused outright when no QA pass is on record: ``close_reconcile`` means
    "the QA said pass but the close-out is unproven", never "skip the QA".
    """
    ok, why = qa_gate(assignment)
    if not ok:
        return {"changed": False, "reason": why, "refused": True}
    if str(assignment.get("status") or "") in TERMINAL_STATES:
        return {"changed": False, "reason": "assignment already completed"}
    assignment["status"] = "close_reconcile"
    assignment["updated_at"] = now()
    if reason:
        assignment["close_reconcile_reason"] = reason
    return {"changed": True, "status": "close_reconcile", "reason": why}


def complete(assignment: dict) -> dict:
    """Write the terminal state - only through :func:`qa_gate`."""
    ok, why = qa_gate(assignment)
    if not ok:
        return {"changed": False, "reason": why, "refused": True}
    assignment["status"] = "completed"
    assignment["updated_at"] = now()
    return {"changed": True, "status": "completed", "reason": why}


def reopen(assignment: dict, *, review_cid=None, delivery_cid=None,
           archived: bool | None = None) -> dict:
    """Return a ``close_reconcile`` thread to ``working`` - all three or none.

    A reopen means the QA verdict changed after the pass, so it needs every one
    of: a newer ``reject`` (``review_cid`` greater than the recorded review), a
    newer delivery than that reject, and a **live** session. One condition
    alone is not enough - that is how a completed thread gets resurrected by a
    stale comment.
    """
    if str(assignment.get("status") or "") != "close_reconcile":
        return {"changed": False, "reason": "not in close_reconcile", "refused": True}
    try:
        newer_review = int(review_cid) > int(assignment.get("review_comment_id") or -1)
        newer_delivery = int(delivery_cid) > int(review_cid)
    except (TypeError, ValueError):
        return {"changed": False, "reason": "incomplete reopen evidence",
                "refused": True}
    live = archived is False     # unknown counts as "not provably live"
    if not (newer_review and newer_delivery and live):
        return {"changed": False, "reason": "reopen needs a newer reject, a newer "
                "delivery and a live session", "refused": True}
    assignment["status"] = "working"
    assignment["qa_verdict"] = "reject"
    assignment["review_comment_id"] = review_cid
    assignment["delivery_comment_id"] = delivery_cid
    assignment["updated_at"] = now()
    return {"changed": True, "status": "working", "reason": "reopened"}


def unblock(assignment: dict, reason: str = "") -> dict:
    """``cli unblock``: ``blocked`` back to ``working``, with a timestamp.

    The tombstone is kept: the next failure needs it to bump the attempt
    counter, and the audit needs the original reason.
    """
    if str(assignment.get("status") or "") != "blocked":
        return {"changed": False, "reason": "not blocked", "refused": True}
    assignment["status"] = "working"
    assignment["unblocked_at"] = now()
    assignment["unblock_reason"] = str(reason or "")
    assignment["updated_at"] = now()
    return {"changed": True, "status": "working", "reason": "unblocked"}


def late_event(assignment: dict, kind: str, cid=None) -> dict:
    """A ``completed`` (or held) assignment saw an event: audit only."""
    status = str(assignment.get("status") or "")
    if status in TERMINAL_STATES:
        return {"changed": False, "audit": True, "reason": "late_event.audit"}
    if status in HOLD_STATES:
        return {"changed": False, "audit": True, "reason": "late_event.audit"}
    return {"changed": False, "audit": False, "reason": "active"}


def active_assignments(index: dict) -> list[str]:
    """Assignments that still need work (the hold states included)."""
    return sorted(aid for aid, rec in (index.get("assignments") or {}).items()
                  if str(rec.get("status") or "") in ACTIVE_STATES)
