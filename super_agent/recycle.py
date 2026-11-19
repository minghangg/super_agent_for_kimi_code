#!/usr/bin/env python
"""会话回收：stop / archive / retire / status（**无 delete**，RULES §33）。

    python -m super_agent.recycle stop    <sid> [--timeout 60]
    python -m super_agent.recycle archive <sid>
    python -m super_agent.recycle retire  <sid> [--key K] [--replaced-by SID] [--reason R]
    python -m super_agent.recycle status  <sid>

「回收」在这里只指**可逆或可核对**的四步：停住当前轮（`stop`）、把会话移出
活动列表（`archive`）、把旧 sid 记进退役清单（`retire`）、把四源现状摊开
（`status`）。**删除会话没有入口**：会话一旦删除，wire 与台账都没有痕迹可核，
退役会话只许归档（用户 2026-11-14 令、RULES §33）。

本模块**不实现**任何编排，全部委托现成函数（ISS-100 D2）：
  · stop    → `console.cmd_stop`（三段式：每轮重取 active_prompt 再 abort → 逐个
              cancel_task → 复查 busy=false）；
  · archive → `sessions_ledger.archive_session` ＋ 第二回读 `direct.session_info`；
  · retire  → `retired_sids.retire`（不吞异常，写失败 rc=1）；
  · status  → `console.archived_state`／`direct.session_info`／
              `retired_sids.check_detail`／`cron_report.describe` 四源。

归档回读为什么读两次：v2 批量的外层 `code` 恒为 0，`results[].ok` 只说明这一批
被受理。ISS-76 的事故形态正是「受理了、列表也动了、`archived` 仍是 false」，
随后 `send` 把它解归档。两处来源不同（写应答 ＋ 服务端状态），任一否即判失败。

退出码：0 成功；1 动作失败；2 参数错（含残号、含 `delete`）；5 归档已生效但
cron 读不出（`未确认`）；6 已归档拒投；8 归档生效但本地事件未落。
"""
import argparse
import sys

from . import console as console_mod
from . import cron_report as CRON
from . import direct as kimi_direct
from . import retired_sids as RS
from . import sess_events as EV
from . import sessions_ledger as session_ledger_cli

EXIT_UNCONFIRMED = 5
EXIT_REFUSED = 2


def _no_delete() -> int:
    print("recycle: 无 delete —— 退役会话只许归档，RULES §33")
    print("  删除会话会让 wire 与台账都没有痕迹可核；要停用请用 archive。")
    return EXIT_REFUSED


def _strict_sid(arg: str) -> str:
    """完整 sid 或 ""（"" ＝已打印拒绝理由）。

    ISS-100：长度与形态只有一份真源（`session_ledger.FULL_SID_RE`／
    `sid_problem`）。`console.sid_ok_or_explain` 是它唯一的 CLI 闸，
    这里先过它，再把台账键解析成 sid 后复判一次——**不写第二份校验**。
    """
    if not console_mod.sid_ok_or_explain(arg, allow_short=False):
        return ""
    sid = console_mod.resolve(arg)
    if not EV.check_sid(sid):
        print(f"recycle: 拒绝执行 —— 目标不是完整 sid（{EV.sid_problem(sid)}）")
        print("  可用 python -m super_agent.console list 取完整 id")
        return ""
    return sid


def _cmd_stop(sid: str, rest: list[str]) -> int:
    return console_mod.cmd_stop([sid] + rest)


def _cmd_archive(sid: str) -> int:
    root = str(EV.resolve_root())
    rc = session_ledger_cli.archive_session(sid, state_dir=root)
    if rc != 0:
        # 1＝服务端归档失败；8＝已生效但本地事件未落。两种都不是「已归档」。
        return rc
    info = kimi_direct.session_info(sid)
    if info.get("archived") is not True:
        print(f"归档回读不一致：服务端 results[].ok 为真，但 "
              f"archived={info.get('archived')!r}（sid={sid}）")
        if info.get("_err"):
            print(f"  回读错误：{info['_err']}")
        return 1
    lines, summary = CRON.describe(sid)
    state = summary.get("state")
    if state == CRON.STATE_UNKNOWN:
        for line in lines:
            print(f"  {line}")
        print("cron=未确认（wire 读不到，勿假定 0 条）")
        return EXIT_UNCONFIRMED
    if state == CRON.STATE_JOBS:
        jobs = CRON.alive_jobs(sid) or {}
        upcoming = [CRON.next_fire(j.get("cron") or "")
                    for j in jobs.values() if j.get("recurring") is not False]
        nxt = min(upcoming) if upcoming else "-"
        print(f"{summary.get('count')} 个 cron 仍活，下次 {nxt}")
        for line in CRON.clear_hint(sid):
            print(f"  {line}")
        return 0
    print("cron=0（wire 已核，无存活 cron）")
    return 0


def _cmd_retire(sid: str, key: str, replaced_by: str, reason: str) -> int:
    root = str(EV.resolve_root())
    try:
        rec = RS.retire(sid, key=key, replaced_by=replaced_by,
                        reason=reason or "recycle retire", state_dir=root)
    except Exception as exc:  # noqa: BLE001  写失败必须让人看见，不吞
        print(f"退役清单写入失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"已退役 {sid}（at={rec.get('at')}）；清单 {RS.path(root)}")
    print(f"  投喂闸现在会拦：python -m super_agent.retired_sids check {sid}")
    return 0


def _cmd_status(sid: str) -> int:
    rc = 0
    archived, why = console_mod.archived_state(sid)
    info = kimi_direct.session_info(sid)
    reason, err = RS.check_detail(sid)
    lines, summary = CRON.describe(sid)
    print(f"sid={sid}")
    print(f"server: archived={archived} ({why})")
    if info.get("_err"):
        print(f"        busy=? 直连不可用：{info['_err']}")
        rc = 1
    else:
        print(f"        busy={info.get('busy')} main_turn_active="
              f"{info.get('main_turn_active')} archived={info.get('archived')!r}")
    if archived == "yes" and info.get("archived") is not True:
        print("        不一致：本地判据说已归档，服务端 GET 说不是 —— 勿假定安全")
        rc = 1
    if err:
        print(f"retired: 清单不可解析（{err}）—— 按 fail-closed 视为仍退役")
        rc = 1
    elif reason:
        print(f"retired: {reason}")
    else:
        print("retired: no（未退役）")
    state = summary.get("state")
    if state == CRON.STATE_UNKNOWN:
        print("cron: 未确认（wire 读不到，勿假定 0 条）")
        for line in lines:
            print(f"        {line}")
        rc = rc or EXIT_UNCONFIRMED
    else:
        print(f"cron: {state} count={summary.get('count')}")
    return rc


def main(argv=None) -> int:
    argv = EV.apply_state_dir(list(argv if argv is not None else sys.argv[1:]))
    ap = argparse.ArgumentParser(
        prog="super_agent.recycle",
        description="stop/archive/retire/status one session (no delete; RULES §33)")
    sub = ap.add_subparsers(dest="cmd")

    p_stop = sub.add_parser("stop", help="停净一个会话（abort → 逐个 cancel → 复查）")
    p_stop.add_argument("sid")
    p_stop.add_argument("--timeout", type=int, default=60, help="秒，默认 60")

    p_arc = sub.add_parser("archive", help="归档（双回读：results[].ok 且 archived=True）")
    p_arc.add_argument("sid")

    p_ret = sub.add_parser("retire", help="记进退役清单（投喂闸的真源）")
    p_ret.add_argument("sid")
    p_ret.add_argument("--key", default="", help="台账键（pm-<项目>／角色名）")
    p_ret.add_argument("--replaced-by", dest="replaced_by", default="",
                       help="取代它的新 sid")
    p_ret.add_argument("--reason", default="", help="原因（默认 recycle retire）")

    p_st = sub.add_parser("status", help="四源现状（服务端／退役清单／cron／归档判据）")
    p_st.add_argument("sid")

    p_del = sub.add_parser("delete", help="不提供：退役会话只许归档（RULES §33）")
    p_del.add_argument("sid", nargs="?", default="")

    args = ap.parse_args(argv)
    if not getattr(args, "cmd", None):
        print(__doc__.strip().splitlines()[0])
        return EXIT_REFUSED
    if args.cmd == "delete":
        return _no_delete()
    sid = _strict_sid(args.sid)
    if not sid:
        return EXIT_REFUSED
    if args.cmd == "stop":
        return _cmd_stop(sid, ["--timeout", str(args.timeout)])
    if args.cmd == "archive":
        return _cmd_archive(sid)
    if args.cmd == "retire":
        return _cmd_retire(sid, args.key, args.replaced_by, args.reason)
    return _cmd_status(sid)


if __name__ == "__main__":
    raise SystemExit(main())
