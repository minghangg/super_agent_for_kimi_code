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

from . import session_map_io
from .store import atomic_write_text

SESSIONS_NAME = "sessions.json"
ISSUE_SESSIONS_NAME = "issue_sessions.json"
LOCK_NAME = "session_map.lock"
LOCK_TIMEOUT_S = 15.0

# Windows refuses ``os.replace`` with ``WinError 5`` while any process holds the
# target open for reading (the engine and the CLIs read these maps every round).
# The rename is still the right primitive -- it is what removed the torn reads a
# plain ``write_text`` produced -- so the fix is a bounded retry, not a revert.
# A reader that holds the file open longer than the whole retry window is not a
# torn read waiting to happen; it is a refusal the caller must see, so no
# in-place fallback is used: that would bring the torn reads back (#129 P0-1).
REPLACE_ATTEMPTS = 6
REPLACE_BACKOFF_S = 0.02
REPLACE_TOTAL_BUDGET_S = 1.0


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


def write_with_retry(path: Path, text: str) -> str:
    """Write a map: rename, retried; then an in-place write **under the lock**.

    Windows returns ``WinError 5`` from ``os.replace`` while any process holds
    the target open for reading, and a plain ``open()`` holder has no delete
    sharing at all -- measured on this machine: rename, ``MoveFileExW``, and
    delete-then-rename all fail while such a handle is open, whatever share
    flags the reader asked for. So a retry alone cannot satisfy QA P0-1 ①.

    Two behaviours, both required:

    * **rename first**, retried with backoff -- it is what removed the torn
      reads a plain overwrite produced;
    * **in-place write as the last resort**, which is the baseline behaviour a
      single open reader used to get for free. It is safe here because readers
      take the same lock (:func:`read_text_shared`), so no reader can observe a
      half-written file: the torn read that made an in-place write unacceptable
      is prevented by the lock, not by the rename.

    Returns ``"rename"`` or ``"inplace"`` so a degraded write is reported.
    """
    last: OSError | None = None
    deadline = time.time() + REPLACE_TOTAL_BUDGET_S
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            atomic_write_text(path, text)
            return "rename"
        except PermissionError as exc:
            last = exc
            wait = REPLACE_BACKOFF_S * (2 ** attempt)
            if attempt + 1 >= REPLACE_ATTEMPTS or time.time() + wait > deadline:
                break
            time.sleep(wait)
    try:
        _inplace_write(path, text)
    except OSError as exc:
        raise BrokerError(
            f"{path.name} 正在被读取，rename（{last}）与原地写（{exc}）都失败，"
            f"未写入任何内容") from exc
    return "inplace"


def _inplace_write(path: Path, text: str) -> None:
    """Overwrite the file in place. Only ever called with the map lock held.

    Readers take the same lock, so this cannot be observed half-written by any
    process in this repo. Its purpose is narrow: a foreign handle opened without
    delete sharing makes a rename impossible on Windows, and failing the write
    there would reintroduce the silent loss this change exists to remove.
    """
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())


def read_text_shared(path: Path) -> str:
    """Read a map under the lock (shared open + lock live in session_map_io)."""
    return session_map_io.read_text_shared(path)


def load_map(path: Path) -> dict:
    """Reader view: ``{}`` when absent or unparseable (writers never use this)."""
    return session_map_io.read_map(path)


def load_map_for_write(path: Path) -> dict:
    """Load a map that is about to be rewritten. **An unreadable map is an error.**

    The read side treats a broken file as ``{}`` so a reader keeps working; a
    writer must not. Loading ``{}`` and saving it back would erase every key in
    the file -- the loss would look like a successful transaction. A file that
    does not exist yet is a new map and is the one allowed empty case.
    """
    if not path.is_file():
        return {}
    try:
        data = json.loads(session_map_io.read_locked(path))
    except (OSError, ValueError) as exc:
        raise BrokerError(
            f"{path.name} 读不出（{type(exc).__name__}: {exc}），拒绝整份重写；"
            f"先修复或移走该文件") from None
    if not isinstance(data, dict):
        raise BrokerError(f"{path.name} 顶层不是 dict，拒绝整份重写")
    return data


def save_and_verify(path: Path, data: dict, sort_keys: bool) -> str:
    """Atomic save, then read back and compare: a mismatch is an error.

    Returns which write path ran (``"rename"`` or ``"inplace"``) so a caller
    can report the degraded case instead of hiding it.
    """
    mode = write_with_retry(path, json.dumps(data, ensure_ascii=False, indent=2,
                                             sort_keys=sort_keys))
    try:
        written = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BrokerError(f"{path.name} 回读失败：{type(exc).__name__}: {exc}") from None
    if written != data:
        raise BrokerError(f"{path.name} 回读与写入不一致（{len(written)} 键）")
    return mode


def transact(mutate, cfg=None) -> dict:
    """Run ``mutate(sessions, issues)`` under the one lock; save what changed.

    Two maps, one answer: when the second save fails the first is put back, so
    a caller never observes a bind that reached ``sessions.json`` but not
    ``issue_sessions.json`` (QA P0-1 ②). The restore is best effort in one
    respect only -- it is reported, never silently skipped.

    Both maps are loaded strictly (:func:`load_map_for_write`): a file that
    cannot be parsed is a refusal, not an empty map to be overwritten.
    """
    sessions_path, issues_path = paths(cfg)
    with FileLock(sessions_path.parent / LOCK_NAME):
        sessions = load_map_for_write(sessions_path)
        issues = load_map_for_write(issues_path)
        original = (json.loads(json.dumps(sessions)), json.loads(json.dumps(issues)))
        result = mutate(sessions, issues) or {}
        changed_sessions = sessions != original[0]
        changed_issues = issues != original[1]
        wrote_sessions = False
        try:
            if changed_sessions:
                save_and_verify(sessions_path, sessions, sort_keys=False)
                wrote_sessions = True
            if changed_issues:
                save_and_verify(issues_path, issues, sort_keys=True)
        except Exception:
            if wrote_sessions:
                try:
                    save_and_verify(sessions_path, original[0], sort_keys=False)
                except Exception as restore:  # noqa: BLE001  report, never mask
                    raise BrokerError(
                        f"{sessions_path.name} 回滚失败，文件可能已半写："
                        f"{type(restore).__name__}: {restore}") from None
            raise
        return result
