"""Single-instance guard: ``state/engine.pid`` created with O_CREAT|O_EXCL.

The file is created atomically, so two processes racing for the same state
directory cannot both win: one creates it, the other gets ``FileExistsError``
and reads the incumbent. Content is ``{pid, started_at, cmdline_hash}``.

A file is *stale* only when the recorded pid is not alive, or when it is alive
but its command line no longer matches the recorded hash (the pid was reused by
an unrelated process). Stale files are taken over by unlink + exclusive
re-create, which keeps the same race semantics. Normal exit removes the file;
an abnormal exit leaves it and the stale detection cleans it up next start.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path


class LockError(RuntimeError):
    pass


def _cmdline_hash(argv: list[str] | None = None) -> str:
    parts = list(argv if argv is not None else ["engine"])
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:16]


def process_alive(pid: int) -> bool:
    """Best-effort liveness probe that does not signal the process."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import subprocess
        try:
            done = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return False
        out = (done.stdout or "").strip()
        return bool(out) and "no tasks" not in out.lower() and str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _wmic_cmdline(pid: int) -> str | None:
    """``wmic`` probe. Absent on recent Windows builds, so this may return None."""
    import subprocess
    try:
        done = subprocess.run(
            ["wmic", "process", "where", f"ProcessId={int(pid)}",
             "get", "CommandLine", "/value"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    for line in (done.stdout or "").splitlines():
        if line.startswith("CommandLine="):
            return line.split("=", 1)[1].strip() or None
    return None


def _cim_cmdline(pid: int) -> str | None:
    """``Get-CimInstance`` fallback: the probe that still works without wmic."""
    import subprocess
    script = (f"Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}' "
              "| Select-Object -ExpandProperty CommandLine")
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    return " ".join((done.stdout or "").split()) or None


def _pid_cmdline(pid: int) -> str | None:
    """Command line of ``pid`` when the platform can report it, else None.

    Windows tries ``wmic`` first and the CIM/PowerShell probe second: wmic is
    gone from recent builds, and without a fallback every pid-reuse check would
    silently pass and only a human could ever clear a stale lock. None is
    returned only after *both* probes failed, which keeps the caller's
    conservative "assume it is the incumbent" behaviour instead of guessing.
    """
    if os.name == "nt":
        text = _wmic_cmdline(pid)
        if text is None:
            text = _cim_cmdline(pid)
        return text
    try:
        raw = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
    except OSError:
        return None
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()


def _cmdline_matches(pid: int, recorded_hash: str) -> bool:
    """True when the live pid's command line still hashes to the record.

    When the platform cannot report the command line (no /proc, wmic missing)
    the check is skipped rather than guessed: a live pid with the recorded
    hash and no way to disprove it is treated as the incumbent.
    """
    text = _pid_cmdline(pid)
    if text is None:
        return True
    return _cmdline_hash(text.split()) == recorded_hash or \
        _cmdline_hash([text]) == recorded_hash


class PidLock:
    def __init__(self, path: str | Path, *, argv: list[str] | None = None):
        self.path = Path(path)
        self.argv = list(argv) if argv is not None else ["engine"]
        # Record the *actual* command line when the platform can report it.
        # Hashing the caller's abstract argv instead would never match the real
        # process, and every live holder would look stale.
        self.hash = self._identity()
        self.info: dict = {}
        self.acquired = False
        self.took_over = False

    def _identity(self) -> str:
        text = _pid_cmdline(os.getpid())
        if text:
            return _cmdline_hash(text.split())
        return _cmdline_hash(self.argv)

    # ------------------------------------------------------------------ state
    def read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _create(self) -> dict:
        payload = {"pid": os.getpid(), "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "cmdline_hash": self.hash, "argv": self.argv}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return payload

    def is_stale(self, info: dict | None = None) -> bool:
        info = self.read() if info is None else info
        if not info:
            return True
        pid = info.get("pid")
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return True
        if not process_alive(pid):
            return True
        return not _cmdline_matches(pid, str(info.get("cmdline_hash") or ""))

    def try_acquire(self) -> tuple[bool, dict]:
        """``(acquired, info)``. On refusal ``info`` is the incumbent's record."""
        try:
            self.info = self._create()
            self.acquired = True
            return True, dict(self.info)
        except FileExistsError:
            pass
        info = self.read()
        if not info:
            # A concurrent creator holds the file but has not written it yet:
            # ``O_EXCL`` makes the creation atomic, the content is written right
            # after. Re-reading briefly stops a mid-create file from looking
            # stale, which would let two processes both "take it over".
            for _ in range(20):
                time.sleep(0.01)
                info = self.read()
                if info:
                    break
        if self.is_stale(info):
            self.took_over = True
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            try:
                self.info = self._create()
                self.acquired = True
                return True, dict(self.info)
            except FileExistsError:
                return False, self.read()
        return False, info

    def release(self) -> None:
        if not self.acquired:
            return
        info = self.read()
        if int(info.get("pid") or -1) == os.getpid():
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self.acquired = False

    def describe(self, info: dict | None = None) -> str:
        info = self.read() if info is None else info
        if not info:
            return "engine.pid absent or empty"
        return (f"engine.pid held by pid={info.get('pid')} "
                f"since={info.get('started_at')} hash={info.get('cmdline_hash')}")

    def __enter__(self) -> "PidLock":
        ok, info = self.try_acquire()
        if not ok:
            raise LockError(f"another engine owns this state dir: {self.describe(info)}")
        return self

    def __exit__(self, *_exc) -> None:
        self.release()
