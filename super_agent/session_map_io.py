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


def read_map(path: Path) -> dict:
    """Read a map under the lock; ``{}`` when it is absent or unparseable.

    A reader degrades to ``{}`` on purpose (it must keep working), which is
    exactly why a writer never uses this function — see
    ``session_map_store.load_map_for_write``.
    """
    lock = _lock_class()
    with lock(Path(path).parent / LOCK_NAME):
        text = _read_bytes(Path(path))
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def read_json(path: Path):
    """Read a map under the lock, returning ``None`` when it cannot be read."""
    lock = _lock_class()
    try:
        with lock(Path(path).parent / LOCK_NAME):
            text = _read_bytes(Path(path))
    except (OSError, ValueError):
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def read_text_shared(path: Path) -> str:
    """Raw text of a map, read under the lock. Raises on an unreadable file."""
    lock = _lock_class()
    with lock(Path(path).parent / LOCK_NAME):
        return _read_bytes(Path(path))
