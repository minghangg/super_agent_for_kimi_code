#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""开发算力配额：机器自检默认值、配置读写、时段窗口与 heavy 并发计数。

统一入口给 `tools/quiet.py` 与派单器使用。只用标准库；配置缺失或字段缺省时
生成并写回 `config/dev_quota.json`，读写失败 WARN 但不崩。北京时间判定不依赖
本机时区。窗口起点含、终点不含，支持跨午夜。

自检公式按任务书字面「剩余核数的一半」＝ (cores - pinned) // 2；8 核 16G
因此是 pinned=3、offpeak_extra=2。临时副本可用环境变量把配置与核锁目录隔离。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_REL = "config/dev_quota.json"
CONFIG_PATH_ENV = "DEV_QUOTA_CONFIG"
STATE_CORES_ENV = "DEV_QUOTA_CORES_DIR"


def default_config_path() -> str:
    return os.path.join(BASE, CONFIG_REL)


def config_path() -> str:
    """Where the quota file lives.

    Three layers, most specific first: the ``DEV_QUOTA_CONFIG`` environment
    variable, the deployment's ``quota.config_file`` in ``agent_config``, then
    this checkout's own ``config/dev_quota.json``.

    The middle layer is the one that matters for an open repository: the quota
    file is generated at runtime, so its default location writes run state into
    a tracked directory. A deployment points ``quota.config_file`` at an
    ignored path (``new_workbench/`` in the private workbench) and nothing
    lands here. Before this, that key had **no consumer at all** -- the file was
    written into the public checkout on every first run (ISS-95).
    """
    env = os.environ.get(CONFIG_PATH_ENV)
    if env:
        return env
    try:
        from . import agent_config
        configured = agent_config.quota_config()
    except Exception:  # noqa: BLE001  配额不该因为配置层坏了而崩
        configured = ""
    if configured:
        return configured
    return default_config_path()


def cores_dir() -> str:
    return os.environ.get(STATE_CORES_ENV) or os.path.join(BASE, "state", "quiet_cores")


BJ_TZ = timezone(timedelta(hours=8))
DEFAULT_WINDOW = "03:00-09:00"
DEFAULT_NOTE = ("开发算力配额：pinned_cores 是全时段 heavy worker 并发上限；offpeak_extra "
                "在窗口内追加核数；heavy_concurrency 为 null 时按 pinned_cores + 窗口内 extra "
                "推导，显式整数则覆盖。默认值由标准库机器自检生成，可用 python tools/dev_quota.py "
                "doctor 复查；窗口北京时间 HH:MM-HH:MM，可跨午夜，起点含终点不含。")
LOCK_FRESH_SECONDS = 5.0
_SELF_CHECK_WARNED = False


def warn(message: str) -> None:
    print(f"[dev_quota] WARN: {message}", file=sys.stderr, flush=True)


def machine_info(cpu_count: int | None = None, ram_bytes: int | None = None) -> dict[str, Any]:
    """标准库自检 CPU 与物理内存；失败时保守回落，不引第三方依赖。"""
    cores = int(cpu_count if cpu_count is not None else (os.cpu_count() or 1))
    cores = max(1, cores)
    if ram_bytes is not None:
        total = max(0, int(ram_bytes))
    else:
        total = 0
        try:
            if os.name == "nt":
                class MEMORYSTATUSEX(ctypes.Structure):
                    _fields_ = [
                        ("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                    ]
                stat = MEMORYSTATUSEX()
                stat.dwLength = ctypes.sizeof(stat)
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                if kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                    total = int(stat.ullTotalPhys)
                else:
                    warn("GlobalMemoryStatusEx 取物理内存失败，按保守小机处理")
            elif hasattr(os, "sysconf"):
                pages = os.sysconf("SC_PHYS_PAGES")
                page_size = os.sysconf("SC_PAGE_SIZE")
                if pages and page_size:
                    total = int(pages) * int(page_size)
        except (AttributeError, OSError, ValueError) as exc:
            warn(f"物理内存自检失败（{type(exc).__name__}: {exc}），按保守小机处理")
    ram_gb = round(total / (1024 ** 3), 1) if total else 0.0
    return {"cores": cores, "ram_bytes": total, "ram_gb": ram_gb}


def self_check(cpu_count: int | None = None, ram_bytes: int | None = None) -> dict[str, Any]:
    """按机器规格给保守默认值：小机减半且不加窗口额度；大机 3＋剩余核数一半。"""
    global _SELF_CHECK_WARNED
    info = machine_info(cpu_count, ram_bytes)
    cores, ram_bytes_value = int(info["cores"]), int(info["ram_bytes"])
    if cores <= 4 or ram_bytes_value <= 8 * (1024 ** 3):
        pinned = max(1, cores // 2)
        extra = 0
    else:
        pinned = min(3, cores)
        extra = max(0, (cores - pinned) // 2)
    doc: dict[str, Any] = {
        "pinned_cores": pinned,
        "offpeak_extra": {"window": DEFAULT_WINDOW, "cores": extra},
        "heavy_concurrency": None,
        "_note": DEFAULT_NOTE,
    }
    if not _SELF_CHECK_WARNED:
        _SELF_CHECK_WARNED = True
        warn(f"按 {cores} 核 {info['ram_gb']:g}G 自检："
             f"pinned={pinned} offpeak_extra={extra}")
    return doc


def self_check_line(cpu_count: int | None = None, ram_bytes: int | None = None) -> str:
    doc = self_check(cpu_count, ram_bytes)
    info = machine_info(cpu_count, ram_bytes)
    return (f"按 {info['cores']} 核 {info['ram_gb']:g}G 自检："
            f"pinned={doc['pinned_cores']} offpeak_extra={doc['offpeak_extra']['cores']}")


def _int_field(value: Any, label: str, minimum: int = 0) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        warn(f"{CONFIG_REL} 的 {label} 不是 ≥{minimum} 的整数：{value!r}；该项按缺省处理")
        return None
    return value


def normalize(doc: Any, *, path: str | None = None) -> tuple[dict[str, Any], bool]:
    """校验并补齐字段；返回 (可用配置, 是否发生改写)。坏 JSON 由 read_config 处理。"""
    target = path or config_path()
    if not isinstance(doc, dict):
        warn(f"{target} 顶层不是 JSON 对象，按机器自检重建")
        doc = {}
    result: dict[str, Any] = {}

    pinned = _int_field(doc.get("pinned_cores"), "pinned_cores", 1)
    if pinned is None:
        pinned = self_check()["pinned_cores"]
    result["pinned_cores"] = pinned

    raw_extra = doc.get("offpeak_extra")
    if not isinstance(raw_extra, dict):
        if raw_extra is not None:
            warn(f"{target} 的 offpeak_extra 不是对象，按机器自检补齐")
        raw_extra = {}
    window = raw_extra.get("window", DEFAULT_WINDOW)
    if not isinstance(window, str) or not window.strip():
        warn(f"{target} 的 offpeak_extra.window 不是非空字符串，按 {DEFAULT_WINDOW} 处理")
        window = DEFAULT_WINDOW
    extra_cores = _int_field(raw_extra.get("cores"), "offpeak_extra.cores", 0)
    if extra_cores is None:
        extra_cores = self_check()["offpeak_extra"]["cores"]
    result["offpeak_extra"] = {"window": window, "cores": extra_cores}

    raw_override = doc.get("heavy_concurrency")
    if raw_override is None:
        override = None
    else:
        override = _int_field(raw_override, "heavy_concurrency", 1)
    result["heavy_concurrency"] = override

    note = doc.get("_note")
    if not isinstance(note, str) or not note.strip():
        note = DEFAULT_NOTE
    result["_note"] = note
    known = {k: v for k, v in doc.items()
             if k in ("pinned_cores", "offpeak_extra", "heavy_concurrency", "_note")}
    return result, result != known


def _write_config(doc: dict[str, Any], path: str) -> bool:
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        return True
    except OSError as exc:
        warn(f"写入 {path} 失败（{type(exc).__name__}: {exc}）；本次用内存默认值继续")
        return False


def read_config(path: str | None = None, *, write_back: bool = True) -> dict[str, Any]:
    """读配置；缺失、坏 JSON 或缺字段时生成默认值。任何 I/O 失败都不崩。"""
    target = path or config_path()
    raw: Any = None
    missing = False
    try:
        with open(target, encoding="utf-8-sig") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        missing = True
        warn(f"{target} 不存在，按机器自检生成默认值")
    except (OSError, ValueError) as exc:
        missing = True
        warn(f"读取 {target} 失败（{type(exc).__name__}: {exc}）；按机器自检生成默认值")
    if missing:
        defaults = self_check()
        public = {k: v for k, v in defaults.items() if not k.startswith("_")}
        public["_note"] = DEFAULT_NOTE
        if write_back and _write_config(public, target):
            warn(f"已生成 {target}（来源：标准库机器自检；"
                 f"pinned_cores={public['pinned_cores']}，"
                 f"offpeak_extra.cores={public['offpeak_extra']['cores']}）")
        return public

    doc, changed = normalize(raw, path=target)
    if changed:
        warn(f"{target} 有缺省/非法字段，已按机器自检补齐")
        if write_back and _write_config(doc, target):
            warn(f"已写回 {target}")
    return doc


def _parse_clock(text: str) -> int | None:
    parts = text.strip().split(":")
    if len(parts) != 2 or not all(part.isdigit() and len(part) == 2 for part in parts):
        return None
    hour, minute = int(parts[0]), int(parts[1])
    return hour * 60 + minute if 0 <= hour <= 23 and 0 <= minute <= 59 else None


def parse_window(window: Any, *, path: str | None = None) -> tuple[int, int] | None:
    """解析 HH:MM-HH:MM；错误格式 WARN 并返回 None（调用方按窗口外处理）。"""
    target = path or config_path()
    if not isinstance(window, str) or window.count("-") != 1:
        warn(f"{target} 窗口格式错误：{window!r}（应为 HH:MM-HH:MM）；按窗口外处理")
        return None
    start_text, end_text = window.split("-", 1)
    start, end = _parse_clock(start_text), _parse_clock(end_text)
    if start is None or end is None or start == end:
        warn(f"{target} 窗口格式错误：{window!r}（应为 HH:MM-HH:MM，起止不等）；按窗口外处理")
        return None
    return start, end


def in_window(window: Any, *, now: datetime | None = None,
              path: str | None = None, assume_hour: int | None = None) -> bool:
    """北京时间窗口判定；assume_hour 仅用于 quiet.py 的旧自测语义。"""
    bounds = parse_window(window, path=path)
    if bounds is None:
        return False
    start, end = bounds
    if assume_hour is not None:
        minute = (assume_hour % 24) * 60
    else:
        current = now or datetime.now(BJ_TZ)
        if current.tzinfo is None:
            current = current.replace(tzinfo=BJ_TZ)
        else:
            current = current.astimezone(BJ_TZ)
        minute = current.hour * 60 + current.minute
    return (minute >= start or minute < end) if start > end else (start <= minute < end)


def current_quota(config: dict[str, Any] | None = None, *,
                  path: str | None = None,
                  assume_hour: int | None = None,
                  now: datetime | None = None,
                  write_back: bool = True) -> dict[str, Any]:
    """当前时段 heavy 并发上限 + 可诊断的当前值。"""
    target = path or config_path()
    doc = config if config is not None else read_config(target, write_back=write_back)
    window = doc.get("offpeak_extra", {}).get("window", DEFAULT_WINDOW)
    extra = doc.get("offpeak_extra", {}).get("cores", 0)
    active_window = in_window(window, now=now, path=target, assume_hour=assume_hour)
    derived = int(doc.get("pinned_cores", 1)) + (int(extra) if active_window else 0)
    override = doc.get("heavy_concurrency")
    limit = int(override) if isinstance(override, int) and not isinstance(override, bool) else derived
    return {
        "limit": max(1, limit),
        "derived": max(1, derived),
        "pinned_cores": int(doc.get("pinned_cores", 1)),
        "offpeak_extra_cores": int(extra),
        "window": window,
        "window_active": active_window,
        "heavy_concurrency": override,
        "config_path": target,
        "config_rel": CONFIG_REL,
    }


def _pid_alive(pid: int) -> bool:
    """跨平台活进程判定：Windows 用 OpenProcess；POSIX 用 os.kill(pid, 0)。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            kernel32.CloseHandle(handle)
            return True
        except (AttributeError, OSError):
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def running_heavy(directory: str | None = None, *,
                  config: dict[str, Any] | None = None) -> int:
    """按 state/quiet_cores/core-N.lock 数在跑 heavy；死锁不计，目录不可读 WARN。"""
    target = directory or cores_dir()
    try:
        names = sorted(os.listdir(target))
    except FileNotFoundError:
        return 0
    except OSError as exc:
        warn(f"读取 {target} 失败（{type(exc).__name__}: {exc}）；在跑 heavy 按 0 处理")
        return 0
    count = 0
    for name in names:
        if not (name.startswith("core-") and name.endswith(".lock")):
            continue
        lock = os.path.join(target, name)
        try:
            with open(lock, encoding="utf-8") as fh:
                pid = int((fh.read() or "0").strip() or 0)
        except (OSError, ValueError):
            pid = 0
        try:
            fresh = (time.time() - os.path.getmtime(lock)) < LOCK_FRESH_SECONDS
        except OSError:
            fresh = False
        if (pid and _pid_alive(pid)) or (not pid and fresh):
            count += 1
    return min(count, max(1, os.cpu_count() or 1))


def quota_error_text(quota: dict[str, Any], running: int) -> str:
    """配额拒绝原文：必须带配置路径与当前值，供派单器 stderr 使用。"""
    override = quota.get("heavy_concurrency")
    override_text = "null" if override is None else str(override)
    return (
        f"配额 {running}/{quota['limit']} 核已满，等窗口或调 {CONFIG_REL}"
        f"（当前 pinned={quota['pinned_cores']} offpeak={quota['offpeak_extra_cores']} "
        f"window={quota['window']} window_active={'true' if quota['window_active'] else 'false'} "
        f"heavy_concurrency={override_text} 配置路径={quota['config_path']}）"
    )


def doctor(path: str | None = None, *, assume_hour: int | None = None,
           now: datetime | None = None) -> int:
    """打印机器自检、当前配置值与当前时段配额；不改动配置。"""
    target = path or config_path()
    line = self_check_line()
    print(f"[dev_quota] {line}", flush=True)
    warn(line)
    config = read_config(target, write_back=False)
    quota = current_quota(config, path=target, assume_hour=assume_hour,
                          now=now, write_back=False)
    print(f"[dev_quota] 配置路径={quota['config_path']} 当前 pinned={quota['pinned_cores']} "
          f"offpeak_extra={quota['offpeak_extra_cores']} window={quota['window']} "
          f"heavy_concurrency={quota['heavy_concurrency']} 窗口内={quota['window_active']} "
          f"配额={quota['limit']} 在跑 heavy={running_heavy()}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="开发算力配额：自检、校验、时段配额与 doctor")
    ap.add_argument("command", nargs="?", default="doctor",
                    choices=("doctor", "config", "quota"),
                    help="doctor（默认）／打印配置／打印当前时段配额")
    ap.add_argument("--config", default=None,
                    help="配置路径（默认本仓 config/dev_quota.json 或 DEV_QUOTA_CONFIG）")
    ap.add_argument("--assume-hour", type=int, default=None,
                    help="只用于自测：假装北京时间是几点（0-23）")
    args = ap.parse_args(argv)
    target = args.config or config_path()
    config = read_config(target)
    if args.command == "config":
        print(json.dumps(config, ensure_ascii=False, indent=2))
        return 0
    if args.command == "quota":
        print(json.dumps(current_quota(config, path=target,
                                       assume_hour=args.assume_hour, write_back=False),
                         ensure_ascii=False, indent=2))
        return 0
    return doctor(target, assume_hour=args.assume_hour)


if __name__ == "__main__":
    sys.exit(main())
