"""Command line entry point.

    python -m super_agent.cli doctor
    python -m super_agent.cli once --mode observe
    python -m super_agent.cli run --mode observe --interval 60
    python -m super_agent.cli status
    python -m super_agent.cli replay --issue 42

``run`` is the resident form of ``once``: the same round, repeated. It writes a
one-line heartbeat per round (``state/run_heartbeat.json``), keeps going when a
round fails, and gives up only after ``--max-failures`` consecutive failures.
SIGINT/SIGTERM stop it after the round in flight, with exit code 0.

Exit codes: 0 success, 1 a check or round failed (or ``run`` gave up after too
many consecutive failures), 2 bad arguments.
"""
from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from datetime import datetime, timezone

from . import config as config_mod
from .bus_client import BusClient
from .engine import build_engine
from .github_source import NullSource
from .store import Store

NONE_TEXT = "无"
DEFAULT_MAX_FAILURES = 10
_WAIT_SLICE = 0.2


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _build(args) -> tuple[dict, Store]:
    overrides: dict = {}
    if args.mode:
        overrides["mode"] = args.mode
    if args.role:
        overrides["role"] = args.role
    if args.state_dir:
        overrides["state_dir"] = args.state_dir
    if args.source:
        overrides.setdefault("github", {})["source"] = args.source
    cfg = config_mod.load(args.config, overrides)
    store_cfg = cfg.get("store") or {}
    store = Store(config_mod.state_root(cfg, args.state_dir),
                  max_event_lines=int(store_cfg.get("max_event_lines") or 10_000),
                  max_outbox_lines=int(store_cfg.get("max_outbox_lines") or 5_000))
    return cfg, store


def _bus(cfg: dict) -> BusClient:
    b = cfg.get("bus") or {}
    return BusClient(b.get("host") or "127.0.0.1", int(b.get("port") or 58800),
                     float(b.get("timeout_s") or 2.0))


def cmd_once(args) -> int:
    cfg, store = _build(args)
    engine = build_engine(cfg, store, _bus(cfg))
    summary = engine.run_once()
    print(f"mode={summary['mode']} issues={summary['issues']} "
          f"new_comments={summary['comments_new']} processed={summary['processed']} "
          f"needs_person={summary['needs_person']} "
          f"external_actions={summary['external_actions']}")
    for err in summary["errors"]:
        print(f"  warning: {err}", file=sys.stderr)
    return 1 if summary["errors"] else 0


# ----------------------------------------------------------------------- run
def _wait(stop_event: threading.Event, seconds: float, sleep) -> None:
    """Sleep in slices so a stop request lands within ~0.2s, not a full interval."""
    remaining = max(0.0, float(seconds))
    while remaining > 0 and not stop_event.is_set():
        chunk = min(remaining, _WAIT_SLICE)
        sleep(chunk)
        remaining -= chunk


def _install_stop_handlers(stop_event: threading.Event) -> dict:
    previous = {}
    for name in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, lambda *_: stop_event.set())
        except (OSError, ValueError):        # not the main thread
            previous.pop(signum, None)
    return previous


def _restore_handlers(previous: dict) -> None:
    for signum, handler in previous.items():
        try:
            signal.signal(signum, handler)
        except (OSError, ValueError):
            pass


def _flush_state(store: Store) -> None:
    """Re-persist cursor and index so stopping between rounds leaves valid files."""
    try:
        store.save_cursor(store.load_cursor())
        store.save_index(store.load_index())
    except OSError as exc:
        print(f"warning: could not persist state on exit: {exc}", file=sys.stderr)


def run_loop(engine, store: Store, *, interval: float = 60.0,
             max_failures: int = DEFAULT_MAX_FAILURES, max_rounds: int = 0,
             stop_event: threading.Event | None = None,
             sleep=time.sleep, now=_now) -> dict:
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
            _wait(stop, interval, sleep)
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
    _flush_state(store)
    return {"rounds": rounds, "consecutive_failures": consecutive,
            "external_actions": external, "reason": reason,
            "exit_code": exit_code}


def cmd_run(args) -> int:
    cfg, store = _build(args)
    engine = build_engine(cfg, store, _bus(cfg))
    interval = args.interval
    if interval is None:
        interval = float((cfg.get("github") or {}).get("poll_interval_s") or 60)
    stop_event = threading.Event()
    previous = _install_stop_handlers(stop_event)
    try:
        result = run_loop(engine, store, interval=interval,
                          max_failures=args.max_failures,
                          max_rounds=args.max_rounds, stop_event=stop_event)
    finally:
        _restore_handlers(previous)
    print(f"run stopped: reason={result['reason']} rounds={result['rounds']} "
          f"consecutive_failures={result['consecutive_failures']} "
          f"external_actions={result['external_actions']}")
    return result["exit_code"]


def cmd_status(args) -> int:
    cfg, store = _build(args)
    engine = build_engine(cfg, store, _bus(cfg), source=NullSource())
    report = engine.status()
    print(f"status  role={engine.role} mode={report['mode']} "
          f"external_actions={report['external_actions']}")
    if not report["items"]:
        print(NONE_TEXT)
        return 0
    for item in report["items"]:
        print(f"- issue #{item['issue']} comment {item['comment_id']} "
              f"({item['reason']}) at {item['at']}")
    return 0


def cmd_doctor(args) -> int:
    cfg, store = _build(args)
    engine = build_engine(cfg, store, _bus(cfg))
    report = engine.doctor()
    worst = "ok"
    width = max((len(c["name"]) for c in report["checks"]), default=4)
    print(f"doctor  role={engine.role} mode={report['mode']} "
          f"external_actions={report['external_actions']}")
    for check in report["checks"]:
        mark = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}[check["level"]]
        print(f"[{mark}] {check['name'].ljust(width)}  {check['detail']}")
        if check["level"] == "fail" and worst == "ok":
            worst = "fail"
        elif check["level"] == "warn" and worst == "ok":
            worst = "warn"
    print(f"result: {worst}")
    return 0 if worst != "fail" else 1


def cmd_replay(args) -> int:
    cfg, store = _build(args)
    engine = build_engine(cfg, store, _bus(cfg))
    try:
        out = engine.replay(args.issue)
    except Exception as exc:  # noqa: BLE001  report, do not dump a traceback
        print(f"replay failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"issue=#{out['issue']} state={out['state']} "
          f"comments={out['comments']} external_actions={out['external_actions']}")
    return 0


# ------------------------------------------------------------------- arguments
def _positive_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return value


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return value


def _non_negative_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return value


def _non_empty_text(text: str) -> str:
    value = str(text).strip()
    if not value:
        raise argparse.ArgumentTypeError("must not be empty")
    return value


def _add_common(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
    """Shared options. On subcommands they use SUPPRESS so they do not clobber
    a value given before the subcommand (``--mode`` works on either side)."""
    default = argparse.SUPPRESS if suppress else None
    parser.add_argument("--config", default=default, help="path to a JSON config file")
    parser.add_argument("--state-dir", default=default,
                        help="override the state directory")
    parser.add_argument("--mode", choices=list(config_mod.MODES), default=default,
                        help="observe (default) or assist")
    parser.add_argument("--role", type=_non_empty_text, default=default,
                        help="role name for this run (default: config's role, "
                             "else supervisor)")
    parser.add_argument("--source", choices=list(config_mod.SOURCES), default=default,
                        help="override github.source (none|fixture|live)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="super_agent",
                                     description="Deterministic task engine (M0 skeleton)")
    _add_common(parser, suppress=False)
    sub = parser.add_subparsers(dest="command", required=True)

    p_once = sub.add_parser("once", help="run exactly one round and exit")
    _add_common(p_once, suppress=True)
    p_once.set_defaults(func=cmd_once)

    p_run = sub.add_parser("run", help="run rounds until SIGINT/SIGTERM")
    _add_common(p_run, suppress=True)
    p_run.add_argument("--interval", type=_positive_float, default=None,
                       help="seconds between rounds (default: github.poll_interval_s)")
    p_run.add_argument("--max-failures", type=_positive_int,
                       default=DEFAULT_MAX_FAILURES,
                       help="give up after N consecutive failed rounds "
                            f"(default {DEFAULT_MAX_FAILURES})")
    p_run.add_argument("--max-rounds", type=_non_negative_int, default=0,
                       help="stop after N rounds, 0 = run forever")
    p_run.set_defaults(func=cmd_run)

    p_status = sub.add_parser("status", help="show only items that need a person")
    _add_common(p_status, suppress=True)
    p_status.set_defaults(func=cmd_status)

    p_doctor = sub.add_parser("doctor", help="check bus, source, cursor, assignments")
    _add_common(p_doctor, suppress=True)
    p_doctor.set_defaults(func=cmd_doctor)

    p_replay = sub.add_parser("replay", help="rebuild one issue's index entry")
    _add_common(p_replay, suppress=True)
    p_replay.add_argument("--issue", type=int, required=True)
    p_replay.set_defaults(func=cmd_replay)

    for sub_parser in (p_once, p_run, p_status, p_doctor, p_replay):
        sub_parser.set_defaults(_has_common=True)

    return parser


def _ensure_common_defaults(args) -> None:
    for name in ("config", "state_dir", "mode", "role", "source"):
        if not hasattr(args, name):
            setattr(args, name, None)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    _ensure_common_defaults(args)
    try:
        return args.func(args)
    except config_mod.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
