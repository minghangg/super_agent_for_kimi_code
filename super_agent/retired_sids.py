#!/usr/bin/env python
"""已退役 sid 清单：**投喂前过滤**用的唯一真源。

为什么需要它（2026-11-02 事故 + ISS-29）：

`sessions.py archive` 只摘台账，`v2 sessions:archive` 只把会话移出列表，
**两者都不拦投喂**——`sess.py send`、`dispatch.py --session` 照样能把它叫醒。
换血时把台账键改指新 sid 只解决「按台账找人」这一条路；凡是把旧 id 记在
自己状态里的看门狗（`issue_watch` 的 `seen`、`worker_watch` 的绑定、进程
命令行里写死的 id）都还会继续投。

所以退役要有一份**独立、可读、可核对**的清单：谁要投喂，先查这里。
清单只增不删（要复活一个 sid 就删那条并留下理由，人工动作）。

文件：`<state 根>/retired_sids.json`
结构：
    {"version": 1,
     "retired": {"session_xxx": {"at": "...", "key": "pm-<项目>",
                                 "replaced_by": "session_yyy", "reason": "..."}}}

读侧 API：
    load(state_dir=None) -> dict[str, dict]        # 宽松：坏清单当空（非安全面）
    load_strict(state_dir=None) -> dict[str, dict]  # 严格：坏清单抛 RetiredUnreadable
    is_retired(sid, state_dir=None) -> bool
    positions_of(reg, sid) -> list[str]           # 台账里所有指向 sid 的位置
    filter_ledger(reg, state_dir=None) -> dict    # 摘掉指向退役 sid 的键
    check(sid, state_dir=None) -> str             # "" 表示可以投，否则给拒绝理由

**投喂闸（安全面）只用 `load_strict`／`check`**：清单坏掉时 `check` 返回非空
拒绝理由，绝不返回 `""`（ISS-29 P1-A：坏清单曾让三处闸门全部放行）。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

from . import sess_events as EV

NAME = "retired_sids.json"


def path(state_dir=None) -> Path:
    return EV.resolve_root(state_dir) / NAME


def load(state_dir=None) -> dict:
    target = path(state_dir)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    retired = data.get("retired")
    return retired if isinstance(retired, dict) else {}


class RetiredUnreadable(Exception):
    """退役清单**存在但读不懂**（坏 JSON／空文件／形状错／顶层非 dict）。

    与「文件不存在」严格区分：不存在是新 state 根的正常情形（空集），
    存在但读不懂是安全事件——投喂闸必须据此**拒投**。
    """


def load_strict(state_dir=None) -> dict:
    """严格读取退役清单。解析失败**不返回空字典**——抛 RetiredUnreadable。

    fail-closed 语义（supervisor 2026-11-08 裁定）：
      投喂闸属于安全面，安全面永远 fail-closed ——
      **清单不可解析 = 全部视为仍退役**，宁可错杀不可放行。
    「文件不存在」与「文件损坏」必须区分：
      不存在 ⇒ 空集（新 state 根是正常情形）；
      存在但坏/空/形状错 ⇒ 抛错，由调用方拒投并给出可读理由。

    为什么 `load()` 不能兼任：它把坏清单读成 `{}`，而 `{}` 在下游等价于
    「没有任何 sid 退役」——那正是 ISS-29 P1-A 的放行路径。
    """
    target = path(state_dir)
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        if not target.exists():
            return {}
        raise RetiredUnreadable(f"{target} 读不出：{exc}") from exc
    if not raw.strip():
        raise RetiredUnreadable(f"{target} 是空文件")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RetiredUnreadable(f"{target} 不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise RetiredUnreadable(
            f"{target} 顶层应是对象，实际是 {type(data).__name__}")
    retired = data.get("retired")
    if not isinstance(retired, dict):
        raise RetiredUnreadable(
            f"{target} 的 retired 应是对象，实际是 "
            f"{type(retired).__name__ if 'retired' in data else '缺失'}")
    return retired


def _save(retired: dict, state_dir=None) -> None:
    target = path(state_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    payload = {"version": 1, "retired": retired}
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, target)


def retire(sid: str, *, key: str = "", replaced_by: str = "",
           reason: str = "", state_dir=None) -> dict:
    """记一个退役 sid（幂等：重复退役只刷新时间与原因）。"""
    sid = str(sid or "").strip()
    if not sid:
        raise ValueError("retire: sid 不能为空")
    retired = load(state_dir)
    rec = dict(retired.get(sid) or {})
    rec.update({"at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "key": key or rec.get("key", ""),
                "replaced_by": replaced_by or rec.get("replaced_by", ""),
                "reason": reason or rec.get("reason", "rotated")})
    retired[sid] = rec
    _save(retired, state_dir)
    return rec


def is_retired(sid: str, state_dir=None) -> bool:
    return str(sid or "") in load(state_dir)


def check_detail(sid: str, state_dir=None) -> tuple[str, str]:
    """``(拒绝理由, 清单故障原因)``；理由为空表示可以投。

    清单不可解析时**理由非空**（fail-closed：一律视为仍退役），第二个返回值
    是给调用方拼可读告警用的原始原因；清单健康时为 ``""``。
    """
    try:
        retired = load_strict(state_dir)
    except RetiredUnreadable as exc:
        return (f"退役清单不可解析（{exc}）；按 fail-closed 一律视为仍退役", str(exc))
    rec = retired.get(str(sid or ""))
    if not rec:
        return "", ""
    by = rec.get("replaced_by") or "(未记新 sid)"
    return (f"sid {sid} 已于 {rec.get('at') or '?'} 退役"
            f"（台账键 {rec.get('key') or '-'}，已被 {by} 取代，"
            f"原因 {rec.get('reason') or '-'}）"), ""


def check(sid: str, state_dir=None) -> str:
    """``""`` 表示可以投；否则返回一句可打印的拒绝理由。

    ISS-29 P1-A：**清单坏掉时也返回理由**（不是空串）。三处投喂闸都走这里，
    所以 fail-closed 必须落在这一层，而不是散在每个调用点。
    """
    return check_detail(sid, state_dir)[0]


def positions_of(reg: dict, sid: str) -> list[str]:
    """台账里所有会让投喂者取到 ``sid`` 的位置（三种形状全认）。

    `{"id": sid}`        — `pm-*`／角色键（issue_watch、worker_watch 的 PM 侧）
    `{"session": sid}`   — `decide_watch.supervisor_sid` 优先读它
    `{"worker": {"session": sid}}` — `worker_watch._tracked_workers`
    """
    out = []
    for key, val in (reg or {}).items():
        if not isinstance(val, dict):
            continue
        if val.get("id") == sid:
            out.append(f"{key}.id")
        if val.get("session") == sid:
            out.append(f"{key}.session")
        worker = val.get("worker")
        if isinstance(worker, dict) and worker.get("session") == sid:
            out.append(f"{key}.worker.session")
    return sorted(out)


def filter_ledger(reg: dict, state_dir=None) -> dict:
    """台账副本，摘掉**任何位置**指向退役 sid 的键。

    只判 `{"id": ...}` 会漏掉 worker 绑定与 `session` 形状——那正是
    「归档了但还在投」在数据层的等价物。
    """
    retired = set(load(state_dir))
    if not retired:
        return dict(reg or {})
    out = {}
    for key, val in (reg or {}).items():
        if isinstance(val, str):
            if val not in retired:
                out[key] = val
            continue
        if not isinstance(val, dict):
            out[key] = val
            continue
        if any(positions_of({key: val}, sid) for sid in retired):
            continue
        out[key] = val
    return out


def retire_safe(sid: str, *, key: str = "", replaced_by: str = "",
                reason: str = "bus unsubscribe", state_dir=None) -> set[str] | None:
    """记一条退役并回读全量集合；写失败返回 ``None``（不抛出）。

    2026-11-19（ISS-100 D4）：从 `wss_bus._retire_sid_safe` 抽出。bus 那条路
    「订阅已经摘了、写清单失败只能告警」，所以吞异常；CLI（`recycle retire`）
    走 `retire()` 直接报错。两者写的是同一份 `path()` —— 本函数内部就调
    `retire()`，不另立第二份写入口。
    """
    try:
        retire(sid, key=key, replaced_by=replaced_by, reason=reason,
               state_dir=state_dir)
        return set(load_strict(state_dir))
    except Exception:  # noqa: BLE001  调用方负责告警；这里只回 None
        return None


def main(argv=None) -> int:
    argv = EV.apply_state_dir(argv or sys.argv[1:])
    if not argv:
        print(__doc__.strip().splitlines()[1])
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "list":
        data = load(None)
        if not data:
            print("(空)")
            return 0
        for sid in sorted(data):
            rec = data[sid] or {}
            print(f"{sid}  key={rec.get('key') or '-':<16} "
                  f"-> {rec.get('replaced_by') or '-'}  at={rec.get('at') or '-'}")
        return 0
    if cmd == "check":
        if not rest:
            print("用法: retired_sids.py check <sid>", file=sys.stderr)
            return 2
        reason = check(rest[0])
        print(reason or "可投（未退役）")
        return 0 if not reason else 1
    print(f"未知子命令：{cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
