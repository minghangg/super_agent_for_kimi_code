#!/usr/bin/env python
"""会话 cron 的只读真源：从会话自己的 `agents/main/wire.jsonl` 复算存活集。

为什么需要它（ISS-76）：

  服务端**既无 cron 读接口也无写接口**（`/api/v1/cron`、`/api/v1/crons`、
  `/api/v1/cron/jobs`、`/api/v1/sessions/<sid>/cron` 全 404；`GET
  /api/v1/sessions/<sid>` 的 data 里没有 cron 字段）。cron 是 Kimi 客户端
  工具（CronCreate／CronList／CronDelete），只作用于**当前会话**。
  所以「归档时该会话名下还有几个钟在跑」只能从 wire 复算。

算法（复用方案里的实测口径）：

  存活集 = `cron.add` 的 id 集合 − `cron.delete` 累计的 id 集合
  `cron.delete` 的键是 **`ids` 数组**（不是单数 `id`）。
  `cron.cursor` 只用来补每个 job 的 `lastFiredAt`，**只保留最后一个**。

三条硬约束（改之前先读）：

  1. **`wire 不存在` ≠ `0 条 cron`。** 归档会话的目录可能已删，wire 读不到；
     此时必须报「未确认」，不许报 0。`alive_jobs()` 用 `None` 表示这个状态，
     `count()` 用 `-1`。
  2. **`next_fire` 不许猜。** 本机没有 croniter／apscheduler，也不许引入依赖。
     只有「单一分钟值 + 其余全 `*`」的表达式直接算；其余打「未确认（表达式复杂）」，
     并用 `last_fired` 作为可核对的替代证据。
  3. **只读。** 本模块不写任何文件、不发任何 HTTP。
     跨会话删除 cron 在服务端根本做不到——服务端既无 cron 读接口也无写接口，
     CronDelete 只在会话自身内可用。本工具只提供报出，不提供删除。

用法：
    python -m super_agent.cron_report list <sid>
    python -m super_agent.cron_report report <sid>     # 归档后那段三态报告
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

STATE_JOBS = "jobs"
STATE_NONE = "none"
STATE_UNKNOWN = "unknown"


def sessions_dir() -> Path:
    """会话目录根：`SESS_WIRE_ROOT` 覆盖，否则 `super_agent.paths.sessions_dir()`。"""
    raw = (os.environ.get("SESS_WIRE_ROOT") or "").strip()
    if raw:
        return Path(os.path.expanduser(raw))
    from . import paths as kimi_paths
    return kimi_paths.sessions_dir()


def find_wire(sid: str) -> Path | None:
    """该会话的 `agents/main/wire.jsonl`；多条取 mtime 最新，一条都没有返回 None。"""
    try:
        cands = sorted(sessions_dir().glob(f"*/{sid}/agents/main/wire.jsonl"),
                       key=lambda p: p.stat().st_mtime)
    except OSError:
        return None
    return cands[-1] if cands else None


def _ts(ms) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return "-"


def next_fire(expr: str, now: datetime | None = None) -> str:
    """下一次触发时刻；算不准的一律「未确认（表达式复杂）」。

    只认「单一分钟值 + 其余四个字段全是 `*`」这种形状（如 `18 * * * *`）。
    `*/20`、`7,27,47`、带时/日/月/周的表达式一律不猜。
    """
    parts = str(expr or "").split()
    if len(parts) < 5:
        return "未确认（表达式复杂）"
    minute, hour, dom, mon, dow = parts[:5]
    if not (hour == dom == mon == dow == "*"):
        return "未确认（表达式复杂）"
    try:
        m = int(minute)
    except ValueError:
        return "未确认（表达式复杂）"
    if not 0 <= m <= 59:
        return "未确认（表达式复杂）"
    base = (now or datetime.now()).replace(second=0, microsecond=0)
    cand = base - timedelta(minutes=(base.minute - m) % 60)
    if cand <= base:
        cand += timedelta(hours=1)
    return cand.strftime("%Y-%m-%d %H:%M")


def alive_jobs(sid: str, wire: Path | None = None) -> dict[str, dict] | None:
    """存活 cron：`{id: {id, cron, recurring, created_at, last_fired}}`。

    `None` 表示 **wire 不存在或读不出**（未确认），与 `{}`（确认 0 条）严格区分。
    12MB 的 wire 也走流式逐行读，只保留每行含 `cron.` 的行解析。
    """
    path = wire or find_wire(sid)
    if path is None:
        return None
    alive: dict[str, dict] = {}
    cursors: dict[str, int] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "cron." not in line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(rec, dict):
                    continue
                kind = rec.get("type")
                if kind == "cron.add":
                    task = rec.get("task")
                    if not isinstance(task, dict):
                        continue
                    jid = str(task.get("id") or "")
                    if not jid:
                        continue
                    alive[jid] = {
                        "id": jid,
                        "cron": str(task.get("cron") or ""),
                        "recurring": task.get("recurring"),
                        "created_at": task.get("createdAt") or rec.get("time"),
                        "last_fired": None,
                    }
                elif kind == "cron.delete":
                    ids = rec.get("ids")
                    if isinstance(ids, list):
                        for jid in ids:
                            alive.pop(str(jid), None)
                    elif rec.get("id"):
                        alive.pop(str(rec["id"]), None)
                elif kind == "cron.cursor":
                    jid = str(rec.get("id") or "")
                    if jid and rec.get("lastFiredAt"):
                        cursors[jid] = rec["lastFiredAt"]
    except OSError:
        return None
    for jid, job in alive.items():
        if cursors.get(jid):
            job["last_fired"] = cursors[jid]
    return alive


def count(sid: str) -> int:
    """存活条数；`-1` = wire 不存在或读不出（未确认）。"""
    jobs = alive_jobs(sid)
    return -1 if jobs is None else len(jobs)


def describe(sid: str) -> tuple[list[str], dict]:
    """明细行 + 摘要。三态：`jobs` / `none` / `unknown`。"""
    wire = find_wire(sid)
    jobs = alive_jobs(sid, wire) if wire is not None else None
    if jobs is None:
        return ([f"未确认：找不到 {sid} 的 wire.jsonl —— 按「未知」处理，勿假定为 0"],
                {"sid": sid, "state": STATE_UNKNOWN, "count": None,
                 "wire": str(wire) if wire else ""})
    lines = [f"cron_jobs={len(jobs)}"]
    for job in jobs.values():
        lines.append(f"id: {job['id']}")
        lines.append(f"cron: {job['cron']}")
        lines.append(f"recurring: {job['recurring']}")
        lines.append(f"created_at: {_ts(job.get('created_at'))}")
        lines.append(f"last_fired: {_ts(job.get('last_fired'))}")
        if job.get("recurring") is False:
            lines.append("next_fire: -（一次性，recurring=false）")
        else:
            lines.append(f"next_fire: {next_fire(job['cron'])}")
    return (lines, {"sid": sid,
                    "state": STATE_JOBS if jobs else STATE_NONE,
                    "count": len(jobs),
                    "wire": str(wire)})


def clear_hint(sid: str) -> list[str]:
    """清除指引：跨会话无写入口，只能报出与压住。"""
    return [
        f"清除指引: 该 cron 只能在 {sid} 会话内用 CronDelete 删除；跨会话无写入口。",
        "          若无法进入该会话，处置顺序为：",
        f"          1) python -m super_agent.console stop {sid}          # 压住当前轮",
        "          2) 确认服务端 archived=true                 # 调度器尊重 archived",
        "          3) 严禁再向该 sid send（sess.py send 已加归档闸，ISS-76）",
    ]


def report(sid: str) -> dict:
    """归档后追加的三态报告（`none`／`jobs`／`unknown`，三者措辞不许混）。"""
    lines, summary = describe(sid)
    state = summary["state"]
    if state == STATE_UNKNOWN:
        print(f"已归档；cron 无法核对（找不到 {sid} 的 wire.jsonl）"
              "——按「未知」处理，勿假定为 0")
        return summary
    if state == STATE_NONE:
        print("已归档；该会话名下无存活 cron（wire 已核）")
        return summary
    print(f"⚠ 归档不等于停用：该会话名下仍有 {summary['count']} 个 cron（wire 证据）")
    for line in lines[1:]:
        print(f"  {line}")
    for line in clear_hint(sid):
        print(f"  {line}")
    return summary


def main(argv=None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv:
        print(__doc__.strip().splitlines()[0])
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "list":
        if not rest:
            print("用法: cron_wire.py list <sid>")
            return 2
        sid = rest[0]
        lines, summary = describe(sid)
        print(f"sid={sid}")
        for line in lines:
            print(line)
        if summary["state"] == STATE_UNKNOWN:
            return 2
        for line in clear_hint(sid):
            print(line)
        return 0
    if cmd == "report":
        if not rest:
            print("用法: cron_wire.py report <sid>")
            return 2
        summary = report(rest[0])
        return 2 if summary["state"] == STATE_UNKNOWN else 0
    print(f"未知子命令：{cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
