"""The engine's ``degraded`` flag: what sets it, and what clears it.

Why this module exists (#142 QA P0-1): five sites set ``Engine.degraded = True``
and nothing ever set it back. One transient fault therefore pinned
``health.json`` at ``degraded: true`` with ``last_error: ""`` for as long as the
process lived - measured in production at 66 rounds and still counting. That is
the exact shape ``rules/shared/rules/fail_loud.md`` calls P0: the snapshot says
something is wrong, and cannot say what.

The fix is the one ``_stream_error`` already used: **the flag is round-scoped.**
It is cleared at the top of a round and set again only by a fault that is live
in that round. A fault that survives is degraded again before the round ends, so
a reader sees it continuously; a fault that clears disappears from the next
snapshot instead of being frozen there.

Recovery is a *transition*, not a state, so it gets one dated row. The
comparison is between two **published snapshots** rather than between in-memory
values: that survives a restart, and a persistent fault cannot fake it because
it is degraded in both snapshots.
"""
from __future__ import annotations

from .. import health

RECOVERED_ROW = "health.recovered"


def previous_snapshot(health_path) -> dict:
    """The snapshot already on disk, or ``{}`` when there is none.

    Unreadable counts as "no previous snapshot": a corrupt health file must not
    be read as a recovery, and the flag is cleared by the round regardless.
    """
    try:
        return health.read(health_path) or {}
    except (OSError, ValueError):
        return {}


def clear(engine) -> None:
    """Start a round clean: no fault is assumed until one is observed.

    Called from ``begin_round``, immediately before the note-* helpers that
    detect this round's faults. Any of them setting ``degraded`` afterwards is
    what carries a live fault into the snapshot.
    """
    engine.degraded = False


def recovery_row(previous: dict, written: dict, *, now: str, round_seq=None):
    """The ``health.recovered`` row to write, or ``None``.

    Both arguments are **snapshots that reached disk**: ``previous`` is what
    was on disk before this round, ``written`` is what ``health.write()``
    returned after the replacement (its return value is the normalized record,
    not the caller's input).

    Taking the two ends this way is the fix for #142 QA P0-1. The first version
    compared ``engine.degraded`` with ``previous["degraded"]``, and those are
    not the same quantity: :meth:`Engine.health_record` folds ``error``,
    ``_index_error``, ``_stream_error`` and ``_bus_last_error`` into the
    published flag, while ``engine.degraded`` is set by three of those sites
    only. A source failure (``error`` non-empty every round) kept the snapshot
    degraded while the memory flag stayed false, so every round looked like a
    recovery - QA measured three false rows over four rounds.

    Comparing two published snapshots removes the class of bug rather than this
    instance of it: there is no second definition of "degraded" left to
    disagree, and no future fault source can reintroduce the gap by forgetting
    to mirror itself into a flag.
    """
    if not previous.get("degraded") or written.get("degraded"):
        return None
    row = {"type": RECOVERED_ROW, "at": now, "level": "info",
           "consumers": ["doctor"]}
    if round_seq is not None:
        row["round_seq"] = round_seq
    return row


def mark(engine, reason: str = "") -> None:
    """A fault is live in this round: say so, and keep the reason.

    Both halves matter. The flag alone produces ``degraded: true`` with an empty
    ``last_error`` - the shape that started #142 - so a caller with something
    to say passes it and the snapshot carries it.
    """
    engine.degraded = True
    if reason:
        engine._index_error = reason


def note_index_state(engine, now: str) -> str:
    """Read the index once and record whether it can be trusted.

    ``load_index`` already records the fault and moves the file aside at most
    once per process, so the store's marker - not a second ``index_problem``
    probe - keeps the reason after the path is empty. This function's job is to
    carry that answer into ``degraded``, ``_index_error`` and the event stream.
    """
    engine.store.load_index()
    problem = engine.store.index_unreadable
    engine._index_error = problem
    if not problem:
        return ""
    mark(engine)
    engine.store.log_event({
        "type": "index.unreadable", "at": now, "error": problem,
        "path": str(engine.store.index_path),
        "quarantined": engine.store.index_quarantined,
        "level": "warn", "consumers": ["doctor", "brief", "health"]})
    return problem


def publish_round_health(engine, error_text: str, *, now: str, round_seq=None):
    """Write this round's snapshot, then date a recovery if one just happened.

    The order is the contract: the snapshot must be stored before the two ends
    can be called "published", and ``health.write()`` returns the normalized
    record it stored - so both ends of the comparison come from disk rather
    than from a caller's idea of what the file says.
    """
    previous = previous_snapshot(engine.store.health_path)
    written = engine.write_health(error=error_text, degraded=engine.degraded)
    row = recovery_row(previous, written, now=now, round_seq=round_seq)
    if row is not None:
        engine.store.log_event(row)
    return written
