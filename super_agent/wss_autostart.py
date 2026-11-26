"""Login autostart for the WSS daemon: register, read back, unregister.

Why this module exists (ISS-131): the block was 243 lines inside
``wss_bus.py``, a file that had to change for the launcher's mutual exclusion
(the old daemon must recognise the launcher as the incumbent). ``worker.md``
§3.1.1 lets such a file change only while shedding at least 5% in the same
commit, and §3.1.1 #4 requires the shed code to be a **real module** rather than
a subclass or a mixin pasted back. Autostart is a separate job - it talks to
``schtasks``/PowerShell, never to the event bus - so it moves whole.

The seam is ``config``: this module has no opinion about where the autostart name
or the injectable PowerShell bridge come from, so the caller passes the config
accessor in. That keeps ``wss_bus`` as the single owner of ``WssBusConfig`` and
avoids a package-level import cycle.

Every name ``wss_bus`` used to export stays callable there: the workbench's
``decide_watch.py`` resolves ``AUTOSTART_NAME``/``task_spec``/``verify_task``/
``install_autostart``/``uninstall_autostart`` **by name** on the module, so
``wss_bus`` keeps live forwarders (see its bottom section) rather than a one-time
``from .autostart import *``.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

AUTOSTART_EXIT_OK = 0
AUTOSTART_EXIT_REGISTER_FAILED = 3      # the registration itself failed
AUTOSTART_EXIT_VERIFY_FAILED = 4        # registered, but the read-back mismatched
AUTOSTART_EXIT_UNINSTALL_FAILED = 5     # still present after uninstall

#: Set by :func:`bind`; a module-level accessor instead of a parameter keeps the
#: signatures the workbench calls by name unchanged.
_CONFIG = None
_QUERY = None


def bind(config) -> None:
    """Install the config accessor (``wss_bus._cfg``). Idempotent."""
    global _CONFIG
    _CONFIG = config


def bind_query_task(query) -> None:
    """Install the read-back callable, resolved through ``wss_bus`` at call time.

    The self-test patches ``wss_bus.query_task`` to simulate three read-back
    failures; routing through that name keeps the seam where it was.
    """
    global _QUERY
    _QUERY = query


def _cfg():
    if _CONFIG is None:
        raise RuntimeError("autostart: bind() 未调用，拒绝猜配置")
    return _CONFIG()


def _own_module_path() -> Path:
    """Where ``wss_bus`` itself lives, for the "is the task this module" test.

    Resolved from this file's parent, so it stays correct even when the caller
    is a shim in another checkout.
    """
    return Path(__file__).resolve().parent / "wss_bus.py"


def AUTOSTART_NAME() -> str:
    return str(_cfg().autostart_name or "WssBus")


def _ps_json(script: str, timeout: float = 60.0):
    """Run PowerShell and parse its JSON output. Returns ``(rc, obj, raw)``."""
    bridge = _cfg().ps_json
    if bridge is not None:
        return bridge(script, timeout)
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        # An absent PowerShell is a refusal with a reason, not a traceback: the
        # autostart path is only ever reached from an operator command.
        return 1, None, f"{type(exc).__name__}: {exc}"
    raw = (r.stdout or "").strip()
    obj = None
    if raw:
        try:
            obj = json.loads(raw)
        except ValueError:
            obj = None
    return r.returncode, obj, raw or (r.stderr or "").strip()


def windowless_python() -> tuple[str, str]:
    """``pythonw.exe`` next to the current interpreter; returns ``(path, note)``.

    Registering the autostart with ``python.exe`` pops a console window whenever
    the trigger fires. ``pythonw.exe`` is the same interpreter with the GUI
    subsystem: identical behaviour, no console. Logs already go to
    ``<state root>/wss_bus.log``, so stdout is not needed.

    Degradation: when the sibling ``pythonw.exe`` is missing (very old or trimmed
    distributions) the function returns ``python.exe`` and puts the reason in the
    note — the caller **prints a warning**, nothing is hidden.
    """
    inject = _cfg().windowless_python
    if inject is not None:
        return inject()
    exe = Path(sys.executable).resolve()
    cand = exe.with_name("pythonw.exe")
    if cand.is_file():
        return str(cand), ""
    return str(exe), ("同目录未找到 pythonw.exe（%s），降级用 python.exe —— "
                      "触发时**会弹控制台窗口**" % cand)


def task_spec() -> dict:
    """Expected scheduled-task definition — one source for register and verify.

    ``arguments`` depends on what the target *is*, and getting that wrong is
    silent: the task registers fine and only fails when it fires at logon.

      · a plain script (the workbench shim, or any file outside this package) ⇒
        ``"<script>" --daemon``;
      · this package's own module ⇒ ``-m super_agent.wss_bus --daemon`` with the
        workdir set to the checkout. Running ``python super_agent/wss_bus.py``
        fails with "attempted relative import with no known parent package" — it
        is a module inside a package, not a script (ISS-102 M3).

    The injection decides which branch runs: a caller that sets
    ``autostart_script`` to its own shim gets the first form, which is what the
    production switch uses.
    """
    cfg = _cfg()
    script = Path(cfg.autostart_script) if cfg.autostart_script else _own_module_path()
    workdir = Path(cfg.autostart_workdir) if cfg.autostart_workdir else script.parent.parent
    exe, note = windowless_python()
    own = _own_module_path()
    as_module = Path(script).resolve() == own
    return {
        "name": AUTOSTART_NAME(),
        "execute": exe,
        "arguments": ("-m super_agent.wss_bus --daemon" if as_module
                      else '"%s" --daemon' % Path(script).resolve()),
        "workdir": str(Path(workdir).resolve()),
        "script": str(Path(script).resolve()),
        "execute_note": note,
        "execute_is_windowless": not note,
    }


def query_task():
    """Read back the registered task; None when it does not exist."""
    ps = (
        "$ErrorActionPreference='Stop';"
        "$t = Get-ScheduledTask -TaskName '%s' -ErrorAction SilentlyContinue;"
        "if (-not $t) { Write-Output 'null'; exit 0 };"
        "$a = $t.Actions[0];"
        "$o = [ordered]@{ name=$t.TaskName; state=[string]$t.State;"
        " execute=$a.Execute; arguments=$a.Arguments; workdir=$a.WorkingDirectory };"
        "Write-Output ($o | ConvertTo-Json -Compress)"
    ) % AUTOSTART_NAME()
    rc, obj, raw = _ps_json(ps)
    if rc != 0 or not isinstance(obj, dict):
        return None
    return obj


def verify_task(spec: dict):
    """Compare the registered task with ``spec``; returns the failure list (empty ⇒ ok).

    ``WorkingDirectory`` is deliberately **not** a hard failure: some Windows
    versions do not echo it back after ``New-ScheduledTaskAction -WorkingDirectory``.
    When absent, verify only Execute + Arguments and say so with the sentinel
    ``__workdir_unverifiable__``.
    """
    actual = _query()
    if not actual:
        return ["任务未注册（Get-ScheduledTask 查无）"]

    fails = []
    exe_ok = str(actual.get("execute") or "").strip().lower() == spec["execute"].strip().lower()
    if not exe_ok:
        fails.append("Execute 不符：期望 %r，实际 %r"
                     % (spec["execute"], actual.get("execute")))

    args = str(actual.get("arguments") or "")
    if spec["script"] not in args:
        fails.append("Arguments 缺脚本路径：期望含 %r，实际 %r"
                     % (spec["script"], args))
    elif "--daemon" not in args:
        fails.append("Arguments 缺 --daemon：实际 %r" % args)

    wd = actual.get("workdir")
    if wd is None or str(wd).strip() == "":
        fails.append("__workdir_unverifiable__")
    elif str(wd).strip().lower() != spec["workdir"].strip().lower():
        fails.append("WorkingDirectory 不符：期望 %r，实际 %r"
                     % (spec["workdir"], wd))
    return fails


def _query():
    """The read-back, resolved through ``wss_bus`` at call time.

    The self-test of ``wss_bus`` patches ``query_task`` **on that module** to
    simulate three read-back failures. Resolving the name at call time keeps
    that seam working now that the body lives here.
    """
    if _QUERY is None:
        return query_task()
    return _QUERY()


def install_autostart() -> int:
    """Register login autostart, then read back and verify. Non-zero on failure.

    Exit codes (do not change): 0 registered+verified, 3 registration failed
    (permission/policy), 4 registered but verification failed.

    Why read back at all: the old implementation trusted its own
    ``registered`` print. ``Register-ScheduledTask`` throws ``Access is denied``
    when unelevated, but PowerShell defaults to ``Continue`` and the script
    still reached ``Write-Output 'registered'`` — success printed, exit 0, no
    task. ``$ErrorActionPreference='Stop'`` now turns the exception into JSON and
    ``Get-ScheduledTask`` verifies the fields.
    """
    spec = task_spec()
    if not spec.get("execute_is_windowless", True):
        print("[autostart] 警告：%s" % spec.get("execute_note"), file=sys.stderr)
    ps = (
        "$ErrorActionPreference='Stop';"
        "try {"
        " $a = New-ScheduledTaskAction -Execute '%s' -Argument '%s' -WorkingDirectory '%s';"
        " $t = New-ScheduledTaskTrigger -AtLogOn;"
        " $s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries"
        " -DontStopIfGoingOnBatteries -RestartCount 3"
        " -RestartInterval (New-TimeSpan -Minutes 1);"
        " Register-ScheduledTask -TaskName '%s' -Action $a -Trigger $t -Settings $s -Force"
        " | Out-Null;"
        " Write-Output '{\"ok\":true}'"
        "} catch {"
        " Write-Output ('{\"ok\":false,\"err\":'"
        " + (ConvertTo-Json ([string]$_.Exception.Message)) + '}')"
        "}"
    ) % (spec["execute"], spec["arguments"], spec["workdir"], spec["name"])

    rc, obj, raw = _ps_json(ps)
    if rc != 0 or not isinstance(obj, dict) or not obj.get("ok"):
        err = (obj or {}).get("err") if isinstance(obj, dict) else raw
        print("[autostart] 注册失败：%s" % err, file=sys.stderr)
        if "denied" in str(err).lower():
            print("[autostart] 提示：注册计划任务需要管理员权限，"
                  "请在**提升权限**的终端里执行本命令。", file=sys.stderr)
        return AUTOSTART_EXIT_REGISTER_FAILED

    fails = verify_task(spec)
    workdir_unverifiable = "__workdir_unverifiable__" in fails
    hard_fails = [f for f in fails if f != "__workdir_unverifiable__"]

    if hard_fails:
        print("[autostart] 注册返回成功，但回读校验失败：", file=sys.stderr)
        for f in hard_fails:
            print("  x %s" % f, file=sys.stderr)
        print("[autostart] 请手工清理：schtasks /Delete /TN %s /F" % spec["name"],
              file=sys.stderr)
        return AUTOSTART_EXIT_VERIFY_FAILED

    actual = _query() or {}
    print("[autostart] 注册成功并已回读验证：")
    print("  name       : %s" % actual.get("name"))
    print("  state      : %s" % actual.get("state"))
    print("  Execute    : %s" % actual.get("execute"))
    print("  Arguments  : %s" % actual.get("arguments"))
    if workdir_unverifiable:
        print("  WorkingDir : (本机 PowerShell 不回显，无法校验；期望 %s)" % spec["workdir"])
    else:
        print("  WorkingDir : %s" % actual.get("workdir"))
    print("  Trigger    : AtLogOn")
    if spec.get("execute_is_windowless"):
        print("  窗口       : 无（pythonw.exe，不弹控制台）")
    else:
        print("  窗口       : **会弹控制台**（降级用 python.exe，见上方警告）")
    return AUTOSTART_EXIT_OK


def uninstall_autostart() -> int:
    """Unregister login autostart, then confirm it is gone. Non-zero on failure.

    Exit codes: 0 uninstalled or absent (idempotent), 5 still present.
    """
    if _query() is None:
        print("[autostart] not installed（本来就没有该任务，无需卸载）")
        return AUTOSTART_EXIT_OK

    ps = (
        "$ErrorActionPreference='Stop';"
        "try {"
        " Unregister-ScheduledTask -TaskName '%s' -Confirm:$false;"
        " Write-Output '{\"ok\":true}'"
        "} catch {"
        " Write-Output ('{\"ok\":false,\"err\":'"
        " + (ConvertTo-Json ([string]$_.Exception.Message)) + '}')"
        "}"
    ) % AUTOSTART_NAME()

    rc, obj, raw = _ps_json(ps)
    if rc != 0 or not isinstance(obj, dict) or not obj.get("ok"):
        err = (obj or {}).get("err") if isinstance(obj, dict) else raw
        print("[autostart] 卸载命令失败：%s" % err, file=sys.stderr)
        return AUTOSTART_EXIT_UNINSTALL_FAILED

    if _query() is not None:
        print("[autostart] 卸载后仍能查到任务 %s，判定失败" % AUTOSTART_NAME(),
              file=sys.stderr)
        return AUTOSTART_EXIT_UNINSTALL_FAILED
    print("[autostart] 已卸载并回读确认查无：%s" % AUTOSTART_NAME())
    return AUTOSTART_EXIT_OK
