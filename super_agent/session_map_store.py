"""The lock and the atomic read-modify-write underneath :mod:`session_broker`.

Split out of ``session_broker`` only to keep each file under the 300-line cap
(charter §3.1). Nothing here decides *what* to write: it opens one lock, loads
both maps, hands them to a mutation, writes back atomically and reads back.

The lock covers **both** files on purpose. Two processes binding different
issues, or one binding while another rotates a key, mutate two maps that the
engine reads as one answer; two locks would let them interleave.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .store import atomic_write_text

SESSIONS_NAME = "sessions.json"
ISSUE_SESSIONS_NAME = "issue_sessions.json"
LOCK_NAME = "session_map.lock"
LOCK_TIMEOUT_S = 15.0


class BrokerError(RuntimeError):
    """The maps could not be written, or a read-back disagreed with the write."""


def paths(cfg: dict | None = None) -> tuple[Path, Path]:
    """``(sessions.json, issue_sessions.json)``, from the one resolver chain."""
    from . import session_ledger

    return (session_ledger.sessions_path(cfg),
            session_ledger.issue_sessions_path(cfg))


def lock_path(cfg: dict | None = None) -> Path:
    return paths(cfg)[0].parent / LOCK_NAME


class FileLock:
    """Cross-process mutex (``msvcrt`` on Windows, ``flock`` elsewhere).

    Non-blocking attempts in a bounded spin: two writers queue instead of
    interleaving, and a holder that never releases surfaces as a timeout naming
    the file, not as a hang.
    """

    def __init__(self, path: Path, timeout: float = LOCK_TIMEOUT_S):
        self.path = Path(path)
        self.timeout = timeout
        self._fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        if os.path.getsize(self.path) == 0:
            fh.write(b"0")
            fh.flush()
        deadline = time.time() + self.timeout
        while True:
            try:
                fh.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() >= deadline:
                    fh.close()
                    raise BrokerError(
                        f"session map lock busy > {self.timeout}s: {self.path}") from None
                time.sleep(0.01)
        self._fh = fh
        return self

    def __exit__(self, *_exc):
        fh, self._fh = self._fh, None
        if fh is None:
            return False
        try:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            fh.close()
        return False


def load_map(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_and_verify(path: Path, data: dict, sort_keys: bool) -> None:
    """Atomic save, then read back and compare: a mismatch is an error."""
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2,
                                       sort_keys=sort_keys))
    try:
        written = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BrokerError(f"{path.name} 回读失败：{type(exc).__name__}: {exc}") from None
    if written != data:
        raise BrokerError(f"{path.name} 回读与写入不一致（{len(written)} 键）")


def transact(mutate, cfg=None) -> dict:
    """Run ``mutate(sessions, issues)`` under the one lock; save what changed."""
    sessions_path, issues_path = paths(cfg)
    with FileLock(sessions_path.parent / LOCK_NAME):
        sessions = load_map(sessions_path)
        issues = load_map(issues_path)
        before = (json.loads(json.dumps(sessions)), json.loads(json.dumps(issues)))
        result = mutate(sessions, issues) or {}
        if sessions != before[0]:
            save_and_verify(sessions_path, sessions, sort_keys=False)
        if issues != before[1]:
            save_and_verify(issues_path, issues, sort_keys=True)
        return result
