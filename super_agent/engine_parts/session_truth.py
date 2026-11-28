"""Session truth: resolving the lifecycle stream and reading it without guessing.

Split out of ``engine.py`` under ``rules/charters/worker.md`` §3.1.1 (a touched
module over the line must shed at least 5%). The seam is the one
``engine_parts.close`` and ``engine_parts.source_faults`` already use: **pure
decisions live here, host state stays in the engine**. Nothing in this module
holds a reference to ``Engine``, mutates a flag, or writes a file; the caller
passes the readers in and applies the answer.

Four questions, and each has exactly one answer:

* **Where is the stream?** :func:`resolve_paths` - never guessed from the
  working directory (ISS-79). Undeclared and misdeclared are different: the
  first is a configured state, the second is a fault.
* **Can it be trusted this round?** :func:`stream_fault` - absent and
  undecodable are kept apart because the operator's next action differs: one
  restores a file, the other fixes an encoding (ISS-79 P1-1).
* **What does ``sessions.json`` say?** :func:`read_session_map` - a read failure
  is returned as a sentence, never folded into ``{}``, or a bound AT/PM/QA looks
  like nobody holds a key (ISS-130 addendum 2).
* **What does the stream say?** :func:`replay_state` - ``{}`` on a broken stream
  is deliberate: "cannot tell" must not become a new way to fail closed.
"""
from __future__ import annotations

from pathlib import Path

from .. import session_events
from . import session_keys as session_key_mod


def resolve_paths(cfg: dict) -> tuple[Path, Path]:
    """``(events, sessions)``; raises ``SessionStreamNotConfigured`` when undeclared.

    ISS-79: the stream is never guessed from the working directory. A config
    that declares nothing has no session truth, and a config that declares a
    path the engine cannot find is a fault the round must report - the two
    cases are deliberately kept apart here.
    """
    engine_cfg = cfg.get("engine") or {}
    events = session_events.resolve_events_path(
        cfg, engine_cfg.get("session_events_path") or None)
    sessions = session_events.resolve_sessions_path(cfg)
    return events, sessions


def stream_fault(events: Path, now: str, *, read_stream=None) -> dict | None:
    """The degraded-round record for a **declared** stream, or ``None``.

    Four cases, and only the last two are faults:

    * declared and readable - ``None``, the round is unaffected;
    * declared but no segment on disk - ``read_stream`` reports ``missing``;
    * declared, present, but not UTF-8 - ``read_stream`` raises
      :class:`session_events.StreamUnreadable`;
    * any other ``OSError``.

    The exception is caught **here**, not left to bubble out of the round: an
    escaping exception skipped the round's own ``write_health``, so
    ``health.json`` kept the previous, healthy snapshot while every round
    failed (ISS-79 P1-1). The caller applies the returned record - this
    function never touches ``engine.degraded``.

    The judgement uses ``segments()`` through ``read_stream``, never
    ``path.is_file()``: a stream whose current file was rotated to ``.1`` is
    still readable, and calling that missing would degrade a healthy engine
    once per rotation.

    Returns ``{"error": str, "row": dict}``; the row is the event to log.
    """
    read = read_stream or session_events.read_stream
    try:
        rows, missing = read(events)
    except session_events.StreamUnreadable as exc:
        error = f"session stream not decodable as UTF-8: {exc}"
        return {"error": error,
                "row": {"type": "session.stream_unreadable", "at": now,
                        "path": str(events), "rows": 0, "encoding": "utf-8",
                        "error": error, "level": "warn",
                        "consumers": ["doctor", "brief", "health"]}}
    except OSError as exc:
        error = f"session stream unreadable: {type(exc).__name__}: {exc}"
        return {"error": error,
                "row": {"type": "session.stream_unreadable", "at": now,
                        "path": str(events), "rows": 0, "error": error,
                        "level": "warn",
                        "consumers": ["doctor", "brief", "health"]}}
    if not missing:
        return None
    error = f"session stream missing: {events}"
    return {"error": error,
            "row": {"type": "session.stream_missing", "at": now,
                    "path": str(events), "rows": len(rows), "error": error}}


def read_session_map(path, *, reader=None) -> tuple[dict, str]:
    """``(map, problem)`` for ``sessions.json`` - one read, one meaning.

    Five call sites used to do ``json.loads(read_text_shared(path))`` inside a
    ``try`` that folded **every** failure into ``data = {}``. Two very
    different states came out identical: a genuinely empty ledger, and a ledger
    the engine could not read. The second one silently emptied the role key
    tables, so a bound AT/PM/QA looked like nobody had a key (ISS-130
    addendum 2).

    So the problem sentence is returned, never swallowed. A caller that only
    needs the explicit ledger binding ignores it; the ones whose answer would
    otherwise read as "no keys" log one event per round per site, and a
    genuinely empty file answers ``({}, "")`` - no fault, empty table.
    """
    load = reader or session_key_mod.read_session_map
    return load(Path(path))


def replay_state(events_path) -> dict:
    """Replayed lifecycle state, ``{}`` when the stream cannot be read.

    ``None``-free on purpose: an unreadable stream returns ``{}`` and the
    callers treat "cannot tell" as "no evidence to exclude a key". That is the
    same weakness the key check already has; it must not become a new way to
    fail closed on a healthy thread.
    """
    try:
        return session_events.replay(session_events.load_events(events_path))
    except Exception:  # noqa: BLE001  a broken stream must not block a verdict
        return {}


def replay_sessions(events_path) -> dict:
    """``sid -> (role, issue, assignment, profile)`` as of now, plus its facts.

    Reads the workbench lifecycle stream (``tools/sess_events.py:emit`` wrote
    it). Strictly read-only: the engine's own ``events.jsonl`` is a different
    stream and stays untouched.
    """
    events, missing = session_events.read_stream(events_path)
    state = session_events.replay(events)
    state["events_path"] = str(events_path)
    state["missing"] = missing
    state["keys_active"] = session_events.active_keys(state)
    return state


def replay_as_of(events_path, at) -> dict:
    """:func:`replay_sessions` at a point in time (``at=None`` means now)."""
    events, missing = session_events.read_stream(events_path)
    state = session_events.replay(events, at)
    state["events_path"] = str(events_path)
    state["missing"] = missing
    state["keys_active"] = session_events.active_keys(state)
    return state


def map_problem_row(where: str, problem: str, now: str) -> dict | None:
    """The ``session_map.unreadable`` row for a failed read, or ``None``.

    ``None`` for a genuinely empty file: "no rows" is not a fault, and writing a
    row for it would turn every fresh state dir into an alarm. The distinction
    is the caller's ``problem`` sentence - an empty file answers ``""``.
    """
    if not problem:
        return None
    return {"type": "session_map.unreadable", "at": now,
            "where": where, "detail": problem}
