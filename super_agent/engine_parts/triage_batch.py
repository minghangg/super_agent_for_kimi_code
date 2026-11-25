"""The triage loop: one comment at a time, in id order, once per round.

Why this module exists (§3.1.1 cleanup of #130): the loop is the one place a
round decides what a comment *means* and who is told. It was a 90-line body
inside ``Engine._read_and_consume``, where the routing branches (delivery,
review, PM verdict, AT verdict, needs-person) were harder to see than the read
that produced the batch.

The rules it keeps, all of which were fought for elsewhere:

* the event must be allow-listed, so a read-only checkout cannot act;
* an id is triaged once per round (``seen_this_round``) - the persisted cursor
  is what stops it in later rounds;
* ``[PM:TASK]`` never reaches the verdict router, so an escalation is answered
  as an unmarked note (fail-closed, one exception frame);
* ``_pm_pending`` is the *whole batch*, built before the loop, not a running
  guess about what the loop has seen so far.

The host supplies the routers, the allowlist and the event log.
"""
from __future__ import annotations

from .. import markers


def now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def triage_batch(host, index: dict, issue: int, key: str, rec: dict, fresh, *,
                 accepted: bool, summary: dict) -> int:
    """Triage ``fresh`` in id order; returns how many comments were consumed.

    ``accepted=False`` means the bounded queue refused this batch, so its rows
    were **not** collected and may not be consumed: consuming them here and
    advancing the cursor afterwards is exactly the loss the bound exists to
    prevent.
    """
    # The window the double-ring guard reads: a PM verdict anywhere in this
    # issue's new comments counts, even when it comes after the QA pass in id
    # order. Built from the same list the loop walks, so the window is exactly
    # the batch - never a guess about future comments.
    host._pm_pending = set()
    if key and any(markers.parse_pm_verdict(str(c.get("body") or ""))
                   for c in fresh):
        host._pm_pending.add(key)
    consumed = 0
    seen_this_round: set[int] = set()
    for comment in (fresh if accepted else []):
        if "issue_comment.created" not in host.allowlist:
            continue
        cid = int(comment["id"])
        if cid in seen_this_round:          # in-run dedupe only
            continue
        seen_this_round.add(cid)
        consumed += 1
        _triage_one(host, index, issue, key, rec, comment, cid, summary)
    # The window closes here: past this point only the committed ledger speaks,
    # so the stall watchdog cannot be silenced by a batch that was already read
    # and answered.
    host._pm_pending = set()
    return consumed


def _triage_one(host, index: dict, issue: int, key: str, rec: dict,
                comment: dict, cid: int, summary: dict) -> None:
    """Route one allow-listed comment by its marker."""
    state, reason = host._triage(comment)
    delivery = markers.parse_delivery(str(comment.get("body") or ""))
    review = markers.parse_review(str(comment.get("body") or ""))
    pm_verdict = markers.parse_pm_verdict(str(comment.get("body") or ""))
    at_verdict = markers.parse_at_verdict(str(comment.get("body") or ""))
    summary["processed"] += 1
    rec["comments_seen"] = int(rec.get("comments_seen") or 0) + 1
    rec["last_comment_id"] = cid
    rec["last_event_ts"] = now()
    host.store.log_event({"type": "comment.processed", "at": now(),
                          "issue": issue, "comment_id": cid, "state": state,
                          "reason": reason})
    # The envelope is built here, on the production path, from the whitelist
    # result - never from the body (#92 §A.4／§D.3). A refusal or an unknown
    # marker logs its own event; either way this runs once per triaged comment.
    host._record_envelope(key, comment, state)
    if state == "delivery":
        state, reason = host._on_delivery(index, key, issue, comment,
                                          delivery, summary)
        summary["deliveries"] += 1
        if state != "needs_person":
            if rec.get("state") == "idle":
                rec["state"] = "tracked"
            return
    elif state == "review":
        state, reason = host._on_review(index, key, issue, comment, review,
                                        summary)
        summary["reviews"] = int(summary.get("reviews") or 0) + 1
    elif state == "pm_verdict":
        # A ``[PM:TASK]`` escalation never reaches ``parse_pm_verdict`` - the
        # prefix is deliberate - so the router is handed the empty verdict, the
        # same shape an unmarked ``[PM]`` note produces: no state, fail-closed,
        # one exception frame (#ISS-85 P1-2).
        verdict = pm_verdict or {"state": "", "matched": "", "issue": None,
                                 "candidate": False}
        state, reason = host._on_pm_verdict(index, key, issue, comment, verdict)
        summary["pm_verdicts"] = int(summary.get("pm_verdicts") or 0) + 1
    elif state == "at_verdict":
        state, reason = host._on_at_verdict(index, key, issue, comment,
                                            at_verdict)
        summary["at_verdicts"] = int(summary.get("at_verdicts") or 0) + 1
    if state == "needs_person":
        rec["state"] = "needs_person"
        host._file_needs_person(index, key, issue, cid, reason, rec, summary)
        return
    if rec.get("state") == "idle":
        rec["state"] = "tracked"
