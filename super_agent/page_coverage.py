"""Page coverage: which pages a walk read, and whether one is missing.

Why this module exists (§3.1.1, #130 QA P1-1): ``batch.py`` grew past 300 lines
and its largest block, ``CollectedBatch``, carried three unrelated jobs - row
identity (``consume``/``pending_rows``), page coverage (this file), and merging.
Only the middle one changes when the page-walk contract changes, and it changes
often: every P0 of #130 landed there.

The judgement is *not* "are the page numbers consecutive". The real walk is
``[1, last, last-1, …, k]``: page 1 is the probe that finds ``rel="last"``, then
it walks back and halts on the first page at/below the cursor. So:

* pages **below the stop** were never needed - their rows are under the cursor;
* a page is reached only if it carried rows, **except the last page**, which may
  legitimately be short or empty (an issue nobody has commented on);
* a walk that says it reached page 1 walked through every page to get there, so
  an empty page in that sequence is a lost request, not an empty tail;
* a walk that ran out of ``max_pages`` claims nothing either way.

Every one of those rules exists because its absence produced a real bug: a
stable false "missing page 2" (#130 P0-1), a fresh issue pausing itself forever
(#130 P0-A), and a page-gap verdict displacing ISS-128's own diagnosis
(#130 P0-B).
"""
from __future__ import annotations


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


def page_spans(pages) -> list[tuple[int, int]]:
        return [(rec.page, rec.page) for rec in pages if rec.page > 0]

def stop_page(pages) -> int | None:
        """The page the walk stopped on, or ``None`` when it walked to page 1."""
        stopped = [rec.page for rec in pages if rec.stopped and rec.page > 0]
        return min(stopped) if stopped else None

def last_page(pages) -> int:
        """The listing's newest page number, as the walk observed it."""
        return max((rec.page for rec in pages if rec.page > 0), default=0)

def observed_pages(pages) -> set[int]:
        """Pages that actually carried rows.

        A *middle* page that answered with nothing did not arrive: in a listing
        of ``last`` pages every page below ``last`` is full by construction, so a
        zero-row page there is the shape a lost request takes. Counting its bare
        entry as "seen" would hide exactly the hole a rebuild exists for.

        **The last page is the exception, and it has to be.** A listing whose
        only page is empty - an issue nobody has commented on yet - is a normal,
        complete read (``walk_listing`` always probes page 1). Treating its zero
        rows as "never arrived" made every fresh issue pause itself on the first
        round and never read the comments that arrived afterwards (#130 QA
        P0-A). A short or empty **final** page is how a listing ends; only a
        non-final page can be missing.
        """
        last = last_page(pages)
        return {rec.page for rec in pages
                if rec.page > 0 and (rec.rows > 0 or rec.page == last)}

def observed_spans(pages) -> list[tuple[int, int]]:
        """The pages that carried rows, as inclusive intervals."""
        return [(page, page) for page in sorted(observed_pages(pages))]

def missing_page(pages, cursor_before: int = 0,
                 pagination_complete: bool = False) -> int | None:
        """The lowest page number this walk should have read and did not.

        Which pages a walk "should have read" is the whole question, and the
        answer changed with #130 QA P0-1. The real walk is ``[1, last, last-1,
        …, k]``: page 1 is the probe that finds ``rel="last"``, then it walks
        back and halts on the first page at/below the cursor. Pages ``2..k-1``
        were **never needed** - every row on them is below the cursor, already
        consumed - so calling them holes reported a fault on every listing of
        three pages or more, which is every active issue.

        So the claim is an **interval with a floor**, never the observed set:

        * a complete walk (page 1 reached) makes no claim of its own: it either
          read every page or the backend said it did. A zero-row page 1 is an
          empty listing, not a hole, and a walk that says complete is not
          second-guessed (#130 QA P0-A);
        * the walk stopped at ``k`` - the pages ``k+1 … last`` are needed, and
          any of them without rows is a hole;
        * the walk ran out of ``max_pages`` - it claims nothing either way.
          "I stopped looking" is not "a page is missing", and inventing a hole
          there would rebuild a listing nobody proved broken.

        In particular the page-gap judgement is only **evidence about pages**.
        It never decides that a truncated history is a page gap: that diagnosis
        belongs to ISS-128's own gate
        (:func:`engine_parts.requirement_gate.apply`), which owns
        ``requirement.history_incomplete`` (#130 QA P0-B).
        """
        last = last_page(pages)
        if last <= 0:
            return None
        stop = stop_page(pages)
        if stop is not None:
            floor = stop + 1                  # above the stop: needed
        elif cursor_before <= 0 and not pagination_complete:
            # A whole-listing walk that did not reach page 1: it read the newest
            # pages and stopped short. Only the pages it actually probed can be
            # called missing - a page below what it never looked at is simply
            # unread history, which the requirement gate reports as such.
            floor = min(rec.page for rec in pages if rec.page > 0)
        elif not pagination_complete:
            # An incremental walk that spent its budget before reaching the
            # cursor: the pages it skipped may all be below it, so no page
            # number can be called missing. The read is already paused for this
            # shape (``walk_covers_cursor``), which is the honest answer.
            return None
        else:
            # A walk that claims it reached page 1 walked **backwards through
            # every page** to get there, so each of them must have had rows. An
            # empty page in that sequence is a lost request, and the claim of
            # completeness cannot overrule it - the two facts contradict each
            # other and the safe reading is the pessimistic one (#130 QA P0-A
            # is the empty *listing*, one page, which ``last_page`` covers).
            floor = 1
        seen = observed_pages(pages)
        missing = [page for page in range(floor, last + 1) if page not in seen]
        return missing[0] if missing else None

def needs_rebuild(pages, cursor_before: int = 0,
                  pagination_complete: bool = False) -> bool:
        """Is this batch evidence that the incremental read cannot be trusted?

        **Ids are never consulted.** A listing whose ids jump (a deleted
        comment) is read perfectly well, and treating that as a gap is the
        counter-example this issue names.

        The answer is exactly "a page this walk needed had no rows", never a
        separate reading of ``pagination_complete``. Those two used to disagree:
        a walk that reached page 1 while one middle page came back empty claims
        completeness **and** is missing data, and letting the claim win meant the
        pause a rebuild exists for was never written (#130 QA P0-2). Page 1 is
        always needed, so a complete walk has no floor to excuse anything.
        """
        return missing_page(pages, cursor_before, pagination_complete) is not None
