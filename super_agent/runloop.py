"""The resident round loop: ``cli run`` calls this, and only this, in a loop.

Why this module exists (ISS-131 仲裁 cid 7178844797): ``cli.py`` was over the
300-line line and the only honest way to bring it down was to move a real
subject out. "Run one round, repeat, stop cleanly" is that subject: it owns the
failure counter, the heartbeat, the stop reason and the exit code, and it needs
nothing from the command layer except the engine and the store.

Two behaviours are load-bearing and easy to lose in a move:

**A clean stop must not look like a crash.** The pid lock is about to go, so the
health snapshot is flipped to ``running=false``. A killed process never reaches
that line and leaves ``running=true`` - which is exactly what ``doctor`` reports.

**Heartbeat every round, unconditionally.** "Nothing changed, skip the write"
makes a live loop look stalled on the next patrol.
"""
from __future__ import annotations

import sys
import threading
import time

from . import stopcontrol

#: Consecutive failures that end the loop with exit code 1.
DEFAULT_MAX_FAILURES = 10

#: Sleep granularity, so a stop request lands within ~0.2 s.
WAIT_SLICE = 0.2


def now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def wait(stop_event: threading.Event, seconds: float, sleep) -> None:
    """Sleep in slices so a stop request lands within ~0.2s, not a full interval."""
    remaining = max(0.0, float(seconds))
    while remaining > 0 and not stop_event.is_set():
        chunk = min(remaining, WAIT_SLICE)
        sleep(chunk)
        remaining -= chunk


def run_loop(engine, store, *, interval: float = 60.0,
             max_failures: int = DEFAULT_MAX_FAILURES, max_rounds: int = 0,
             stop_event: threading.Event | None = None,
             sleep=time.sleep, now=now) -> dict:
    """Run ``engine.run_once()`` until stopped.

    A round counts as failed when it raises or when it reports errors; a clean
    round resets the counter. Every round overwrites the heartbeat with the
    latest ``{ts, round, ok, external_actions}``. ``stop_event`` (set by a
    signal or by a test) ends the loop with exit code 0 after the round in
    flight; ``max_failures`` consecutive failures end it with exit code 1.
    """
    stop = stop_event if stop_event is not None else threading.Event()
    interval = float(interval)
    max_failures = max(1, int(max_failures))
    rounds = 0
    consecutive = 0
    reason = "max_rounds"
    external = int(store.load_counters().get("external_actions") or 0)

    while max_rounds <= 0 or rounds < max_rounds:
        if stop.is_set():
            reason = "signal"
            break
        rounds += 1
        summary: dict = {}
        ok, err = True, ""
        try:
            summary = engine.run_once()
        except KeyboardInterrupt:            # no handler installed (embedded use)
            rounds -= 1
            stop.set()
            reason = "signal"
            break
        except Exception as exc:             # noqa: BLE001  a bad round is not fatal
            ok, err = False, f"{type(exc).__name__}: {exc}"
        else:
            errors = summary.get("errors") or []
            if errors:
                ok, err = False, "; ".join(str(e) for e in errors)

        external = int(store.load_counters().get("external_actions") or 0)
        try:
            store.save_heartbeat({"ts": now(), "round": rounds, "ok": ok,
                                  "external_actions": external})
        except OSError as exc:
            print(f"warning: heartbeat write failed: {exc}", file=sys.stderr)

        if ok:
            consecutive = 0
            print(f"round={rounds} ok={summary.get('mode')} "
                  f"issues={summary.get('issues', 0)} "
                  f"new_comments={summary.get('comments_new', 0)} "
                  f"processed={summary.get('processed', 0)} "
                  f"external_actions={external}", flush=True)
        else:
            consecutive += 1
            store.log_event({"type": "run.failed", "at": now(), "round": rounds,
                             "error": err})
            print(f"round={rounds} failed ({consecutive}/{max_failures}): {err}",
                  file=sys.stderr, flush=True)

        if consecutive >= max_failures:
            reason = "max_failures"
            break

        if max_rounds > 0 and rounds >= max_rounds:
            break

        try:
            wait(stop, interval, sleep)
        except KeyboardInterrupt:
            stop.set()
            reason = "signal"
            break
        if stop.is_set():
            reason = "signal"
            break

    exit_code = 1 if reason == "max_failures" else 0
    store.log_event({"type": "run.stopped", "at": now(), "reason": reason,
                     "rounds": rounds, "external_actions": external})
    # A clean stop must not look like a crash: the pid lock is about to go, so
    # flip the health snapshot to running=false. A killed process never gets
    # here and leaves running=true, which is exactly what doctor reports.
    write_health = getattr(engine, "write_health", None)
    if callable(write_health):
        try:
            write_health(running=False)
        except OSError as exc:
            print(f"warning: health write failed: {exc}", file=sys.stderr)
    stopcontrol.flush_state(store)
    return {"rounds": rounds, "consecutive_failures": consecutive,
            "external_actions": external, "reason": reason,
            "exit_code": exit_code}
