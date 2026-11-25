"""The read half of one issue: may we read, what did we get, what is unread.

Why this module exists (ISS-130 §3.1.1 cleanup): the read decision is a chain of
questions that must be asked in order, and getting the order wrong is how rows
get skipped:

1. **may this issue be read at all?** A live read pause means an earlier walk
   could not prove its rows were gap-free, so reading again risks consuming a
   slice that skips the rows below it. The answer is "no read, no writing more
   rows" until a person lifts it;
2. **what is the page budget?** A lifted pause may carry the budget the operator
   chose, and that budget is what makes the lift a real "continue" rather than a
   pause on the very next round;
3. **one walk, two answers.** The rows the round may consume and the rows the AT
   projection is rebuilt from must be the **same** snapshot - two walks could
   straddle a comment arriving between them;
4. **did the walk prove it reached the cursor?** If not, the issue pauses instead
   of consuming a prefix, and the AT history gate does not add a second row for
   the same cause.

Nothing here dispatches, writes the index or reads the store: the caller passes
its readers and its side effects in, so the order above is visible in one place.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .. import github_source, markers
from . import claims as walk_claims
from . import pause as read_pause_mod


@dataclass
class ReadResult:
    """What one issue's read produced."""

    fresh: list = field(default_factory=list)
    history_rows: list = field(default_factory=list)
    history_done: bool = False
    rows_read: int = 0
    paused_now: bool = False
    skipped: bool = False
    cursor_before: int = 0
    batch: object = None


def read_issue(source, issue: int, key: str, rec: dict, cur: int, *,
               at_mode: bool, qa_recovery: bool, max_comments: int,
               max_pages: int, pause_read=None, log=None, summary=None) -> ReadResult:
    """Run the read chain for one issue and report what came out of it.

    ``pause_read(index, key, issue, cur, rows, budget, summary)`` and
    ``log(event)`` are the caller's side effects; ``rec`` is the issue's index
    entry, which this function may update (the spent ``read_budget``).
    """
    summary = summary if summary is not None else {}
    out = ReadResult(cursor_before=int(cur or 0))
    if read_pause_mod.is_paused(rec):
        # The hard cap was reached in an earlier round and nobody has lifted it
        # yet: do not read this issue at all. Writing ``needs_person`` and then
        # reading again would be the silent retry the issue forbids - the walk
        # cannot prove its rows are contiguous, so every extra round risks
        # consuming a slice that skips the rows below it.
        summary["read_paused"] = int(summary.get("read_paused") or 0) + 1
        out.skipped = True
        if log is not None:
            paused = rec.get("read_paused") or {}
            log({"type": "read.skipped", "at": _now(), "issue": int(issue),
                 "thread": str(key), "reason": str(paused.get("reason") or ""),
                 "since": str(paused.get("at") or ""),
                 "resume_with": f"python -m super_agent.cli resume-read --issue {issue}"})
        return out
    # A lifted pause may carry the page budget the operator chose when clearing
    # it (``resume-read --max-pages``). That budget is what makes the lift a real
    # "continue" instead of "pause again on the next round": without it a listing
    # larger than the configured window re-pauses immediately, and the command
    # would be lying about its own effect. It applies to this issue only and is
    # spent once.
    budget, pending_budget = read_pause_mod.read_budget(rec, max_pages)
    if pending_budget:
        rec["read_budget"] = {}
    batch = github_source.paged_batch(
        source, issue, cursor_before=cur, max_pages=budget,
        whole_listing=bool(qa_recovery or at_mode))
    out.batch = batch
    out.rows_read = len(batch.rows)
    if not walk_claims.walk_covers_cursor(batch, cur):
        if pause_read is not None:
            pause_read(out.rows_read, budget)
        out.paused_now = True
        return out
    out.fresh = [c for c in batch.rows
                 if int(c["id"]) > cur
                 or (qa_recovery and markers.parse_review(
                     str(c.get("body") or "")))][:max_comments]
    if at_mode:
        out.history_rows = walk_claims.walk_rows(batch)
        out.history_done = walk_claims.history_is_complete(batch)
    return out


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def qa_recovery(assignments, *, terminal, output_cids) -> bool:
    """Is this thread's only live work a QA pass with no review recorded?

    The answer decides whether the read must walk the **whole** listing: with no
    worker delivery on record the QA verdict may be anywhere in the history, so
    an incremental read would not see it.
    """
    worker_output = any(str(rec.get("role") or "") == "worker"
                        and output_cids(rec) for rec in assignments)
    return not worker_output and any(
        str(rec.get("role") or "") == "qa"
        and str(rec.get("status") or "") not in terminal
        and rec.get("review_comment_id") is None for rec in assignments)
