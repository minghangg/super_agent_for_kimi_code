"""Operator entry for AT sessions and the loopback questionnaire service."""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
import webbrowser
from pathlib import Path

from . import agent_config
from . import at_serve
from . import config as config_mod
from . import flow as flow_mod
from . import issue_cli
from . import requirements
from . import session_ledger


def _config(args):
    overrides = agent_config.overrides()
    return config_mod.load(getattr(args, "config", None), overrides)


def _store_root(args, cfg=None):
    cfg = cfg or _config(args)
    return config_mod.state_root(cfg, getattr(args, "state_dir", ""))


def _charter_task(name: str, task: str) -> str:
    from . import dispatch_cli

    baseline, supplement = dispatch_cli.charter_files(name)
    files = [dispatch_cli.contract_file(), baseline, supplement]
    files = [path.replace(os.sep, "/") for path in files if path]
    return ("先用 Read 工具读取并遵守这些文件：" + "、".join(files)
            + "。读取后不要只回复 Read done，继续执行以下任务。\n" + task)


def _dispatch(cwd: str, task: str, *, name: str = "", sid: str = "",
              state_dir: str = "", provider: str = "", model: str = "",
              effort: str = "") -> dict:
    command = [sys.executable, "-m", "super_agent.dispatch_cli", "--cwd", cwd,
               "--permission-mode", "auto", "--detach", "--json", "--task", task]
    if state_dir:
        command += ["--state-dir", state_dir]
    if sid:
        command += ["--session", sid, "--no-activate", "--role", "supervisor"]
    else:
        command += ["--name", name, "--activate-only", "--no-activate"]
        if provider:
            command += ["--provider", provider]
        if model:
            command += ["--model", model]
        if effort:
            command += ["--effort", effort]
    proc = subprocess.run(command, capture_output=True, text=True, timeout=300)
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    if proc.returncode:
        raise RuntimeError(f"dispatch failed with rc={proc.returncode}")
    for line in reversed(proc.stdout.splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("session_id"):
            if not sid:
                return _dispatch(cwd, _charter_task(name, task),
                                 sid=str(value["session_id"]), state_dir=state_dir)
            return value
    raise RuntimeError("dispatch returned no machine-readable session id")


def cmd_start(args) -> int:
    cfg = _config(args)
    ctx = issue_cli._Ctx(args)
    role = issue_cli._role(args.role, ctx.roles)
    if role != "supervisor":
        raise issue_cli.Denied("AT 创建与绑定只能由 supervisor 执行")
    issue_cli._check("bind", role, ctx.roles)
    sessions = session_ledger.load_sessions(cfg)
    prefix = f"at-{args.project.lower()}-"
    existing = next((key for key in sorted(sessions, reverse=True)
                     if key.startswith(prefix) and not args.new), "")
    name = existing or f"{prefix}{uuid.uuid4().hex[:8]}"
    sid = str((sessions.get(existing) or {}).get("id") or "") if existing else ""
    if args.dry_run:
        print(f"[dry-run] AT {name} existing={bool(existing)} "
              f"project={args.project} issue={args.issue} cwd={args.cwd}")
        return 0
    issue_row = issue_cli._call(
        "GET", f"/repos/{issue_cli._repo(args.project, ctx.projects)}/issues/{args.issue}",
        **ctx.call)
    if flow_mod.from_labels(issue_row.get("labels")) == flow_mod.STANDARD:
        raise issue_cli.Denied("该 Issue 是 flow:standard；不得启动 AT")
    task = args.task or (
        f"处理 {args.project}#{args.issue}：只读 Issue 与项目事实，建立版本化需求包。"
        "需要澄清就发布带选项的问题；无需澄清就发布 READY。不要开发、不要派工。")
    result = _dispatch(args.cwd, task, name=name, sid=sid,
                       state_dir=str(session_ledger.root(cfg)), provider=args.provider,
                       model=args.model, effort=args.effort)
    sid = str(result["session_id"])
    # Register before binding: a binding with no key is the silent state #129
    # removes, so a failure here aborts the command (rc != 0) instead of
    # leaving an AT running that nothing can address.
    try:
        session_ledger.register_session_key(name, sid, cfg=cfg)
    except (OSError, RuntimeError) as exc:
        print(f"[失败] AT 会话 {sid} 已创建，但会话键 {name} 未登记：{exc}",
              file=sys.stderr)
        print(f"       补救：python -m super_agent.sessions_ledger set {name} {sid}",
              file=sys.stderr)
        return 1
    session_ledger.bind_session(f"{args.project}#{args.issue}", "at", sid,
                                name=name, extra={"awaiting": True}, cfg=cfg)
    session_ledger.emit("session.bound", cfg=cfg, issue=args.issue,
                        role="at", sid=sid, key=name)
    print(f"AT ready: {key} -> {name} ({sid})")
    print("问卷服务：python -m super_agent.at serve start --allow-send")
    return 0


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection((at_serve.HOST, int(port)), 0.3):
            return True
    except OSError:
        return False


def _pid(root: Path) -> int:
    try:
        return int((root / "atserve" / "atserve.pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _windows_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE,
                                            ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) \
            and code.value == 259
    finally:
        kernel32.CloseHandle(handle)


def _alive(pid: int) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        return _windows_alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def cmd_serve(args) -> int:
    cfg = _config(args)
    root = _store_root(args, cfg)
    pid = _pid(root)
    if args.action == "status":
        running = _alive(pid) and _port_open(args.port)
        print(f"status={'running' if running else 'stopped'} pid={pid or '-'} "
              f"url=http://{at_serve.HOST}:{args.port} root={root}")
        return 0 if running else 1
    if args.action == "stop":
        if not pid or not _alive(pid):
            print("status=stopped")
            return 0
        os.kill(pid, signal.SIGTERM)
        print(f"stop requested: pid={pid}")
        return 0
    if _alive(pid) and _port_open(args.port):
        print(f"status=running pid={pid} url=http://{at_serve.HOST}:{args.port}")
        return 0
    # start is deliberately a foreground service. The terminal, service manager,
    # or the agent's background-task runner owns its lifetime; no fake daemon.
    return at_serve.run(
        root=root, port=args.port,
        transport=at_serve._real_transport if args.allow_send else None)


def cmd_questions(args) -> int:
    root = _store_root(args)
    projection = requirements.load_projection(root)
    rows = []
    for path in sorted((root / "atserve" / "questions").glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if record.get("submitted") and not args.all:
            continue
        current = requirements.current(projection, str(record.get("requirement_id") or ""))
        stale = at_serve.stale_reason(record, lambda rid: current)
        state = "expired" if stale else ("submitted" if record.get("submitted") else "pending")
        rows.append((state, record))
        print(f"{state:9} #{record.get('issue')} r{record.get('revision') or 0} "
              f"qid={record.get('id')} {at_serve.url(args.port, record.get('id'))}")
    if not rows:
        print("(no pending questions)")
    return 0


def cmd_open(args) -> int:
    root = _store_root(args)
    record = at_serve.load(root, args.qid)
    if record is None:
        print(f"question not found: {args.qid}", file=sys.stderr)
        return 1
    link = at_serve.url(args.port, args.qid)
    print(link)
    return 0 if webbrowser.open(link) else 1


def _parser():
    parser = argparse.ArgumentParser(prog="python -m super_agent.at")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--state-dir", default="")
        p.add_argument("--config")

    p = sub.add_parser("start", help="create/reuse, bind and prompt a project AT")
    common(p)
    p.add_argument("--role", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--cwd", required=True)
    p.add_argument("--task", default="")
    p.add_argument("--new", action="store_true")
    p.add_argument("--provider", default="")
    p.add_argument("--model", default="")
    p.add_argument("--effort", default="")
    p.add_argument("--projects-file")
    p.add_argument("--roles-file")
    p.add_argument("--env-file")
    p.add_argument("--allowlist")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("serve", help="start/status/stop the loopback answer page")
    common(p)
    p.add_argument("action", choices=("start", "status", "stop"))
    p.add_argument("--port", type=int, default=at_serve.DEFAULT_PORT)
    p.add_argument("--allow-send", action="store_true")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("questions", help="list pending/expired/submitted questions")
    common(p)
    p.add_argument("--port", type=int, default=at_serve.DEFAULT_PORT)
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_questions)

    p = sub.add_parser("open", help="open one question in the local browser")
    common(p)
    p.add_argument("--port", type=int, default=at_serve.DEFAULT_PORT)
    p.add_argument("qid")
    p.set_defaults(func=cmd_open)
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.func(args)
    except (OSError, RuntimeError, ValueError, issue_cli.Denied,
            session_ledger.SessionLedgerError) as exc:
        print(f"[拒绝] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
