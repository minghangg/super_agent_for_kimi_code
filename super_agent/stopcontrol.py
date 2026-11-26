"""Signal handling and exit-state flushing, shared by ``cli`` and ``launcher``.

Why this module exists (ISS-131 QA P1): ``cli.run_loop`` and the launcher each
grew their own ``_install_stop_handlers`` / ``_restore_handlers`` / ``_flush_state``.
Two copies of "how do we stop cleanly" is the shape that drifts one fix at a
time - and the launcher's copy had already drifted: it called
``asyncio.get_running_loop()`` before the loop existed, so its SIGTERM handler
was installed onto nothing and never fired.

The two callers need **different event types** (``threading.Event`` for the
synchronous loop, ``asyncio.Event`` for the coroutine host), so the function
takes whichever it is given and calls ``set()``. On Windows ``loop.add_signal_handler``
is unavailable; the fallback is ``signal.signal`` plus a thread-safe ``set``,
which is the same event either way.
"""
from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

SIGNALS = ("SIGINT", "SIGTERM")


def now() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def install_stop_handlers(stop_event) -> dict:
    """Route SIGINT/SIGTERM to ``stop_event.set()``; return what to restore.

    Works for both event kinds on purpose: the caller's stop condition is one
    object in both designs, and a second parameter saying "which kind" would let
    a caller pick the wrong branch silently.
    """
    previous: dict = {}
    loop = None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    def _set(*_a) -> None:
        stop_event.set()

    for name in SIGNALS:
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            previous[signum] = signal.getsignal(signum)
        except (OSError, ValueError):
            continue
        installed = False
        if loop is not None and isinstance(stop_event, asyncio.Event):
            try:
                loop.add_signal_handler(signum, _set)
                installed = True
            except (NotImplementedError, RuntimeError, ValueError, OSError):
                installed = False
        if not installed:
            try:
                if loop is not None:
                    signal.signal(signum,
                                  lambda *_a, _l=loop: _l.call_soon_threadsafe(_set))
                else:
                    signal.signal(signum, lambda *_a: _set())
            except (OSError, ValueError):
                # Not the main thread: nothing was installed, so nothing is
                # recorded as restorable.
                previous.pop(signum, None)
    return previous


def restore_handlers(previous: dict) -> None:
    """Put the handlers back. Never raises: this runs from a ``finally``."""
    loop = None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    for signum, handler in (previous or {}).items():
        if loop is not None:
            try:
                loop.remove_signal_handler(signum)
                continue
            except (NotImplementedError, RuntimeError, ValueError):
                pass
        try:
            signal.signal(signum, handler)
        except (OSError, ValueError, TypeError):
            pass


def flush_state(store) -> None:
    """Re-persist cursor and index so stopping between rounds leaves valid files.

    ``IndexRefused`` is the point of this guard: ``save_index(load_index())`` is
    the second path that writes an empty index over a file that could not be
    read. A Ctrl-C during the fault must leave the bytes alone, so the refusal
    is reported and the stop continues.
    """
    from . import store as store_mod

    try:
        store.save_cursor(store.load_cursor())
        store.save_index(store.load_index())
    except store_mod.IndexRefused as exc:
        print(f"warning: index not flushed on exit: {exc}", file=sys.stderr)
    except OSError as exc:
        print(f"warning: could not persist state on exit: {exc}", file=sys.stderr)


def mark_clean_stop(store, engine, *, reason: str, stalled: list | None = None) -> None:
    """Record a deliberate stop so it does not read as a crash.

    Two writes, both of which the launcher used to skip (QA P1-1):

    * ``health.json`` goes to ``running=false``. A killed process never gets
      here and leaves ``running=true``, which is exactly what ``doctor`` reports
      as ``crashed``;
    * ``run.stopped`` goes to the event stream, the same row ``cli run`` writes,
      so the two entry points produce one shape of history.

    ``stalled`` is passed in rather than assumed: a stop where a thread did not
    return is *not* clean, and flipping the flag then would hand the next
    launcher a directory a live thread still owns.
    """
    if stalled:
        return
    store.log_event({"type": "run.stopped", "at": now(), "reason": reason,
                     "entry": "launcher"})
    write_health = getattr(engine, "write_health", None)
    if callable(write_health):
        try:
            write_health(running=False)
        except (OSError, AttributeError) as exc:
            # An engine double without a store is a test shape, not a runtime
            # one; the flag still has to go down.
            print(f"warning: health write failed: {exc}", file=sys.stderr)
    # Unconditional: a caller that returns normally without touching the file
    # would otherwise leave running=true behind, which is exactly the "clean
    # stop looks like a crash" fault this function exists to remove.
    _force_snapshot(store)


def _force_snapshot(store) -> None:
    """Write ``running=false`` directly, for a caller with no engine snapshot.

    Reads the **raw** file, not ``health.read()``: that helper fills defaults for
    absent fields, so feeding its output back would erase ``last_loop_at`` - and
    without that stamp ``doctor`` cannot say "stopped cleanly" at all.
    """
    import json

    from . import health as health_mod

    path = store.health_path
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raw = {}
    except (OSError, ValueError):
        raw = {}
    raw["running"] = False
    raw["phase"] = "stopped"
    try:
        health_mod.write(path, raw)
    except OSError as exc:
        print(f"warning: health write failed: {exc}", file=sys.stderr)
