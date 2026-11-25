"""One bounded collection batch, and what a round may claim once it is consumed.

Why this module exists (ISS-130 D): a round used to read a listing, consume
whatever prefix it liked and call itself done. Three facts were missing, and
each one fails silently on its own:

* **coverage** - which pages the walk actually read, so "the ids are not
  consecutive" and "a page is missing" stop looking the same. Comment ids have
  holes in real listings (deleted comments), so id adjacency can never be the
  test; page intervals can be.
* **backpressure** - a bounded queue that refuses instead of dropping. A queue
  that silently discards rows loses comments, and the cursor it then advances
  makes the loss permanent.
* **roundness** - ``collection_complete`` (the walk proved it saw the listing)
  and ``consumption_complete`` (everything collected was consumed) are two
  answers. Only both together make a round complete.

Pure data and decisions, no I/O: :mod:`super_agent.batch_queue` owns the queue,
``engine_parts.round`` owns the orchestration.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PageRecord:
    """One fetched page: which page, how many rows, and the ids it carried."""

    page: int
    rows: int = 0
    first_id: int = 0
    last_id: int = 0
    status: str = "ok"

    @classmethod
    def from_walk(cls, raw: dict) -> "PageRecord":
        """``{"page": n, "rows": k, "status": "ok"}`` from the page walk."""
        try:
            return cls(page=int(raw.get("page") or 0),
                       rows=int(raw.get("rows") or 0),
                       status=str(raw.get("status") or "ok"))
        except (TypeError, ValueError):
            return cls(page=0, status="malformed")


def merge_spans(spans) -> list[tuple[int, int]]:
    """Merge inclusive intervals; overlapping or abutting ones become one.

    ``[(5, 7), (7, 3), (9, 9)]`` → ``[(3, 9)]``. Adjacency counts: page 4 and
    page 5 are one run of pages, because there is no page between them.
    """
    cleaned: list[tuple[int, int]] = []
    for low, high in spans:
        low, high = int(low), int(high)
        if low > high:
            low, high = high, low          # (7, 3) is the same run as (3, 7)
        cleaned.append((low, high))
    cleaned.sort()
    merged: list[tuple[int, int]] = []
    for low, high in cleaned:
        if merged and low <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], high))
        else:
            merged.append((low, high))
    return merged


def spans_contiguous(spans) -> bool:
    """Do these intervals form exactly one run?"""
    return len(merge_spans(spans)) <= 1


def first_hole(spans) -> int | None:
    """The lowest page number missing between two observed runs, or ``None``.

    A hole is only reported **between** pages the walk did return: the pages
    below a walk that stopped at the cursor were never claimed to exist, so
    their absence is not evidence of anything.
    """
    merged = merge_spans(spans)
    for (_low, high), (next_low, _n) in zip(merged, merged[1:]):
        if next_low > high + 1:
            return high + 1
    return None


@dataclass(frozen=True)
class ScanPolicy:
    """The budgets one round collects under.

    ``max_batches`` is the global queue cap; ``max_issue_rows`` is the per-issue
    cap. Both refuse rather than drop: a row that does not fit stays above the
    cursor, so the next round reads it again.
    """

    max_batches: int = 8
    max_batch_rows: int = 200
    max_issue_rows: int = 400
    max_pages: int = 10
    page_size: int = 100

    def __post_init__(self) -> None:
        for name in ("max_batches", "max_batch_rows", "max_issue_rows"):
            if int(getattr(self, name)) < 1:
                raise ValueError(f"ScanPolicy.{name} must be >= 1")


class CollectedBatch:
    """Rows from one walk, plus what that walk proved about coverage.

    ``collection_complete`` is the walk's own claim that it read the whole
    listing. ``consumption_complete`` is this consumer's claim that it consumed
    every row; it starts False and becomes True only through :meth:`consume`.
    """

    def __init__(self, thread: str, rows, *, cursor_before: int = 0,
                 pages=None, complete: bool = False, contiguous: bool = False,
                 reason: str = "") -> None:
        self.thread = str(thread)
        self.rows = _dedupe(rows)
        self.cursor_before = int(cursor_before or 0)
        self.pages = [PageRecord.from_walk(raw) if isinstance(raw, dict) else raw
                      for raw in (pages or [])]
        self.pagination_complete = bool(complete)
        self.contiguous = bool(contiguous)
        self.reason = str(reason)
        self.consumed_ids: set[int] = set()

    # ------------------------------------------------------------ claims
    @property
    def collection_complete(self) -> bool:
        """Did the walk prove it read the listing **and** left no page behind?

        A missing page is a hole whatever ``contiguous`` says: the backend's own
        claim is about the pages it *returned*, so a walk that skipped page 2 and
        then reported "the rows above the cursor are gap-free" is describing a
        subset. That is why ``missing_page`` is checked first and never inferred
        from adjacency of ids.
        """
        if self.missing_page() is not None:
            return False
        if self.pagination_complete:
            return True
        return self.contiguous

    @property
    def pending_rows(self) -> list[dict]:
        """Rows **above the cursor** that this batch did not consume.

        Rows at or below ``cursor_before`` were consumed by an earlier round -
        that is what the cursor means - so counting them as pending would make
        every incremental round look incomplete for ever. The AT path reads the
        whole listing for its projection; those history rows are not work.
        """
        return [row for row in self.rows
                if int(row["id"]) > self.cursor_before
                and int(row["id"]) not in self.consumed_ids]

    @property
    def consumption_complete(self) -> bool:
        return not self.pending_rows

    @property
    def remaining(self) -> int:
        return len(self.pending_rows)

    def page_spans(self) -> list[tuple[int, int]]:
        return [(rec.page, rec.page) for rec in self.pages if rec.page > 0]

    def missing_page(self) -> int | None:
        """The lowest page number the walk skipped, or ``None``."""
        return first_hole(self.page_spans())

    def needs_rebuild(self) -> bool:
        """Is this batch evidence that the incremental read cannot be trusted?

        **Ids are never consulted.** A listing whose ids jump (a deleted
        comment) is read perfectly well, and treating that as a gap is the
        counter-example this issue names. ``pagination_complete`` answering
        True settles the question by itself: the walk reached page 1.
        """
        if self.pagination_complete:
            return False
        return self.missing_page() is not None

    # ------------------------------------------------------------ rows
    def consume(self, ids) -> int:
        """Mark rows consumed; returns how many of them this batch held."""
        known = {int(row["id"]) for row in self.rows}
        wanted = {int(cid) for cid in ids} & known
        self.consumed_ids |= wanted
        return len(wanted)

    def merge(self, other: "CollectedBatch") -> "CollectedBatch":
        """One batch carrying both reads; the weaker claim wins on each fact."""
        if self.thread != other.thread:
            raise ValueError("a batch belongs to one thread")
        merged = CollectedBatch(
            self.thread, self.rows + other.rows,
            cursor_before=max(self.cursor_before, other.cursor_before),
            pages=self.pages + other.pages,
            complete=self.pagination_complete and other.pagination_complete,
            contiguous=self.contiguous and other.contiguous,
            reason="; ".join(x for x in (self.reason, other.reason) if x))
        merged.consumed_ids = self.consumed_ids & other.consumed_ids
        return merged

    def __len__(self) -> int:
        return len(self.rows)

    def __repr__(self) -> str:
        return (f"CollectedBatch({self.thread!r}, rows={len(self.rows)}, "
                f"cursor_before={self.cursor_before}, "
                f"collection_complete={self.collection_complete}, "
                f"consumed={len(self.consumed_ids)}, reason={self.reason!r})")


def _dedupe(rows) -> list[dict]:
    """One row per comment id; the first sighting wins, order is by id."""
    seen: dict[int, dict] = {}
    for row in rows or []:
        if not isinstance(row, dict) or row.get("id") is None:
            continue
        try:
            cid = int(row["id"])
        except (TypeError, ValueError):
            continue
        seen.setdefault(cid, dict(row, id=cid))
    return [seen[cid] for cid in sorted(seen)]


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


class CollectedRound:
    """Everything one round collected, and whether it may be called complete.

    ``seq`` advances on every round, unconditionally, so ``round.start``/
    ``round.end`` pairs stay countable even when a round never completes.
    ``run_count`` is a different counter and is bumped only for a complete one.
    """

    def __init__(self, seq: int, *, policy: ScanPolicy | None = None) -> None:
        self.seq = int(seq)
        self.policy = policy or ScanPolicy()
        self.batches: dict[str, CollectedBatch] = {}
        self.failed: set[str] = set()
        self.carry: dict[str, list[dict]] = {}
        self.consumed: dict[str, int] = {}
        self.backpressure: list[str] = []

    def add(self, thread: str, batch: CollectedBatch) -> None:
        thread = str(thread)
        existing = self.batches.get(thread)
        self.batches[thread] = existing.merge(batch) if existing else batch

    def fail(self, thread: str) -> None:
        self.failed.add(str(thread))

    @property
    def collection_complete(self) -> bool:
        if self.failed or self.carry:
            return False
        return all(batch.collection_complete for batch in self.batches.values())

    @property
    def consumption_complete(self) -> bool:
        return not self.carry and all(batch.consumption_complete
                                      for batch in self.batches.values())

    @property
    def round_complete(self) -> bool:
        """Both halves. Never one: a prefix is not the listing."""
        return self.collection_complete and self.consumption_complete

    def summary(self) -> dict:
        return {"round_seq": self.seq,
                "collection_complete": self.collection_complete,
                "consumption_complete": self.consumption_complete,
                "round_complete": self.round_complete,
                "batches": {k: len(v) for k, v in self.batches.items()},
                "failed": sorted(self.failed),
                "carry": {k: len(v) for k, v in self.carry.items() if v},
                "backpressure": list(self.backpressure)}
