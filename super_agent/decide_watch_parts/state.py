"""The delivery-watch state file: default shape, load, atomic save.

Why this is a module (ISS-132 C2): the shape is a contract with the previous
process. ``offset`` has to distinguish "never written" (``None``) from "written
as an empty ledger" (``0``), and that distinction is the whole reason the
2026-11-07 replay incident cannot repeat - so the rule deserves a docstring and
a file rather than a spot in the middle of the consumer.

The paths are **injected** (:func:`bind`), not read from a config here: the host
module owns the ``DecideWatchConfig`` resolution order, and a second resolution
would let a fixture run write production.

``save`` keeps the ``_DELIVERY_WRITE_FAILED`` latch on the host (see
:func:`set_write_failed`): the consumer checks that flag before every tick, so
it must be one value owned by one module, not a copy per writer.
"""
from __future__ import annotations

import json
import os
from datetime import datetime

_STATE_PATH = None
_LOG = None
_SET_WRITE_FAILED = None


def bind(*, state_path, log, set_write_failed) -> None:
    """Install the three seams. Idempotent; called by the host on every entry."""
    global _STATE_PATH, _LOG, _SET_WRITE_FAILED
    _STATE_PATH = state_path
    _LOG = log
    _SET_WRITE_FAILED = set_write_failed


def _path():
    if _STATE_PATH is None:
        raise RuntimeError("decide_watch_parts.state: bind() 未调用，拒绝猜状态文件")
    return _STATE_PATH()


def _log(msg: str) -> None:
    if _LOG is not None:
        _LOG(msg)


def default() -> dict:
    return {
        "v": 1,
        "baseline_established": False,
        # 方案 A（issue #15 V1 补修）：默认 **None** 而非 0。
        # 旧写法预填 0 ⇒ `"offset" not in st` 恒为假（死代码）⇒ state 只写
        # `{"baseline_established": true}` 时会**从 0 读、回放全部历史**。
        # 用 None 才能区分「默认值（从未写过）」与「显式写入的 0（空台账，合法）」。
        "offset": None,
        "line_no": 0,
        "pending_partial": "",
        "last_partial": "",
        "seen": {},
        "last_nudge": {},
        "day_nudges": {},
        "breakers": {},
        "pending": {},
        "summaries": {},
        "stats": {},
        "last_heartbeat": 0.0,
        "last_pending_check": 0.0,
        "undelivered_offset": 0,
    }


def load() -> dict:
    st = default()
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        st["_baseline_reason"] = "no_state"
        return st
    except (OSError, ValueError):
        st["_baseline_reason"] = "bad_json"
        return st
    if not isinstance(raw, dict):
        st["_baseline_reason"] = "bad_json"
        return st
    st.update(raw)
    if raw.get("baseline_established") is not True:
        st["_baseline_reason"] = "no_flag"
    elif "undelivered_offset" not in raw:
        st["_baseline_reason"] = "offset_missing"
    return st


def save(st: dict) -> bool:
    """Atomic save; returns whether it landed. Failure is reported, never raised.

    A raised exception here would skip the caller's bookkeeping and let the next
    tick re-read the same ledger rows with ``seen`` unpersisted - i.e. send the
    same deliveries again. So the failure becomes a latch
    (``set_write_failed(True)``) that makes ``delivery_tick`` stop sending.
    """
    p = _path()
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        clean = {k: v for k, v in st.items() if not k.startswith("_")}
        tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
    except OSError as exc:
        if _SET_WRITE_FAILED is not None:
            _SET_WRITE_FAILED(True)
        _log(f"状态写盘失败（停止后续收单发送；修复后重启）：{exc}")
        return False
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    if _SET_WRITE_FAILED is not None:
        _SET_WRITE_FAILED(False)
    return True


def date_of(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
