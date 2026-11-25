"""Round bookkeeping: the two counters, the cursor rule, and what "complete" is.

Why this module exists (ISS-130 D): a round used to bump ``run_count`` at the
end no matter what happened, so a round that read half a listing and consumed
half of that was counted exactly like a clean one. Downstream, ``run_count`` is
read as "how many rounds have passed" - by the close-reconcile streak and by the
stall watchdogs - and a counter that moves for an incomplete round makes every
one of those answers optimistic.

Two counters, two meanings, and they are never the same number:

* ``round_seq`` - **every** round, unconditionally. A round that failed, was cut
  short by backpressure, or consumed nothing still gets a sequence number, so
  ``round.start``/``round.end`` pairs stay countable and a stall can be dated.
* ``run_count`` - only a round with ``collection_complete AND
  consumption_complete``. That is the number the streak and the watchdogs read.

The cursor rule is here for the same reason: it may only ever name ids that were
**consumed**, so it advances to the highest consumed id and never to the highest
observed one. Advancing to the observed maximum would jump over rows the queue
refused, and those rows would never be read again.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..batch import (BatchCursor, CollectedRound, DEFAULT_MAX_BATCH_ROWS,
                     DEFAULT_MAX_ISSUE_ROWS, ScanPolicy)
from ..batch_queue import BoundedBatchQueue


def next_round_seq(index: dict) -> int:
    """``round_seq + 1`` - unconditional, written before anything is read."""
    meta = index.setdefault("meta", {})
    seq = int(meta.get("round_seq") or 0) + 1
    meta["round_seq"] = seq
    return seq


def run_count(index: dict) -> int:
    return int((index.get("meta") or {}).get("run_count") or 0)


def round_policy(gh_cfg: dict | None) -> ScanPolicy:
    """The round's budgets, from ``github`` config with safe defaults.

    Read here rather than in the engine so one place answers "what are the caps
    this round collects under", and the queue and the batch agree by
    construction.

    **The caps are not derived from ``max_comments_per_issue``.** That setting is
    the *consumption window* - how many comments one round may act on, usually
    small - and using it as the collection cap made the queue refuse a listing
    with more than a couple of outstanding comments. Refusing is correct
    behaviour when the cap is genuinely reached, but deriving a small cap from an
    unrelated setting turns every busy issue into a permanently stalled one.
    """
    cfg = gh_cfg or {}

    def _int(name: str, default: int) -> int:
        try:
            value = int(cfg.get(name) or default)
        except (TypeError, ValueError):
            return default
        return value

    return ScanPolicy(
        max_batches=_int("max_batches", 8),
        max_batch_rows=_int("max_batch_rows", DEFAULT_MAX_BATCH_ROWS),
        max_issue_rows=_int("max_issue_rows", DEFAULT_MAX_ISSUE_ROWS),
        max_pages=_int("max_comment_pages", 10),
        page_size=_int("page_size", 100))


def complete_round(index: dict, round_result: CollectedRound) -> bool:
    """Bump ``run_count`` **only** when the round collected and consumed it all."""
    if not round_result.round_complete:
        return False
    meta = index.setdefault("meta", {})
    meta["run_count"] = run_count(index) + 1
    return True


def consumed_prefix(previous: int, consumed_ids) -> int:
    """The highest id this round may claim, given what it actually consumed.

    ``0`` means "nothing was consumed", and the caller must leave the cursor
    exactly where it was - a paused issue keeps its position byte for byte.
    """
    ids = sorted(int(cid) for cid in consumed_ids or ())
    if not ids:
        return int(previous or 0)
    return max(int(previous or 0), ids[-1])


def carry_remaining(round_result: CollectedRound) -> dict:
    """Threads with rows collected and not consumed, with the count left over."""
    out = {}
    for thread, batch in round_result.batches.items():
        left = batch.remaining
        if left:
            out[thread] = left
    return out


@dataclass
class RoundState:
    """What one round carries between its three phases.

    ``source_ok`` is the single fact the allowlist sync is gated on: an issue
    list that came from a failed request is not "the live set", and syncing it
    would empty the in-flight set - the overlay would then say no issue is live
    and every frame for every issue would be refused until the source came back.
    """

    seq: int
    policy: ScanPolicy
    round_result: CollectedRound
    queue: BoundedBatchQueue
    cursor: BatchCursor
    source_ok: bool = True
    issues: list[int] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    #: Threads whose cursor may not advance this round. A failed rebuild names
    #: one: the rows it could not prove it read are exactly what a cursor move
    #: would bury (#130 QA P0-3). Kept on the state rather than passed around so
    #: the two write sites cannot disagree about it.
    cursor_blocked: set[str] = field(default_factory=set)

    @classmethod
    def begin(cls, index: dict, cursor_doc: dict,
              policy: ScanPolicy | None = None) -> "RoundState":
        policy = policy or ScanPolicy()
        seq = next_round_seq(index)
        prefix = {str(k): int(v or 0)
                  for k, v in (cursor_doc.get("issues") or {}).items()}
        return cls(seq=seq, policy=policy,
                   round_result=CollectedRound(seq, policy=policy),
                   queue=BoundedBatchQueue(policy),
                   cursor=BatchCursor(prefix))

    def block_cursor(self, thread: str) -> None:
        self.cursor_blocked.add(str(thread))

    def cursor_allowed(self, thread: str) -> bool:
        return str(thread) not in self.cursor_blocked

    def fail(self, thread: str) -> None:
        self.round_result.fail(thread)

    def complete(self) -> bool:
        """Is the round complete? Stops the queue from being read as complete."""
        return self.round_result.round_complete
