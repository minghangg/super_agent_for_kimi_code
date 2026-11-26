"""The injections the launcher must supply to the consumer legs (ISS-132 C2/F).

``decide_watch`` has no default for any of these - it refuses to guess a path, a
transport or a shell - and the workbench used to inject them from outside this
repo. The launcher is a new host, so it needs its own implementation of each, and
putting them in one module keeps "what the host provides" reviewable in one place.

Four of them carry a rule that is easy to undo:

**``ps_json`` never raises.** Its only caller reads back a scheduled task; a
missing PowerShell is "cannot verify", not "the consumer leg dies".

**``windowless_python`` tells the truth when it falls back.** A registered task
that opens a console window is a defect people notice at login, so the note is
returned with the path instead of being dropped.

**``heartbeat`` binds the state root explicitly.** ``wss_heartbeat`` refuses to
guess one on purpose (an isolated instance once wrote production), so the
launcher passes the root it owns on every beat rather than at import time.

**``send_transport`` is the package's own console client** - the same call
``decide_watch`` made in the workbench. It is not a fixture and not a stub: the
fixture gates decide whether a send may leave the machine, not this function.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def powershell_exe() -> str:
    import shutil

    return shutil.which("powershell") or "powershell.exe"


def ps_json(script: str, timeout: float = 60.0):
    """Run a PowerShell snippet, parse stdout as JSON: ``(rc, obj, raw)``."""
    try:
        proc = subprocess.run(
            [powershell_exe(), "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, {}, f"{type(exc).__name__}: {exc}"
    raw = (proc.stdout or "").strip()
    if proc.returncode != 0:
        return proc.returncode, {}, (proc.stderr or raw).strip()
    try:
        return 0, json.loads(raw or "null"), raw
    except ValueError:
        return 1, {}, raw


def windowless_python() -> tuple[str, str]:
    """``pythonw.exe`` beside this interpreter, plus a note when it is missing."""
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    if candidate.is_file():
        return str(candidate), ""
    return str(exe), "pythonw.exe 不在解释器旁边 ⇒ 注册的任务会带控制台窗口"


def probe_session(sid: str):
    """``(info, error)`` - an unreachable console is "unknown", not a crash."""
    from .. import api

    try:
        return api.session_info(sid), ""
    except Exception as exc:            # noqa: BLE001
        return {}, f"{type(exc).__name__}: {exc}"


def send_transport(sid: str, text: str, timeout: float = 60.0):
    from .. import direct

    return direct.send(sid, text)


def boot_id() -> str:
    """The daemon's boot id, or ``""`` when it is down (which is not an error)."""
    try:
        from .. import wss_bus

        return str((wss_bus.bus_health() or {}).get("boot_id") or "")
    except Exception:                   # noqa: BLE001
        return ""


def thresholds():
    from .. import wss_bus

    return wss_bus.guardian_thresholds()


def heartbeat(store_root: Path):
    """A heartbeat writer bound to **this** state root on every beat."""
    from .. import wss_heartbeat

    def write(name: str, extra: dict | None = None) -> None:
        try:
            wss_heartbeat.bind(thresholds=thresholds,
                               state_root=lambda: Path(store_root),
                               boot_id=boot_id, log=lambda _msg: None)
            wss_heartbeat.write_heartbeat(name, extra)
        except Exception:               # noqa: BLE001  a beat never kills a round
            pass

    return write


def workspace_root(cfg: dict) -> Path:
    return Path(str(cfg.get("workspace_root") or os.getcwd())).resolve()
