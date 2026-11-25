"""The AT history gate: may the requirement projection be rebuilt this round?

Why this module exists (ISS-130 §3.1.1 cleanup): the projection is a **disposable
cache** rebuilt from the Issue, never from the cursor, because approval may
predate ``at_mode`` being enabled. That rebuild has one precondition and one
failure mode, and both are easy to get wrong when they sit inside a 200-line
method:

* the walk must have read the **whole** listing. A package on a page that was
  never fetched would vanish from the projection and read as "nobody published
  it" - the failure looks like an empty roadmap, not like a bug;
* when it did not, the projection is left **alone** and the issue pauses with a
  manual way back. A prefix written as if it were the listing is the defect.

One cause, one row: the page walk may already have paused this issue in the same
round, and a second ``needs_person`` row under ``cid=0`` would survive the lift
(``needs`` keys on ``(thread, cid)``), so the caller is told which of the two
shapes to take.
"""
from __future__ import annotations


def apply(index: dict, key: str, issue: int, *, history_done: bool,
          history_rows, fresh, rows_read: int, paused_now: bool,
          pause_history=None, apply_rows=None, save_projection=None,
          append=None, log=None) -> str:
    """Run the gate and return which branch was taken.

    ``"history_incomplete"`` - paused, projection untouched;
    ``"history_untouched"`` - the walk already paused it, projection untouched;
    ``"projection"`` - the projection was rebuilt.
    """
    if not history_done:
        if not paused_now:
            if pause_history is not None:
                pause_history(len(history_rows) or len(fresh))
            return "history_incomplete"
        if log is not None:
            log({"type": "requirement.history_incomplete",
                 "thread": str(key), "issue": int(issue),
                 "rows_read": int(rows_read),
                 "reason": "the same round already paused the page walk, "
                           "so the projection is left untouched"})
        return "history_untouched"
    # Runs even with zero rows: ``history_scope`` records that this thread's
    # history was read whole, and a thread whose first package is not published
    # yet has no other place to carry it.
    if apply_rows is not None:
        apply_rows(history_rows)
    if save_projection is not None:
        save_projection()
    return "projection"
