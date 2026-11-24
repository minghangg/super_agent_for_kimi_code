"""What a comment walk proved - the two questions a consumer may ask.

Same walk, two depths:

* **the unread prefix** - may a round consume the rows above the cursor?
  (``walk_covers_cursor``)
* **the whole listing** - may the AT requirement projection be rebuilt from it?
  (``history_is_complete``)

Both answers come from the walk itself (``PagedComments``), never from the rows:
a page that happens to reach the cursor looks identical whether the middle was
read or lost, so any rule phrased on the rows would license the silent stall
that #127 fixed.

The pause that stops a read, and the way a person lifts it, live in
:mod:`super_agent.engine_parts.pause` - one responsibility per module.
"""
from __future__ import annotations


# ------------------------------------------------------------------ claims
def walk_covers_cursor(walked, cursor: int) -> bool:
    """Did this walk prove every row above ``cursor`` is present?

    Only a backend that reports a claim is asked the question. A plain list
    answer (a fixture, a test double, ``NullSource``) carries no claim either
    way, and the pre-existing contract for those backends is "the list is the
    listing": reading that silence as a hole would pause every issue on every
    legacy backend, which is a regression, not a guard.

    The claim being read is ``contiguous``, not ``pagination_complete``: a
    consumer that only needs the unread prefix is satisfied by a walk that
    stopped on a page at/below the cursor, and demanding the whole listing
    would turn every normal incremental round into a pause.

    **``contiguous=False`` is a refusal, not a hint.** It means the backend
    is stating that it did not prove the rows above the cursor are gap-free,
    and no property of the rows that *were* returned can override that
    statement - a page that happens to reach the cursor while a middle page
    is missing looks exactly like a safe walk from the rows alone. That
    inference is what produced the silent stall this function exists to
    prevent, so it is not made here: the caller pauses instead.
    """
    if not hasattr(walked, "pagination_complete"):
        return True
    return bool(getattr(walked, "contiguous", False))


def history_is_complete(walked) -> bool:
    """Did this walk read the whole listing?

    Stricter than :func:`walk_covers_cursor` on purpose: the AT requirement
    projection is rebuilt from the history every round, so a prefix is not
    enough - a requirement package published on a page that was never read is
    a requirement that silently does not exist.

    A backend that answers with a plain list is read as complete: that is the
    pre-existing contract for fixtures and test doubles, and they serve their
    whole listing by definition.
    """
    if not hasattr(walked, "pagination_complete"):
        return True
    return bool(getattr(walked, "pagination_complete", False))


def walk_rows(walked) -> list[dict]:
    """The rows of a walk, whatever shape the backend answered with."""
    return [row for row in walked if isinstance(row, dict)]
