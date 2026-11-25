"""The two verdict ledgers: one shape, two books, never one.

Why this module exists (ISS-130 §3.1.1 cleanup): the PM and AT verdict ledgers
are written the same way - newest comment wins, one record per thread, and the
role's own stall counter is cleared - while staying **separate** tables. That
separateness is the whole point: an AT answer that reset the PM watchdog would
hide a PM that never spoke, and the two silences are different facts. Sharing one
writer is what makes "separate tables, same shape" visible in one place instead
of two copies drifting apart.
"""
from __future__ import annotations


def record(index: dict, thread: str, verdict: dict, cid, *, now: str,
           table: str = "at_verdicts", stall: str = "at_stall") -> dict:
    """Commit one verdict; returns the record written.

    ``stall`` may be empty for a role with no watchdog of its own. The counter is
    popped rather than reset, because a verdict ends the hold the counter was
    about - the next hold starts from zero.
    """
    ledger = index.setdefault(table, {})
    if not isinstance(ledger, dict):
        ledger = index[table] = {}
    record_ = {"thread": thread, "comment_id": int(cid),
               "state": str(verdict.get("state") or ""),
               "matched": str(verdict.get("matched") or ""),
               "issue": verdict.get("issue"), "at": now}
    for extra in ("requirement_id", "revision", "requirement_hash"):
        if extra in verdict:
            record_[extra] = verdict.get(extra)
    ledger[thread] = record_
    if stall and isinstance(index.get(stall), dict):
        index[stall].pop(thread, None)
    return record_


def frame_record(existing, *, thread: str, cid, issue, kind: str,
                 exception: bool, question_url: str, text: str,
                 action_id: str, now: str, delivery=None) -> dict:
    """The AT frame record, written **before** the dispatch.

    Snapshot first, dispatch second: a crash between them loses one send and
    never the decision. The record is what a retry reads, so it must carry
    everything the frame needs (text included) without re-deriving it.
    """
    record = dict(existing) if isinstance(existing, dict) else {
        "thread": thread, "comment_id": cid, "at": now}
    record.update({"exception": bool(exception), "issue": int(issue),
                   "kind": kind, "question_url": question_url,
                   "text": text, "action_id": action_id})
    if delivery is not None or "delivery" in record:
        record["delivery"] = delivery
    return record


def supervisor_record(existing, *, thread: str, cid, review: dict,
                      action_id: str, now: str) -> dict:
    """The supervisor notice record, written **before** the dispatch.

    A crash between the write and the send loses one send, never the decision,
    and the retry pass reads ``sup_eligible`` back from this record. ``fix_needed``
    is stored beside the verdict because the retry sees a stub review and must not
    re-derive the decision from it.
    """
    record = dict(existing) if isinstance(existing, dict) else {
        "thread": thread, "comment_id": cid, "at": now,
        "verdict": str(review.get("verdict") or ""),
        "fix_needed": review.get("fix_needed")}
    record["sup_eligible"] = True
    record["action_id"] = action_id
    return record


def route_action(thread: str, issue: int, cid, payload: dict, event: str, *,
                 target_role: str, origin_role: str,
                 assignment_id: str) -> dict:
    """A dispatch record whose id is the route key, not a hash.

    ``_dispatch`` derives the action id from ``assignment + verb + input_ref``;
    naming the route as the "assignment" keeps the route's attempt counter
    findable from ``index["routes"]``.
    """
    return {"kind": "bus_inject", "event": event, "issue": issue,
            "thread": thread, "input_ref": str(cid),
            "assignment_id": assignment_id,
            "origin_role": origin_role, "target_role": target_role,
            "payload": payload}
