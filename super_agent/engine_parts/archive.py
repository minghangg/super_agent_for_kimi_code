"""Is this sid archived? Two sources, one answer, cached per round.

Why this module exists (ISS-130 §3.1.1 cleanup): the answer decides whether a
frame may be sent at all, so getting it wrong sends into a session that no
longer exists - or refuses one that is alive. The logic is one responsibility
(the archive snapshot, the per-sid server read-back, and the "already logged"
memory that keeps one refusal from writing a line every round) and it works on
injected readers, so it lives here instead of inside ``engine.py``.

Two sources, in this order:

1. the lifecycle stream (``rec["archived"]``) - cheap, but incomplete, because an
   archive made through the API writes no ``session.archived`` row;
2. the server's ``session_info(sid)["archived"]`` - asked **once per sid per
   round**, and only for a sid the stream still calls live.

Fail **closed**: if the session service cannot be asked, the safe answer is
"assume closed, refuse the send". The reverse choice keeps pushing events into a
session that no longer exists.
"""
from __future__ import annotations

import json


def refusal_key(sid: str, action: dict) -> tuple:
    """Identity of one archive refusal: sid, action kind and assignment.

    Two different actions aimed at the same archived session are two facts, so
    they are two rows; the same action refused again is one fact, so it is not a
    second row.
    """
    return (str(sid), str((action or {}).get("kind") or ""),
            str((action or {}).get("assignment_id") or ""))


def load_noted(events_path, key_of=refusal_key) -> set:
    """Every archive refusal the current ``events.jsonl`` already records.

    Unreadable or absent log means "nothing written yet", which costs one
    duplicate event and never a delivered frame - the fail-open side here is the
    log, not the dispatch.
    """
    found: set = set()
    try:
        lines = events_path.read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        return found
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("type") != "dispatch.archived_sid":
            continue
        found.add(key_of(str(row.get("sid") or ""),
                         {"kind": row.get("action"),
                          "assignment_id": row.get("assignment")}))
    return found


def from_stream(events, missing: bool) -> tuple[dict, bool]:
    """``(sid -> archived, unreadable)`` from the replayed lifecycle stream.

    "Unavailable" is *no segment exists* or an empty read, decided by the caller
    with :func:`session_events.read_stream` - not ``path.is_file()``. The writer
    rotates the current file to ``.1``, and an ``is_file()`` test read that
    instant as an unreadable stream: every sid then became "unknown" and every
    binding fell to the server check (ISS-79).
    """
    if missing or not events:
        return {}, True
    try:
        from .. import session_events
        state = session_events.replay(events)
    except Exception:  # noqa: BLE001  unreadable means "assume archived"
        return {}, True
    sessions = state.get("sessions") or {}
    return {sid: bool(rec.get("archived")) for sid, rec in sessions.items()}, False


def delivered(rec: dict) -> bool:
    """Did the engine's delivery pass handle this assignment's delivery?

    Two witnesses, and either one is enough:

    * ``waiting_review_logged`` - the marker ``_record_waiting_review`` writes
      for every delivery matched since #143;
    * ``notified_at`` - the field the *pre-#143* code wrote once the bus
      accepted the notification. The notification is gone, so nothing writes it
      now, but every record stored before #143 still carries it.

    The second is not a courtesy. Measured on the live index (2026-11-28): of
    200 assignments, **0** carry the marker and **92** carry ``notified_at`` -
    the entire stored population predates the marker. A gate that accepted only
    the marker would have skipped every one of them forever, and a record that
    never archives never reaches ``waiting_release``, which is the one state the
    PM watchdog watches.

    Reading "was the notification attempted" from the store's try counter is
    not an option either: with no dispatch there is no attempt, so the counter
    is 0 for every new delivery. That is why this asks about the delivery, not
    about a notification that no longer exists.
    """
    return bool(str(rec.get("waiting_review_logged") or "")
                or str(rec.get("notified_at") or ""))


def eligible(aid: str, rec: dict, *, archive_attempted) -> tuple[str, str]:
    """``(decision, detail)`` for one assignment before any side effect.

    ``decision`` is one of:

    * ``"skip"`` - nothing to do here (already archived, wrong state, no
      delivery, the delivery pass never handled it, or this archive already
      ran);
    * ``"go"`` - proceed.

    **The precondition is "the delivery pass handled this delivery"**, see
    :func:`delivered`. It exists because archiving a row that no delivery pass
    ever produced is how a ``blocked`` state appears for a thread the PM
    watchdog then stops watching - ``blocked`` is not a held state.

    ``archive_attempted`` is the store's try counter, read through
    ``action_id(aid, "archive", delivery)`` by the caller: the action id has a
    shape of its own, and rebuilding it here would be a second copy.

    **The order is the contract.** The idempotency checks come before the QA gate
    because ``begin_action`` is the first side effect; a gate placed after it
    would have written state it then refuses.
    """
    status = str(rec.get("status") or "")
    if status == "waiting_release" and (rec.get("tombstone") or {}).get("archived"):
        return "skip", "already archived"
    if status not in ("waiting_review", "waiting_release"):
        return "skip", f"status {status or 'none'}"
    delivery = rec.get("delivery_comment_id")
    if delivery is None:
        return "skip", "no delivery"
    if not delivered(rec):
        return "skip", "delivery pass never handled this row"
    if archive_attempted(aid, delivery) > 0:
        return "skip", "archive already attempted"
    return "go", ""


def record_archived(rec: dict, *, delivery, now: str) -> dict:
    """The tombstone written after a confirmed archive."""
    rec["status"] = "waiting_release"
    rec["archived_at"] = now
    rec["tombstone"] = {"archived": True, "read_back": True,
                        "notified": False, "at": now,
                        "cid": delivery, "attempt": 1}
    return rec


QA_REFUSED_ROW = "assignment.qa_gate_refused"


QA_REFUSED_REALERT_ROUNDS = 30


def qa_refusal_row(rec: dict, reason: str, *, issue_is_closed,
                   aid: str, thread: str, delivery, now: str):
    """The ``assignment.qa_gate_refused`` row to write, or ``None``.

    A delivery parked in ``waiting_review`` without a verdict is *not a fault* -
    it simply has not been reviewed yet - and the archive pass asks the gate
    again every round. Writing one row per round per assignment turned that
    answer into 388 identical lines per stuck thread (#142): the log grew and
    nobody learned anything.

    The rule is the one the refusal ledger already uses for routes: the **first**
    sighting is news, then the same reason stays quiet until it has run for
    ``QA_REFUSED_REALERT_ROUNDS`` more rounds, so a thread stuck for a day is
    still visible at a glance without becoming the log. A *different* reason
    starts over - "no qa pass recorded" turning into "qa rejected" is news.

    ``issue_is_closed`` is a callable because reading the Issue costs a probe:
    a round whose answer is already quiet must not pay for it.
    """
    text = str(reason or "")
    streak = rec.get("qa_refused_streak")
    if not isinstance(streak, dict) or streak.get("reason") != text:
        streak = rec["qa_refused_streak"] = {"reason": text, "rounds": 0}
    streak["rounds"] = int(streak.get("rounds") or 0) + 1
    if streak["rounds"] > 1 and streak["rounds"] % QA_REFUSED_REALERT_ROUNDS:
        return None
    if issue_is_closed():
        return None
    return {"type": QA_REFUSED_ROW, "at": now, "assignment": aid,
            "thread": thread, "delivery_comment_id": delivery,
            "reason": reason, "rounds": streak["rounds"],
            "level": "info", "consumers": ["doctor"]}


def note_qa_refusal(engine, index: dict, aid: str, rec: dict,
                    reason: str, now: str) -> None:
    """Write the QA-gate refusal row, when this answer is news.

    Takes the engine for the two inputs ``rec`` cannot supply: the Issue state,
    read lazily, and the event sink. Keeping the whole decision here means the
    archive pass reads as one line per assignment.
    """
    issue = rec.get("issue")
    row = qa_refusal_row(
        rec, reason,
        issue_is_closed=lambda: issue is not None
        and engine._issue_state(issue) == "closed",
        aid=aid, thread=str(rec.get("thread") or ""),
        delivery=rec.get("delivery_comment_id"), now=now)
    if row is not None:
        engine.store.log_event(row)
