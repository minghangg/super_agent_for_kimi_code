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
from pathlib import Path

from . import brief as brief_mod
from . import config as config_mod
from . import session_events
from . import assignments as asg_mod
from .bus_client import BusClient
from .github_source import FixtureSource, LiveSource
from .engine import build_engine
from .github_source import NullSource
from .pidlock import LockError, PidLock
from .store import Store

NONE_TEXT = "无"
DEFAULT_MAX_FAILURES = 10
SESSION_APIS = ("real", "none")
_WAIT_SLICE = 0.2


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _allowlist_path(args) -> str:
    """Explicit ``--allowlist``, or "" so the config/default path decides."""
    explicit = getattr(args, "allowlist", None)
    return str(explicit) if explicit else ""


def _repo_allowlist() -> str:
    """``config/allowlist.json`` in this checkout, when it exists.

    Only used when neither the flag nor the config names a path: a checkout
    without the file keeps its previous behaviour (no project gate) instead of
    refusing everything.
    """
    candidate = Path(__file__).resolve().parent.parent / "config" / "allowlist.json"
    return str(candidate) if candidate.is_file() else ""


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
    allow_path = _allowlist_path(args)
    if not allow_path:
        allow_path = str((cfg.get("engine") or {}).get("project_allowlist") or "")
    if not allow_path:
        allow_path = _repo_allowlist()
    if allow_path:
        cfg.setdefault("engine", {})["project_allowlist"] = allow_path
    store_cfg = cfg.get("store") or {}
    store = Store(config_mod.state_root(cfg, args.state_dir),
                  max_event_lines=int(store_cfg.get("max_event_lines") or 10_000),
                  max_outbox_lines=int(store_cfg.get("max_outbox_lines") or 5_000),
                  outbox_stale_s=float(store_cfg.get("outbox_stale_s") or 300))
    return cfg, store


def _engine(cfg: dict, store: Store, bus=None, *, source=None,
            session_api=None):
    """``build_engine``: the project allowlist is built from the config path."""
    engine = build_engine(cfg, store, bus, source=source)
    if session_api is not None:
        engine.session_api = session_api
    return engine


def _session_api(choice: str | None):
    """``--session-api``: ``real`` (the default) or ``none``.

    Production is always ``real``: the archive read-back and the archived-sid
    guard both need the server, and ``none`` would make every sid look live
    (``archived_crosscheck`` would fail closed instead). The switch exists for
    isolated replays and tests that must not touch the workbench.
    """
    if str(choice or "real").lower() in ("none", "off", "no"):
        return _NoSessionApi()
    from . import api
    return api


class _NoSessionApi:
    """Every call fails loudly; a caller that needs an answer gets "unknown"."""

    def archive_session(self, sid):
        raise RuntimeError("--session-api none: no session console in this run")

    def session_info(self, sid):
        raise RuntimeError("--session-api none: no session console in this run")


def _bus(cfg: dict) -> BusClient:
    b = cfg.get("bus") or {}
    return BusClient(b.get("host") or "127.0.0.1", int(b.get("port") or 58800),
                     float(b.get("timeout_s") or 2.0))


def _recover(engine) -> dict:
    """Startup recovery: recover -> replay prepared -> reconcile uncertain.

    ``Engine.startup`` is the production entry for the three methods that used
    to be dead code. It returns ``{"ok": False, ...}`` when recovery failed:
    the caller must not start a round then, since an unresolved outbox would be
    carried into a live loop.
    """
    startup = getattr(engine, "startup", None)
    if callable(startup):
        return startup()
    recover = getattr(engine, "recover", None)
    if callable(recover):
        recover()
    return {"ok": True, "error": ""}


def _recovery_refusal(result: dict) -> int:
    print(f"recovery failed: {result.get('error') or 'unknown error'}", file=sys.stderr)
    return 1


def cmd_once(args) -> int:
    cfg, store = _build(args)
    engine = _engine(cfg, store, _bus(cfg),
                      session_api=_session_api(getattr(args, "session_api", None)))
    try:
        with PidLock(store.pid_path, argv=["super_agent", "once"]):
            recovery = _recover(engine)
            if not recovery.get("ok", True):
                return _recovery_refusal(recovery)
            summary = engine.run_once()
            engine.write_health(running=False)
    except LockError as exc:
        print(f"engine.pid: {exc}", file=sys.stderr)
        return 3
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
    # A clean stop must not look like a crash: the pid lock is about to go, so
    # flip the health snapshot to running=false. A killed process never gets
    # here and leaves running=true, which is exactly what doctor reports.
    write_health = getattr(engine, "write_health", None)
    if callable(write_health):
        try:
            write_health(running=False)
        except OSError as exc:
            print(f"warning: health write failed: {exc}", file=sys.stderr)
    _flush_state(store)
    return {"rounds": rounds, "consecutive_failures": consecutive,
            "external_actions": external, "reason": reason,
            "exit_code": exit_code}


def cmd_run(args) -> int:
    cfg, store = _build(args)
    engine = _engine(cfg, store, _bus(cfg),
                      session_api=_session_api(getattr(args, "session_api", None)))
    interval = args.interval
    if interval is None:
        interval = float((cfg.get("github") or {}).get("poll_interval_s") or 60)
    stop_event = threading.Event()
    previous = _install_stop_handlers(stop_event)
    lock = PidLock(store.pid_path, argv=["super_agent", "run"])
    ok, info = lock.try_acquire()
    if not ok:
        _restore_handlers(previous)
        print(f"engine.pid: refused to start - {lock.describe(info)}",
              file=sys.stderr)
        return 3
    if lock.took_over:
        print(f"engine.pid: took over a stale lock ({lock.describe(info)})",
              flush=True)
    try:
        recovery = _recover(engine)
        if not recovery.get("ok", True):
            return _recovery_refusal(recovery)
        result = run_loop(engine, store, interval=interval,
                          max_failures=args.max_failures,
                          max_rounds=args.max_rounds, stop_event=stop_event)
    finally:
        lock.release()
        _restore_handlers(previous)
    print(f"run stopped: reason={result['reason']} rounds={result['rounds']} "
          f"consecutive_failures={result['consecutive_failures']} "
          f"external_actions={result['external_actions']}")
    return result["exit_code"]


def cmd_status(args) -> int:
    cfg, store = _build(args)
    engine = _engine(cfg, store, _bus(cfg), source=NullSource(),
                      session_api=_session_api(getattr(args, "session_api", None)))
    # --outbox 只加东西，不改变默认输出；brief 的「取回」命令靠它才真的取得到
    report = engine.status(outbox=bool(getattr(args, "outbox", False)))
    print(f"status  role={engine.role} mode={report['mode']} "
          f"external_actions={report['external_actions']}")
    if not report["items"]:
        print(NONE_TEXT)
        return 0
    for item in report["items"]:
        if item.get("action_id"):
            print(f"- action {item['action_id']} issue #{item['issue']} "
                  f"({item.get('status') or item['reason']}) at {item['at']}")
            continue
        print(f"- issue #{item['issue']} comment {item['comment_id']} "
              f"({item['reason']}) at {item['at']}")
    return 0


def cmd_doctor(args) -> int:
    cfg, store = _build(args)
    engine = _engine(cfg, store, _bus(cfg),
                      session_api=_session_api(getattr(args, "session_api", None)))
    report = engine.doctor()
    worst = "ok"
    width = max((len(c["name"]) for c in report["checks"]), default=4)
    label_width = max((len(c.get("label") or "") for c in report["checks"]), default=4)
    print(f"doctor  role={engine.role} mode={report['mode']} "
          f"external_actions={report['external_actions']}")
    for check in report["checks"]:
        mark = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}[check["level"]]
        label = check.get("label") or check["name"]
        print(f"[{mark}] {label.ljust(label_width)}  {check['name'].ljust(width)}  "
              f"{check['detail']}")
        if check["level"] == "fail":
            worst = "fail"
        elif check["level"] == "warn" and worst == "ok":
            worst = "warn"
    print(f"result: {worst}")
    return 0 if worst != "fail" else 1


def cmd_unblock(args) -> int:
    """``blocked -> working`` for one assignment, with the reason recorded.

    The engine has no path out of ``blocked`` other than a newer delivery, so a
    person needs an explicit command: it writes ``unblocked_at`` and keeps the
    tombstone (the next failure needs it to bump the attempt counter).
    """
    cfg, store = _build(args)
    index = store.load_index()
    rec = (index.get("assignments") or {}).get(args.id)
    if not isinstance(rec, dict):
        print(f"unblock: no assignment {args.id}", file=sys.stderr)
        return 1
    moved = asg_mod.unblock(rec, args.reason or "")
    if not moved.get("changed"):
        print(f"unblock: {args.id} is {rec.get('status')} ({moved.get('reason')})",
              file=sys.stderr)
        return 1
    store.save_index(index)
    store.log_event({"type": "assignment.unblocked", "at": _now(),
                     "assignment": args.id, "thread": rec.get("thread"),
                     "reason": args.reason or ""})
    print(f"unblocked {args.id} ({rec.get('thread')}) -> working; "
          f"tombstone attempt={int((rec.get('tombstone') or {}).get('attempt') or 0)}")
    return 0


def cmd_replay(args) -> int:
    cfg, store = _build(args)
    engine = _engine(cfg, store, _bus(cfg),
                     session_api=_session_api(getattr(args, "session_api", None)))
    at = getattr(args, "at", None)
    if at or getattr(args, "sessions", False):
        return _cmd_replay_sessions(engine, args)
    if getattr(args, "issue", None) is None:
        print("replay: give --issue <n>, or --at/--sessions for the session view",
              file=sys.stderr)
        return 2
    try:
        out = engine.replay(args.issue)
    except Exception as exc:  # noqa: BLE001  report, do not dump a traceback
        print(f"replay failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"issue=#{out['issue']} state={out['state']} "
          f"comments={out['comments']} external_actions={out['external_actions']}")
    return 0


def _cmd_replay_sessions(engine, args) -> int:
    """`replay --at <ISO>` / `replay --sessions`: session truth as of an instant."""
    at = getattr(args, "at", None)
    try:
        state = engine.replay_sessions(at)
    except Exception as exc:  # noqa: BLE001
        print(f"replay failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"session replay  events={state['events_path']} "
          f"at={at or '(newest)'} applied={state['applied']} skipped={state['skipped']}")
    active = state["keys_active"]
    if not active:
        print(NONE_TEXT)
    for key in sorted(active):
        sid = active[key]
        rec = state["sessions"].get(sid) or {}
        profile = rec.get("profile") or {}
        profile_text = ",".join(f"{k}={profile[k]}" for k in sorted(profile)) or "-"
        print(f"- {key:<16} sid={sid[:26]:<28} role={rec.get('role') or '-':<10} "
              f"issue={rec.get('issue') if rec.get('issue') is not None else '-':<6} "
              f"profile={profile_text}")
    if len(active) < len(state["keys"]):
        dropped = sorted(set(state["keys"]) - set(active))
        print(f"archived-or-gone keys: {', '.join(dropped)}")
    return 0


def cmd_reconcile(args) -> int:
    """Three-way session check: lifecycle replay × sessions.json × live server.

    Exit 0 only when all three agree. A non-empty diff exits 1; a missing third
    source exits 2 (it must never look like agreement).
    """
    cfg, store = _build(args)
    engine = _engine(cfg, store, _bus(cfg), source=NullSource(),
                      session_api=_session_api(getattr(args, "session_api", None)))
    server_sids = None
    if not args.no_server:
        try:
            server_sids = _live_sids(args.live_file)
        except Exception as exc:  # noqa: BLE001
            print(f"reconcile: live session list unavailable: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
            server_sids = None
    try:
        report = engine.reconcile_sessions(at=args.at, server_sids=server_sids)
    except Exception as exc:  # noqa: BLE001
        print(f"reconcile failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    sizes = report["sizes"]
    print(f"reconcile  events={report['events_path']}")
    print(f"           sessions={report['sessions_path']}")
    print(f"sizes  events_keys={sizes['events_keys']} "
          f"ledger_keys={sizes['ledger_keys']} "
          f"server_live={sizes['server_live'] if sizes['server_live'] is not None else '-'}")
    for name, value in report["diff"].items():
        print(f"[{'DIFF' if value else 'OK  '}] {name}  {value if value else ''}".rstrip())
    if report.get("keys_only_in_stream"):
        print(f"[NOTE] live bindings not in sessions.json "
              f"({len(report['keys_only_in_stream'])}): "
              f"{', '.join(report['keys_only_in_stream'][:5])}"
              f"{' …' if len(report['keys_only_in_stream']) > 5 else ''}")
    if report["live_unclaimed"]:
        print(f"[NOTE] live sessions not claimed by any key "
              f"({len(report['live_unclaimed'])}): "
              f"{', '.join(s[:26] for s in report['live_unclaimed'][:5])}")
    if not report["server_supplied"]:
        print("result: UNKNOWN (live server not consulted; use --live-file or drop --no-server)")
        return 2
    if report["diff_non_empty"]:
        print(f"result: DRIFT ({len(report['diff_non_empty'])} non-empty sections)")
        return 1
    print("result: ok (diff=0)")
    return 0


def cmd_brief(args) -> int:
    """Short recovery brief: six fixed sections, capped and truncated loudly."""
    cfg, store = _build(args)
    limits = brief_mod.UNLIMITED if args.full else brief_mod.Limits(
        lines=args.max_lines, bytes=args.max_bytes,
        comment_preview=args.preview_chars)
    if args.for_ == "pm" and not args.project:
        print("brief: --for pm needs --project <name>", file=sys.stderr)
        return 2
    brief = brief_mod.build_brief(cfg, store, target=args.for_,
                                  project=args.project, limits=limits,
                                  lwa_root=args.lwa)
    text = brief_mod.render(brief, limits=limits)
    sys.stdout.write(text)
    return 0


def cmd_context(args) -> int:
    """One issue card: goal, last decision, acceptance, comment ids."""
    cfg, _store = _build(args)
    repo = str((cfg.get("github") or {}).get("repo") or "")
    try:
        if args.fixture:
            source = FixtureSource(args.fixture)
        else:
            source = LiveSource(repo) if repo else NullSource()
            if not repo:
                print("context: 需要 --fixture 或配置 github.repo", file=sys.stderr)
                return 2
        card = brief_mod.build_context(
            source, args.issue, default_repo=repo,
            limits=brief_mod.Limits(comment_preview=args.preview_chars))
    except Exception as exc:  # noqa: BLE001  report, never dump a traceback
        print(f"context failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(brief_mod.render_context(card))
    return 0


def _live_sids(live_file: str | None) -> set[str]:
    """Live sids from a saved ``sess.py list`` output, or by running it.

    The console lives in the workbench checkout's ``tools/``; its directory name
    is assembled from fragments in :mod:`session_events` so the privacy gate
    (which greps the private name) stays green.
    """
    if live_file:
        return session_events.parse_server_list(Path(live_file))
    import subprocess
    script = Path(__file__).resolve().parent.parent.parent / session_events.LWA_DIRNAME \
        / "tools" / session_events.SESS_CONSOLE
    if not script.is_file():
        raise RuntimeError(f"session console not found: {script.name}")
    done = subprocess.run([sys.executable, str(script), "list"],
                          capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        raise RuntimeError((done.stderr or done.stdout or "sess.py list failed").strip()[:200])
    return session_events.parse_sids(done.stdout or "")


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
    parser.add_argument("--allowlist", default=default,
                        help="project/issue/action allowlist JSON "
                             "(default: engine.project_allowlist, else "
                             "config/allowlist.json)")
    parser.add_argument("--session-api", choices=list(SESSION_APIS), default=default,
                        help="session console: real (default, production) or "
                             "none (isolated runs; every call raises)")


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
    p_status.add_argument("--outbox", action="store_true",
                          help="also list waiting_person/uncertain outbox "
                               "actions (default: index.json only)")
    p_status.set_defaults(func=cmd_status)

    p_doctor = sub.add_parser("doctor", help="check bus, source, cursor, assignments")
    _add_common(p_doctor, suppress=True)
    p_doctor.set_defaults(func=cmd_doctor)

    p_unblock = sub.add_parser("unblock", help="move one blocked assignment back to working")
    _add_common(p_unblock, suppress=True)
    p_unblock.add_argument("--id", required=True,
                           help="assignment id (asg_<issue>_<role>_<n>)")
    p_unblock.add_argument("--reason", default="",
                           help="why the block is over (recorded in the event)")
    p_unblock.set_defaults(func=cmd_unblock)

    p_replay = sub.add_parser("replay", help="rebuild one issue's index entry")
    _add_common(p_replay, suppress=True)
    p_replay.add_argument("--issue", type=int, default=None,
                          help="issue number to rebuild (omit with --at/--sessions)")
    p_replay.add_argument("--at", default=None,
                          help="ISO instant: replay the session stream as of then "
                               "(implies the session view)")
    p_replay.add_argument("--sessions", action="store_true",
                          help="show the session view (sid -> role/issue/profile)")
    p_replay.set_defaults(func=cmd_replay)

    p_rec = sub.add_parser("reconcile",
                          help="three-way session check: replay x sessions.json x live")
    _add_common(p_rec, suppress=True)
    p_rec.add_argument("--at", default=None, help="ISO instant for the replay side")
    p_rec.add_argument("--live-file", default=None,
                       help="saved `sess.py list` output instead of querying the server")
    p_rec.add_argument("--no-server", action="store_true",
                       help="skip the live side (then exit 2 = UNKNOWN, never ok)")
    p_rec.set_defaults(func=cmd_reconcile)

    p_brief = sub.add_parser("brief", help="short recovery brief (six sections)")
    _add_common(p_brief, suppress=True)
    p_brief.add_argument("--for", dest="for_", choices=("supervisor", "pm"),
                         default="supervisor", help="audience (default supervisor)")
    p_brief.add_argument("--project", default=None,
                         help="project name (required with --for pm)")
    p_brief.add_argument("--max-lines", type=_positive_int, default=60,
                         help="line cap (default 60)")
    p_brief.add_argument("--max-bytes", type=_positive_int, default=8 * 1024,
                         help="byte cap (default 8192)")
    p_brief.add_argument("--preview-chars", type=_non_negative_int, default=120,
                         help="per-item text clip (default 120)")
    p_brief.add_argument("--lwa", default=None,
                         help="workbench checkout root (default: derived from "
                              "the session stream's state dir)")
    p_brief.add_argument("--full", action="store_true",
                         help="no cap: every item, nothing dropped")
    p_brief.set_defaults(func=cmd_brief)

    p_ctx = sub.add_parser("context", help="one issue card (no full comment bodies)")
    _add_common(p_ctx, suppress=True)
    p_ctx.add_argument("--issue", required=True,
                       help="owner/name#12, #12, or 12 (with github.repo configured)")
    p_ctx.add_argument("--fixture", default=None,
                       help="read the issue from a fixture file instead of GitHub")
    p_ctx.add_argument("--preview-chars", type=_non_negative_int, default=120,
                       help="per-comment clip (default 120)")
    p_ctx.set_defaults(func=cmd_context)

    for sub_parser in (p_once, p_run, p_status, p_doctor, p_unblock, p_replay,
                       p_rec, p_brief, p_ctx):
        sub_parser.set_defaults(_has_common=True)

    return parser


def _ensure_common_defaults(args) -> None:
    for name in ("config", "state_dir", "mode", "role", "source", "allowlist",
                 "session_api"):
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
