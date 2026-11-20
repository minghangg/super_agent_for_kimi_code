#!/usr/bin/env python
"""助手会话台账：list / set / rm / busy。

**与 `session_ledger.py`（issue 侧写侧）不是同一模块**；本模块是 `sessions.json`
台账 CLI。两者符号集不相交（`session_ledger` 有 `sid_problem/root/sessions_path`，
本模块有 `cmd_set/cmd_archive/cmd_retire`），import 错必然 AttributeError。

路径一律走 `sess_events` 的解析（显式 `--state-dir` > 声明式根 > 环境变量）
——**本文件不再自己拼 `BASE/"state"`**。改成固定常量会让 `--state-dir` 只影响
事件、台账仍写生产（#18 的 N-1 就这么来的）。

每次台账写入都配一条生命周期事件（ISS-27）：
`set` → `session.key_changed`（已存在换 sid 时再补 `session.rotated`）、
`rm` → `session.key_changed`（不带 sid＝摘登）、`archive` → `session.archived`。
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from . import api as kimi_api
from . import cron_report as cron_wire
from . import retired_sids as RS
from . import sess_events as EV
from .console import _request_verdict

ASSISTANTS = ("steward", "reviewer", "qa", "scout", "auditor", "planner", "watch", "worker")
ALLOW_PREFIX = ("pm-", "at-")   # 项目经理与 AT 按项目命名：pm-<项目>／at-<项目> …
ROLE_AS_IS = set(ASSISTANTS)


def _emit_soft(event_type, *, strict=False, **fields):
    """台账**已落盘**后补事件；返回 True＝已写入，False＝未写入。

    默认（`strict=False`）与 `gh_issue.cmd_bind` 同做法：事件流是旁路事实，
    丢了不该让一次成功的台账写入变成失败退出。

    ISS-76 P1-1（2026-11-17 QA 第四轮）：`session.archived` **不是**旁路事件，
    它是归档闸的输入——`sess.archived_state` 的判据 C 读它，bus 又只能给出
    60 秒缓存的旧值。此前这里一并吞掉，`cmd_archive` 仍 `return 0` 并打印
    「已归档」，于是「事件流没有该行 ⇒ archived_state 判 no ⇒ send 放行」
    整条残留链无人知情。所以 `cmd_archive` 用 `strict=True` 取回失败信号，
    转成 `EXIT_EVENT_LOST`（rc=8）——与「服务端归档失败」（rc=1）分开。
    """
    try:
        EV.emit(event_type, **fields)
        return True
    except Exception as exc:  # noqa: BLE001  台账已成功，事件失败只告警
        print(f"警告：{event_type} 未写入（{type(exc).__name__}: {exc}）", file=sys.stderr)
        if strict:
            print(_ARCHIVE_EVENT_LOST_HINT.format(event_type=event_type,
                                                 sid=fields.get("sid"),
                                                 root=EV.resolve_root(fields.get("root"))),
                  file=sys.stderr)
        return False


_ARCHIVE_EVENT_LOST_HINT = (
    "      归档已生效（服务端不可撤销），但本地事件未落 ⇒ 归档闸对该会话将退化：\n"
    "      事件流不再为它作证，判据 C 只剩「直连＋bus」两路，bus 又可能给出 60 秒内的旧值。\n"
    "      补救：python -m super_agent.sess_events emit {event_type} --sid {sid} --state-dir {root}")


# ISS-76 P1-1：归档成功但事件未落的退出码。与 1（服务端归档失败）分开，
# 脚本能判「该会话归档了，但闸门对其降级了」。
EXIT_EVENT_LOST = 8


def role_of(key):
    """台账键 → 事件里的 role。固定角色名照原样，`pm-<项目>` 归一成 `pm`，`at-<项目>` 归一成 `at`。"""
    if key in ROLE_AS_IS:
        return key
    if key.startswith("at-"):
        return "at"
    if key.startswith(ALLOW_PREFIX):
        return "pm"
    return ""


def load(root=None):
    path = EV.sessions_path(root)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save(data, root=None):
    path = EV.sessions_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def cmd_list(args):
    """列台账。

    ISS-74 A-2：此前表头写死 `{'助手':<10}{'id':<14}`，键名超过 10 字符（`pm-<项目>-…`）
    就顶掉后面的列，于是 `grep '^pm-[^ ]+session_'` 能量到 9 行——**粘连行**。
    现在列宽按实际内容取 `max(len(name))`，不塞分隔符，键名再长也不吃 id 列。
    id 出**完整形态**（此前 `[:12]` 截断，复制回去当入参必被拒）。
    """
    data = load(getattr(args, "state_dir", None))
    if not data:
        print("(空) 先 python -m super_agent.sessions_ledger set steward session_xxx")
        return 0
    names = sorted(data)
    rows = []
    for name in names:
        item = data[name] if isinstance(data[name], dict) else {}
        rows.append((name, str(item.get("id", "")),
                     str(item.get("updated", "")), str(item.get("note", ""))))
    name_w = max([len(n) for n in names] + [len("助手")])
    sid_w = max([len(r[1]) for r in rows] + [len("id")])
    upd_w = max([len(r[2]) for r in rows] + [len("updated")])
    print(f"{'助手':<{name_w}}  {'id':<{sid_w}}  {'updated':<{upd_w}}  note")
    for name, sid, updated, note in rows:
        print(f"{name:<{name_w}}  {sid:<{sid_w}}  {updated:<{upd_w}}  {note}")
    return 0


def cmd_set(args):
    root = getattr(args, "state_dir", None)
    # ISS-74 B：入参必过形态校验。此前 `set steward session_a8fd` 照写，
    # 台账从此挂着一个解析不到的残号（#74 的 6 条存量就这么来的）。
    # 逃生门 `--allow-short-sid` 保留「明知要写残号」的路径，但必须打 WARN。
    problem = EV.sid_problem(args.session_id)
    if problem and not getattr(args, "allow_short_sid", False):
        print(f"拒绝写入：{args.assistant} 的 sid 不是完整形态")
        print(f"  {problem}")
        print("  确要写残号请加 --allow-short-sid（会写入但打 WARN）")
        return 2
    if problem:
        print(f"警告：{args.assistant} 写入的是非完整 sid（--allow-short-sid）", file=sys.stderr)
        print(f"  {problem}", file=sys.stderr)
    data = load(root)
    old = data.get(args.assistant)
    old_sid = str(old.get("id") or "") if isinstance(old, dict) else ""
    data[args.assistant] = {
        "id": args.session_id,
        "updated": datetime.now().strftime("%Y-%m-%dT%H:%M"),
        "note": old.get("note", "") if isinstance(old, dict) else "",
    }
    save(data, root)
    role = role_of(args.assistant)
    _emit_soft("session.key_changed", root=root, key=args.assistant, sid=args.session_id,
               role=role, **({"from_sid": old_sid} if old_sid else {}))
    if old_sid and old_sid != args.session_id:
        # 换血：同一台账键从旧 sid 换到新 sid（rrotate.sh 就是这么换的）
        _emit_soft("session.rotated", root=root, key=args.assistant, role=role,
                   from_sid=old_sid, to_sid=args.session_id, sid=args.session_id)
    print(f"已记 {args.assistant} -> {args.session_id}")
    return 0


def cmd_rm(args):
    root = getattr(args, "state_dir", None)
    data = load(root)
    if args.assistant not in data:
        print(f"无此助手: {args.assistant}")
        return 1
    old = data.pop(args.assistant)
    save(data, root)
    old_sid = str(old.get("id") or "") if isinstance(old, dict) else ""
    _emit_soft("session.key_changed", root=root, key=args.assistant,
               role=role_of(args.assistant), **({"from_sid": old_sid} if old_sid else {}))
    print(f"已删 {args.assistant}")
    return 0


def _retire_issue_sessions(root, old_sid: str, new_sid: str, stamp: str) -> list[str]:
    """把 `issue_sessions.json` 里两处指向旧 sid 的角色改指新 sid。

    形状：`{"<项目>#<issue>": {"qa": {"session": sid}, "worker": {"session": sid}}}`
    （`gh_issue._sessions_db`／`worker_watch._tracked_workers` 都按这个读）。
    路径走 `sess_events.issue_sessions_path(root)`，所以隔离 state-dir 下只动自己的。
    """
    path = EV.issue_sessions_path(root)
    if not path.is_file():
        return []
    try:
        db = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(f"警告：{path} 读不出，跳过（{old_sid[:12]}… 可能还留在里面）",
              file=sys.stderr)
        return []
    if not isinstance(db, dict):
        return []
    moved = []
    for issue_key, roles in db.items():
        if not isinstance(roles, dict):
            continue
        touched = False
        for role, rec in list(roles.items()):
            if not isinstance(rec, dict):
                continue
            if str(rec.get("session") or "") != old_sid:
                continue
            rec = dict(rec)
            rec["session"] = new_sid
            rec["updated"] = stamp
            roles[role] = rec
            touched = True
            moved.append(f"{issue_key}.{role}.session")
        if touched:
            db[issue_key] = roles
    if not moved:
        return []
    path.write_text(json.dumps(db, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    for issue_key in {m.rsplit(".", 2)[0] for m in moved}:
        _emit_soft("session.key_changed", root=root, key=issue_key, sid=new_sid,
                   role="worker", from_sid=old_sid)
    return moved


def cmd_retire(args):
    """退役旧 sid：写退役清单 + 台账里**所有**指向它的位置改指新 sid（ISS-29）。

    为什么不能只 archive：`archive` 只摘台账键，`v2 archive` 只移列表，
    **都不拦投喂**。凡是把旧 id 记在自己状态里、或从台账读目标的地方，
    归档之后照样会把旧会话叫醒。本命令做三件可核对的事：

      1. `retired_sids.json` 记一条（投喂前的过滤真源，可被 `check` 查询）；
      2. 台账里**凡是指向旧 sid 的位置**改指新 sid——两种形状都要改：
         `{"id": sid}`（pm-*/角色键）与 `{"worker": {"session": sid}}`
         （`issue_sessions.json` 风格，`worker_watch.py:269` 读的就是它）；
         漏掉任一形状 = 留了一条活路；
      3. 发 `session.key_changed` 事件（真相流与台账保持一致）。

    幂等：重复调用只刷新时间；旧 sid 与新 sid 相同则拒绝（那是误用）。
    """
    root = getattr(args, "state_dir", None)
    old_sid = str(args.old_sid or "").strip()
    new_sid = str(args.new_sid or "").strip()
    if not old_sid or not new_sid:
        print("retire: 需要旧 sid 与新 sid")
        return 2
    if old_sid == new_sid:
        print("retire: 旧 sid 与新 sid 相同，拒绝（没有要退役的东西）")
        return 2

    stamp = datetime.now().strftime("%Y-%m-%dT%H:%M")
    data = load(root)
    moved = []
    for key, val in list(data.items()):
        if not isinstance(val, dict):
            continue
        entry = dict(val)
        touched = False
        if str(entry.get("id") or "") == old_sid:
            entry["id"] = new_sid
            touched = True
            moved.append(f"{key}.id")
        # `decide_watch.supervisor_sid` 读 `session` 优先、`id` 兜底：只改 id 会让它继续用旧 sid
        if str(entry.get("session") or "") == old_sid:
            entry["session"] = new_sid
            touched = True
            moved.append(f"{key}.session")
        worker = entry.get("worker")
        if isinstance(worker, dict) and str(worker.get("session") or "") == old_sid:
            worker = dict(worker)
            worker["session"] = new_sid
            entry["worker"] = worker
            touched = True
            moved.append(f"{key}.worker.session")
        if not touched:
            continue
        entry["updated"] = stamp
        entry["note"] = (entry.get("note") or "") + f" [retired {old_sid[:12]}]"
        data[key] = entry
    if moved:
        save(data, root)
        for key in {m.rsplit(".", 1)[0] for m in moved}:
            _emit_soft("session.key_changed", root=root, key=key, sid=new_sid,
                       role=role_of(key), from_sid=old_sid)

    # `issue_sessions.json` 是**第二本台账**：`{项目#issue: {qa:{session}, worker:{session}}}`
    # （`gh_issue.binding` 读它，`worker_watch._tracked_workers` 也认这个形状）。
    # 只改 `sessions.json` = 留了一条活路——PM 实测该文件原地不动（ISS-29 同区第 1 件）。
    moved += _retire_issue_sessions(root, old_sid, new_sid, stamp)

    rec = RS.retire(old_sid, key=(moved[0].rsplit(".", 1)[0] if moved else ""),
                    replaced_by=new_sid, reason=args.reason, state_dir=root)
    shown = ", ".join(moved) if moved else "(台账里没有位置指向它)"
    print(f"已退役 {old_sid[:12]}… -> 新 {new_sid[:12]}…；改指位置：{shown}")
    print(f"退役清单：{RS.path(root)}（at={rec.get('at')}）")
    return 0


def fetch_sessions():
    payload = kimi_api.get("/api/v1/sessions?page_size=50")[1]
    if isinstance(payload, dict) and payload.get("code") not in (0, None):
        return None, f"code={payload.get('code')}"
    if not isinstance(payload, (dict, list)):
        return None, "bad payload"
    return payload, None


def archive_session(session_id: str, *, state_dir=None) -> int:
    """POST /api/v1/sessions:archive —— 归档一个会话，返回退出码。

    服务端对这个批量端点**恒回外层 `code: 0`**，失败写在 `data.results[]` 里
    （`ok: false` / `succeeded: 0` / `failed: 1`），只认顶层 code 会给失败的
    归档补一条假的 `session.archived`。判据复用 `sess._request_verdict`
    （**全仓只有这一份**），失败不记事件且返回非零。

    ISS-100 D2：从 `cmd_archive` 拆出，`cmd_archive` 变薄包装。`recycle archive`
    调本函数后再做第二回读（服务端 `archived`），**不复制归档逻辑**。
    """
    status, payload = kimi_api.post("/api/v1/sessions:archive",
                                    {"ids": [session_id]})
    ok, why = _request_verdict(status, payload)
    if not ok:
        print(f"归档失败: http={status} {why}")
        return 1
    if not _emit_soft("session.archived", strict=True, root=state_dir, sid=session_id):
        print(f"已归档 {session_id[:12]}…（事件未落，见上方告警）")
        cron_wire.report(session_id)
        return EXIT_EVENT_LOST
    print(f"已归档 {session_id[:12]}…")
    cron_wire.report(session_id)
    return 0


def cmd_archive(args):
    """POST /api/v1/sessions:archive —— 归档会话（归档后收不下新 turn）。

    服务端对这个批量端点**恒回外层 `code: 0`**，失败写在 `data.results[]` 里
    （`ok: false` / `succeeded: 0` / `failed: 1`），只认顶层 code 会给失败的
    归档补一条假的 `session.archived`。判据复用 `sess._request_verdict`
    （**全仓只有这一份**），失败不记事件且返回非零。

    ISS-76：归档成功后再**点名报出**该会话名下仍存活的 cron。

    为什么只能「报出」不能「停掉」：服务端既无 cron 读接口也无写接口，
    `cron.disabled` 是全局开关（关掉会连在岗巡查钟一起停）。而归档本身足以
    让调度器不再触发——调度器尊重 `archived`；cron 之所以能在归档后复活，
    是因为 `sess.py send` 把会话隐式解归档（同一张单的根因，已加闸）。

    三态必须分开，`wire 不存在` 不许当成 `0 条`：
      · 有存活 cron ⇒ 列明细 + 下次触发时刻 + 清除指引；
      · 确认 0 条   ⇒ 明说「无存活 cron（wire 已核）」；
      · wire 读不到 ⇒ 「无法核对……按未知处理，勿假定为 0」。
    报告**不改变退出码**：归档本身已成功，仍返回 0。

    ISS-76 P1-1（2026-11-17 QA 第四轮）：**事件写入失败不再算成功**。
    `session.archived` 是归档闸的输入（`sess.archived_state` 判据 C 读它），
    漏写会让闸门对该会话退化，而此前这里吞掉异常仍返回 0、打印「已归档」，
    失败完全静默。现在退出码三态：

      · `0` 归档成功且事件已落；
      · `1` 服务端归档失败（没有「已归档」可报，也不写事件）；
      · `8`（`EXIT_EVENT_LOST`）**归档已生效，但本地事件未落** ⇒ stderr 说清
        后果与补救命令。脚本据此与 `1` 分开判。

    **不回滚归档**：服务端归档不可撤销，假装失败会误导调用方以为会话还活着。
    """
    return archive_session(args.session_id, state_dir=getattr(args, "state_dir", None))


def cmd_busy(args):
    payload, err = fetch_sessions()
    if payload is None:
        print("server: down")
        return 1
    items = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(items, dict):
        items = items.get("items") or items.get("sessions") or []
    if not isinstance(items, list):
        items = []
    by_id = {}
    for it in items:
        if isinstance(it, dict):
            by_id[str(it.get("id", ""))] = it
    recorded = load(getattr(args, "state_dir", None))
    names = list(ASSISTANTS) + sorted(n for n in recorded if n.startswith(ALLOW_PREFIX))
    for name in names:
        sid = str((recorded.get(name) or {}).get("id", "")) if isinstance(recorded.get(name), dict) else ""
        item = by_id.get(sid)
        if not sid:
            print(f"{name}: - (未记账)")
        elif item is None:
            print(f"{name}: - (不在最近 50 条)")
        else:
            print(f"{name}: busy={item.get('busy', item.get('status', '?'))}")
    return 0


def main(argv=None):
    argv = EV.apply_state_dir(argv if argv is not None else sys.argv[1:])
    ap = argparse.ArgumentParser(description="助手会话台账（state/sessions.json）。token 永不打印。")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list", help="表格列出")
    p_list.set_defaults(func=cmd_list)
    p_set = sub.add_parser("set", help="记一个会话 id")
    p_set.add_argument("assistant")   # 任意名字；约定：固定角色名，或 pm-<项目>
    p_set.add_argument("session_id")
    p_set.add_argument("--allow-short-sid", action="store_true", dest="allow_short_sid",
                       help="逃生门：明知是残号也照写（会打 WARN）")
    p_set.set_defaults(func=cmd_set)
    p_rm = sub.add_parser("rm", help="删一个助手记录")
    p_rm.add_argument("assistant")
    p_rm.set_defaults(func=cmd_rm)
    p_busy = sub.add_parser("busy", help="查本机 server 的 busy 状态")
    p_busy.set_defaults(func=cmd_busy)
    p_arc = sub.add_parser("archive", help="归档一个会话（交接换血时用）")
    p_arc.add_argument("session_id")
    p_arc.set_defaults(func=cmd_archive)
    p_ret = sub.add_parser("retire",
                           help="退役旧 sid：写退役清单 + 台账改指新 sid（ISS-29）")
    p_ret.add_argument("old_sid")
    p_ret.add_argument("new_sid")
    p_ret.add_argument("--reason", default="rotated", help="退役原因（写进清单）")
    p_ret.set_defaults(func=cmd_retire)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
