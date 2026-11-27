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

``resolve`` extends 2 on purpose: there it means "some keys were not written"
(a failed save or a row missing after the read-back), not "bad arguments".
Argparse's own parameter errors still exit 2 and print ``usage:``, so the two
are told apart by the message.
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import brief as brief_mod
from . import config as config_mod
from . import github_source
from . import reporter as reporter_mod
from . import session_events
from . import session_ledger
from . import assignments as asg_mod
from . import cli_resolve
from . import needs as needs_mod
from . import runloop
from . import stopcontrol
from . import store as store_mod
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


def _legacy_root(cfg: dict) -> Path | None:
    """The pre-migration runtime root, or ``None`` when it is not declared.

    ``legacy_state_dir`` is the **only** source (#140). It used to default to
    the ``state`` sibling of the chosen root, which read a directory by its
    name: once a deployment moved its root to ``<workbench>/data`` the guess
    named ``<workbench>/state``, a directory that no longer exists, so the
    look-up returned "no legacy data" instead of failing. A guess that silently
    answers "empty" is worse than no guess at all.

    An upgrade from a pre-migration layout therefore declares the old root::

        {"state_dir": ".../data", "legacy_state_dir": ".../state"}
    """
    raw = str(cfg.get("legacy_state_dir") or "").strip()
    if raw:
        return Path(os.path.expanduser(raw)).resolve()
    return None


def _build(args) -> tuple[dict, Store]:
    from . import agent_config
    overrides: dict = agent_config.overrides()
    if args.mode:
        overrides["mode"] = args.mode
    if args.role:
        overrides["role"] = args.role
    if args.state_dir:
        overrides["state_dir"] = args.state_dir
        # ``--state-dir`` isolates the **whole** run, session state included
        # (ISS-139 P0-2). ``agent_config.overrides()`` has already filled
        # ``engine.session_state_dir`` from the private deployment file, and
        # ``resolve_session_root`` prefers that key - so setting ``state_dir``
        # alone left an isolated launcher scanning the production ledger and
        # resolving the production PM (measured: 227 rows, the real PM sid).
        # #132 recorded the same accident for the session backend and
        # ``at_service._deployment_resolver`` pins the rule for the AT maps: an
        # isolated run reads its own root, never the deployment's.
        overrides.setdefault("engine", {})["session_state_dir"] = args.state_dir
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
    root = config_mod.state_root(cfg, args.state_dir)
    legacy = _legacy_root(cfg)
    if legacy is not None and legacy != root:
        root, note = store_mod.choose_root(root, legacy=legacy)
        if note:
            print(f"WARN {note}", file=sys.stderr)
    store = Store(root,
                  max_event_lines=int(store_cfg.get("max_event_lines") or 10_000),
                  max_outbox_lines=int(store_cfg.get("max_outbox_lines") or 5_000),
                  outbox_stale_s=float(store_cfg.get("outbox_stale_s") or 300),
                  accept_empty_index=bool(getattr(args, "accept_empty_index", False)))
    _apply_index_options(args, store)
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


def _cursor_refusal(exc, store) -> int:
    """Refuse to run on an untrustworthy cursor, and name the way out.

    Two ways out, in the order a person should try them: rebuild the cursor
    from the event log (the ``comment.processed`` rows carry the ids), or - if
    the log is gone too - accept the re-scan explicitly. The explicit flag is
    printed as a command, so it cannot be reached by forgetting a setting.
    """
    print(f"cursor refused: {exc}", file=sys.stderr)
    print("  rebuild it first: replay each issue from the source "
          "(python -m super_agent.cli replay --issue <n>),", file=sys.stderr)
    print("  which rewrites cursor.json from the comments it reads.", file=sys.stderr)
    print("  to accept a full re-scan instead, run once with "
          "--allow-empty-cursor.", file=sys.stderr)
    print(f"  state dir: {store.root}", file=sys.stderr)
    return 4


def _index_options(parser: argparse.ArgumentParser, *, suppress: bool = False) -> None:
    """``--accept-empty-index``: the explicit answer to :class:`IndexRefused`.

    置位此开关表示接受索引为空并继续。注意：release_frames 在事件日志里没有任何证据可重建，走到这一步它永久丢失；人工恢复后，此前已发的 release 会重投一次。

    A flag, never a setting, for the same reason as ``--allow-empty-cursor``:
    a default would quietly re-enable the silent empty write-back. ``suppress``
    stops a subparser's default from clobbering a value given before it.
    """
    parser.add_argument(
        "--accept-empty-index", action="store_true",
        default=argparse.SUPPRESS if suppress else False,
        help="write the index back anyway when index.json cannot be read. "
             "置位此开关表示接受索引为空并继续。注意：release_frames 在事件日志里"
             "没有任何证据可重建，走到这一步它永久丢失；人工恢复后，此前已发的 "
             "release 会重投一次。")


def _apply_index_options(args, store: Store) -> None:
    """Open the write-back gate when the flag was given, and say so."""
    if not getattr(args, "accept_empty_index", False):
        return
    if store.index_unreadable:
        print(f"accepted empty index: {store.index_unreadable} "
              f"(kept copy: {store.index_quarantined or 'none'})", file=sys.stderr)
    store.accept_empty_index()


def _common_options(parser: argparse.ArgumentParser) -> None:
    """Flags shared by the commands that can start a round.

    ``--allow-empty-cursor`` is the explicit "re-scan every comment" answer to
    the refusal in :class:`super_agent.store.CursorRefused`. It is a flag on
    the round-running commands, never a setting: a default would quietly
    re-enable the silent full re-scan the guard exists to stop.
    """
    parser.add_argument("--allow-empty-cursor", action="store_true",
                        help="run even when cursor.json cannot be trusted, "
                             "accepting a full re-scan of every comment")
    _index_options(parser)


def _refuse_bad_cursor(args, cfg: dict, store) -> int | None:
    """Refuse **before** recovery, or return None when the cursor is fine.

    Ordered before ``_recover`` on purpose: recovery replays ``prepared``
    outbox actions, and a replay is a POST to GitHub. Refusing after that would
    have already acted on the untrustworthy state.
    """
    if getattr(args, "allow_empty_cursor", False):
        cfg.setdefault("engine", {})["allow_empty_cursor"] = True
        return None
    problem = store.cursor_problem()
    if not problem:
        return None
    return _cursor_refusal(store_mod.CursorRefused(problem), store)


def cmd_once(args) -> int:
    cfg, store = _build(args)
    refusal = _refuse_bad_cursor(args, cfg, store)
    if refusal is not None:
        return refusal
    engine = _engine(cfg, store, _bus(cfg),
                      session_api=_session_api(getattr(args, "session_api", None)))
    try:
        with PidLock(store.pid_path, argv=["super_agent", "once"]):
            recovery = _recover(engine)
            if not recovery.get("ok", True):
                return _recovery_refusal(recovery)
            summary = engine.run_once()
            engine.write_health(running=False)
    except store_mod.CursorRefused as exc:
        return _cursor_refusal(exc, store)
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
    """Forwarder: the one implementation is :func:`stopcontrol.install_stop_handlers`."""
    return stopcontrol.install_stop_handlers(stop_event)


def _restore_handlers(previous: dict) -> None:
    """Forwarder: see :func:`stopcontrol.restore_handlers`."""
    stopcontrol.restore_handlers(previous)


def _flush_state(store: Store) -> None:
    """Forwarder: see :func:`stopcontrol.flush_state`."""
    stopcontrol.flush_state(store)


def run_loop(engine, store, *, interval: float = 60.0,
             max_failures: int = DEFAULT_MAX_FAILURES, max_rounds: int = 0,
             stop_event: threading.Event | None = None,
             sleep=time.sleep, now=_now) -> dict:
    """Forwarder: the loop lives in :func:`runloop.run_loop` (ISS-131 收尾)."""
    return runloop.run_loop(engine, store, interval=interval,
                            max_failures=max_failures, max_rounds=max_rounds,
                            stop_event=stop_event, sleep=sleep, now=now)


def cmd_run(args) -> int:
    cfg, store = _build(args)
    refusal = _refuse_bad_cursor(args, cfg, store)
    if refusal is not None:
        return refusal
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
    show_all = bool(getattr(args, "all", False))
    report = engine.status(outbox=bool(getattr(args, "outbox", False)), all=show_all)
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
        line = (f"- issue #{item['issue']} comment {item['comment_id']} "
                f"({item['reason']}) at {item['at']}")
        if "state" in item:
            line += (f"  [{item['state']} by {item.get('resolved_by') or '-'}"
                     f" resolved_at={item.get('resolved_at') or '-'}"
                     f" via comment {item.get('resolved_by_comment') or '-'}"
                     f" entries={item.get('entries')}"
                     f" entry_at={item.get('entry_at') or '-'}]")
        print(line)
    return 0


def _doctor_project(project: str, root: str = "", origin: str = "") -> int:
    """Print one project's environment document verbatim (ISS-101).

    The file name is the project key itself -- the same token ``gh_projects.json``
    and dispatch use. Nothing here parses the markdown: the document is the
    answer, and a schema on top of it would be a second ``config.validate``.

    ``root`` is the **resolved** directory (``config.load`` already merged the
    file, the environment and the overrides). Reading it from
    ``agent_config`` again would bypass ``--config``: a ``--config`` file's
    ``paths.project_env_dir`` would be ignored and the answer would silently
    come from a different layer -- the same "looks configured, is not" shape
    this change exists to remove.

    Exit codes: 0 printed, 1 no such entry (or two candidates), 2 the directory
    itself is unconfigured or missing -- a configuration fault is a different
    action from a missing registration, so they do not share a code.
    """
    from . import agent_config

    key = str(project or "").strip()
    if not key:
        print("doctor --project: 需要项目键（gh_projects.json 的键）", file=sys.stderr)
        return 2
    if not root:
        print("project_env_dir 未配置（paths.project_env_dir：--config / agent.json / "
              "$SAFKC_PROJECT_ENV_DIR 三处皆空）", file=sys.stderr)
        return 2
    base = Path(root)
    if not base.is_dir():
        print(f"目录不存在：{base}", file=sys.stderr)
        print(f"来源：paths.project_env_dir（{origin or '配置层'}）", file=sys.stderr)
        return 2
    # Two separate branches on purpose: "no candidate" is a missing
    # registration, "several candidates" is an ambiguous one. Taking the first
    # hit would make the answer depend on ``glob``'s order.
    exact = [p for p in base.glob("*.md") if p.stem == key]
    if len(exact) >= 2:
        names = ", ".join(sorted(p.name for p in exact))
        print(f"歧义：{base} 下有 {len(exact)} 个候选：{names}", file=sys.stderr)
        return 1
    if not exact:
        print(f"未登记：{key}（目录 {base} 下无 {key}.md）", file=sys.stderr)
        # A near-miss is reported, never adopted: "looks like" is not "is".
        near = [p for p in base.glob("*.md")
                if p.stem.strip().lower().replace("_", "")
                == key.strip().lower().replace("_", "")]
        if near:
            hint = ", ".join(sorted({p.stem.strip() for p in near}))
            print(f"最接近 {hint}（未自动采用）", file=sys.stderr)
        return 1
    path = exact[0]
    # Name the layer that produced the directory, not just the private file:
    # with ``--config`` the value may have come from there, and a source line
    # that names the wrong file sends the reader to the wrong place.
    print(f"来源：{path}（paths.project_env_dir 来自 {origin or '配置层'}）",
          file=sys.stderr)
    # Bytes out when the stream has one: the document is printed as stored, so
    # ``diff`` against the file is empty whatever its line endings (a text-mode
    # write would translate LF to CRLF on Windows). A redirected StringIO has
    # no buffer; there the text form is the only option and is exact enough.
    raw = path.read_bytes()
    sys.stdout.flush()
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(raw)
        buffer.flush()
    else:
        sys.stdout.write(raw.decode("utf-8-sig", "replace"))
    return 0


def _agent_overrides(args) -> dict:
    """The private layer's overrides, without the engine's CLI flags.

    ``doctor --project`` needs ``paths.project_env_dir`` and nothing else, so
    it asks for the private overrides exactly the way ``_build`` does and skips
    the flags that only shape a round (mode/role/source/...).
    """
    from . import agent_config

    return agent_config.overrides()


def _resolve_path(raw: str) -> str:
    """A declared path, resolved the one way this package resolves paths.

    Relative values anchor to this checkout, never the working directory --
    ``state_dir`` / ``project_env_dir`` already do it inside ``agent_config``;
    a ``--config`` file's value arrives here unresolved, so it gets the same
    rule rather than a second one.
    """
    from . import agent_config

    text = str(raw or "").strip()
    if not text:
        return ""
    path = Path(os.path.expanduser(text))
    if not path.is_absolute():
        path = agent_config.CHECKOUT / path
    return str(path.resolve())


def cmd_doctor(args) -> int:
    project = getattr(args, "project", None)
    if project is not None:
        # ISS-101: ``--project`` is a view of doctor, not a new command. It
        # returns before the engine is built, so it works with no bus, no
        # source and no network -- the file is the whole answer. The directory
        # comes from ``config.load`` so ``--config`` and the environment are
        # merged first; reading ``agent_config`` directly would ignore them.
        cfg = config_mod.load(getattr(args, "config", None),
                              _agent_overrides(args))
        raw = str((cfg.get("paths") or {}).get("project_env_dir") or "").strip()
        # A ``--config`` file is the layer the operator typed **this run**, so
        # it outranks the private file. ``config.load`` merges overrides last
        # (that is right for the engine: the private file is the deployment's
        # answer), so for this one view the file is re-read on top. Without
        # this, an explicit ``--config`` silently answered from agent.json --
        # measured: rc=1 "unregistered" against a directory nobody named.
        config_file = getattr(args, "config", None)
        origin = ""
        if config_file:
            resolved = Path(os.path.expanduser(str(config_file))).resolve()
            from . import config as _cfg_mod
            declared = str(((_cfg_mod._read_json(resolved).get("paths") or {})
                            .get("project_env_dir") or "")).strip()
            if declared:
                raw = declared
            origin = str(resolved)
        else:
            from . import agent_config
            origin = str(agent_config.config_path())
        return _doctor_project(project, _resolve_path(raw), origin)
    cfg, store = _build(args)
    engine = _engine(cfg, store, _bus(cfg),
                      session_api=_session_api(getattr(args, "session_api", None)))
    report = engine.doctor()
    worst = "ok"
    width = max((len(c["name"]) for c in report["checks"]), default=4)
    label_width = max((len(c.get("label") or "") for c in report["checks"]), default=4)
    print(f"doctor  role={engine.role} mode={report['mode']} "
          f"external_actions={report['external_actions']}")
    from . import agent_config
    allowlist = getattr(engine.projects, "path", "") or "(none)"
    config_origin = getattr(args, "config", None) or agent_config.config_path()
    print(f"paths   engine_state={store.root} "
          f"session_state={session_ledger.sessions_path(cfg).parent} "
          f"allowlist={allowlist} config={config_origin}")
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


def cmd_resolve(args) -> int:
    """Dismiss one ``needs_person`` entry; the logic lives in ``cli_resolve``."""
    cfg, store = _build(args)
    return cli_resolve.run(args, cfg, store)


def cmd_resume_read(args) -> int:
    """Lift a read pause: the only way back in after a hard cap.

    Exit codes: 0 lifted (or already readable), 1 nothing matched, 2 unusable
    arguments - the same split ``resolve`` documents, kept for the same reason:
    a script must be able to tell "a person did it" from "there was nothing to
    do".
    """
    cfg, store = _build(args)
    if not str(args.by or "").strip():
        print("resume-read: --by is required (who is lifting the pause)",
              file=sys.stderr)
        return 2
    engine = _engine(cfg, store, _bus(cfg), source=NullSource(),
                     session_api=_session_api(getattr(args, "session_api", None)))
    out = engine.resume_read(args.issue, by=str(args.by), detail=str(args.note or ""),
                             max_pages=getattr(args, "max_pages", None))
    if out.get("number") is None:
        print(f"resume-read: {out.get('reason')}", file=sys.stderr)
        return 2
    if not out.get("changed"):
        print(f"resume-read: {out.get('reason')}", file=sys.stderr)
        return 1
    print(f"resume-read: #{out['number']} lifted ({out.get('reason')}) by {args.by}; "
          f"the next round reads it again")
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
    # The refusal names ``replay --issue <n>`` as the repair, so the repair has
    # to say when it is not finished: one thread rebuilt leaves every other
    # issue without a cursor, and a round started now would re-read them.
    gap = out.get("cursor_gap") or []
    if gap:
        print(f"cursor still missing for {len(gap)} issue(s): "
              f"{', '.join(gap[:5])}" + (" ..." if len(gap) > 5 else ""),
              file=sys.stderr)
        print("  run replay --issue <n> for each of them before starting a round",
              file=sys.stderr)
        return 0
    print("cursor complete: every issue that read a comment has an entry")
    return 0


def _cmd_replay_sessions(engine, args) -> int:
    """`replay --at <ISO>` / `replay --sessions`: session truth as of an instant."""
    at = getattr(args, "at", None)
    try:
        state = engine.replay_sessions(at)
    except session_events.SessionStreamNotConfigured as exc:
        print(f"replay refused: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"replay failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    missing = " (missing)" if state.get("missing") else ""
    print(f"session replay  events={state['events_path']}{missing} "
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
    except session_events.SessionStreamNotConfigured as exc:
        print(f"reconcile refused: {exc}", file=sys.stderr)
        return 2
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
    if report.get("ledger_error"):
        print(f"result: UNKNOWN ({report['ledger_error']})")   # nothing compared
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


def cmd_recycle(args) -> int:
    """``recycle`` 只是转发：解析只在 ``recycle.main`` 一处（ISS-100 D2）。"""
    from . import recycle as recycle_mod
    return recycle_mod.main(list(args.rest or []))


def cmd_reporter(args) -> int:
    """First screen "what needs me": three buckets, then a collapsed second screen.

    Read-only throughout. ``--no-quotes`` never constructs a live source at all
    (not "builds one and skips the quotes"): the page then says, per row, why
    the quote is missing instead of silently dropping it.
    """
    cfg, store = _build(args)
    source_error = ""
    if args.no_quotes:
        source = NullSource()
    elif args.fixture:
        try:
            source = FixtureSource(args.fixture)
        except Exception as exc:  # noqa: BLE001  report, never a traceback
            print(f"reporter: 夹具不可读: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
    else:
        # ``github.source`` decides, exactly as ``github_source.build`` does for
        # the engine: ``none`` stays offline even when a repo is configured.
        # Building a LiveSource unconditionally would have made the page reach
        # the network on a state dir that says it must not.
        try:
            source = github_source.build(cfg)
        except Exception as exc:  # noqa: BLE001  report, never a traceback
            print(f"reporter: 数据源不可用: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        if isinstance(source, github_source.BrokenSource):
            # Keep the real reason: a fixture path typo must not be reported as
            # ``github.source=none``, which the user never configured.
            source_error = f"{source.error}"
            print(f"reporter: 数据源不可用: {source_error}", file=sys.stderr)
            source = NullSource()
    # The engine is built for its allowlist and role only; nothing here runs a
    # round, so ``source`` above is the only backend that can be touched.
    engine = _engine(cfg, store, None, source=NullSource())
    try:
        report, text = reporter_mod.collect_and_render(
            engine, source=source, quotes=not args.no_quotes,
            source_error=source_error)
    except Exception as exc:  # noqa: BLE001  report, never dump a traceback
        print(f"reporter failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if args.out:
        path = Path(args.out).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"reporter: 状态={report['status']} 待处理="
              f"{sum(v for v in report['counts'].values() if v) or 0} "
              f"引用不可用={report['quotes_missing']} -> {path}")
    else:
        sys.stdout.write(text)
    return 0


def _live_sids(live_file: str | None) -> set[str]:
    """Live sids from a saved console list, or by asking this package's console.

    The console is :mod:`super_agent.console` -- this checkout's own module, run
    as ``python -m super_agent.console list`` with ``cwd`` set to the checkout so
    the package is importable. The module form is required, not a preference:
    running the file by path breaks its relative imports
    (``ImportError: attempted relative import with no known parent package``).

    ``engine.session_console_path`` (when configured) may name another console,
    but it is still invoked as a module or an executable that answers ``list``.
    """
    if live_file:
        return session_events.parse_server_list(Path(live_file))
    import subprocess
    checkout = Path(__file__).resolve().parents[1]
    configured: list[str] = []
    try:
        console_cfg = str((_load_config().get("engine") or {}).get(
            "session_console_path") or "").strip()
    except Exception:                     # noqa: BLE001  config faults surface elsewhere
        console_cfg = ""
    if console_cfg.startswith("-m "):
        configured = [sys.executable, *console_cfg.split()]
    elif console_cfg:
        configured = [sys.executable, console_cfg]
    console_module = checkout / "super_agent" / "console.py"
    if not configured and not console_module.is_file():
        raise RuntimeError("session console not found: super_agent/console.py")
    argv = configured or [sys.executable, "-m", "super_agent.console", "list"]
    if configured:
        argv = argv + ["list"]
    done = subprocess.run(argv, capture_output=True, text=True, timeout=60, cwd=str(checkout))
    if done.returncode != 0:
        raise RuntimeError(
            (done.stderr or done.stdout or "console list failed").strip()[:200])
    return session_events.parse_sids(done.stdout or "")


def _load_config() -> dict:
    """This package's config, private overrides included (one loader, one gate)."""
    from . import agent_config
    return config_mod.load(overrides=agent_config.overrides())


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
    _common_options(p_once)
    p_once.set_defaults(func=cmd_once)

    p_run = sub.add_parser("run", help="run rounds until SIGINT/SIGTERM")
    _add_common(p_run, suppress=True)
    _common_options(p_run)
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
    p_status.add_argument("--all", action="store_true",
                          help="also list dismissed entries, with the audit "
                               "(resolved_at=/by/via comment/entries)")
    p_status.set_defaults(func=cmd_status)

    p_doctor = sub.add_parser("doctor", help="check bus, source, cursor, assignments")
    _add_common(p_doctor, suppress=True)
    # ISS-101: a read-only view. Name the key and get that project's
    # environment document; it needs no bus and no source.
    p_doctor.add_argument("--project", default=None,
                          help="print one project's environment document "
                               "(file name = gh_projects.json key)")
    p_doctor.set_defaults(func=cmd_doctor)

    p_unblock = sub.add_parser("unblock", help="move one blocked assignment back to working")
    _add_common(p_unblock, suppress=True)
    _index_options(p_unblock, suppress=True)
    p_unblock.add_argument("--id", required=True,
                           help="assignment id (asg_<issue>_<role>_<n>)")
    p_unblock.add_argument("--reason", default="",
                           help="why the block is over (recorded in the event)")
    p_unblock.set_defaults(func=cmd_unblock)

    p_resolve = sub.add_parser(
        "resolve", help="dismiss one needs_person entry (exit 2 = some keys not written)")
    _add_common(p_resolve, suppress=True)
    _index_options(p_resolve, suppress=True)
    p_resolve.add_argument("--issue", required=True,
                           help="owner/name#12, #12, or 12 (every key form is "
                                "resolved, legacy and qualified alike)")
    p_resolve.add_argument("--comment", type=int, required=True,
                           help="comment id of the needs_person entry")
    p_resolve.add_argument("--by", required=True,
                           help="who is dismissing it (recorded in the event)")
    p_resolve.add_argument("--comment-ref", type=int, default=None,
                           help="comment that carries the ruling (audit only)")
    p_resolve.add_argument("--state", choices=list(needs_mod.STATES),
                           default=needs_mod.RESOLVED,
                           help="resolved (a person decided, default) or "
                                "superseded (the premise is gone)")
    p_resolve.add_argument("--note", default="",
                           help="with --state superseded: what superseded it")
    p_resolve.set_defaults(func=cmd_resolve)

    p_replay = sub.add_parser("replay", help="rebuild one issue's index entry")
    _add_common(p_replay, suppress=True)
    _index_options(p_replay, suppress=True)
    p_replay.add_argument("--issue", type=int, default=None,
                          help="issue number to rebuild (omit with --at/--sessions)")
    p_replay.add_argument("--at", default=None,
                          help="ISO instant: replay the session stream as of then "
                               "(implies the session view)")
    p_replay.add_argument("--sessions", action="store_true",
                          help="show the session view (sid -> role/issue/profile)")
    p_replay.set_defaults(func=cmd_replay)

    p_resume = sub.add_parser(
        "resume-read", help="lift a read pause (the way back in after a hard cap)")
    _add_common(p_resume, suppress=True)
    _index_options(p_resume, suppress=True)
    p_resume.add_argument("--issue", required=True,
                          help="owner/name#12, #12, or 12 (every key form is lifted)")
    p_resume.add_argument("--by", required=True,
                          help="who is lifting the pause (recorded in the event)")
    p_resume.add_argument("--note", default="",
                          help="why the pause is over (recorded in the event)")
    p_resume.add_argument("--max-pages", type=int, default=None,
                          help="recorded only: the page budget the operator intends "
                               "to use next (the config value still decides)")
    p_resume.set_defaults(func=cmd_resume_read)

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
                         help="workbench checkout root (default: engine.lwa_root; "
                              "there is no directory-name guess)")
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

    p_rep = sub.add_parser("reporter", help="read-only first screen: what needs me")
    _add_common(p_rep, suppress=True)
    p_rep.add_argument("--out", default=None,
                       help="write the page to this path (default: stdout)")
    p_rep.add_argument("--no-quotes", action="store_true",
                       help="never construct a source; every row says why its "
                            "quote is unavailable")
    p_rep.add_argument("--fixture", default=None,
                       help="read comment bodies from a fixture file instead of "
                            "GitHub (same shape as context --fixture)")
    p_rep.set_defaults(func=cmd_reporter)

    # ISS-100 D2: `recycle` per-command flags live in `recycle.main` only
    # (REMAINDER here); `cli.py` does not repeat them.
    p_recycle = sub.add_parser("recycle",
                               help="stop/archive/retire/status one session (no delete)")
    p_recycle.add_argument("rest", nargs=argparse.REMAINDER)
    p_recycle.set_defaults(func=cmd_recycle)

    for sub_parser in (p_once, p_run, p_status, p_doctor, p_unblock, p_resolve,
                       p_replay, p_resume, p_rec, p_brief, p_ctx, p_rep):
        sub_parser.set_defaults(_has_common=True)

    return parser


def _ensure_common_defaults(args) -> None:
    for name in ("config", "state_dir", "mode", "role", "source", "allowlist",
                 "session_api", "accept_empty_index"):
        if not hasattr(args, name):
            setattr(args, name, None)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    _ensure_common_defaults(args)
    try:
        return args.func(args)
    except store_mod.IndexRefused as exc:
        # A command that only writes the index (``unblock``, ``resolve``,
        # ``replay``) gets here instead of a traceback: the fault is the file,
        # not the command, and the message names the kept copy.
        print(f"index refused: {exc}", file=sys.stderr)
        print("  repair index.json from the kept copy (or a backup), then run "
              "again. To rebuild from empty instead, rerun with "
              "--accept-empty-index.", file=sys.stderr)
        return 5
    except config_mod.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
