"""The scheduled task's definition and its read-back verification.

Split out of :mod:`autostart_install` (ISS-132, ``worker.md`` §3.1): registering a
task and **verifying** one are different jobs, and the verification half is the
one that matters. "Registered" is what the registrar printed; only a read-back
can say the task points at this script, this interpreter and this directory.

``__workdir_unverifiable__`` is a sentinel, not a message: a schtasks read-back
sometimes omits ``WorkingDirectory``, and the caller distinguishes "cannot see
it" from "it is wrong" - one is downgraded to a warning, the other refuses.
"""
from __future__ import annotations

import sys

from . import autostart_install as _host


def _config():
    return _host._config()


def _windowless():
    return _host._windowless()


def _ps_json(script: str, timeout: float = 60.0):
    return _host._ps_json(script, timeout)


def _module_path():
    return _host._module_path()


def _workspace_root():
    return _host._workspace_root()


def AUTOSTART_NAME():
    return _host.AUTOSTART_NAME()


def task_spec() -> dict:
    """本消费者的计划任务定义（单一来源：注册与回读校验都用它）。"""
    exe, note = _windowless()
    cfg = _config()
    script = str(cfg.startup_script or _module_path())
    return {
        "name": cfg.autostart_name,
        "execute": exe,
        "arguments": '"%s"' % script,
        "workdir": str(_workspace_root()),
        "script": script,
        "execute_note": note,
        "execute_is_windowless": not note,
    }


def query_task():
    """回读本任务；不存在返回 None。"""
    ps = (
        "$ErrorActionPreference='Stop';"
        "$t = Get-ScheduledTask -TaskName '%s' -ErrorAction SilentlyContinue;"
        "if (-not $t) { Write-Output 'null'; exit 0 };"
        "$a = $t.Actions[0];"
        "$o = [ordered]@{ name=$t.TaskName; state=[string]$t.State;"
        " execute=$a.Execute; arguments=$a.Arguments; workdir=$a.WorkingDirectory };"
        "Write-Output ($o | ConvertTo-Json -Compress)"
    ) % AUTOSTART_NAME()
    rc, obj, _raw = _ps_json(ps)
    return obj if rc == 0 and isinstance(obj, dict) else None


def verify_task(spec: dict) -> list[str]:
    """校验已注册任务等于 spec；返回失败项（空＝全过）。WorkingDirectory 不可读时降级。"""
    actual = query_task()
    if not actual:
        return ["任务未注册（Get-ScheduledTask 查无）"]
    fails = []
    if str(actual.get("execute") or "").strip().lower() != spec["execute"].strip().lower():
        fails.append("Execute 不符：期望 %r，实际 %r" % (spec["execute"], actual.get("execute")))
    args = str(actual.get("arguments") or "")
    if spec["script"] not in args:
        fails.append("Arguments 缺脚本路径：期望含 %r，实际 %r" % (spec["script"], args))
    wd = actual.get("workdir")
    if wd is None or str(wd).strip() == "":
        fails.append("__workdir_unverifiable__")
    elif str(wd).strip().lower() != spec["workdir"].strip().lower():
        fails.append("WorkingDirectory 不符：期望 %r，实际 %r" % (spec["workdir"], wd))
    return fails


def print_spec(spec: dict) -> None:
    """干跑输出（第 7 项要求：不注册也要能看清将建什么）。"""
    print("[autostart] 计划任务定义（干跑，未注册）：")
    print("  name       : %s" % spec["name"])
    print("  Execute    : %s" % spec["execute"])
    print("  Arguments  : %s" % spec["arguments"])
    print("  WorkingDir : %s" % spec["workdir"])
    print("  Trigger    : AtLogOn（登录时）")
    print("  Settings   : AllowStartIfOnBatteries / DontStopIfGoingOnBatteries / Restart×3")
    if spec.get("execute_note"):
        print("  ⚠ %s" % spec["execute_note"], file=sys.stderr)
