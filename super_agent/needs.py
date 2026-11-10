"""Dismissal marks for ``needs_person`` entries (side table, read-side only).

``needs_person`` only ever grew: a person handled the item, and ``status()`` /
``brief`` kept showing it forever, hiding the real problems. Dismissing one is
recorded **next to** the index, never inside it, so the entries themselves are
left byte-for-byte alone: no history is rewritten and nothing is deleted.

Identity
--------

Identity is ``(index key, comment_id)`` - never "one entry per triage".

The same comment_id CAN appear more than once under one issue record:
``_route_review`` appends on every failed dispatch, and ``_retry_routes``
calls it again on each round while the dispatch stays suppressed or refused
(no attempt counter is written in those paths). So N rows for one cid is a
normal shape, not a bug this module fixes.

Therefore: one side-table row per (key, cid) suppresses *all* rows with that
cid under that key. Counting rows is never used to decide identity.

``entries`` (three hard rules, audit snapshot)
---------------------------------------------

1. ``entries`` = the number of rows with that cid under that key **at the
   moment of dismissal**. It is a historical snapshot: a later ``replay``
   rebuilds the entries and ``entries`` does not follow. **No consumer may
   use it to decide how many rows exist now.**
2. It must be computed from the index re-read just before the save (the
   ``disk1`` copy), never from the first read.
3. ``entries == 0`` has its own meaning: the first read did have rows, and by
   the time the dismissal was written they had been cleared by another write.
   It is not "there were never any rows" - a cid with no rows produces no
   side-table row at all.

Pure stdlib: this module is imported by the engine, the brief and the CLI, so
it must not pull in the bus, the network or any sibling module.
"""
from __future__ import annotations

from datetime import datetime, timezone

MARKS = "needs_person_resolved"

RESOLVED = "resolved"
SUPERSEDED = "superseded"
STATES = (RESOLVED, SUPERSEDED)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def entry_key(thread, comment_id) -> str:
    """``owner/repo#42|9100`` - the identity of one dismissal."""
    return f"{thread}|{int(comment_id)}"


def cid_of(entry):
    """The comment id of an index entry (a dict) or of a bare id; None if unusable."""
    raw = entry.get("comment_id") if isinstance(entry, dict) else entry
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def marks(index) -> dict:
    """The side table; never raises, never creates the key."""
    if not isinstance(index, dict):
        return {}
    table = index.get(MARKS)
    return table if isinstance(table, dict) else {}


def is_open(index, thread, entry) -> bool:
    """True when the entry still needs a person.

    Pure read: it NEVER writes an event and never touches the index. An entry
    without a usable ``comment_id`` is reported open - guessing an identity
    would be worse than showing the row.
    """
    cid = cid_of(entry)
    if cid is None:
        return True
    return entry_key(thread, cid) not in marks(index)


def mark(index, thread, comment_id, *, state, by, reason="", by_comment=None,
         superseded_by=None, entries=1, at=None) -> dict:
    """Dismiss one ``(thread, comment_id)``. Idempotent: an existing row is kept.

    Returns ``{"changed": bool, "reason": str, "row": dict}``. When the key is
    already there ``changed`` is False and ``at``/``by``/``entries`` are left
    exactly as they were - a second dismissal must not rewrite the audit trail.
    """
    if state not in STATES:
        raise ValueError(f"state must be one of {STATES}, got {state!r}")
    cid = cid_of(comment_id)
    if cid is None:
        raise ValueError(f"comment_id is not an integer: {comment_id!r}")
    table = index.setdefault(MARKS, {})
    if not isinstance(table, dict):
        table = index[MARKS] = {}
    key = entry_key(thread, cid)
    existing = table.get(key)
    if isinstance(existing, dict):
        return {"changed": False, "reason": "already", "row": dict(existing)}
    row = {
        "state": state,
        "reason": str(reason or ""),
        "at": at or _now(),
        "by": str(by or ""),
        "by_comment": None if by_comment is None else int(by_comment),
        "superseded_by": superseded_by,
        "entries": int(entries),
    }
    table[key] = row
    return {"changed": True, "reason": "marked", "row": dict(row)}


def merge_into(index, disk_index) -> int:
    """Union the whole side table of ``disk_index`` into ``index``; returns rows changed.

    Called by ``run_round`` right before every ``save_index``: a round holds
    its index in memory for the whole round, so without this a dismissal
    written meanwhile would be overwritten. The table is monotonic, so a union
    is always safe.
    """
    if not isinstance(index, dict):
        return 0
    other = marks(disk_index)
    if not other:
        return 0
    table = index.setdefault(MARKS, {})
    if not isinstance(table, dict):
        table = index[MARKS] = {}
    changed = 0
    for key, row in other.items():
        merged = _winner(table.get(key), row)
        if table.get(key) != merged:
            table[key] = merged
            changed += 1
    return changed


def merge_into_side(index, rows) -> int:
    """Union ONLY the given rows into ``index[MARKS]``; no other key is read or written.

    Used by ``cli resolve``: the index that gets saved is the one re-read from
    disk, and the only thing added to it is the dismissal rows.
    """
    if not isinstance(index, dict):
        return 0
    table = index.setdefault(MARKS, {})
    if not isinstance(table, dict):
        table = index[MARKS] = {}
    changed = 0
    for key, row in (rows or {}).items():
        merged = _winner(table.get(key), row)
        if table.get(key) != merged:
            table[key] = merged
            changed += 1
    return changed


def _state(row) -> str:
    return str((row or {}).get("state") or "open")


def _winner(a, b) -> dict:
    """Arbitrate two rows for the same key. The winner is kept verbatim.

    Union by key. Never delete a key present on either side.

    When both sides hold the same key:

      1. ``resolved`` ALWAYS beats ``superseded``, regardless of ``at``
         (a person's ruling is never overwritten by an automatic inference);
      2. same ``state``, different ``at``: the EARLIER ``at`` wins;
      3. same ``state``, same ``at``: the row with the lexicographically SMALLER
         ``by`` wins. The clock has second precision, so a tie is reachable;
         without this rule the result would depend on dict iteration order and
         the union would stop being commutative;
      4. ``entries`` = max(both sides): never lose how many rows were folded.

    A missing ``at`` sorts before every timestamp, so an undated row never
    overwrites a dated one.
    """
    if not isinstance(a, dict):
        return dict(b or {})
    if not isinstance(b, dict):
        return dict(a)
    win, lose = (a, b) if _beats(a, b) else (b, a)
    row = dict(win)
    row["entries"] = max(int(a.get("entries") or 0), int(b.get("entries") or 0))
    return row


def _beats(a, b) -> bool:
    """True when row ``a`` wins over row ``b`` (rule 1-3)."""
    sa, sb = _state(a), _state(b)
    if sa != sb:
        return sa == RESOLVED
    ta, tb = str(a.get("at") or ""), str(b.get("at") or "")
    if ta != tb:
        return ta < tb
    return str(a.get("by") or "") <= str(b.get("by") or "")
