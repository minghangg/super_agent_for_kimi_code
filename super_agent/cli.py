"""Command line entry point.

    python -m super_agent.cli doctor
    python -m super_agent.cli once --mode observe
    python -m super_agent.cli status
    python -m super_agent.cli replay --issue 42

Exit codes: 0 success, 1 a check or round failed, 2 bad arguments.
"""
from __future__ import annotations

import argparse
import sys

from . import config as config_mod
from .bus_client import BusClient
from .engine import build_engine
from .github_source import NullSource
from .store import Store

NONE_TEXT = "无"


def _build(args) -> tuple[dict, Store]:
    overrides: dict = {}
    if args.mode:
        overrides["mode"] = args.mode
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
    summary = engine.run_round()
    print(f"mode={summary['mode']} issues={summary['issues']} "
          f"new_comments={summary['comments_new']} processed={summary['processed']} "
          f"needs_person={summary['needs_person']} "
          f"external_actions={summary['external_actions']}")
    for err in summary["errors"]:
        print(f"  warning: {err}", file=sys.stderr)
    return 1 if summary["errors"] else 0


def cmd_status(args) -> int:
    cfg, store = _build(args)
    engine = build_engine(cfg, store, _bus(cfg), source=NullSource())
    report = engine.status()
    print(f"mode={report['mode']} external_actions={report['external_actions']}")
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
    print(f"doctor  mode={report['mode']} "
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


def _add_common(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
    """Shared options. On subcommands they use SUPPRESS so they do not clobber
    a value given before the subcommand (``--mode`` works on either side)."""
    default = argparse.SUPPRESS if suppress else None
    parser.add_argument("--config", default=default, help="path to a JSON config file")
    parser.add_argument("--state-dir", default=default,
                        help="override the state directory")
    parser.add_argument("--mode", choices=list(config_mod.MODES), default=default,
                        help="observe (default) or assist")
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

    for sub_parser in (p_once, p_status, p_doctor, p_replay):
        sub_parser.set_defaults(_has_common=True)

    return parser


def _ensure_common_defaults(args) -> None:
    for name in ("config", "state_dir", "mode", "source"):
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
