"""Login autostart for ``decide_watch``: register, read back, unregister.

Why moved out of ``decide_watch.py`` (ISS-132 C2, ``worker.md`` §3.1.1): this is
250 lines that talk to ``schtasks``/PowerShell and to nothing else. It shares no
state with the three consumer legs (cursor, notice ledger, liveness table) - it
reads the injected config, prints, and returns an exit code. Keeping it inline
made the consumer file longer every time the scheduler grew an option, and
"where is the delivery rule" started to mean scrolling past autostart.

The seam is :func:`bind`, the same shape ``wss_autostart`` uses: the host module
passes its config accessor, its PowerShell bridge, its windowless-python probe
and **its own file path** in. The path matters - the registered task must point
at ``decide_watch.py`` (the module that is started), not at this file.

Every name ``decide_watch`` used to export stays callable there: the workbench
resolves ``AUTOSTART_NAME`` / ``task_spec`` / ``verify_task`` /
``install_autostart`` / ``uninstall_autostart`` by name on the module, so the
host keeps live forwarders.
"""
from __future__ import annotations

import sys
from pathlib import Path

from .. import autostart

#: Set by :func:`bind`. Module-level accessors, not parameters, so the signatures
#: the workbench calls by name stay unchanged.
_CONFIG = None
_WINDOWLESS = None
_PS_JSON = None
_MODULE_PATH = None

EXIT_REGISTER_FAILED = 3      # 注册动作本身失败（多为权限不足，需 UAC）
EXIT_VERIFY_FAILED = 4        # 注册返回成功但回读校验不通过
EXIT_UNINSTALL_FAILED = 5     # 卸载后仍能查到任务
EXIT_OK = 0                    # 成功（issue #18：与 autostart 模块同口径）
# issue #18, QA18-3: 8 让给 --send-mode
EXIT_STARTUP_INSTALL_FAILED = autostart.EXIT_STARTUP_INSTALL_FAILED   # 10
EXIT_STARTUP_UNINSTALL_FAILED = autostart.EXIT_STARTUP_UNINSTALL_FAILED  # 11


def bind(*, config, windowless, ps_json, module_path) -> None:
    """Install the four seams. Idempotent; called by the host on every entry."""
    global _CONFIG, _WINDOWLESS, _PS_JSON, _MODULE_PATH
    _CONFIG = config
    _WINDOWLESS = windowless
    _PS_JSON = ps_json
    _MODULE_PATH = module_path


def _config():
    if _CONFIG is None:
        raise RuntimeError("decide_watch_parts.autostart_install: bind() 未调用，拒绝猜配置")
    return _CONFIG()


def _windowless() -> tuple[str, str]:
    if _WINDOWLESS is None:
        raise RuntimeError("decide_watch_parts.autostart_install: bind() 未调用，拒绝猜 pythonw")
    return _WINDOWLESS()


def _ps_json(script: str, timeout: float = 60.0):
    """跑 PowerShell 并把 stdout 解析成 JSON（桥接由宿主注入）。"""
    if _PS_JSON is None:
        raise RuntimeError("decide_watch_parts.autostart_install: bind() 未调用，拒绝猜 PowerShell")
    return _PS_JSON(script, timeout)


def _workspace_root() -> Path:
    return Path(_config().workspace_root).expanduser()


def _module_path() -> Path:
    """The **host** module (``decide_watch.py``), i.e. what the task must start."""
    if _MODULE_PATH is None:
        raise RuntimeError("decide_watch_parts.autostart_install: bind() 未调用，拒绝猜入口")
    return Path(_MODULE_PATH).resolve()


def AUTOSTART_NAME() -> str:
    return _config().autostart_name


def windowless_python() -> tuple[str, str]:
    return _windowless()


def _this_spec() -> autostart.Spec:
    """本进程的自启定义（startup 后端用；两个后端共用同一份 name/script/workdir）。"""
    exe, note = _windowless()
    return autostart.Spec(name=AUTOSTART_NAME(), script=_module_path(),
                          args=[], workdir=_workspace_root(), execute_note=note)


def _norm_backend(backend: str) -> str:
    """归一化后端名；空＝默认 startup（issue #18）。"""
    b = (backend or "").strip().lower()
    if not b:
        return autostart.DEFAULT_BACKEND
    if b not in autostart.BACKENDS:
        print(f"[autostart] 未知后端 {backend!r}（可选：{'/'.join(autostart.BACKENDS)}）",
              file=sys.stderr)
        return autostart.DEFAULT_BACKEND
    return b


def startup_install(dry_run: bool = False) -> int:
    """startup 后端安装（幂等 ＋ 回读校验 ＋ 失败非零）。"""
    spec = _this_spec()
    exe, note = _windowless()
    if note:
        print(f"[autostart] 警告：{note}", file=sys.stderr)
    text = autostart.render_vbs(spec, exe)
    path = spec.startup_file
    if dry_run:
        print("[autostart] backend=startup（干跑，未落盘）")
        print(f"  target : {path}")
        print("  encode : UTF-16LE + BOM")
        print("  content:")
        for ln in text.splitlines():
            print("    " + ln)
        exists = path.is_file()
        print(f"[autostart] 当前状态：{'已存在' if exists else '未安装'}")
        return EXIT_OK

    if path.is_file():
        try:
            old = autostart.read_vbs(path)
        except OSError as exc:
            old = ""
            print(f"[autostart] 读取现有文件失败（将覆盖）：{exc}", file=sys.stderr)
        if old == text:
            print("[autostart] 已安装（内容一致，跳过）")
            return EXIT_OK
        print("[autostart] 已安装但内容不一致 ⇒ 覆盖，变更点：")
        for d in autostart.diff_summary(old, text):
            print("  " + d)

    try:
        autostart.write_vbs(path, text)
    except OSError as exc:
        print(f"[autostart] 写盘失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_STARTUP_INSTALL_FAILED

    fails = autostart.verify_vbs(spec, exe)
    if fails:
        print("[autostart] 写入后回读校验失败：", file=sys.stderr)
        for f in fails:
            print("  x " + f, file=sys.stderr)
        return EXIT_STARTUP_INSTALL_FAILED
    print(f"[autostart] 安装成功并已回读验证：{path}")
    return EXIT_OK


def startup_uninstall(dry_run: bool = False) -> int:
    """startup 后端卸载（幂等 ＋ 回读查无）。

    QA18-4：`--uninstall-autostart --dry-run` 曾**仍真删**。现在 dry_run
    只打印将删除的路径，不落盘。
    """
    spec = _this_spec()
    path = spec.startup_file
    if dry_run:
        print("[autostart] backend=startup（干跑，不删除）")
        print(f"  would remove : {path}")
        print(f"[autostart] 当前状态：{'存在（真跑会删）' if path.is_file() else '不存在（无需删）'}")
        return EXIT_OK
    if not path.is_file():
        print("[autostart] 未安装（文件不存在），无需卸载")
        return EXIT_OK
    try:
        path.unlink()
    except OSError as exc:
        print(f"[autostart] 删除失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_STARTUP_UNINSTALL_FAILED
    if path.is_file():
        print(f"[autostart] 卸载后文件仍存在：{path}", file=sys.stderr)
        return EXIT_STARTUP_UNINSTALL_FAILED
    print(f"[autostart] 已卸载（回读确认查无）：{path}")
    return EXIT_OK




def install_autostart(dry_run: bool = False, backend: str = "") -> int:
    """注册登录自启；`dry_run=True` 只打印定义（不落盘）。

    issue #18：**双后端**。`startup`（默认，零提权，写启动文件夹 `.vbs`）
    或 `scheduled`（原 UAC 计划任务路径，行为一行不改）。
    """
    backend = _norm_backend(backend)
    if backend == "startup":
        return startup_install(dry_run=dry_run)
    spec = task_spec()
    if spec.get("execute_note"):
        print("[autostart] 警告：%s" % spec["execute_note"], file=sys.stderr)
    if dry_run:
        print_spec(spec)
        cur = query_task()
        print("[autostart] 当前注册状态：%s" % ("已存在" if cur else "未注册"))
        print("[autostart] 真实注册需 UAC 提升权限的终端（由调用方授权），本命令到此为止。")
        return 0

    ps = (
        "$ErrorActionPreference='Stop';"
        "try {"
        " $a = New-ScheduledTaskAction -Execute '%s' -Argument '%s' -WorkingDirectory '%s';"
        " $t = New-ScheduledTaskTrigger -AtLogOn;"
        " $s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries"
        " -DontStopIfGoingOnBatteries -RestartCount 3"
        " -RestartInterval (New-TimeSpan -Minutes 1);"
        " Register-ScheduledTask -TaskName '%s' -Action $a -Trigger $t -Settings $s -Force | Out-Null;"
        " Write-Output '{\"ok\":true}'"
        "} catch {"
        " Write-Output ('{\"ok\":false,\"err\":' + (ConvertTo-Json ([string]$_.Exception.Message)) + '}')"
        "}"
    ) % (spec["execute"], spec["arguments"], spec["workdir"], spec["name"])
    rc, obj, raw = _ps_json(ps)
    if rc != 0 or not isinstance(obj, dict) or not obj.get("ok"):
        err = (obj or {}).get("err") if isinstance(obj, dict) else raw
        print("[autostart] 注册失败：%s" % err, file=sys.stderr)
        if "denied" in str(err).lower():
            print("[autostart] 提示：注册计划任务需要管理员权限（UAC 由调用方处理）。", file=sys.stderr)
        return EXIT_REGISTER_FAILED

    fails = verify_task(spec)
    hard = [f for f in fails if f != "__workdir_unverifiable__"]
    if hard:
        print("[autostart] 注册返回成功，但回读校验失败：", file=sys.stderr)
        for f in hard:
            print("  x %s" % f, file=sys.stderr)
        print("[autostart] 请手工清理：schtasks /Delete /TN %s /F" % spec["name"], file=sys.stderr)
        return EXIT_VERIFY_FAILED
    actual = query_task() or {}
    print("[autostart] 注册成功并已回读验证：")
    for k in ("name", "state", "execute", "arguments", "workdir"):
        print("  %-11s: %s" % (k, actual.get(k)))
    return 0


def uninstall_autostart(backend: str = "", dry_run: bool = False) -> int:
    """卸载自启（issue #18：双后端；QA18-4 / R2-1：两个后端都支持 dry-run）。"""
    backend = _norm_backend(backend)
    if backend == "startup":
        return startup_uninstall(dry_run=dry_run)
    # R2-1（QA 二轮）：scheduled 分支此前**没有** dry_run 判断，`--dry-run` 仍会
    # 执行 `Unregister-ScheduledTask` —— 而目标是**在役任务**，一旦误用等于把正在跑的
    # 自启拆掉。干跑必须早退，绝不能走到 _ps_json。
    if dry_run:
        print("[autostart] backend=scheduled（干跑，未卸载）："
              "将执行 Unregister-ScheduledTask -TaskName '%s'" % AUTOSTART_NAME())
        print("[autostart] 当前注册状态：%s" % ("已存在" if query_task() else "未注册"))
        return EXIT_OK
    ps = ("$ErrorActionPreference='Stop';"
          "Unregister-ScheduledTask -TaskName '%s' -Confirm:$false -ErrorAction SilentlyContinue;"
          "Write-Output '{}'") % AUTOSTART_NAME()
    _ps_json(ps)
    if query_task():
        print("[autostart] 卸载后仍能查到任务 %s" % AUTOSTART_NAME(), file=sys.stderr)
        return EXIT_UNINSTALL_FAILED
    print("[autostart] 已卸载（回读确认查无）")
    return 0

# The definition and read-back live in :mod:`task_spec`; kept importable here
# because ``decide_watch`` resolves these names by name on this module.
from .task_spec import task_spec, query_task, verify_task, print_spec  # noqa: E402,F401

