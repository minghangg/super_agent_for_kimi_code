"""The page-walk API: what a walk returned, and what it proved.

Kept beside (not inside) :mod:`super_agent.github_source` because these are the
pure parts - a ``list`` subclass carrying two claims, one walk helper, and the
single-snapshot batch a round consumes. The backends themselves stay in
``github_source``; this module must not import it, so the two never form a cycle.
"""
from __future__ import annotations

import re


class PagedComments(list):
    """A list of comments that also says how the page walk ended.

    The return type stays a ``list`` on purpose: every caller (and every test
    double) indexes it, slices it and compares it with a list, so making
    ``paged()`` return a tuple would break them for no gain. The completeness
    rides along as an attribute instead - a list subclass is still a list.

    Two separate facts, never merged:

    * ``pagination_complete`` - the walk positively reached the end of the
      listing (page 1), so the rows are the whole listing;
    * ``contiguous`` - everything **above the cursor** was collected with no
      hole. This is the weaker claim and the one a consumer may act on when it
      only needs the unread prefix; stopping at a page that sits at/below the
      cursor proves it, and so does walking all the way to page 1.

    A full page is not evidence of either, and a failure is never folded into
    an empty list: the walk raises instead.
    """

    def __init__(self, rows=(), *, complete: bool = False,
                 contiguous: bool | None = None,
                 pages: list | None = None, reason: str = ""):
        super().__init__(rows)
        self.pagination_complete = bool(complete)
        self.contiguous = bool(complete if contiguous is None else contiguous)
        self.pages = list(pages or [])
        self.stop_reason = str(reason)

    def __repr__(self) -> str:  # keeps assertion messages readable
        return (f"PagedComments({list.__repr__(self)}, "
                f"complete={self.pagination_complete}, "
                f"contiguous={self.contiguous}, "
                f"reason={self.stop_reason!r})")


def paged_result(source, issue: int, *, since_id: int = 0, max_pages: int = 10):
    """One page walk, returned as whatever the backend produced.

    This is the single read the engine uses: the result is kept whole so the
    rows and the completeness answer come from the *same* walk instead of two
    requests (a second walk could see a different listing, and would double the
    network cost of every issue on every round).
    """
    paged = getattr(source, "paged", None)
    if callable(paged):
        return paged(issue, since_id=since_id, max_pages=max_pages)
    rows = source.comments(issue)
    return [c for c in rows if int(c["id"]) >= int(since_id or 0)]


def comments_from(source, issue: int, *, since_id: int = 0,
                  max_pages: int = 10) -> list[dict]:
    """``source.paged()`` when the backend has it, else the whole list.

    Keeping this behind one helper means the engine never needs to know which
    backend it was given, and a test double that only implements ``comments()``
    keeps working unchanged.

    The return is always a plain ``list`` of rows - never a ``(rows, complete)``
    tuple. A backend may return either a list or a ``list``-shaped result that
    also carries ``pagination_complete``; both are accepted here by duck typing
    rather than by unpacking, so a backend that returns a plain list (every test
    double, ``FixtureSource``, ``NullSource``) cannot raise ``ValueError`` on a
    two-way unpack. Completeness is published through :func:`walk_complete`,
    which takes the walk's own result rather than issuing a second one.
    """
    return list(paged_result(source, issue, since_id=since_id,
                             max_pages=max_pages))


def walk_complete(result) -> bool:
    """Did this walk positively reach the end of the listing?

    Only a backend that reports completeness can answer True (see
    :class:`PagedComments`). Everything else answers False: a backend that
    cannot say where its walk stopped has not proven it read everything, and
    "unknown" must never be read as "complete".
    """
    return bool(getattr(result, "pagination_complete", False))


class PagedBatch:
    """One read of one listing, with the two claims attached.

    ``rows`` is every row the walk returned; ``cursor_before`` is the position
    the round started from, recorded here so a consumer can never mix rows from
    one snapshot with a cursor from another. ``complete`` and ``contiguous``
    keep the meanings :class:`PagedComments` gives them.

    Deliberately not a ``list``: a batch is one read, and the point of naming it
    is that the incremental rows and the history rows come from *this* object
    rather than from two requests.
    """

    def __init__(self, rows, *, cursor_before: int = 0, complete: bool = False,
                 contiguous: bool = False, reason: str = ""):
        self.rows = list(rows)
        self.cursor_before = int(cursor_before or 0)
        self.complete = bool(complete)
        self.contiguous = bool(contiguous)
        self.reason = str(reason)

    #: Same names ``PagedComments`` uses, so one claim reader serves both and a
    #: consumer cannot accidentally ask the batch a question it answers with
    #: ``hasattr`` = False (which would read as "no claim, proceed").
    @property
    def pagination_complete(self) -> bool:
        return self.complete

    @property
    def stop_reason(self) -> str:
        return self.reason

    def __len__(self) -> int:
        return len(self.rows)

    def __iter__(self):
        return iter(self.rows)

    def __repr__(self) -> str:
        return (f"PagedBatch(rows={len(self.rows)}, "
                f"cursor_before={self.cursor_before}, complete={self.complete}, "
                f"contiguous={self.contiguous}, reason={self.reason!r})")


def paged_batch(source, issue: int, *, cursor_before: int = 0,
                max_pages: int = 10, whole_listing: bool = False) -> PagedBatch:
    """Read one listing once and report what that one walk proved.

    The increment a round consumes and the history the AT projection is rebuilt
    from are **the same rows**, so they must come from **the same request run**:
    two walks could straddle a comment that arrives between them, and the round
    would then consume a row the projection never saw (or the reverse) while
    both sides believe they agree. ``whole_listing`` decides the starting point -
    a history rebuild needs ``since_id=0`` because a package may predate the
    cursor, while an incremental round only needs everything above it.

    A backend that answers with a plain list makes no claim (see
    :class:`PagedComments`); the pre-existing contract for those backends is
    that the list *is* the listing, so the batch reports both claims as true and
    nothing that worked before starts pausing.
    """
    walked = paged_result(source, issue,
                          since_id=0 if whole_listing else int(cursor_before or 0),
                          max_pages=max_pages)
    if isinstance(walked, PagedComments):
        return PagedBatch(walked, cursor_before=cursor_before,
                          complete=walked.pagination_complete,
                          contiguous=walked.contiguous,
                          reason=walked.stop_reason)
    rows = [row for row in walked if isinstance(row, dict)]
    return PagedBatch(rows, cursor_before=cursor_before, complete=True,
                      contiguous=True, reason="backend returned the whole list")
