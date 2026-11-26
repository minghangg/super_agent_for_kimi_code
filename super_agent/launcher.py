"""The one daily entry point: ``python -m super_agent.launcher run`` (ISS-131 E).

What this module is, and what it is not: it is a **host**. It takes the engine
lock, builds the components through the same configuration chain ``cli`` uses,
runs them on one event loop, and stops them within a budget. It owns no
business logic - every notification, verdict, archive and outbox action stays
in ``engine.py`` and ``decide_watch.py``, called exactly as before.

Three boundaries are load-bearing:

**One state directory, one owner.** ``PidLock`` on ``store.pid_path`` is the
*same* lock ``cli run`` and ``cli once`` take. A second launcher, and the old
single-shot commands, are refused by the incumbent rather than by a new rule
nobody else enforces. ``wss_bus.acquire_lock()`` keeps ``wss_bus.lock`` doing
the same job for the WSS half, so an old ``wss_bus --daemon`` is refused too.

**The loop never runs a synchronous component.** B and C are synchronous to the
bottom of the stack (HTTP, session archive, read-back), so they reach the loop
only through :meth:`runner.Runner.trigger`, which uses ``asyncio.to_thread``.
Awaiting one inline would freeze A for as long as the network takes.

**A stall is reported as a stall.** ``to_thread`` cannot be cancelled; a thread
that outlives its budget marks the component ``stalled`` and keeps the lock.
Writing ``stopped`` there would invite a second launcher onto a directory a live
thread is still writing.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from . import launcher_cli
from .launcher_cli import Args
from . import launcher_wss
from . import runner as runner_mod
from . import stopcontrol
from .pidlock import LockError, PidLock


def _state(store, action: str, **fields) -> None:
    """Record a host event in the engine's own stream.

    No new file: ``events.jsonl`` already carries every other host observation,
    and a ``launcher.json`` under the state root would be a fourth place to look
    for one answer.
    """
    try:
        store.log_event({"type": f"launcher.{action}", **fields})
    except OSError:
        pass


def build_engine_component(cfg: dict, store, bus=None, *, session_api=None):
    """B (and C): one engine round, in a worker thread.

    The collection leg (C) and the consumption leg (B) are already two phases of
    ``run_round`` - ``run_issue_phase`` reads a listing and consumes the prefix
    in one place, so running them as two components would read every listing
    twice and move one cursor from two threads. The isolation the issue asks for
    is therefore provided where it belongs: the whole round runs in
    ``asyncio.to_thread``, which covers C's paging call and B's archive /
    ``session_info`` call alike.
    """
    from .cli import _bus, _engine, _session_api

    engine = _engine(cfg, store, bus if bus is not None else _bus(cfg),
                     session_api=_session_api(session_api))
    failure = engine.startup()
    if not failure.get("ok", True):
        raise RuntimeError(f"outbox recovery failed: {failure.get('error')}")
    return engine


def build_wss_service(cfg: dict, store=None):
    """Forwarder: the WSS wiring lives in :mod:`launcher_wss`."""
    return launcher_wss.build_wss_service(cfg, store)


def build_runner(cfg: dict, store, *, engine=None, bus=None, session_api=None,
                 enable_wss: bool | None = None,
                 wss_service=None, keep=None,
                 enable_decide_watch: bool | None = None,
                 enable_at: bool | None = None,
                 enable_reporter: bool | None = None,
                 enable_readapi: bool | None = None,
                 at_transport=None, supervisor: str | None = None
                 ) -> runner_mod.Runner:
    """Assemble the component set. Tests pass ``engine`` / ``wss_service``.

    ``keep`` is an out-parameter dict: the caller needs the engine object for the
    clean-stop write, and threading a second return value through every caller
    would change the shape for the ones that do not care.

    ``enable_wss=None`` means "enable it when it could actually run": the daemon
    needs six injections, and registering a component that can only raise would
    put a permanent red row in every health summary.

    The four legs added by ISS-132 follow the same rule (``None`` = decide from
    the config), because each one has a failure that must not be silent:

    * ``decide_watch`` refuses to start when its injections are incomplete, so it
      is registered **disabled with the reason in its row** rather than left out -
      a missing row reads as "this launcher does not have that leg", which is a
      different and wrong story;
    * ``at`` needs a port; a bind failure must be a visible row, not a traceback
      that takes the host down;
    * ``reporter`` writes a page into the state dir, which is always possible;
    * ``readapi`` binds loopback, and a port already held is a refusal to report.

    ``supervisor`` is the rescue override ``decide_watch`` takes (ISS-136 §三). It
    is threaded here rather than read from the config so ``launcher run
    --supervisor`` has a real production injection point; empty keeps the
    configuration chain, which is the normal case.
    """
    gh = cfg.get("github") or {}
    try:
        interval = float(gh.get("poll_interval_s") or 60)
    except (TypeError, ValueError):
        interval = 60.0
    budget = float((cfg.get("engine") or {}).get("launcher_stop_budget_s") or 30)

    if engine is None:
        engine = build_engine_component(cfg, store, bus, session_api=session_api)
    if keep is not None:
        keep["engine"] = engine

    rn = runner_mod.Runner()
    rn.register("engine", engine.run_once, kind=runner_mod.THREAD,
                interval=interval, budget=budget)

    if enable_wss is None:
        service, reason = build_wss_service(cfg, store)
        enable_wss = service is not None
    else:
        service, reason = (wss_service, "") if enable_wss else (None, "disabled")
        if enable_wss and service is None:
            service, reason = build_wss_service(cfg, store)
    if enable_wss and service is not None:
        rn.register("wss", service, kind=runner_mod.SERVICE, budget=budget)
    else:
        row = rn.register("wss", _noop_service, kind=runner_mod.SERVICE,
                          budget=budget, enabled=False)
        row.health.last_error = reason

    from .launcher_parts import host as host_parts

    host_parts.register_c2(cfg, store, rn, runner_mod=runner_mod, bus=bus,
                           interval=interval, budget=budget,
                           enable=enable_decide_watch, supervisor=supervisor)
    host_parts.register_at(cfg, store, rn, runner_mod=runner_mod, budget=budget,
                           enable=enable_at, transport=at_transport)
    host_parts.register_reporter(cfg, store, rn, runner_mod=runner_mod,
                                 interval=interval, budget=budget,
                                 enable=enable_reporter)
    host_parts.register_readapi(cfg, store, rn, runner_mod=runner_mod,
                                budget=budget, enable=enable_readapi,
                                get_health=rn.health, keep=keep)
    return rn




async def _noop_service(stop_event) -> None:
    """Placeholder for a disabled A: it must not look like a missing component."""
    await stop_event.wait()


async def serve(rn: runner_mod.Runner, stop: asyncio.Event) -> dict:
    """Start, wait for the stop signal, then stop. Returns the final health."""
    await rn.start()
    await stop.wait()
    return await rn.stop()


async def _serve_with_signals(rn: runner_mod.Runner) -> dict:
    """Install the stop handlers **on the loop that actually serves**.

    ``asyncio.get_running_loop()`` requires the loop to exist already, so
    installing before ``asyncio.run`` raises - and on Windows a SIGTERM handler
    is exactly what the stall budget is measured against. Installing inside the
    coroutine puts it on ``serve``'s own loop.
    """
    stop = asyncio.Event()
    previous = stopcontrol.install_stop_handlers(stop)
    try:
        return await serve(rn, stop)
    finally:
        stopcontrol.restore_handlers(previous)


def cmd_run(args) -> int:
    from . import cli as cli_mod

    built = Args(config=args.config, state_dir=args.state_dir, mode=args.mode,
                  role=args.role, source=args.source, allowlist=args.allowlist,
                  session_api=args.session_api,
                  accept_empty_index=args.accept_empty_index,
                  allow_empty_cursor=args.allow_empty_cursor)
    cfg, store = cli_mod._build(built)
    refusal = cli_mod._refuse_bad_cursor(built, cfg, store)
    if refusal is not None:
        return refusal

    lock = PidLock(store.pid_path, argv=["super_agent", "launcher", "run"])
    ok, info = lock.try_acquire()
    if not ok:
        print(f"engine.pid: refused to start - {lock.describe(info)}",
              file=sys.stderr)
        return 3
    if lock.took_over:
        print(f"engine.pid: took over a stale lock ({lock.describe(info)})",
              flush=True)

    rn = None
    held: dict = {}
    health: dict = {}
    try:
        rn = build_runner(cfg, store, enable_wss=args.wss, keep=held,
                          supervisor=args.supervisor)
        _state(store, "started", components=rn.names(),
               pid=os.getpid(), runner="launcher")
        health = asyncio.run(_serve_with_signals(rn))
    except LockError as exc:
        print(f"engine.pid: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001  a failed host is a reportable state
        _state(store, "failed", error=f"{type(exc).__name__}: {exc}")
        print(f"launcher failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        stalled = bool(health.get("stalled"))
        if not stalled:
            lock.release()

    stalled = list(health.get("stalled") or [])
    engine = held.get("engine")
    if not stalled and engine is not None:
        # QA P1-1: ``cli run`` flips running=false and writes ``run.stopped`` on
        # a clean exit; without it the launcher leaves running=true behind, and
        # ``doctor`` then reports a crash that never happened. The two entry
        # points must produce the same history.
        stopcontrol.mark_clean_stop(store, engine, reason="signal")
    stopcontrol.flush_state(store)
    _state(store, "stalled" if stalled else "stopped",
           stalled=stalled, components=health.get("components") or {})
    print(f"launcher {'stalled' if stalled else 'stopped'}: "
          f"phase={health.get('phase')} stalled={stalled or 'none'}")
    if stalled:
        # The lock was kept on purpose: a live thread still owns the state dir,
        # so the next start must be refused until that thread returns.
        print("engine.pid: kept - a component thread did not return in budget",
              file=sys.stderr)
        return 4
    return 0


def cmd_status(args) -> int:
    import json

    from . import cli as cli_mod
    from . import health as health_mod

    built = Args(config=args.config, state_dir=args.state_dir,
                  accept_empty_index=False)
    cfg, store = cli_mod._build(built)
    snap = health_mod.read(store.health_path)
    lock = PidLock(store.pid_path)
    info = lock.read()
    out = {"lock": info, "engine_health": snap}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if info else 1


def build_parser():
    """The flag surface lives in :mod:`launcher_cli` (see its docstring)."""
    return launcher_cli.build_parser()


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        return cmd_run(args)
    return cmd_status(args)


if __name__ == "__main__":
    sys.exit(main())
