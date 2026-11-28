"""Retry decisions: which stored attempt may be re-sent, and which must not.

Why this module exists (ISS-130 §3.1.1 cleanup): the retry passes ask the same
three questions of three different tables - has it already landed, was it
refused as a **decision**, and is there a decision snapshot to trust. Each wrong
answer has its own cost:

* a record that already landed must not be re-sent, or one verdict rings twice;
* a **refused** frame is a decision, not an unfinished send - the allowlist said
  no before the action was begun, so no attempt counter exists. Retrying it
  regenerated 56 frames and 56 decision lines a minute, forever (#75);
* a record with no decision snapshot must not be guessed at, in either
  direction: guessing "send" delivers a release nobody decided on, guessing
  "skip" drops one somebody did.

Nothing here dispatches or writes: the caller owns both, and takes the decision
as an argument.
"""
from __future__ import annotations


def pm_frame_candidate(key: str, record: dict, *, attempted) -> dict | None:
    """``{"thread", "issue", "cid", "base", "exception"}`` or ``None``.

    ``None`` means "nothing to retry here", and every reason is a different
    fact: a frame that already landed, a record with no snapshot (never guessed
    at), and a **refused** frame. The last one matters most: a refusal is a
    *decision*, not an unfinished send - the allowlist said no before the action
    was even begun, so no attempt counter exists and the old skip rule retried it
    every round: 56 frames regenerating 56 decisions a minute, forever (#75).
    Re-asking an allowlist produces the same no; a different answer needs a
    configuration change and a new verdict, not this loop.
    """
    if not isinstance(record, dict) or record.get("ok"):
        return None
    if "exception" not in record:
        return None
    if record.get("refused"):
        return None
    base = str(record.get("action_id") or "")
    cid = record.get("comment_id")
    if cid is None or (base and attempted(base) > 0):
        return None
    return {"thread": str(record.get("thread") or ""), "issue": record.get("issue"),
            "cid": cid, "base": base,
            "exception": bool(record.get("exception")),
            "reason": str(record.get("reason") or ""),
            "text": str(record.get("text") or ""),
            "delivery": _int_or_none(record.get("delivery"))}


def _int_or_none(value):
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def route_candidate(route) -> bool:
    """Is this route record a retry candidate at all?

    ``routes`` is a plain dict in the index, and the index is a file a person may
    edit, an older version may have written, or a crash may have truncated. The
    retry passes run outside any ``try``, so one malformed record would take down
    the retries, the archiving and both saves of the round. Skipping it keeps the
    round alive.
    """
    return (isinstance(route, dict) and not route.get("ok")
            and not route.get("permanent"))


def closed_route(route: dict, now: str) -> None:
    """Mark a route whose issue is closed as no longer a candidate.

    Closed is a business end state, not a transient refusal: without this mark
    the same dead route is re-probed and re-logged every round (#84: 229
    identical events for one issue). **A reopened issue needs a person to clear
    these three keys** - ``permanent`` keeps the route out of the loop before the
    issue state is ever asked again, so a reopen cannot be noticed from inside
    the retry pass.
    """
    route["skipped_closed_at"] = now
    route["permanent"] = True
    route["permanent_reason"] = "issue closed"
    route["permanent_at"] = now


def permanent_route(route: dict, reason: str, now: str) -> None:
    """Record a permanent refusal: retried is futile, so it stops being one."""
    route["permanent"] = True
    route["permanent_reason"] = str(reason or "")
    route["permanent_at"] = now


def supervisor_candidate(record, *, attempted) -> str:
    """``"retry"``, ``"undecidable"``, or ``"skip"`` for one supervisor record.

    ``"undecidable"`` is its own answer: a record that never stored
    ``sup_eligible`` is a record whose decision was never made, and retrying it
    would mean re-judging a verdict - or dropping it quietly, which leaves
    ``ok=False`` forever with nothing to look at. The caller writes the row.
    """
    if not isinstance(record, dict) or record.get("ok"):
        return "skip"
    # ``permanent`` is the same end state ``route_candidate`` honours: a closed
    # issue (or a creation-mode refusal) is not retried, and without this check
    # the caller re-probed the issue every round to reach the same answer
    # (#142: three records, 382 rows each).
    if record.get("permanent"):
        return "skip"
    if "sup_eligible" not in record:
        return "undecidable"
    if record.get("sup_eligible") is not True:
        return "skip"
    base = str(record.get("action_id") or "")
    if not base or attempted(base) > 0:
        return "skip"
    return "retry"


def malformed_sid(index, sid: str, *, full_sid, row: str, now: str,
                  tail) -> bool:
    """Should this round write the "sid is not a full id" warning? 

    A truncated id still resolves from the config, so it is *returned* and every
    frame built from it is refused - silently, because a refusal is a route event
    about the *frame*, not about the sid being unusable. This is the missing half:
    one ``warn`` naming the shape problem, consumed by the doctor.

    Three dedupe layers, each with its own reason:

    * the index remembers it wrote the row (survives a log rotation);
    * the event tail is checked too, because the index may be new while the log
      already carries the row from an earlier run;
    * nothing here decides *delivery* - the refusal path is unchanged.

    The sid **value** is never logged: its length says "cut short" without
    copying a possibly-still-valid prefix into the event stream.

    Returns ``True`` when the caller should write the row.
    """
    if not sid or full_sid(sid) or not isinstance(index, dict):
        return False
    meta = index.setdefault("meta", {})
    if not isinstance(meta, dict):
        meta = index["meta"] = {}
    warned = meta.setdefault("warned", {})
    if not isinstance(warned, dict):
        warned = meta["warned"] = {}
    if row in warned:
        return False
    warned[row] = now
    try:
        if any(r.get("type") == row for r in tail(limit=500)):
            return False
    except OSError:
        pass
    return True


def undeliverable_row(rows, *, comment_id, reason: str, assignment: str,
                      detail: str, cid_of, now: str) -> dict | None:
    """The ``needs_person`` row for an undeliverable notice, or ``None`` if it exists.

    Identity is ``(thread, comment, assignment, detail)``: the same comment can
    fail twice for different reasons (no sid, then a refused dispatch) and two
    different assignments can share a comment id (a ``None`` delivery id is not
    an identity). Keying on the comment alone folds those into one row, which is
    the loss this exists to prevent. The same failure across rounds still writes
    one row, because the key does not mention the round.
    """
    wanted = cid_of(comment_id)
    for entry in rows or ():
        if (cid_of(entry) == wanted
                and str(entry.get("reason") or "") == reason
                and str(entry.get("assignment") or "") == str(assignment or "")
                and str(entry.get("detail") or "") == str(detail or "")):
            return None
    return {"comment_id": comment_id, "reason": reason, "at": now,
            "assignment": str(assignment or ""), "detail": str(detail or "")}

def issue_of(index: dict, thread: str):
    """The issue number a thread belongs to, or ``None``.

    Every retry pass needs it and each one used to walk ``assignments`` with
    its own copy of the loop. The three copies drifted: one gained a
    closed-issue skip and the others did not, so a closed issue's frames kept
    regenerating refusals every round (#142).
    """
    for rec in (index.get("assignments") or {}).values():
        if str(rec.get("thread") or "") == thread:
            return rec.get("issue")
    return None

def closed_thread_event(log_event, etype: str, thread: str, issue, cid,
                        now: str) -> None:
    """One ``info`` row when a retry pass first sees its issue closed.

    The row is written on the pass that *marks* the record permanent, so it
    appears exactly once: after that the record is no longer a candidate and
    this function is not reached again. Both retry passes share it, which is
    what keeps the two event names from growing two different payload shapes.
    """
    log_event({"type": etype, "at": now, "thread": thread,
               "issue": int(issue), "comment_id": cid,
               "level": "info", "consumers": ["doctor"]})


def note_closed_route(route: dict, log_event, etype: str, thread: str,
                      issue, cid, now: str) -> None:
    """End a route on a closed issue, and say so exactly once.

    The mark and the row always travel together: a mark without the row hides
    the decision, and a row without the mark regenerates it every round. Both
    retry passes call this, so the two keep one shape (#142).
    """
    closed_route(route, now)
    closed_thread_event(log_event, etype, thread, issue, cid, now)
