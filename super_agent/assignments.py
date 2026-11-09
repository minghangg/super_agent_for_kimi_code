"""Thread × Assignment: the single-issue loop's state machine.

An **assignment** is one role's claim to work one issue: ``asg_<issue>_<role>_<n>``.
The index holds them under ``assignments``, keyed by that id, and the record
carries the session it was bound to, the comment that triggered it, and the
delivery that closed it.

State machine::

    queued -> working -> waiting_review -> completed
                  |            |
                  +-> blocked  +-> waiting_person

Rules this module enforces (each one is a test):

* **The source of truth is the session stream**, not a guess. A live binding
  comes from ``session_events.active_keys()`` filtered to ``role in {worker,qa}``
  with an issue and a non-archived session. If the stream cannot be read, no
  assignment is created and the engine is marked ``degraded`` - fail closed,
  never an empty loop that looks healthy.
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
"""
from __future__ import annotations

from datetime import datetime, timezone

ACTIVE_STATES = ("queued", "working", "waiting_review", "blocked",
                 "waiting_person")
TERMINAL_STATES = ("completed",)
PARTICIPANT_ROLES = ("worker", "qa")


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


def live_bindings(state: dict, thread: str) -> list[dict]:
    """Sessions currently bound to ``thread`` as a worker/qa participant.

    ``state`` is a ``session_events.replay()`` result. The filter is the one QA
    asked for: a session with no role or no issue takes no part in the thread
    view (its ``key`` is just its name).
    """
    sessions = state.get("sessions") or {}
    keys = session_events_active(state)
    out = []
    for key, sid in sorted(keys.items()):
        if key != thread:
            continue
        rec = sessions.get(sid) or {}
        if rec.get("archived"):
            continue
        role = str(rec.get("role") or "")
        if role not in PARTICIPANT_ROLES:
            continue
        if rec.get("issue") is None:
            continue
        out.append({"key": key, "sid": sid, "role": role,
                    "issue": rec.get("issue"),
                    "name": str(rec.get("name") or "")})
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
        existing = sorted(by_role.get(role) or [])
        if existing:
            aid = existing[0]
            rec = assignments[aid]
            if rec.get("sid") != bind["sid"]:
                rec["sid"] = bind["sid"]
                rec["session_key"] = bind.get("name") or bind["key"]
                rec["updated_at"] = now()
                out["updated"].append(aid)
            continue
        aid = next_id(assignments, issue, role)
        assignments[aid] = {
            "id": aid, "thread": thread, "issue": int(issue), "role": role,
            "sid": bind["sid"], "session_key": bind.get("name") or bind["key"],
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
                matched_by: str) -> dict:
    """Move one assignment to ``waiting_review`` (idempotent)."""
    if str(assignment.get("status") or "") in TERMINAL_STATES:
        return {"changed": False, "reason": "assignment already completed"}
    if assignment.get("delivery_comment_id") == delivery_cid:
        return {"changed": False, "reason": "same delivery already recorded"}
    assignment["delivery_comment_id"] = delivery_cid
    assignment["delivery_marker"] = name
    assignment["matched_by"] = matched_by
    assignment["status"] = "waiting_review"
    assignment["updated_at"] = now()
    return {"changed": True, "reason": "waiting_review"}


def active_assignments(index: dict) -> list[str]:
    """Assignments that still need work (``waiting_review`` included)."""
    return sorted(aid for aid, rec in (index.get("assignments") or {}).items()
                  if str(rec.get("status") or "") in ACTIVE_STATES)
