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
# One swap attempt, no retry loop. Direction B (#129 cid 7142911506) asks for a
# loud failure rather than a writer that waits, and a retry window would also
# hold the map lock for up to a second -- which every other bind pays for.
REPLACE_ATTEMPTS = 1


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


def atomic_replace(tmp: Path, path: Path) -> None:
    """Swap the temporary file into place with ``os.replace`` -- nothing else.

    Only one mechanism is used, and it is the atomic one: the target name always
    refers to one whole file, so a reader never sees a torn or absent map.

    A Windows reader that holds the target open without delete sharing makes
    this swap impossible. That case is a **refusal**, not a cue to write in
    place: an in-place overwrite is what produced torn reads, and
    ``ReplaceFileW`` (the other candidate) was measured to leave the name absent
    ~1000 times per 300 swaps under a concurrent reader. Both are therefore not
    used, per #129 direction B (cid 7142911506).
    """
    try:
        os.replace(tmp, path)
    except OSError as exc:
        raise BrokerError(
            f"{path.name} 无法原子替换（{exc}）；未写入任何内容。"
            f"若提示文件被占用：有外部进程用非共享方式打开了它，"
            f"关闭该进程后重试（写入端不会原地覆盖）") from exc


def write_atomic(path: Path, text: str) -> None:
    """Write through a temporary file and swap it in atomically.

    One attempt, no retry loop: a writer that waits holds the map lock, which
    every other bind pays for, and direction B asks for a loud failure rather
    than a wait. A failed write leaves the old file byte-identical and removes
    the temporary file -- it is never degraded into an in-place overwrite.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")

    def _fill() -> None:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())

    try:
        _fill()
        atomic_replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def read_text_shared(path: Path) -> str:
    """Read a map without any lock, riding out the writer's swap window.

    Forwarder to :mod:`session_map_io`; the retry is what keeps a reader from
    reporting "unreadable" for the microseconds a swap takes (#129 direction B).
    """
    return session_map_io.read_text_retry(path) or ""


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

    Only one write path exists: temporary file, then atomic replace. A caller
    cannot get a degraded in-place write, so the return value is always
    ``"atomic"`` -- kept so the signature stays stable for callers that logged
    the old mode.
    """
    write_atomic(path, json.dumps(data, ensure_ascii=False, indent=2,
                                  sort_keys=sort_keys))
    mode = "atomic"
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
