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


def eligible(aid: str, rec: dict, *, notify_attempted, archive_attempted
             ) -> tuple[str, str]:
    """``(decision, detail)`` for one assignment before any side effect.

    ``decision`` is one of:

    * ``"skip"`` - nothing to do here (already archived, wrong state, no
      delivery, the notification never went out, or this archive already ran);
    * ``"unnotified"`` - proceed, but the supervisor was never told (recorded in
      the tombstone as ``notified: false``);
    * ``"go"`` - proceed.

    ``notify_attempted``/``archive_attempted`` are the store's try counters,
    read through ``action_id(aid, verb, delivery)`` by the caller: the action id
    has a shape of its own, and rebuilding it here would be a second copy of
    that shape. An *attempt* is not a delivery: ``uncertain`` means the bus call
    raised, so the archive still proceeds (a worker that delivered is done)
    while the tombstone tells the truth about the notification.

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
    if notify_attempted(aid, delivery) < 1:
        return "skip", "review notification not sent"
    if archive_attempted(aid, delivery) > 0:
        return "skip", "archive already attempted"
    if not str(rec.get("notified_at") or ""):
        return "unnotified", "notification unconfirmed"
    return "go", ""


def record_archived(rec: dict, *, delivery, notified: bool, now: str) -> dict:
    """The tombstone written after a confirmed archive."""
    rec["status"] = "waiting_release"
    rec["archived_at"] = now
    rec["tombstone"] = {"archived": True, "read_back": True,
                        "notified": bool(notified), "at": now,
                        "cid": delivery, "attempt": 1}
    return rec
