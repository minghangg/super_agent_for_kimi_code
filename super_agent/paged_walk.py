"""The page walk itself: locate the newest page, walk back, report what it proved.

Pure inference over a ``page_of(n)`` callback - the caller owns HTTP and row
normalisation, this module owns "how many pages are there" and "may I stop yet".
It never issues a request and never looks at a row's content beyond its id.

Kept separate from :mod:`super_agent.paged_comments` (the result types and the
batch) because the two change for different reasons: one is the shape a caller
receives, the other is how the walk decides it is finished.
"""
from __future__ import annotations

import re

from .paged_comments import PagedComments


def link_last_page(headers: dict) -> int | None:
    """The ``page`` of ``rel="last"`` in the ``Link`` header, or None.

    ``Link`` is the API's own statement about pagination, so it is the only
    thing this reader trusts to know how many pages exist. Its absence means
    "there is no next/last link", which is evidence of a single page - not
    evidence that the listing was short.
    """
    raw = ""
    for name, value in (headers or {}).items():
        if str(name).lower() == "link":
            raw = str(value)
            break
    for part in raw.split(","):
        if 'rel="last"' not in part.replace(" ", ""):
            if "rel=last" not in part.replace(" ", ""):
                continue
        match = re.search(r"[?&]page=(\d+)", part)
        if match:
            return int(match.group(1))
    return None

def walk_listing(page_of, *, page_size: int, since_id: int = 0,
                 max_pages: int = 10) -> PagedComments:
    """The newest pages first, walked back to ``since_id``, via ``Link``.

    ``GET /repos/{repo}/issues/{n}/comments`` has **no ordering parameter**:
    it answers oldest-first whatever ``direction`` says. Measured against the
    public API (with and without a token), and the ``Link`` header even
    rewrites ``direction`` back to ``asc``. A reader that trusts
    ``direction=desc`` therefore gets page 1 = the *oldest* rows: with 120
    comments and a cursor at 100 that is ids 1-100, so a stop rule phrased on
    one page ends the walk before 101-120 are fetched. That is exactly how
    the previous revision stalled silently at 100.

    What the endpoint *does* state is ``Link: ...; rel="last"``, so this
    reader never guesses about order:

    1. **locate the newest page.** Read page 1 and take ``rel="last"``. No
       ``Link`` and a short page ⇒ the listing is one page, complete. No
       ``Link`` and a *full* page ⇒ the fact is unknown, so read page 2 once:
       a full page is never by itself proof that the listing ended. If page 2
       also states a last page, that is the total.
    2. **walk back from the newest page to page 1.** Rows are collected as
       the walk goes and page numbers strictly decrease, so the newest rows
       are always collected first. The walk stops as soon as a page's
       **smallest** id is at/below the cursor: every earlier page is older
       still, so nothing above the cursor can be left. A page that merely
       *contains* one old row does not stop it - its other rows are newer,
       and dropping them is the bug this rule exists for.
    3. **only reaching page 1 proves completeness.** Reaching that far means
       the listing was read from end to end, so ``pagination_complete`` is
       True and every page's rows are the answer. Stopping at the cursor
       means everything above the cursor is present - ``contiguous`` - while
       older rows may sit on pages never read. Running out of ``max_pages``
       proves neither.

    Any failure raises :class:`SourceError` instead of returning what was
    gathered so far. The return value is a :class:`PagedComments`, a ``list``
    subclass, so every old caller keeps working while the two claims ride
    along.
    """
    since_id = int(since_id or 0)
    max_pages = max(1, int(max_pages or 1))

    last, pages, probe = locate_last_page(page_of, max_pages, page_size)
    if last is None:
        # A full page with no Link and no budget left to look further: the
        # listing may well continue, and a full page is not proof it does
        # not. Claim neither completeness nor contiguity.
        return PagedComments(
            sorted(probe, key=lambda c: c["id"]), complete=False,
            contiguous=False, pages=pages,
            reason="one full page, no Link, budget spent: the listing may "
                   "continue")
    if last <= 1:
        return PagedComments(sorted(probe, key=lambda c: c["id"]),
                             complete=True, contiguous=True, pages=pages,
                             reason="the listing is a single page")

    collected: list[dict] = []
    walked = len(pages)          # every request already spent counts
    complete = False
    contiguous = False
    reason = (f"max_pages={max_pages} reached before page 1 or a page at or "
              f"below the cursor (newest page is {last})")
    for page_no in range(last, 0, -1):
        if page_no in {p["page"] for p in pages}:
            continue             # already fetched while locating the end
        if walked >= max_pages:
            break
        raw_count, rows, _headers = page_of(page_no)
        pages.append({"page": page_no, "rows": raw_count, "status": "ok"})
        walked += 1
        collected.extend(rows)
        if since_id and rows and min(int(c["id"]) for c in rows) <= since_id:
            contiguous = True
            reason = "whole page at or below the cursor"
            break
    else:
        complete = True
        contiguous = True
        reason = "walked back to page 1"
    if complete:
        collected = probe + collected
    return PagedComments(sorted(collected, key=lambda c: c["id"]),
                         complete=complete, contiguous=contiguous,
                         pages=pages, reason=reason)

def locate_last_page(page_of, max_pages: int, page_size: int):
    """``(last_page_number | None, pages_fetched, rows)`` - how many pages exist.

    ``page_of(n)`` returns ``(raw_row_count, normalised_rows, headers)`` for one
    page; the caller owns the HTTP and the normalisation, this function owns the
    inference. ``page_size`` is the row count a full page carries, which is the
    only evidence a short page can be told apart from a truncated one.

    ``rel="last"`` is the API's own statement, so it is preferred. Without it
    the answer is inferred only from evidence that cannot lie: a short page
    means the listing ended at the page before it. A *full* page with no
    ``Link`` is ambiguous, so the next page is read - and so on, up to the
    page budget. **The budget decides the probe depth**: a caller that lifts
    the cap is saying "look deeper", and a probe that ignored that would
    report the same "unknown" no matter the budget, which is what made
    ``resume-read`` a no-op on listings that are a whole number of pages
    (200, 300, ...) with no ``Link``.

    Exhausting the budget on full pages still answers ``None`` - "unknown",
    never "that many pages". Saying "2 pages" for two full pages would let
    the walk reach page 1, declare itself complete and silently truncate
    everything past row 200; that is the defect this function exists for.
    """
    max_pages = max(1, int(max_pages or 1))
    rows: list[dict] = []
    pages: list[dict] = []
    for page in range(1, max_pages + 1):
        count, page_rows, headers = page_of(page)
        pages.append({"page": page, "rows": count, "status": "ok"})
        rows.extend(page_rows)
        last = link_last_page(headers)
        if last is not None:
            return last, pages, rows
        if count < page_size:
            # A short page (including an empty one) is the end of the
            # listing, and it is the only bare fact that proves it.
            return page, pages, rows
    # Every page the budget allowed came back full and said nothing: the
    # listing may well continue past them, and a full page is not proof.
    return None, pages, rows
