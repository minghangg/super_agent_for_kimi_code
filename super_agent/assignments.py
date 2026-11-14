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
    17  waiting_release                   [QA] pass                   unchanged (verdict
                                                                      recorded; E58)
    18  waiting_release                   [QA] reject / no verdict     unchanged (refused)


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

    Two states are handled as **exits**, not as fresh verdicts, because work has
    already stopped there:

    * ``waiting_person`` is where an unreadable conclusion parks a delivery.
      It is re-enterable, and only by a **newer, readable** verdict: the
      watermark ``last_review_comment_id`` records which comment was already
      answered, so a replay cannot promote the thread and an older comment
      cannot arrive late to decide it. An unreadable comment here changes
      nothing at all - the state exists precisely because wording was not
      enough, and "it is in the candidate set now" is not an answer.
    * ``waiting_release`` takes only a ``pass``: the archive pass may run before
      the verdict exists (E58), so the delivery is already on record and this
      function is the only writer of ``qa_verdict``. A reject there would
      contradict a hold that has a pass on record, and re-deciding that is what
      :func:`reopen` exists for - it demands a newer review, a newer delivery
      and one live session, none of which a bare comment supplies.

    ``close_reconcile`` and ``completed`` take nothing.

    Every refusal above returns ``refused: True``. The caller turns that into an
    event: a verdict that lands and is not applied is exactly the shape that
    used to be silent, and silence here is indistinguishable from a lost
    comment.
    """
    status = str(assignment.get("status") or "")
    if status in TERMINAL_STATES or status == "close_reconcile":
        return {"changed": False, "refused": True,
                "reason": "review after the hold state"}
    verdict = str(review.get("verdict") or "")
    edited = _is_edited(assignment, review_cid, verdict)
    # A comment that has already been read and has **not** changed is a replay,
    # whatever the current state is. It is not news, so it must not be
    # reported - the short-circuit below is about "we have seen this exact
    # line before", which is a different question from "where does this id sit".
    #
    # This is what keeps a re-read of already-answered comments from growing a
    # refusal row every round: with the state check below they used to re-enter
    # as "a hold took nothing from me".
    if _reading_of(assignment, review_cid) is not None and not edited:
        return {"changed": False, "reason": "same review already recorded"}
    same_cid = _cid_eq(assignment.get("last_review_comment_id"), review_cid)
    if same_cid and not edited:
        return {"changed": False, "reason": "same review already recorded"}
    delivery = assignment.get("delivery_comment_id")
    if delivery_cid is not None:
        delivery = delivery_cid
    try:
        delivery = int(delivery)
    except (TypeError, ValueError):
        delivery = None
    if status == "waiting_release":
        return _pass_after_hold(assignment, verdict, review_cid, verified,
                                delivery, edited=edited)
    if status == "waiting_person":
        return _verdict_after_person(assignment, verdict, review_cid, verified,
                                     delivery, edited=edited)
    assignment["last_review_comment_id"] = review_cid
    assignment["review_verified"] = bool(verified)
    assignment["updated_at"] = now()
    _remember_reading(assignment, review_cid, verdict)
    if not verdict:
        assignment["status"] = "waiting_person"
        return {"changed": True, "status": "waiting_person",
                "reason": "no readable verdict"}
    if verdict == "pass":
        # A pass re-read after a newer delivery is stale: the worker delivered
        # again, so the review no longer describes the code on the table.
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


def _cid_eq(left, right) -> bool:
    """``True`` when both sides are the same comment id (int-normalised).

    Deliberately an equality helper, not a comparison: "is this the comment we
    last read" and "is this comment older than that one" are different
    questions, and the first one must not be answered by the second.
    """
    if left is None or right is None:
        return False
    try:
        return int(left) == int(right)
    except (TypeError, ValueError):
        return False


# Where the *reading* of each review comment is kept: ``{cid: verdict}``. It is
# the only way to tell an in-place edit from a replay, because the comment id
# does not move when a QA fixes the wording. Without it, "did this comment
# change" can only be guessed from the assignment's current state, which is a
# different question - and the wrong one: #65's QA fixed a comment that was
# **older** than the watermark, so any "newer than the watermark" rule refuses
# exactly the case the issue is about.
READINGS = "review_readings"


def _readings(assignment: dict) -> dict:
    table = assignment.get(READINGS)
    if not isinstance(table, dict):
        table = assignment[READINGS] = {}
    return table


def _reading_of(assignment: dict, review_cid) -> str | None:
    """The verdict this comment read last time, or ``None`` if never read."""
    table = assignment.get(READINGS)
    if not isinstance(table, dict):
        return None
    try:
        key = str(int(review_cid))
    except (TypeError, ValueError):
        return None
    value = table.get(key)
    return None if value is None else str(value)


def _remember_reading(assignment: dict, review_cid, verdict: str) -> None:
    try:
        key = str(int(review_cid))
    except (TypeError, ValueError):
        return
    _readings(assignment)[key] = str(verdict or "")


def _is_edited(assignment: dict, review_cid, verdict: str) -> bool:
    """Is this the same comment, read differently than last time?

    An edit is **the same comment id with a different reading**. Anything else
    - a new id, an unreadable line both times, a byte-identical replay - is not
    an edit, so the replay guard still short-circuits it.
    """
    previous = _reading_of(assignment, review_cid)
    return previous is not None and previous != str(verdict or "")


def _verdict_after_person(assignment: dict, verdict: str, review_cid,
                          verified: bool, delivery: int | None,
                          *, edited: bool = False) -> dict:
    """Apply a verdict that arrives while the delivery waits for a person.

    Only a **newer, readable** conclusion may leave ``waiting_person`` - and,
    since the same comment may be **edited in place**, a readable conclusion
    that differs from the one already recorded at that same id counts as newer
    (``edited``, decided by the caller; the id itself cannot express an edit):

    * unreadable - stay put. The state is the answer to "the wording was not
      enough", so more wording of the same kind cannot answer it. The
      watermark does move: the comment *was* read and answered by the state it
      left behind, and moving it keeps a replay from looking newer later.
    * readable but not newer than ``last_review_comment_id`` - stay put. The
      watermark is the record of what this thread has already decided; a
      comment below it is history, and history must not reopen a stop. An
      in-place edit is the one exception: the id ties with the watermark, but
      the reading changed, which is what a person was waiting for.
    * readable and newer (or a differing in-place edit) - the normal
      transitions, unchanged: ``pass`` to ``waiting_release`` (unless a newer
      delivery already exists), ``reject`` to ``working``.

    Comparing comment ids rather than timestamps is deliberate: ids are
    monotonic per repository and already used as the delivery ordering
    everywhere else in this module (:func:`on_delivery`, the stale check), so
    one clock cannot disagree with another.

    Who may answer is decided by **identity, not by ordering**: this comment is
    the one we are answering if it is the comment on record
    (``review_comment_id`` or ``last_review_comment_id``) or if its reading
    changed (``edited``). A comment *older* than the watermark is refused only
    when it is neither of those - #65's fix was an edit of a comment that sat
    **below** the watermark, and a "must be newer" rule refuses exactly that.
    """
    last = assignment.get("last_review_comment_id")
    try:
        last = int(last) if last is not None else None
    except (TypeError, ValueError):
        last = None
    try:
        cid = int(review_cid)
    except (TypeError, ValueError):
        cid = None
    if not verdict:
        assignment["last_review_comment_id"] = (
            cid if cid is not None and (last is None or cid > last) else last)
        assignment["review_verified"] = bool(verified)
        assignment["updated_at"] = now()
        _remember_reading(assignment, review_cid, "")
        return {"changed": False, "refused": True,
                "reason": "no readable verdict after waiting_person"}
    on_record = (_cid_eq(cid, last)
                 or _cid_eq(cid, assignment.get("review_comment_id")))
    if cid is None or (not on_record and not edited
                       and last is not None and cid < last):
        return {"changed": False, "refused": True,
                "reason": "review is not newer than the recorded one"}
    if verdict == "pass":
        if delivery is not None and delivery > cid:
            return {"changed": False, "refused": True, "reason": "review.stale"}
        assignment["last_review_comment_id"] = cid
        assignment["review_verified"] = bool(verified)
        assignment["status"] = "waiting_release"
        assignment["qa_verdict"] = "pass"
        assignment["review_comment_id"] = cid
        assignment["updated_at"] = now()
        _remember_reading(assignment, review_cid, verdict)
        return {"changed": True, "status": "waiting_release", "reason": "qa pass"}
    assignment["last_review_comment_id"] = cid
    assignment["review_verified"] = bool(verified)
    assignment["status"] = "working"
    assignment["qa_verdict"] = "reject"
    assignment["review_comment_id"] = cid
    assignment["updated_at"] = now()
    _remember_reading(assignment, review_cid, verdict)
    return {"changed": True, "status": "working", "reason": "qa reject"}


def _pass_after_hold(assignment: dict, verdict: str, review_cid, verified: bool,
                     delivery: int | None, *, edited: bool = False) -> dict:
    """Apply a verdict that arrives when the assignment is already archived.

    The status does not move - the hold is what the archive established, and
    nothing a comment says can undo an archive. What is written is the QA's
    answer: the comment is on record, the pass is on record, and a second read
    of the same comment changes nothing.

    An **edit** counts here too, and that is not a detail: without it a QA that
    corrected the line of the comment already on record got ``sup=1`` and
    ``release_frames`` written while ``qa_gate`` still said "no qa pass
    recorded" - the original E1 shape, unchanged, one state over.

    ``refused: True`` marks the answers that are **not** applied here (reject,
    unreadable, stale). The caller writes an event for them; a hold state that
    swallows a verdict is how the verdict gets lost.
    """
    _remember_reading(assignment, review_cid, verdict)
    if verdict != "pass":
        return {"changed": False, "refused": True,
                "reason": "review after the hold state"}
    if delivery is not None and delivery > int(review_cid):
        return {"changed": False, "refused": True, "reason": "review.stale"}
    if (not edited and str(assignment.get("qa_verdict") or "") == "pass"
            and assignment.get("review_comment_id") is not None):
        # An **edit** is the exception: the QA corrected this comment, so its
        # new reading is the answer - "a pass is already recorded" would keep
        # the refusal row alive for a comment that has since said the opposite.
        assignment["last_review_comment_id"] = review_cid
        return {"changed": False, "reason": "pass already recorded"}
    assignment["last_review_comment_id"] = review_cid
    assignment["review_verified"] = bool(verified)
    assignment["qa_verdict"] = "pass"
    assignment["review_comment_id"] = review_cid
    assignment["updated_at"] = now()
    return {"changed": True, "status": "waiting_release", "reason": "qa pass"}


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
