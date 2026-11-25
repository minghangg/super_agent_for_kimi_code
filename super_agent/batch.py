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

from . import page_coverage

__all__ = ["PageRecord", "merge_spans", "spans_contiguous", "first_hole",
           "ScanPolicy", "CollectedBatch", "CollectedRound", "BatchCursor",
           "DEFAULT_MAX_BATCH_ROWS", "DEFAULT_MAX_ISSUE_ROWS"]


def __getattr__(name: str):
    """Names that moved out of this module but stay part of its surface.

    ``BatchCursor`` now lives with consumption
    (``engine_parts.consume``) and the interval helpers with the page-coverage
    judgement (``page_coverage``). Every caller and every test spells them
    ``batch.<name>``, so moving the code must not rename it for them. Imported
    lazily so the modules do not form a cycle at import time.
    """
    if name == "BatchCursor":
        from .engine_parts.consume import BatchCursor
        return BatchCursor
    if name in ("merge_spans", "spans_contiguous", "first_hole"):
        return getattr(page_coverage, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

#: The two row caps' defaults, named here because this is the module that owns
#: the budget and :func:`engine_parts.round.round_policy` reads them. Two
#: literals in two modules is how the delivery text and the code drifted apart
#: (#130 QA P1-5).
DEFAULT_MAX_BATCH_ROWS = 5000
DEFAULT_MAX_ISSUE_ROWS = 20000


@dataclass(frozen=True)
class PageRecord:
    """One fetched page: which page, how many rows, and where the walk stopped.

    ``stopped`` is what makes a page number usable as evidence. The real walk
    reads ``[1, last, last-1, …]`` and halts on the first page whose smallest
    id is at/below the cursor (#130 QA P0-1): the pages below that one are
    older still, so their absence is not a hole - they were never needed. A
    page number is therefore only dense **above** the stop page, and
    :meth:`CollectedBatch.missing_page` reads it that way.
    """

    page: int
    rows: int = 0
    first_id: int = 0
    last_id: int = 0
    status: str = "ok"
    stopped: bool = False

    @classmethod
    def from_walk(cls, raw: dict) -> "PageRecord":
        """``{"page": n, "rows": k, "status": "ok", "stopped": bool}``."""
        try:
            return cls(page=int(raw.get("page") or 0),
                       rows=int(raw.get("rows") or 0),
                       status=str(raw.get("status") or "ok"),
                       stopped=bool(raw.get("stopped")))
        except (TypeError, ValueError):
            return cls(page=0, status="malformed")




@dataclass(frozen=True)
class ScanPolicy:
    """The budgets one round collects under.

    ``max_batches`` is the global queue cap; ``max_issue_rows`` is the per-issue
    cap. Both refuse rather than drop: a row that does not fit stays above the
    cursor, so the next round reads it again.

    The two row defaults live in module constants so
    :func:`engine_parts.round.round_policy` reads the **same** numbers instead of
    restating them. Two literals in two modules is how the delivery text and the
    code drifted apart (#130 QA P1-5).
    """

    max_batches: int = 8
    max_batch_rows: int = DEFAULT_MAX_BATCH_ROWS
    max_issue_rows: int = DEFAULT_MAX_ISSUE_ROWS
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

    # ------------------------------------------------------- page coverage
    # The judgement lives in :mod:`super_agent.page_coverage`: it changes when
    # the page-walk contract changes, which is not when row identity or merging
    # changes. These forwarders keep the call sites - and the tests that pin the
    # behaviour - reading the same names.

    def page_spans(self) -> list[tuple[int, int]]:
        return page_coverage.page_spans(self.pages)

    def stop_page(self) -> int | None:
        return page_coverage.stop_page(self.pages)

    def last_page(self) -> int:
        return page_coverage.last_page(self.pages)

    def observed_pages(self) -> set[int]:
        return page_coverage.observed_pages(self.pages)

    def observed_spans(self) -> list[tuple[int, int]]:
        return page_coverage.observed_spans(self.pages)

    def missing_page(self) -> int | None:
        return page_coverage.missing_page(
            self.pages, self.cursor_before, self.pagination_complete)

    def needs_rebuild(self) -> bool:
        return page_coverage.needs_rebuild(
            self.pages, self.cursor_before, self.pagination_complete)


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
        self.backpressure: list[dict] = []

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
