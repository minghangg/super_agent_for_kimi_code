"""A bounded collection queue: backpressure instead of a dropped comment.

Why this module exists (ISS-130 D): the interesting failure is not "the queue
filled up" - it is what happens **next**. A queue that discards when full loses
a comment, and the cursor it advances afterwards makes the loss permanent: the
row is below the cursor, so no later round ever reads it again. So the bound
here is a *refusal*, and a refusal has a defined meaning: the rows beyond it
were never consumed, the cursor stops at the consumed prefix, and the next
round reads the rest.

Two caps, because one is not enough:

* ``ScanPolicy.max_batches`` / ``max_batch_rows`` - the whole queue;
* ``ScanPolicy.max_issue_rows`` - one noisy issue must not crowd out the rest.

Dedupe is by comment id, per issue: two walks that overlap (a retried page, a
page boundary read twice) are one row. Merging is per issue, so a second batch
for the same thread joins the slot it already has instead of taking another
one - a second slot would double-count the cap and could still lose the rows.
"""
from __future__ import annotations

import queue

from .batch import CollectedBatch, ScanPolicy, merge_spans, spans_contiguous


class Backpressure(RuntimeError):
    """The queue is full. Rows stay **above the cursor** - never dropped."""

    def __init__(self, thread: str, accepted: int, refused: int, reason: str) -> None:
        super().__init__(f"{reason}: {thread} accepted {accepted}, refused {refused}")
        self.thread = thread
        self.accepted = accepted
        self.refused = refused
        self.reason = reason


class BoundedBatchQueue:
    """A bounded ``queue.Queue`` of per-issue batches, with backpressure.

    ``put`` never blocks and never drops: it takes what fits and raises
    :class:`Backpressure` for a batch it could not take whole. Partial
    acceptance is impossible by construction - a batch is one walk of one
    listing, and half a walk is the shape that makes a cursor skip rows.
    """

    def __init__(self, policy: ScanPolicy | None = None) -> None:
        self.policy = policy or ScanPolicy()
        self._order: queue.Queue = queue.Queue(maxsize=int(self.policy.max_batches))
        self._batches: dict[str, CollectedBatch] = {}
        self.refused: list[dict] = []

    # ------------------------------------------------------------- writing
    def put(self, batch: CollectedBatch) -> int:
        """Enqueue one batch; returns how many **new** rows it added.

        The bound counts the rows **above the cursor** - the work this round may
        consume - not the whole walk. An AT read fetches the entire listing for
        its projection, and counting that history against the queue would refuse
        every issue whose history is longer than the cap, which is not
        backpressure, it is a wall.

        Raises :class:`Backpressure` when the batch cannot be taken whole. The
        caller is expected to leave the rows above the cursor and record the
        refusal, which is what makes the next round pick them up again.
        """
        thread = batch.thread
        held = self._batches.get(thread)
        known = {int(row["id"]) for row in held.rows} if held else set()
        work = [row for row in batch.rows
                if int(row["id"]) > batch.cursor_before
                and int(row["id"]) not in known]
        if not work:
            return 0             # already queued: a retried page, not a failure
        held_rows = sum(1 for row in (held.rows if held else ())
                        if int(row["id"]) > (held.cursor_before
                                             if held else batch.cursor_before))
        if held_rows + len(work) > int(self.policy.max_issue_rows):
            raise self._refuse(batch, len(work), "issue row cap")
        if len(work) > int(self.policy.max_batch_rows):
            raise self._refuse(batch, len(work), "batch row cap")
        if held is None and self._order.full():
            raise self._refuse(batch, len(work), "queue full")
        if held is not None:
            self._batches[thread] = held.merge(batch)
        else:
            self._batches[thread] = batch
            self._order.put_nowait(batch)
        return len(work)

    def _refuse(self, batch: CollectedBatch, rows: int,
                reason: str) -> Backpressure:
        self.refused.append({"thread": batch.thread, "rows": rows,
                             "reason": reason, "at": len(self.refused)})
        return Backpressure(batch.thread, 0, rows, reason)

    # ------------------------------------------------------------- reading
    def get(self, thread: str | None = None) -> CollectedBatch | None:
        """Pull one batch (the oldest, or this issue's); ``None`` when empty."""
        if thread is None:
            try:
                head = self._order.get_nowait()
            except queue.Empty:
                return None
            return self._take(head)
        for _ in range(self._order.qsize()):
            try:
                head = self._order.get_nowait()
            except queue.Empty:
                return None
            if head.thread == thread:
                return self._take(head)
            self._order.put_nowait(head)      # rotate, do not lose
        return None

    def _take(self, head: CollectedBatch) -> CollectedBatch:
        current = self._batches.pop(head.thread, head)
        return current

    def drop(self, thread: str) -> None:
        """Forget one issue's queued rows without consuming them.

        Used only when the round abandons a thread (its read failed). The cursor
        is not moved for it, so the rows come back next round.
        """
        self._batches.pop(str(thread), None)

    def reoffer(self, batch: CollectedBatch) -> None:
        """Put a taken batch back, so its unconsumed rows stay visible.

        Used by the consumer when a batch it took still has rows above the
        cursor: returning it keeps ``remaining`` (and therefore
        ``round_complete``) honest, and the slot is reused rather than lost.
        Takes no queue slot - it is put back into the same per-issue map, and
        the order deque still holds nothing for it.
        """
        thread = batch.thread
        held = self._batches.get(thread)
        self._batches[thread] = held.merge(batch) if held else batch
        if not self._order.full():
            try:
                self._order.put_nowait(self._batches[thread])
            except queue.Full:      # raced with another put: the map is the truth
                pass

    def threads(self) -> list[str]:
        return [batch.thread for batch in list(self._order.queue)]

    def size(self) -> int:
        return self._order.qsize()

    def rows_held(self) -> int:
        return sum(len(batch.rows) for batch in self._batches.values())

    def stalled(self) -> bool:
        """Is collection blocked right now?"""
        return self._order.full() or any(
            len(batch.rows) >= int(self.policy.max_issue_rows)
            for batch in self._batches.values())

    def __len__(self) -> int:
        return self._order.qsize()


def contiguous_with(spans, *, expect_from: int | None = None) -> bool:
    """Do these page intervals form one unbroken run (from ``expect_from``)?

    The real question a consumer asks: *are the pages I read one run, with no
    page missing in between?* - never *are the comment ids consecutive*, which
    a deleted comment breaks while the read is perfect. Page intervals survive
    that: page numbers are dense by construction.

    ``expect_from`` anchors the run: given page 1 as the first page a full
    listing needs, ``contiguous_with([(1,3),(4,6)], expect_from=1)`` is True
    and ``expect_from=3`` is False (the walk never read page 3 downward).
    """
    merged = merge_spans(spans)
    if not merged or not spans_contiguous(merged):
        return False
    if expect_from is None:
        return True
    return merged[0][0] <= int(expect_from) <= merged[-1][1]
