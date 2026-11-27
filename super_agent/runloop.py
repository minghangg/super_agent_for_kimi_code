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
from .engine_parts import fault_report as fault_report_mod
from .engine_parts import stop_notice as stop_notice_mod

#: Consecutive failures that end the loop with exit code 1.
#: Three, not ten (user ruling 2026-11-28: an engine that keeps running while it
#: keeps failing is worse than one that stops). Network blips are the exception
#: the counter buys: a couple of them pass, a third in a row is a broken link.
DEFAULT_MAX_FAILURES = 3

#: Hard ceiling on one failure streak, in seconds. ``fail_loud`` §2.2: rounds and
#: wall clock are both honoured, whichever comes first - raising
#: ``--max-failures`` cannot buy an hour of retrying a broken link.
FAIL_BUDGET_S = 600.0

#: Sleep granularity, so a stop request lands within ~0.2 s.
WAIT_SLICE = 0.2


def now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _over_budget(started: str, current: str) -> bool:
    """Has one failure streak outlived :data:`FAIL_BUDGET_S`?

    ``started`` is the ISO stamp of the streak's first failure; an empty or
    unparseable stamp answers ``False`` (a clock we cannot read must not invent
    a stop - the round limit still applies).
    """
    if not started:
        return False
    from datetime import datetime
    try:
        first = datetime.fromisoformat(started)
        latest = datetime.fromisoformat(current)
    except ValueError:
        return False
    return (latest - first).total_seconds() >= FAIL_BUDGET_S


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
    streak_started = ""
    reason = "max_rounds"
    external = int(store.load_counters().get("external_actions") or 0)

    while max_rounds <= 0 or rounds < max_rounds:
        if stop.is_set():
            reason = "signal"
            break
        rounds += 1
        summary: dict = {}
        ok, err, trace = True, "", ""
        try:
            summary = engine.run_once()
        except KeyboardInterrupt:            # no handler installed (embedded use)
            rounds -= 1
            stop.set()
            reason = "signal"
            break
        except Exception as exc:             # noqa: BLE001  a bad round is not fatal
            ok, err = False, f"{type(exc).__name__}: {exc}"
            # The reason, not the symptom: a crash is recorded with its frames
            # so the next reader does not have to reproduce it (2026-11-28).
            trace = fault_report_mod.describe(exc, frames=8)
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
            if consecutive == 0:
                streak_started = now()  # first failure of this streak
            consecutive += 1
            store.log_event({"type": "run.failed", "at": now(), "round": rounds,
                             "error": err, "trace": trace})
            print(f"round={rounds} failed ({consecutive}/{max_failures}): {err}",
                  file=sys.stderr, flush=True)
            if trace:
                print(trace, file=sys.stderr, flush=True)

        # A fault the engine cannot retry its way out of ends the run now: the
        # loop's failure counter exists for transient trouble, and using it for
        # a 401 would turn one loud error into three quiet ones. This sits
        # **after** the round's bookkeeping on purpose - a run that stops for a
        # fatal must not print ``consecutive_failures=0``, which reads as "no
        # failure happened" in the one line an operator sees first.
        if summary.get("source_fatal"):
            store.log_event({"type": "run.fatal", "at": now(), "round": rounds,
                             "error": err, "trace": trace})
            reason = "fatal"
            break
        if consecutive >= max_failures or _over_budget(streak_started, now()):
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

    exit_code = 1 if reason in ("max_failures", "fatal") else 0
    if reason in ("max_failures", "fatal"):
        # A stop that nobody hears is not fail-loud, it is fail-quiet: the
        # process is gone and only health.json knows why. One injection, on the
        # leg the consumer already watches, before the final record is written.
        stop_notice_mod.notice(engine, store, reason=reason, error=err, now=now)
    store.log_event({"type": "run.stopped", "at": now(), "reason": reason,
                     "rounds": rounds, "external_actions": external})
    # A clean stop must not look like a crash: the pid lock is about to go, so
    # flip the health snapshot to running=false. A killed process never gets
    # here and leaves running=true, which is exactly what doctor reports.
    write_health = getattr(engine, "write_health", None)
    if callable(write_health):
        # A stop caused by a failure names it: ``health.json`` is what the next
        # person opens first, and "running=false" alone says nothing about why.
        last_error = err if reason in ("fatal", "max_failures") else ""
        try:
            write_health(running=False, error=last_error,
                         degraded=bool(last_error))
        except OSError as exc:
            print(f"warning: health write failed: {exc}", file=sys.stderr)
    stopcontrol.flush_state(store)
    return {"rounds": rounds, "consecutive_failures": consecutive,
            "external_actions": external, "reason": reason,
            "exit_code": exit_code}
