"""Heartbeat read/write for the guarded parties (ISS-131).

Why moved out of ``wss_bus.py``: this is a small, complete job - one file per
guarded party, atomic write, visible failure marker - and it shared nothing with
the event ring, the journal or the HTTP surface. It moved in the same commit as
the launcher's lock work, which is what ``worker.md`` §3.1.1 #4 requires: the
shed code becomes a real module, not a subclass pasted back.

Four decisions are carried over unchanged, because each one is a fault that
already happened:

**Single writer per name.** Two parties sharing one heartbeat file means a dead
party is masked by the other's fresh one. The file name comes from the threshold
table, injected here (:func:`bind`), so registration and the write side can never
disagree.

**Unconditional every round.** "Nothing changed, skip the write" makes a healthy
party look stale on the next patrol.

**Reads and writes from one root.** Both go through the injected
``state_root`` accessor (``current_state_root()`` in the daemon). When the two
sides resolved independently, an isolated instance wrote production while
production read production - healthy on somebody else's heartbeat.

**Failure is visible.** ``os.replace`` is retried at 0/20/50/100 ms; if all four
fail, a marker file appears and ``/healthz.guardians.<name>.write_fail`` reports
it. The next successful write deletes the marker, so it cannot outlive the fault.
A log line alone is not enough: the daemon's stderr is not where anyone looks.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

#: 0/20/50/100 ms, ~170 ms total. Short on purpose: a heartbeat write must never
#: hold the patrol round hostage.
HEARTBEAT_WRITE_BACKOFF = (0.0, 0.02, 0.05, 0.1)

#: ``<state_root>/heartbeat/_writefail_<name>.json``
HEARTBEAT_WRITEFAIL_PREFIX = "_writefail_"

_THRESHOLDS = None
_STATE_ROOT = None
_BOOT_ID = None
_LOG = None


def bind(*, thresholds, state_root, boot_id, log) -> None:
    """Install the four seams. Idempotent, and called on every daemon entry.

    Bindings are callables rather than values so a ``configure()`` that moves the
    state root takes effect on the next beat - the alternative (capturing a Path
    at import time) is the "wrote production while reading isolated" bug again.
    """
    global _THRESHOLDS, _STATE_ROOT, _BOOT_ID, _LOG
    _THRESHOLDS = thresholds
    _STATE_ROOT = state_root
    _BOOT_ID = boot_id
    _LOG = log


def _thresholds():
    if _THRESHOLDS is None:
        raise RuntimeError("wss_heartbeat: bind() 未调用，拒绝猜监护表")
    return _THRESHOLDS()


def _root() -> Path:
    if _STATE_ROOT is None:
        raise RuntimeError("wss_heartbeat: bind() 未调用，拒绝猜状态根")
    return _STATE_ROOT()


def _log(msg: str) -> None:
    if _LOG is not None:
        _LOG(msg)


def relative_path(name: str) -> str:
    """The heartbeat file for ``name``, relative to the state root, or ``""``."""
    return _thresholds().get(name, ("", 0.0, ""))[0]


def writefail_path(name: str) -> Path:
    return _root() / "heartbeat" / f"{HEARTBEAT_WRITEFAIL_PREFIX}{name}.json"


def read_heartbeat(name: str) -> dict | None:
    """Read one guarded party's heartbeat; returns ``{ts, ...}``, None, or a marker.

    The three failures are kept apart on purpose:
      * ``FileNotFoundError`` ⇒ ``None`` (state=missing, the original meaning);
      * any other ``OSError`` (``PermissionError``, …) ⇒ ``{"_read_error": "<class>"}``
        (state=unknown — on Windows a concurrent read during ``os.replace`` must
        never be mistaken for "file does not exist");
      * ``ValueError`` (bad JSON) ⇒ ``None`` (still missing; unchanged here).
    """
    rel = relative_path(name)
    if not rel:
        return None
    try:
        d = json.loads((_root() / rel).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except FileNotFoundError:
        return None
    except OSError as exc:
        return {"_read_error": type(exc).__name__}
    except ValueError:
        return None


def mark_write_fail(name: str, attempts: int, err: BaseException) -> None:
    """Terminal-failure marker: **visible** in `/healthz.guardians.<name>.write_fail`.

    Why a file and not just a log line (ISS-102 M5): the daemon's own stderr is
    not where anyone looks, and a heartbeat that never lands is indistinguishable
    from a party that is simply idle. The marker turns "silently not written"
    into a field a caller can assert on, and it is deleted the moment a write
    succeeds so it cannot outlive the fault.
    """
    try:
        p = writefail_path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"ts": time.time(),
                                 "iso": datetime.now().isoformat(timespec="seconds"),
                                 "attempts": attempts,
                                 "err": f"{type(err).__name__}: {err}",
                                 "pid": os.getpid()}, ensure_ascii=False),
                     encoding="utf-8")
    except OSError as exc:
        _log(f"guardian: 写 {name} 的写失败标记也没写成（就剩日志这一条线索）：{exc}")


def clear_write_fail(name: str) -> None:
    try:
        writefail_path(name).unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        _log(f"guardian: 清 {name} 的写失败标记失败（下次成功会再试）：{exc}")


def read_write_fail(name: str) -> dict | None:
    """The marker for `/healthz`; ``None`` when the last write succeeded."""
    try:
        return json.loads(writefail_path(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_heartbeat(name: str, extra: dict | None = None) -> None:
    """Write a heartbeat (single writer per name, see :func:`relative_path`).

    **Unconditional every round**: "no change" must not look stale.
    **Which root**: the injected accessor — same source as the read side,
    otherwise an isolated instance writes production and production reads
    production, looking "healthy" on someone else's heartbeat.
    **Failure is visible** (ISS-102 M5): ``os.replace`` retries at
    0/20/50/100ms; if all four fail, the marker written by :func:`mark_write_fail`
    shows up as ``/healthz.guardians.<name>.write_fail``, and the next successful
    write deletes it. The atomic-write semantics are unchanged (tmp +
    ``os.replace``); what changed is that a permanent failure no longer ends as
    one log line.
    """
    rel = relative_path(name)
    if not rel:
        return
    root = _root()
    body = {"ts": time.time(), "iso": datetime.now().isoformat(timespec="seconds"),
            "pid": os.getpid(),
            "boot_id": _BOOT_ID() if _BOOT_ID is not None else "",
            "state_dir": str(root)}
    if extra:
        body.update(extra)
    p = root / rel
    tmp = p.with_suffix(".json.tmp")
    last: BaseException | None = None
    for delay in HEARTBEAT_WRITE_BACKOFF:
        if delay:
            time.sleep(delay)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, p)
        except OSError as exc:
            last = exc
            continue
        clear_write_fail(name)
        return
    _log(f"guardian: 写 {name} 心跳失败（已重试 {len(HEARTBEAT_WRITE_BACKOFF)} 次）：{last}")
    mark_write_fail(name, len(HEARTBEAT_WRITE_BACKOFF), last)
