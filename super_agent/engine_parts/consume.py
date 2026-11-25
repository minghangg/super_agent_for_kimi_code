"""From one walk to one consumed prefix: the queue, the cursor, and the refusal.

Why this module exists (ISS-130 D, split out under §3.1.1): ``engine.run_issue_phase``
calls the same six decisions once per issue, in an order that is easy to get
wrong and impossible to see when it is spread over one large class:

1. **what did this walk prove?** - a :class:`~super_agent.batch.CollectedBatch`
   built from the walk's own claims, never from the rows;
2. **may it be trusted?** - a gap is evidence that the incremental read is
   broken, so it rebuilds before anything is consumed;
3. **does the queue have room?** - a refusal is a refusal: the rows stay above
   the cursor and the next round reads them again;
4. **release the slot** - the batch is taken back off the queue once the issue
   is done. Without this the bound starves every issue past the cap (#130 QA
   P0-4);
5. **how far may the cursor go?** - the consumed prefix, never the highest row
   read, and never at all when a rebuild could not prove its rows (#130 QA P0-3);
6. **what is left over?** - rows above the cursor that were not consumed keep
   ``round_complete`` false.

The host is passed in rather than inherited: this module reads the round state
and logs events, and those are the only two couplings. Kept as functions so the
order above is the code's own order.
"""
from __future__ import annotations

from .. import batch as batch_mod
from .. import batch_queue as queue_mod


def now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class BatchCursor:
    """How far consumption got: one prefix per thread, never a max.

    The cursor may only name ids that every observed row at or below it was
    consumed. So it is ``min(pending) - 1`` while anything is pending (or the
    highest observed id once nothing is), never ``max(consumed)`` - the latter
    jumps over a row the queue refused and loses it forever.
    """

    def __init__(self, prefix: dict[str, int] | None = None) -> None:
        self.prefix: dict[str, int] = {str(k): int(v or 0)
                                       for k, v in (prefix or {}).items()}
        self._pending: dict[str, dict[int, int]] = {}
        self._observed: dict[str, int] = {}

    def pending(self, thread: str) -> dict[int, int]:
        return self._pending.setdefault(str(thread), {})

    def observe(self, thread: str, rows) -> int:
        """Record rows as seen-but-unconsumed; returns how many were new."""
        thread = str(thread)
        base = max(self.prefix.get(thread, 0), 0)
        pending = self.pending(thread)
        added = 0
        for row in rows or []:
            try:
                cid = int(row["id"])
            except (TypeError, ValueError, KeyError):
                continue
            if cid <= base or cid in pending:
                continue
            pending[cid] = 1
            added += 1
        if pending:
            self._observed[thread] = max(self._observed.get(thread, 0), max(pending))
        return added

    def consume(self, thread: str, ids) -> int:
        """Consume rows, then advance the prefix only over the consumed prefix."""
        thread = str(thread)
        pending = self.pending(thread)
        hit = 0
        for cid in ids or []:
            try:
                key = int(cid)
            except (TypeError, ValueError):
                continue
            if pending.pop(key, None) is not None:
                hit += 1
        if pending:
            candidate = min(pending) - 1
        else:
            candidate = self._observed.get(thread, 0)
        if candidate > self.prefix.get(thread, 0):
            self.prefix[thread] = candidate
        return hit

    def value(self, thread: str, fallback: int = 0) -> int:
        """Where the cursor stands: the prefix, raised to ``fallback`` if given."""
        return max(int(fallback or 0), int(self.prefix.get(str(thread), 0) or 0))

    def snapshot(self) -> dict:
        return {"prefix": dict(self.prefix),
                "pending": {k: sorted(v) for k, v in self._pending.items() if v}}


def note_collected(host, thread: str, batch, cursor_before: int, fresh,
                   summary: dict, index: dict | None = None,
                   cursor: dict | None = None) -> bool:
    """Record one walk in the round's collection state, with backpressure.

    Returns ``True`` when the batch was **accepted** (queued or already queued)
    and ``False`` when the bounded queue refused it. The distinction is the whole
    point of a refusal: the rows beyond the cap stay above the cursor, the round
    is marked incomplete, and the next round reads them again. Consuming anyway
    and moving the cursor would drop the refused rows permanently.

    ``complete``/``contiguous`` come from the walk itself; ``pages`` is the page
    list it actually fetched, which is what makes "the ids jump" and "a page is
    missing" different answers (see :mod:`super_agent.batch`).

    ``index``/``cursor`` are the round's copies, passed to a rebuild so its pause
    rows survive the round's own write-back (#130 QA P0-2).
    """
    state = getattr(host, "_round_state", None)
    if state is None:
        return True
    collected = batch_mod.CollectedBatch(
        thread, batch.rows, cursor_before=cursor_before,
        pages=list(getattr(batch, "pages", []) or []),
        complete=bool(getattr(batch, "pagination_complete", False)),
        contiguous=bool(getattr(batch, "contiguous", False)),
        reason=str(getattr(batch, "stop_reason", "") or ""))
    state.round_result.add(thread, collected)
    if collected.needs_rebuild():
        summary.setdefault("rebuild_needed", []).append(thread)
        host.store.log_event({
            "type": "batch.gap", "at": now(), "thread": str(thread),
            "missing_page": collected.missing_page(),
            "pagination_complete": collected.pagination_complete,
            "reason": collected.reason})
        host.rebuild_issue(str(thread), "batch.gap", collected=collected,
                           index=index, cursor=cursor)
    try:
        state.queue.put(collected)
    except queue_mod.Backpressure as exc:
        summary.setdefault("backpressure", []).append(
            {"thread": exc.thread, "rows": exc.refused, "reason": exc.reason})
        host.store.log_event({
            "type": "batch.backpressure", "at": now(), "thread": exc.thread,
            "rows": exc.refused, "reason": exc.reason, "round_seq": state.seq})
        return False
    state.cursor.observe(thread, fresh)
    return True


def note_consumed(host, thread: str, consumed) -> None:
    """Mark rows consumed on the batch **and** let the cursor prefix follow.

    Both are needed and they answer different questions: the batch's own
    ``consumed_ids`` is what makes ``consumption_complete`` true, the cursor
    prefix is what the next round reads from. Marking only one of them is how a
    round reports itself complete while the cursor stays behind (or the reverse,
    which loses rows).
    """
    state = getattr(host, "_round_state", None)
    if state is None:
        return
    ids = [int(c["id"]) for c in consumed]
    batch = state.round_result.batches.get(str(thread))
    if batch is not None:
        batch.consume(ids)
    state.cursor.consume(thread, ids)


def advance_cursor(host, key: str, cursor: dict, cur: int, fresh) -> None:
    """Move this thread's cursor to the prefix it may honestly claim.

    Two refusals and one rule:

    * a rebuild that could not prove its rows blocked this thread for the round,
      so the position stays byte for byte (#130 QA P0-3);
    * the claimed prefix is the **consumed** one, never ``max(fresh)`` - the
      highest row read is not the highest row acted on, and jumping to it buries
      whatever the row cap or a refusal left behind;
    * nothing consumed leaves the previous value alone.
    """
    state = getattr(host, "_round_state", None)
    if state is not None and not state.cursor_allowed(key):
        cursor.setdefault("issues", {}).setdefault(key, cur)
        return
    ids = [int(c["id"]) for c in fresh]
    note_consumed(host, key, fresh)
    if state is not None:
        prefix = state.cursor.value(key, fallback=cur)
    else:
        prefix = max(int(cur or 0), max(ids, default=0))
    cursor.setdefault("issues", {})[key] = max(int(cur or 0), int(prefix or 0))


def drain_queue(host, key: str, summary: dict) -> None:
    """Take this issue's batch off the queue, releasing the slot.

    Consumption is already recorded on ``CollectedBatch``/``BatchCursor`` by the
    triage loop; what this adds is the **release**: the slot is what the next
    issue needs. Taking the batch before triage would lose it on a refused read,
    which is why the take happens here, after the decision.

    A batch that is still carrying rows stays queued: its ``remaining`` is what
    makes ``round_complete`` false, and dropping it here would let the round call
    itself complete with rows unconsumed.
    """
    state = getattr(host, "_round_state", None)
    if state is None:
        return
    batch = state.queue.get(key)
    if batch is None:
        return
    if not batch.consumption_complete:
        # Put it back: the rows are real work the next round must see, and a
        # queue that quietly forgot them would report a complete round.
        state.queue.reoffer(batch)
        summary.setdefault("carry_back", []).append(key)
