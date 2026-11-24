"""Reading a session map without blocking (or being blocked by) a writer.

Windows refuses ``os.replace`` while any process holds the target open for
reading, and — measured on this machine — it refuses it *whatever* share flags
the reader asks for, as long as the holder is a plain ``open()`` somewhere else.
That makes a pure rename-based writer impossible to keep correct here, and a
pure in-place writer reintroduces torn reads.

So readers and writers share one rule:

* a reader takes the map lock (:func:`read_map`), so it never observes a
  half-written file. That is what makes the writer's in-place path safe;
* the handle asks for ``FILE_SHARE_DELETE`` anyway, so a *writer's* rename in
  another process is not blocked by this read.

The lock is the correctness argument; the share flags are what keep the fast
path (rename) fast. Split out of :mod:`session_map_store` to keep that module
under the 300-line cap (charter §3.1) — this is I/O, that one is the
transaction.

Callers that already hold the lock use :func:`read_locked` instead: taking it
twice would block against itself.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

LOCK_NAME = "session_map.lock"


def _lock_class():
    """The lock implementation, imported lazily to avoid an import cycle."""
    from .session_map_store import FileLock

    return FileLock


def open_text(path: Path):
    """Open a map for reading with delete sharing (``FILE_SHARE_DELETE`` on Windows).

    A reader that is only reading has no reason to block a rename; this is what
    makes the writer's fast path (rename) work while the engine reads.
    """
    if os.name != "nt":
        return open(path, "rb")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    generic_read, share_all, open_existing, normal = 0x80000000, 0x7, 3, 0x80
    handle = kernel32.CreateFileW(str(path), generic_read, share_all, None,
                                  open_existing, normal, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    return os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDONLY), "rb")


def _read_bytes(path: Path) -> str:
    with open_text(path) as fh:
        return fh.read().decode("utf-8")


def read_locked(path: Path) -> str:
    """Read a map when the caller already holds :class:`FileLock`."""
    return _read_bytes(path)


# ``ReplaceFileW``/``os.replace`` swap the name, so for a few microseconds the
# name can be absent on Windows. That window is not a torn read -- the bytes are
# always one whole version -- but a reader that treats "absent" as "no keys"
# reports every key as missing, which is the silent error #129 is about. So a
# reader retries briefly and then says "unreadable", never "empty".
# The swap window is microseconds; a handful of sub-millisecond
# retries covers it without turning a missing file into a stall.
# ``os.replace`` is atomic with no name gap (measured: 0 gaps in 300 swaps).
# ``ReplaceFileW`` -- the fallback used when a reader holds the file -- does have
# a short gap, so the retries below are sized for it and cost nothing on the
# normal path (the first attempt succeeds and never sleeps).
READ_ATTEMPTS = 8
READ_BACKOFF_S = 0.002
# A read that lands during a swap must not be counted as "unreadable" in one
# breath: the first attempt is free, so the retries cost nothing when the file
# is simply there.


def read_json(path: Path):
    """One consistent view of a map, or ``None`` when it truly cannot be read.

    ``None`` and ``{}`` are different answers and callers must be able to tell
    them apart: "no keys" and "could not read" lead to opposite conclusions
    (a false ``keys_missing_in_events`` versus a clean ledger). Retries ride out
    the swap window without taking a lock, so the writer never depends on
    readers cooperating (#129 direction B).
    """
    path = Path(path)
    last: Exception | None = None
    for attempt in range(READ_ATTEMPTS):
        try:
            return json.loads(_read_bytes(path))
        except FileNotFoundError as exc:
            # A map that is not on disk at all is a normal state (a fresh state
            # dir has no issue_sessions.json). Retrying it slept 77 ms per call
            # -- measured -- and the engine reads these maps several times per
            # round, which is what made the suite hang (#129 supervisor
            # 7145038778). Only the *swap* window is worth retrying, and a swap
            # never leaves the name absent for longer than a rename; the first
            # read after a real creation succeeds.
            if not path.exists():
                return None
            last = exc
            time.sleep(READ_BACKOFF_S * (attempt + 1))
        except (OSError, ValueError) as exc:
            last = exc
            time.sleep(READ_BACKOFF_S * (attempt + 1))
    return None


def read_map(path: Path) -> dict:
    """Reader view: the map, or ``{}`` only when the file genuinely is empty.

    An unreadable file returns ``{}`` for callers that only need "best effort"
    (the context gate, the monitor), but every caller that draws a conclusion
    from an *empty* answer must use :func:`read_json` instead -- see
    ``session_events.read_sessions_text``.
    """
    data = read_json(path)
    return data if isinstance(data, dict) else {}


def read_text_shared(path: Path) -> str:
    """Raw text of a map. Raises when the file cannot be read.

    Name kept for the callers that already use it; it no longer takes the lock
    (see :func:`read_unlocked` for why the writer does not need it to).
    """
    return read_unlocked(path)


def read_unlocked(path: Path) -> str:
    """Read a map without taking the map lock, riding out the swap window.

    Safe because the writer never overwrites the target in place: it writes a
    temporary file and swaps it in with ``ReplaceFileW``/``os.replace``, so this
    handle always sees one whole version of the file -- the old one or the new
    one, never a torn blend. The *name* can be momentarily absent during the
    swap, so the read retries that window instead of reporting an empty file.

    Deliberately lock-free: a reader that had to hold the write lock would be
    part of the correctness argument for the writer, and direction B requires
    the writer to stand on its own.
    """
    text = read_text_retry(path)
    if text is None:
        raise FileNotFoundError(f"持续读不到 {path}（可能被外部进程独占）")
    return text


def read_text_retry(path: Path) -> str | None:
    """Bytes of a map, retried across the swap window; ``None`` if never readable.

    ``None`` means "unreadable", never "empty": the empty file is returned as
    ``""`` and parsed by the caller, which is what keeps ``reconcile`` from
    reporting an absent name as an empty ledger.
    """
    path = Path(path)
    for attempt in range(READ_ATTEMPTS):
        try:
            return _read_bytes(path)
        except FileNotFoundError:
            # Same rule as :func:`read_json`, and it must stay the same: a map
            # that is not on disk at all is a normal state (a fresh state dir
            # has no ``issue_sessions.json``), and retrying it cost 61 ms per
            # call in this hot path -- which is what held the suite at ~22 s
            # (#129 QA 7145879381). Only a *present* read is worth retrying.
            if not path.exists():
                return None
            if attempt + 1 >= READ_ATTEMPTS:
                break
            time.sleep(READ_BACKOFF_S * (attempt + 1))
        except (OSError, ValueError):
            return None      # a real read error is not a swap window
    return None

def read_sessions_text(path: Path) -> str | None:
    """One consistent view of a map; ``None`` when it cannot be read at all.

    ``None`` and ``"{}"`` are different answers and callers must be able to tell
    them apart: the second one is a real (empty) ledger, the first is "I could
    not read this", which must never be reported as "no keys" -- that is what
    made ``reconcile`` invent ``keys_missing_in_events`` (#129 cid 7142911506).

    Lock-free by design: the writer swaps the file atomically, so this read sees
    one whole version; the retry covers the moment the name is absent during
    the swap.
    """
    return read_text_retry(path)

def read_ledger(path: Path) -> tuple[dict, str]:
    """``(ledger, problem)``: ``{key: sid}`` plus why it could not be read.

    ``problem`` is ``""`` when the map was read and a sentence when it was not.
    The two answers must stay separate: an unreadable map is **not** an empty
    one, and reporting it as empty let ``reconcile`` pass a run whose ledger it
    could not see (#129 QA 7145879381). Kept here rather than in
    ``session_events`` so that module does not grow.
    """
    text = read_sessions_text(path)
    if text is None:
        return {}, f"sessions.json 读不出：{path}"
    try:
        data = json.loads(text)
    except ValueError as exc:
        return {}, f"sessions.json 解析失败：{path}（{type(exc).__name__}）"
    if not isinstance(data, dict):
        return {}, f"sessions.json 顶层不是对象：{path}"
    out = {}
    for key, item in data.items():
        sid = str((item or {}).get("id") or "") if isinstance(item, dict) else ""
        if sid:
            out[key] = sid
    return out, ""
