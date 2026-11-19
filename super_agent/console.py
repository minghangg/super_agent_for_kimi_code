"""会话控制台：查状态、打断、派任务、看/停后台任务、归档恢复。

依据实测报告 `state/evidence/session_control_api_20261031.md`。

用法：
  python -m super_agent.console list [--busy] [--archived]
  python -m super_agent.console status <sid|名字>
  python -m super_agent.console abort <sid|名字> [--burst]   # 打断当前轮；--burst=5 次×2s + 自动核实
  python -m super_agent.console send <sid|名字> "<文本>"     # 入队一轮（不等待结果）
  python -m super_agent.console send <sid|名字> "<文本>" --expect-confirm 30
      # 发送后再看目标会话语 wire.jsonl 是否真落出该 promptId（ISS-35）
      # 推荐「评论留档＋send 冗余」双件流程：gh_issue.py comment 留档，
      # 再用本命令确收；评论本身**不唤醒**任何会话。
  python -m super_agent.console tasks <sid|名字>            # 列后台 bash 任务
  python -m super_agent.console cancel-task <sid|名字> <tid>
  python -m super_agent.console stop <sid|名字> [--timeout 60]   # 彻底停：abort→逐个 cancel→复查 busy
  python -m super_agent.console steer <sid|名字> <正文|--body-file 路径>   # 并入当前 turn，不打断
  python -m super_agent.console messages <sid|名字> [n]
  python -m super_agent.console snapshot <sid|名字>
  python -m super_agent.console takeover <sid|名字>
  python -m super_agent.console archive <sid|名字>
  python -m super_agent.console restore <sid|名字>
  python -m super_agent.console cron list <sid|名字>          # 报出该会话存活 cron（读 wire）
  python -m super_agent.console cron rm <sid|名字> <jobId>    # 一律拒绝：跨会话无写入口

坑（都实测过）：
  - abort/steer 成功失败**都是 HTTP 200**，必须看响应体 `code`/`aborted`；
  - abort 对已结束的 prompt 无效（prompt 完成即从表里消失）；
  - steer 不打断在跑的前台工具；2.5.3 起 steer 会打断当前 LLM 生成（未产出 tool call 时）
    并同 turn 继续（新增 steered 中断），不再等到下一步 —— 要连工具一起打断就 abort＋重派；
  - 归档会话若 cwd 已删除，读 messages 会 50001。

版本适配（2026-11-06，issue #6）：2.5.0 删了 `/status`、`/snapshot`、REST `tasks`
（含 `:cancel`）。三条读取路径统一改走 `kimi_api`（按 server_version 双态），
`stop` 在新代用官方 WS `task.stop`、旧代仍走 `:cancel`。**本文件的 HTTP 细节
一律不自己拼**——要改协议只改 `super_agent/api.py` 一处。
"""
import json
import os
import sys
import time
from pathlib import Path

from . import api as kimi_api
from . import config as config_mod
from . import bus as kimi_bus
from . import cron_report as CRON
from . import direct as kimi_direct
from . import paths as kimi_paths
from . import retired_sids as RS
from . import sess_events as EV

BUSD = kimi_bus

BASE = Path(__file__).resolve().parent.parent

# ISS-35：send 的退出码。0=已投递（带了 --expect-confirm 时还须已确收）；
# 1=上游明确拒绝；2=参数错；3=不确定（可能已投、禁止重发）；4=退役清单拦下；
# 5=daemon 已受理但目标会话 wire 未见该 promptId（**视为未唤醒**）；
# 6=目标会话已归档（ISS-76 归档闸；与 4 分开，脚本要能分开判）。
EXIT_UNCONFIRMED = 5
EXIT_ARCHIVED = 6
# ISS-76 P1-1（2026-11-17 QA 第四轮）：动作已生效（服务端不可撤销），但本地
# 生命周期事件未落 ⇒ 归档闸对该会话退化。与 1（动作失败）分开，脚本可分辨。
EXIT_EVENT_LOST = 8
DEFAULT_CONFIRM_TIMEOUT = 30.0
CONFIRM_POLL_INTERVAL = 0.5
# 找到 turn.prompt 之后，再看该轮有没有立刻死掉（`turn.ended reason=failed`）。
# 2026-11-09 实测：wire 里 turn.prompt 与 turn.ended 之间只隔几十毫秒，所以
# 这个 grace 很短就够；仍在跑的轮不会命中，报 `pending`。
OUTCOME_GRACE = 3.0
OUTCOME_POLL_INTERVAL = 0.25

# ISS-74 A：`list` 的排版宽度。完整 sid 恒 44 字符（`session_` + UUID），
# 终端按 80 列算：44 + 1 空格 + 标题 + `busy=` 尾部必须塞进 80。标题不够就压它，
# 不压 id —— id 被截断就等于「列出来的不能当入参用」。
# 宽度按**显示列**算（CJK 占 2 列），否则中文标题的行走 awk 是 88 而终端是 96。
SID_COL = 44
LIST_COLS = 80
MIN_TITLE_COL = 8


def _char_width(ch: str) -> int:
    """单字符的终端显示列宽（东亚宽字符记 2 列）。"""
    import unicodedata
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _width(text: str) -> int:
    return sum(_char_width(ch) for ch in text)


def _clip(text: str, limit: int) -> str:
    """按显示列裁剪；被剪过就补 `…`（ISS-74 F：截断必带省略号）。"""
    if _width(text) <= limit:
        return text
    out, used = [], 0
    for ch in text:
        w = _char_width(ch)
        if used + w > limit - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"

# 生命周期事件类型。`TAKEN_OVER` 与 `RESTORED` **必须分开**（ISS-76 P1-2）：
# restore 是「归档被撤销」，takeover 只是「接管一个正在用的会话」。此前两者
# 共用 `session.restored`，于是本地旁证把 takeover 读成「未归档」，把真正归档
# 的会话判活。safkc 的 `session_events.replay` 遇未知类型是 no-op，所以
# `session.taken_over` 对它是无害新增。
EV_ARCHIVED = "session.archived"
EV_RESTORED = "session.restored"
EV_TAKEN_OVER = "session.taken_over"


def sessions_file() -> Path:
    """台账路径走 `sess_events` 的解析（隔离运行时读打桩目录）。"""
    return EV.sessions_path()


def bus_cache_file() -> Path:
    """bus 缓存文件：跟会话根同处（`sessions.json` 的旁边），不另立路径。"""
    return EV.resolve_root() / "wss_bus_cache.json"


def _req(method, path, body=None):
    """保留给未适配的端点（takeover/archive/restore）；已适配的请用 kimi_api。"""
    if method == "GET":
        return kimi_api.get(path)
    return kimi_api.post(path, body)


def _data(payload):
    return kimi_api.data_of(payload)


def resolve(arg):
    if arg.startswith("session_"):
        return arg
    try:
        rec = json.loads(sessions_file().read_text(encoding="utf-8")).get(arg)
    except Exception:  # noqa: BLE001
        rec = None
    return rec["id"] if isinstance(rec, dict) and rec.get("id") else arg


def cmd_list(args):
    """列会话。

    2026-11-06 步 2：列表**目录**仍直连（daemon 不持全量会话目录），但 busy
    用 daemon 已知状态覆盖（更鲜）。`--json` 结构与既有字段不变。
    """
    items = BUSD.list_envelopes(caller="sess.py list")
    if items is None:
        flag = ""
        if "--busy" in args:
            flag = "&busy=true"
        if "--archived" in args:
            flag += "&archived_only=true"
        st, payload = _req("GET", f"/api/v1/sessions?page_size=30{flag}")
        items = _data(payload)
        if isinstance(items, dict):
            items = items.get("items") or items.get("sessions") or []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        if "--busy" in args and not it.get("busy"):
            continue
        if "--archived" in args and not it.get("archived"):
            continue
        # ISS-74 A：**出完整 id**。此前截到 26 位（`session_6a5eac50-df38-4735`），
        # 复制回去当入参必然被格式校验拒（组 3），于是「列出来的不能直接用」。
        # 终端仍是 80 列：id 列吃满 44 位，标题按剩余宽度压，busy= 结尾对右。
        sid = str(it.get("id") or "")
        title = " ".join(str(it.get("title") or "").split())
        tail = f"busy={it.get('busy')}"
        sid_col = max(SID_COL, len(sid))
        room = max(MIN_TITLE_COL, LIST_COLS - sid_col - 1 - _width(tail))
        title = _clip(title, room)
        print(f"{sid:<{sid_col}} {title}{' ' * (room - _width(title))}{tail}")
    return 0


def cmd_status(args):
    """会话状态。bus 优先，直连降级（结构差异只有 _source 前缀）。"""
    sid = resolve(args[0])
    st = BUSD.status(sid, caller="sess.py status",
                     cache_file=bus_cache_file())
    if st is None:
        st = kimi_api.status(sid)
        if isinstance(st, dict):
            st["_source"] = f"direct({st.get('_source') or '?'})"
    print(json.dumps(st, ensure_ascii=False, indent=2))
    return 0


def session_exists(sid):
    """会话是否还活在服务端。

    返回 (exists, detail)：
      - (True,  {...})  正常会话
      - (False, "none") 服务端 40401 —— 会话不在服务端内存里
      - (None,  "err")  查不到（网络/服务挂了），无法判定

    2026-11-05 加：此前 `abort` 在「会话已不存在」与「没有在跑的 prompt」两种情况下
    都打印同一句「没有在跑的 prompt」并 exit 1，外部无法区分 ⇒ 用户误以为 abort 失效。
    """
    st, payload = _req("GET", f"/api/v1/sessions/{sid}")
    if not isinstance(payload, dict):
        return None, "err"
    code = payload.get("code")
    if code == 40401:
        return False, "none"
    if code not in (0, None):
        return None, f"err(code={code})"
    data = payload.get("data")
    if not isinstance(data, dict):
        return None, "err(data)"
    return True, data


def archived_state(sid):
    """该会话是否已归档。返回 `("yes"|"no"|"unknown", 详情)`。

    ISS-76：**服务端 GET 的 `archived` 字段是唯一可靠真源**——归档就是那个字段，
    不是 `state.json` 里的副本。三个服务端确实 `archived=true` 的会话，其
    `state.json` 的同名字段也一致；事故会话之所以两处不一致，是因为它的归档
    **被打穿了**（send 把它解归档），不是端点漏写。

    ISS-76 P1-1（2026-11-17 QA 返工）：**直连判不出来时必须补问一次 bus**。
    此前只看直连：`KIMI_SERVER` 指向不可达端口时直连恒判 `unknown` ⇒ 无条件
    放行，已归档会话照样投达（实测 rc=0、ok=True、state=accepted）。这与事故
    同构，等于闸门靠「直连恰好可用」成立。现在 bus 是第二个真源：
    `super_agent.bus.session_info` 走 `/state/<sid>` 时带 `archived`
    （daemon 侧 `_norm_archived` 会回查一次服务器），足以判 yes／no。

    取数顺序（**yes 与 no 都受旁证约束，但只对可能过期的来源**）：
      1. 直连 `GET /api/v1/sessions/<sid>` 的 `data.archived`；
      2. 直连判 unknown ⇒ `BUSD.session_info(sid)`（bus 的 `archived`，bool）；
      3. **任一路说 `yes`，且看门狗式的末条旁证说 `no`（`session.restored` 比那条
         归档更晚）⇒ 以 `no` 为准**；旁证没有更新 ⇒ 保持 `yes`；
      4. **bus 说 `no`，而事件流末条是 `session.archived`（其后无 restore）⇒ 以
         `yes` 为准**（P0-1，见下）；
      5. 直连说 `no` ⇒ 直接采信，不被旁证翻转（见下）；
      6. 两路都判 unknown ⇒ 读 `state/events.jsonl` 旁证；
      7. 三条都判 unknown ⇒ 才 `unknown`，放行并告警。
    **两条真源都不可达才放行**——这是本函数的窗口，写在 docstring 里以便复核。

    **怎样才算「确定未归档」（可执行定义）**——满足任一条即放行：
      · A. 直连 `GET /api/v1/sessions/<sid>` 返回 `code=0` 且 `data.archived`
        为假。直连**每次现取服务器**，不经 daemon，没有缓存窗口。
      · B. A 不成立时，事件流里该 sid 的末条生命周期事件是 `session.restored`
        （其后无 `session.archived`）。
      · C. A、B 都不成立时，bus 报 `archived=false` **且**事件流里没有该 sid 的
        生命周期记录——没有任何旁证与它矛盾。**C 成立时必须再直连一次复核**
        （P1-1 加强，2026-11-17 QA 第四轮）：直连可达 ⇒ 回落判据 A（直连的
        `archived` 是现取真值）；直连仍不可达 ⇒ 才 `unknown`，放行并告警。
        这一步把窗口从「事件漏写 **或** 直连不可达」缩到「事件漏写 **且**
        直连也不可达」——两个故障同时发生才开窗。
    不满足以上任一条、又拿不到真源 ⇒ `unknown`（放行并告警），不许写「大致未归档」。

    **为什么 bus 的 `no` 也要被旁证否决（P0-1，2026-11-17 QA 第二轮实测）**：
    daemon 的 `_norm_archived` 给 REST 结果缓存 60 秒（`_archived_chk`，`/state`
    是热路径）。归档是 false→true 的跳变，缓存里存的是归档**前**的 `false`，
    于是归档后约 60 秒内 `/state/<sid>` 恒报 `archived=false`——QA 采样 t+0…t+40s
    全 `no`，t+48s 才翻 `yes`，同期直连恒 `archived=True`。此时直连一断，闸门
    唯一能问的 bus 恰好给出相反的旧值 ⇒ rc=0 并真唤醒会话（#76 通道重开）。
    上一轮只堵了反方向（bus 粘滞的 `yes`），漏了这一半：**缓存双向都会过期**。

    **为什么直连的 `no` 不被旁证翻转**：直连每轮现取服务器，不存在陈旧窗口；
    它的 `no` 就是「服务器此刻说未归档」。若把这份现取的结果交给一份可能漏写的
    本地事件流去否决，代价是真正未归档的会话被永久拒投（rc=6），而堵死派单是
    比漏拦更常见的故障方向。所以事件流只否决**可能过期**的那一路。

    **P2-1 口径（PM 2026-11-17 裁，有意设计，勿改）**：两条真源都不可达时——
       · 该 sid **没有**任何生命周期旁证 ⇒ `unknown`，放行并告警；
       · 该 sid **有** `session.archived` 旁证 ⇒ 判 `yes`，拒投（rc=6）。
    后者是刻意的：两源皆死本身已属异常态，此时宁可不投；与本地审计件
    fail-closed 同向。任务书旧文那句「两源皆死 ⇒ 放行」指的是**无任何旁证**。

    **为什么 `yes` 要让旁证一票否决（2026-11-17 真实进程实测）**：
    daemon 收到 `event.session.archived` 帧时会把该 sid **摘出订阅**
    （`tools/wss_bus.py` 的 `event.session.archived` 分支），于是它再也看不到
    随后的 restore——`/state/<sid>` 的 `archived=true` 是**粘滞**的，实测恢复
    之后仍报 true。此时若只信 bus，就会把一个已经恢复的会话永久拒投（rc=6），
    正是 P1-2 漏判侧的同一种错，只是换了个真源。
    反向不成立：旁证说 `no` 而真源说 `yes` 时，真源可信——直连／bus 都是从
    服务器现取的，比事件流新。所以只有「yes ⇒ 需要 no 旁证确认」这一条单向放宽，
    绝不允许旁证把 `yes` 之外的状态翻成 `yes`（那会让一次漏写的事件永久拒投）。

    三态分界（与退役清单的 fail-closed **方向相反**，理由保留并扩写）：
      · 服务端说 `archived=true`          ⇒ `yes`，调用方拒投（rc=6）；
      · 服务端 40401／取不到 data          ⇒ 先补问 bus，仍未知才 `unknown`。放行。
        退役清单坏掉要拒投（那是我们自己的审计件，坏掉说明有人乱改）；
        服务端查不到只是网络面，查不到不等于已归档——阻断会把全系统派单打死。
      · 网络／端口不可达                  ⇒ 先补问 bus，仍未知才 `unknown`，放行并告警。
    """
    verdict, why = _archived_direct(sid)
    origin = "直连"
    if verdict == "unknown":
        verdict, why = _archived_from_bus(sid)
        origin = "bus"
    if verdict == "yes":
        # bus 的 archived 会粘滞（归档即退订），所以 yes 必须被更新的事件流确认
        ev_verdict, ev_why = _archived_from_events(sid)
        if ev_verdict == "no":
            return "no", f"{why}；但 {ev_why}"
        if origin == "bus" and ev_verdict == "yes":
            return "yes", f"{why}；{ev_why}"
        return "yes", why
    if verdict == "no" and origin == "直连":
        # 直连每次都现取服务器，没有缓存窗口；它的 no 就是服务器此刻的答案。
        return "no", why
    if verdict == "no":
        # P0-1：bus 的 no 可能来自 `_norm_archived` 的 60 秒缓存（归档后的
        # stale-false），所以它与 yes 一样要过事件流。末条是归档 ⇒ 翻回 yes；
        # 末条是恢复 ⇒ 采信 no（不许把一次漏写的事件变成永久拒投）。
        ev_verdict, ev_why = _archived_from_events(sid)
        if ev_verdict == "yes":
            return "yes", f"{why}；但 {ev_why}（bus 缓存可能陈旧，P0-1）"
        if ev_verdict == "no":
            return "no", why
        # 判据 C（事件流没有该 sid 的记录）不许单独放行：事件漏写＋bus 陈旧
        # 两个故障一起出现时，C 会把真归档会话放行。再直连一次。
        again, again_why = _archived_direct(sid)
        if again == "yes":
            return "yes", (f"判据 C 不成立：{why}；{ev_why}；"
                           f"复直连改判 —— {again_why}")
        if again == "no":
            return "no", f"判据 C 复核：{again_why}"
        return "unknown", (f"判据 C：{why}；{ev_why}；复直连仍不可达（{again_why}）"
                           "⇒ 未知，放行并告警")
    ev_verdict, ev_why = _archived_from_events(sid)
    if ev_verdict != "unknown":
        return ev_verdict, f"{why}；{ev_why}"
    return "unknown", why


def _archived_direct(sid):
    """直连真源：`GET /api/v1/sessions/<sid>`。返回 `("yes"|"no"|"unknown", 详情)`。"""
    st, payload = _req("GET", f"/api/v1/sessions/{sid}")
    if not isinstance(payload, dict):
        return "unknown", f"服务端响应非对象（http={st}），按未知处理"
    code = payload.get("code")
    if code == 40401:
        return "unknown", f"服务端不认识该会话（code=40401，http={st}），按未知处理"
    if code not in (0, None):
        return "unknown", f"服务端返回 code={code}（http={st}），按未知处理"
    data = payload.get("data")
    if not isinstance(data, dict):
        return "unknown", f"服务端返回无 data（http={st}），按未知处理"
    if data.get("archived") is True:
        return "yes", f"服务端报告 archived=true（archived_at={data.get('archived_at') or '-'}）"
    return "no", "服务端报告 archived=false"


def _archived_from_bus(sid):
    """第二真源：daemon 的 `/state/<sid>`（`super_agent.bus.session_info`）。

    bus 的 `archived` 是 bool（daemon 侧 `_norm_archived` 回查一次服务器，
    结果缓存 60 秒）；拿不到该会话／daemon 不可用／字段缺失 ⇒ `unknown`。
    **只看 `archived` 是否为真值**，不把「bus 有记录」当「未归档」——
    归档会话 bus 根本不订阅，404 是常态，正是它回落直连的原因。
    """
    try:
        info = BUSD.session_info(sid, caller="sess.py archived_state")
    except Exception as exc:  # noqa: BLE001  bus 故障不许让闸门炸掉
        return "unknown", f"bus 查询异常（{type(exc).__name__}: {exc}），按未知处理"
    if not isinstance(info, dict) or info.get("_err"):
        return "unknown", "bus 也拿不到该会话状态，按未知处理"
    arch = info.get("archived")
    if arch is None:
        return "unknown", "bus 回了该会话但没有 archived 字段，按未知处理"
    src = str(info.get("_source") or "bus")
    if arch:
        return "yes", f"bus 报告 archived=true（{src}）"
    return "no", f"bus 报告 archived=false（{src}）"


def _archived_from_events(sid):
    """本地旁证：`events.jsonl` 里该 sid 的最后一条生命周期事件是归档还是恢复。

    只在两条真源都判 unknown 时用作补充判据。读不到事件流 ⇒ 未知。

    ISS-76 P1-2：**只认 `session.archived` 与 `session.restored`**。`takeover`
    写的是 `session.taken_over`，不参与判定——接管一个正在用的会话不代表它的
    归档被撤销，把它读成「未归档」会让归档闸失效（实测形态）。
    """
    try:
        rows = EV.read_events()
    except Exception:  # noqa: BLE001  旁证失败不许让闸门炸掉
        return "unknown", "事件流读不出，按未知处理"
    last = None
    for rec in rows:
        if str(rec.get("sid") or "") != sid:
            continue
        if rec.get("type") in (EV_ARCHIVED, EV_RESTORED):
            last = rec
    if last is None:
        return "unknown", "事件流里没有该 sid 的生命周期记录，按未知处理"
    if last.get("type") == EV_ARCHIVED:
        return "yes", (f"事件流末条是 {EV_ARCHIVED}"
                       f"（at={last.get('at')}）⇒ 判已归档")
    return "no", (f"事件流末条是 {EV_RESTORED}"
                  f"（at={last.get('at')}）⇒ 不判归档")


def active_prompt(sid):
    """取「正在跑的那一轮」的 prompt id。

    2026-11-01 修：服务端快照字段已改为 `in_flight_turn.current_prompt_id`，
    旧的 `active` / `queued` 早已不存在 ⇒ 之前 abort 永远拿不到 id、静默失效。
    2026-11-06（issue #6）：`/snapshot` 在 2.5.0 被删，改由 `kimi_api` 双态取。
    """
    return kimi_api.active_prompt(sid)


def cmd_abort(args):
    sid = resolve(args[0])
    # 2026-11-02 用户实测口径：对方有 bash 在跑时一次 abort 常打不停，
    # 要 2 秒一次连发 5 次再核实。--burst 走这个配方；默认仍单次。
    times, interval = 1, 0
    if len(args) > 1 and args[1] == "--burst":
        times, interval = 5, 2
    # 2026-11-05 加：先判「会话在不在服务端」。此前不存在与无 active prompt 同码同文案，
    # 导致用户误判「abort 失效」（见 state/audit/abort_ability_20261105.md §4）。
    exists, detail = session_exists(sid)
    if exists is False:
        print(f"ABORT-NOSESSION {sid}")
        print("  服务端 40401 ＝ 服务端暂时不认识这个会话（kimi 重启后的瞬态），不代表会话真死")
        print("  处置：等 60–120 秒重查；或发一条消息触发 restore；持续 404 才算真死")
        return 3
    if exists is None:
        print(f"ABORT-UNKNOWN {sid}：查不到会话状态（{detail}），无法判定是否该 abort")
        return 4
    for round_no in range(times):
        pid = active_prompt(sid)
        if not pid:
            print("没有在跑的 prompt（已完成的不在表里，事后无法取消）")
            print("  提示：若会话 busy=true 但此处恒为空，可能是「幽灵忙」，见 §7.1b")
            return 1
        ok, detail, _via = BUSD.ctl(
            "abort", sid, caller="sess.py abort",
            payload={"pid": pid},
            direct_fn=lambda: kimi_api.abort_prompt(sid, pid))
        code = detail.get("code") if isinstance(detail, dict) else None
        if ok:
            print(f"abort aborted=True prompt={pid}"
                  + (f"（第 {round_no + 1}/{times} 发）" if times > 1 else ""))
            break
        # 2026-11-01 实测：HTTP 恒 200，成败看 code；40402=prompt not found（已结束）
        if code == 40402:
            print(f"该 prompt 已结束（code=40402），无可中止：{pid}")
            return 1
        # 2026-11-05 修：此前只打 data={}，把真正的 code（实测 50001）吞掉了，
        # 导致 PM 判不出「服务端拒绝」还是「已生效但无返回体」。现在把 code 明写出来。
        print(f"abort 第 {round_no + 1}/{times} 发未生效 code={code}")
        if code == 50001:
            print("  code=50001 ＝ 服务端拒绝中止该轮（多见于工具正在执行）。"
                  "abort 打不断在跑的 bash —— 先 cancel-task 再 abort，见 §7.0i")
        if round_no < times - 1 and interval:
            time.sleep(interval)
    else:
        # 2026-11-05：abort 全失败时，回读 tasks 给出「到底谁还在跑」的硬证据。
        running = [t for t in kimi_api.tasks(sid) if t.get("status") == "running"]
        if running:
            print(f"ABORT-BLOCKED 仍有 {len(running)} 个工具任务在跑（abort 拦不住它们）：")
            for t in running[:5]:
                print(f"  {t.get('id')}  {(t.get('command') or '')[:70]}")
            print("  正解：先 cancel-task 逐个杀掉，轮次随之退出（§7.0i）")
            print(f"    python -m super_agent.console cancel-task {sid} <tid>")
        else:
            print("ABORT-NOEFFECT 无 running 工具但 abort 仍未生效 —— 按 §7.1b 查幽灵忙")
        return 1
    # 核实：abort 生效后再查一次 active，确认真的停了（用户 2026-11-02 要求）
    time.sleep(1)
    if active_prompt(sid):
        print("⚠ abort 返回 True 但 active prompt 仍在——需人工再核（可能 bash 又拉起新 prompt）")
        return 2
    print("核实：active prompt 已清，会话终止确认")
    return 0



def cmd_steer(args):
    """把一条新指令并入**正在跑**的 turn（等同 TUI 的 Ctrl-S），不打断、保留已有工具结果。

    用法：sess.py steer <sid|名字> <正文>
          sess.py steer <sid|名字> --body-file <路径>

    语义（2026-11-05 实测）：
      1. 先 POST /prompts 投一条，它在 turn 活跃时是 `queued`（排队，不会自己跑）；
      2. 再 POST /prompts/{prompt_id}:steer 把这条**并入当前 turn**；
      3. 当前 turn 在下一个可打断点消费它，已完成的工作保留。
    实测：让靶子数 1–400，中途 steer「数到 200 就停，回 DONE」→ 它数到中途即回 DONE，
    且前半段输出全在，未被丢弃。

    前提：**当前必须有 active turn**。若 turn 已结束，steer 无意义（那条会当普通新轮跑）。
    退出码：0 成功并入；1 无 active turn 或 steer 失败。
    """
    sid = resolve(args[0])
    text = _read_body(args[1:])
    if not text.strip():
        print("steer: 正文为空（用 --body-file 可绕开 shell 转义）")
        return 2
    if not active_prompt(sid):
        # 409 语义（步 3 反例 2）：无 active turn 时 steer 必须显式拒绝，
        # **不得**残留一条普通排队消息——投了就等于偷偷变成 send。
        # HTTP 语义是 409，但进程退出码在 Windows 上会被截断（409&0xFF=153），
        # 不便脚本判读。这里打 409 字样、退出码用 2（与其它参数/前提错误一致）。
        print("steer: 409 当前没有 active turn —— steer 只能并入正在跑的轮次。"
              "要开新轮请用 send；要先停再插请用 stop 再 send。")
        return 2
    # 控制面走门面（bus 优先，三态不重放）。直连实现即原来的两步编排。
    ok, detail, via = BUSD.ctl(
        "steer", sid, caller="sess.py steer",
        payload={"text": text},
        direct_fn=lambda: kimi_direct.steer(sid, text))
    code = detail.get("code") if isinstance(detail, dict) else None
    pid = detail.get("prompt_id") if isinstance(detail, dict) else None
    print(f"steer steered={ok} code={code} prompt_id={pid} [{via}]")
    return 0 if ok else 1


def cmd_stop(args):
    """彻底停一个会话：取消 turn → 逐个停 running 任务 → 复查到 busy=false。

    2026-11-05 实测（§7.0i）：`:abort` 只取消主 Agent 当前 turn，
    **后台 shell（run_in_background=true）／子 agent 不会被它停掉**。
    必须再逐个 task 停，并循环复查（停止过程中可能有新任务延迟出现）。

    2026-11-06（issue #6）：停任务的路子两代不同——
      旧代 `POST /tasks/{tid}:cancel`；新代官方 WS `task.stop`。
    差异全在 `kimi_api.cancel_task` 里，本函数只管编排。

    用法：sess.py stop <sid|名字> [--timeout 60]
    退出码：0 已停净（busy=false 且 running 为空）；1 超时仍有残留。
    """
    sid = resolve(args[0])
    timeout = 60
    if len(args) > 1 and args[1] == "--timeout" and len(args) > 2:
        try:
            timeout = int(args[2])
        except ValueError:
            timeout = 60

    src = kimi_api.source()

    # 1) 取消主 turn
    ok, _d, _via = BUSD.ctl(
        "abort", sid, caller="sess.py stop",
        direct_fn=lambda: kimi_api.abort_prompt_session(sid))
    code = _d.get("code") if isinstance(_d, dict) else None
    print(f"[1/3] turn:abort aborted={ok} code={code}  [{src}]")
    if not ok:
        print(f"      警告：abort 未受理（code={code}），继续尝试停任务")

    # 2) 循环：**每轮都补 aborted** ＋ 停 running 任务
    #
    # 2026-11-06（2.5.2 实测）：只 abort 一次不够。排队中的 prompt 会被服务端
    # 接着取出来跑（abort 返回 True 后，active_prompt 立刻换成下一个 pid），
    # 于是 busy 一直不转 false、`stop` 空转到超时。所以每轮都要重新取
    # active prompt 再 abort 一次，直到 busy=false。
    deadline = time.time() + timeout
    killed = set()
    round_no = 0
    aborts = 0
    while time.time() < deadline:
        round_no += 1
        pid_now = kimi_api.active_prompt(sid)
        if pid_now:
            ok3, d3, _v3 = BUSD.ctl(
                "abort", sid, caller="sess.py stop-loop",
                payload={"pid": pid_now},
                direct_fn=lambda pp=pid_now: kimi_api.abort_prompt(sid, pp))
            code3 = d3.get("code") if isinstance(d3, dict) else None
            if ok3:
                aborts += 1
                print(f"[2/3] abort#{aborts} {pid_now[:16]}… ok code={code3}")
        running = [t for t in kimi_api.tasks(sid) if t.get("status") == "running"]
        if running:
            for t in running:
                tid = t.get("id")
                if not tid or tid in killed:
                    continue
                bg = t.get("run_in_background")
                ok2, _d2, _v2 = BUSD.ctl(
                    "cancel-task", sid, caller="sess.py stop-loop",
                    payload={"tid": tid},
                    direct_fn=lambda t=tid: kimi_api.cancel_task(sid, t))
                print(f"[2/3] stop {tid} bg={bg} -> ok={ok2}")
                killed.add(tid)
        # 2026-11-05：工具停掉后 busy 要 5–15 秒才转 false，不能一见 running 空就退出
        info = kimi_api.session_info(sid)
        busy_now = info.get("busy") if isinstance(info, dict) else None
        if not running and busy_now is False:
            break
        time.sleep(2)

    # 3) 复查
    info = kimi_api.session_info(sid)
    busy = info.get("busy") if isinstance(info, dict) else None
    main_active = info.get("main_turn_active") if isinstance(info, dict) else None
    left = [t for t in kimi_api.tasks(sid) if t.get("status") == "running"]
    print(f"[3/3] busy={busy} main_turn_active={main_active} running_left={len(left)} "
          f"（停任务 {len(killed)} 个，abort {aborts} 次，{round_no} 轮）")
    if busy is False and not left:
        print("STOP-OK 已停净")
        return 0
    for t in left[:5]:
        print(f"      残留 {t.get('id')} {(t.get('command') or '')[:60]}")
    print("STOP-INCOMPLETE 仍有残留；加大 --timeout 或按 §7.0i 手工复查")
    return 1


def cmd_approvals(args):
    """列出/答复待批交互。答复必须带 kind（只给 decision 会返回 null 且静默无效）。"""
    sid = resolve(args[0])
    # 先用一个 REST 请求判有无待批，避免每次 approvals 都开 WS 探针
    if not kimi_api.has_pending(sid):
        print("没有待批交互")
        return 0
    if len(args) > 1 and args[1] in ("approve", "reject", "cancel"):
        decision = {"approve": "approved", "reject": "rejected", "cancel": "cancelled"}[args[1]]
        items = [x for x in kimi_api.pending_interactions(sid) if x.get("approval_id")]
        if not items:
            print("没有待批交互")
            return 0
        ok = True
        for it in items:
            aid = it["approval_id"]
            good, _d, _via = BUSD.ctl(
                "interaction", sid, caller="sess.py approvals",
                payload={"aid": aid, "kind": it.get("kind") or "approval",
                         "decision": decision},
                direct_fn=lambda a=aid, k=it.get("kind") or "approval":
                    kimi_api.answer_interaction(sid, a, decision, k))
            print(f"{aid} -> {'ok' if good else 'fail'}")
            ok &= good
        return 0 if ok else 1
    for it in kimi_api.pending_interactions(sid):
        if it.get("kind") == "question":
            print("question", json.dumps(it.get("raw") or {}, ensure_ascii=False)[:160])
        else:
            print("approval", it.get("approval_id"), it.get("tool_name"),
                  str(it.get("action"))[:80])
    return 0


def _read_body(rest):
    """取正文：支持 --body-file <路径> 或位置参数。"""
    if rest and rest[0] == "--body-file":
        path = rest[1] if len(rest) > 1 else ""
        if not path or not os.path.isfile(path):
            return ""
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    return rest[0] if rest else ""


def _pop_expect_confirm(args):
    """摘出 `--expect-confirm [秒]`；返回 (余下参数, 超时秒数或 None)。

    `--expect-confirm` 不给数字 ⇒ 用默认 30 秒（与 2026-11-09 已实测的配方一致）。
    """
    rest, timeout = [], None
    i = 0
    while i < len(args):
        if args[i] == "--expect-confirm":
            nxt = args[i + 1] if i + 1 < len(args) else ""
            try:
                timeout = float(nxt)
                i += 2
            except ValueError:
                timeout = DEFAULT_CONFIRM_TIMEOUT
                i += 1
            continue
        rest.append(args[i])
        i += 1
    return rest, timeout


def wire_candidates(sid):
    """目标会话的 `agents/main/wire.jsonl` 候选（按修改时间旧→新）。

    `SESS_WIRE_ROOT` 可整体改根（隔离自测只能指向 tempfile 造的目录，
    **不许**写死 `C:/tmp` 之类路径）。
    """
    raw = (os.environ.get("SESS_WIRE_ROOT") or "").strip()
    root = Path(os.path.expanduser(raw)) if raw else kimi_paths.sessions_dir()
    try:
        return sorted(root.glob(f"*/{sid}/agents/main/wire.jsonl"),
                      key=lambda p: p.stat().st_mtime)
    except OSError:
        return []


def scan_wire_for_prompt(wire, prompt_id):
    """在一条 wire 里找 `promptId == prompt_id` 的 `turn.prompt` 事件。

    返回 `(行号, turnId)`；没有返回 `None`。坏行跳过、半行（并发写截断）不崩——
    判据只是「有没有这一行」，解析失败的行本来就不可能匹配上。
    """
    hit = None
    try:
        with open(wire, "r", encoding="utf-8", errors="replace") as fh:
            for lineno, line in enumerate(fh, 1):
                if prompt_id not in line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(rec, dict):
                    continue
                if rec.get("type") != "turn.prompt":
                    continue
                if rec.get("promptId") != prompt_id:
                    continue
                hit = (lineno, rec.get("turnId"))
    except OSError:
        return None
    return hit


def scan_turn_outcome(wire, start_line, turn_id):
    """在 wire 里找该 turnId 的进展：`agent.turn.started` / `turn.ended`。

    **为什么必须看这一步**：`turn.prompt` 只证明「这一行进了 wire」，不等于
    「这一轮跑起来了」。2026-11-09 实测：watch 会话 wire 第 8 行有 turn.prompt，
    第 16 行紧接着 `turn.step.interrupted reason=error [model.not_configured]
    Model not set`、第 18 行 `turn.ended reason=failed`——只查 turn.prompt 会把
    「投到了但立刻失败」报成成功。

    返回 `("started", None, {})`／`("ended", reason, error)`；两者都没看到返回 None。
    """
    seen_start = False
    try:
        with open(wire, "r", encoding="utf-8", errors="replace") as fh:
            for lineno, line in enumerate(fh, 1):
                if lineno <= start_line:
                    continue
                if "turn.started" not in line and "turn.ended" not in line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(rec, dict):
                    continue
                kind = rec.get("type")
                if turn_id is not None and str(rec.get("turnId")) != str(turn_id):
                    continue
                if kind == "agent.turn.started":
                    seen_start = True
                    continue
                if kind == "turn.ended":
                    return ("ended", rec.get("reason"), rec.get("error") or {})
    except OSError:
        return None
    return ("started", None, {}) if seen_start else None


def confirm_delivery(sid, prompt_id, timeout, grace=None):
    """发送**之后**的独立观测：目标会话 wire 里有没有该 promptId 的 turn.prompt。

    四态必须分开，前三者**不得**当成「跑起来了」：
      · `confirmed`        —— 找到该 turn.prompt，且该轮没有立即失败
      · `confirmed_failed` —— 找到了，但该轮随即 `turn.ended reason=failed`
                              （投递成功 ≠ 执行成功；必须如实分开）
      · `no_wire`          —— 一条 wire 都没有（会话从未在本机落盘／目录不可读）
      · `no_prompt`        —— wire 在，但超时内没出现该 promptId（＝未唤醒）
    这是观测，**不重发**：投递语义仍是 at-most-once。
    """
    started = time.time()
    deadline = started + max(0.0, timeout)
    grace = OUTCOME_GRACE if grace is None else grace
    hit_wire = None
    hit_line = None
    hit_turn = None
    while True:
        for wire in reversed(wire_candidates(sid)):
            hit = scan_wire_for_prompt(wire, prompt_id)
            if hit:
                hit_wire, hit_line, hit_turn = wire, hit[0], hit[1]
                break
        if hit_wire is not None or time.time() >= deadline:
            break
        time.sleep(CONFIRM_POLL_INTERVAL)
    if hit_wire is None:
        if not wire_candidates(sid):
            return ("no_wire", {"elapsed": round(time.time() - started, 1)})
        return ("no_prompt", {"elapsed": round(time.time() - started, 1)})

    info = {"wire": str(hit_wire), "line": hit_line, "turn_id": hit_turn}
    # 给结局一点时间落盘：失败的轮通常几十毫秒内就写 turn.ended；仍在跑的轮
    # 只会看到 agent.turn.started，等到 grace 用尽再报 `pending`。
    # （看到 started 不能立刻收工——那会漏掉紧随其后的 turn.ended reason=failed）
    stop = time.time() + max(0.0, grace)
    outcome = None
    while True:
        outcome = scan_turn_outcome(hit_wire, hit_line, hit_turn)
        if outcome is not None and outcome[0] == "ended":
            break
        if time.time() >= stop:
            break
        time.sleep(OUTCOME_POLL_INTERVAL)
    info["elapsed"] = round(time.time() - started, 1)
    if outcome is None:
        info["turn_reason"] = "pending"
        return ("confirmed", info)
    kind, reason, error = outcome
    info["turn_reason"] = reason or "pending"
    if kind == "ended" and reason == "failed":
        info["error_code"] = str((error or {}).get("code") or "")
        info["error_message"] = str((error or {}).get("message") or "")
        return ("confirmed_failed", info)
    return ("confirmed", info)


def refuse_if_archived(sid, *, retired_reason: str = "", revive: bool = False):
    """归档闸：已归档会话一律拒投（ISS-76）。返回 ``None`` ＝放行，否则退出码。

    ISS-100 D3：从 `cmd_send` 的 `:872-895` **原样搬出**，`send` 与 `recycle`
    共用同一份判定，不许各写一份。`send` 的行为与措辞逐字不变。

    `retired_reason` 非空表示退役闸已经命中过：此时两个开关都给，报 4（退役是
    终态，归档闸同样不放行，只是不必再提）。`revive` 只解归档闸，且**不隐式解
    归档**——它只打印「先去 restore」并仍然拒绝。
    """
    archived, why = archived_state(sid)
    if retired_reason and archived == "yes" and revive:
        # 两个开关都给：退役是终态，报 4（归档闸同样不放行，只是不必再提）。
        print(f"send: 拒绝投递 —— {retired_reason}")
        print("  退役闸与归档闸都没被绕过（--allow-retired 只解退役，"
              "--revive 只解归档且不隐式解归档）。")
        return 4
    if archived == "yes" and not revive:
        print(f"send: 拒绝投递 —— 该会话已归档（sid={sid}）：{why}")
        print("  （归档会话被 send 投达会被服务端隐式解归档，cron 随之复活——ISS-76 根因）")
        print(f"  要先恢复请显式执行：python -m super_agent.console restore {sid}")
        return EXIT_ARCHIVED
    if archived == "yes":
        # `revive` 是「指向 restore 的拒绝态」，不产生任何恢复动作。
        print(f"send: 拒绝投递 —— 该会话已归档（sid={sid}）：{why}")
        print("  --revive 不隐式解归档，本次未投递、未改会话状态。请先：")
        print(f"    python -m super_agent.console restore {sid}")
        print("  复核：GET /api/v1/sessions/<sid> 的 archived=false，然后重发本条。")
        return EXIT_ARCHIVED
    if archived == "unknown":
        print(f"send: ⚠ 无法确认归档状态（{why}）——按放行处理；"
              "若该会话确已归档，本条会重新激活它（ISS-76）", file=sys.stderr)
    return None


def cmd_send(args):
    """给会话投一轮。

    用法：sess.py send <sid|名字> <正文>
          sess.py send <sid|名字> --body-file <路径>   # 推荐：绕开 shell 解析

    2026-11-05 实测（§7.0i）：反引号被吃掉**不是本脚本的锅**，是调用方写法问题——
    双引号内的 `...` 会被 shell 先执行。稳妥写法有两种：用单引号，或走 --body-file。
    本函数只做一道保险：若正文里同时出现反引号与「疑似已被 shell 展开」的痕迹
    （空串或与预期不符无法判断，故只警告不阻断），提示改用 --body-file。

    ISS-29：投喂前先查退役清单。归档不拦投喂（`sessions.py archive` 只摘台账），
    所以这里是**唯一能在投出去之前拦住旧会话的地方**；`--allow-retired` 明确
    绕过（会打印一行告警，不静默）。

    ISS-29 P1-A：清单**坏掉也要拒**。`RS.check()` 已经 fail-closed（坏清单返回
    非空理由），这里只需把「坏」与「退役」分开措辞，让人知道该去修清单。

    ISS-35：`--expect-confirm [秒]` 是**发送之后**的独立观测——去目标会话的
    `agents/main/wire.jsonl` 找 `promptId == 回执 prompt_id` 的 `turn.prompt`。
    它只观测、**不重发**（投递语义仍是 at-most-once）。不带该参数时行为与
    改动前逐字一致。

    ISS-76（根因修复）：投喂前再查**归档**。此前 `sess.py send` 会把已归档会话
    隐式解归档——归档事件之后 18 秒仍有 `turn.prompt` 投达，服务端随即报
    `archived=false`，且全库没有该会话的 `session.restored`；cron 随之复活。
    调度器本身尊重 `archived`（三个至今 archived=true 的会话归档后零触发），
    所以要堵的就是这道投喂口。

    三道闸定序：退役（rc=4）→ 归档（rc=6）→ 投递。退役是终态，优先。
    两个开关语义严格单向，**两个 if 不合并**：
      · `--allow-retired` 只解退役闸；
      · `--revive` 只解归档闸，且它**不隐式解归档**——只打印「先去 restore」的
        指引并仍然拒绝（自动 restore 会重新引入隐式解归档，那正是本单要消灭的）。
    """
    args, expect_confirm = _pop_expect_confirm(args)
    sid = resolve(args[0])
    # ---- 第 1 闸：退役清单（行为与措辞不变，rc=4）---------------------------
    retired_reason, retired_err = RS.check_detail(sid)
    if retired_reason and "--allow-retired" not in args:
        if retired_err:
            print(f"拒投：退役清单不可解析（{retired_err}）；"
                  f"按 fail-closed 一律视为仍退役。"
                  f"修好清单或加 --allow-retired 明确绕过。")
        else:
            print(f"send: 拒绝投递 —— {retired_reason}")
            print("  （确认要投请加 --allow-retired；那是明确绕过退役清单）")
        return 4
    if retired_reason:
        print(f"send: ⚠ 绕过退役清单投递 —— {retired_reason}")
    args = [a for a in args if a != "--allow-retired"]
    # ---- 第 2 闸：服务端 archived（ISS-76，rc=6）---------------------------
    # 与第 1 闸分开写、不合并：`--allow-retired` 不许顺带绕过归档闸。
    refused = refuse_if_archived(sid, retired_reason=retired_reason,
                                 revive="--revive" in args)
    if refused is not None:
        return refused
    args = [a for a in args if a != "--revive"]
    if len(args) > 1 and args[1] == "--body-file":
        path = args[2] if len(args) > 2 else ""
        if not path or not os.path.isfile(path):
            print(f"send: --body-file 路径不存在：{path!r}")
            return 2
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = args[1] if len(args) > 1 else ""

    if not text.strip():
        print("send: 正文为空，拒绝投递（多半是 shell 把内容吃掉了——改用 --body-file）")
        return 2

    # 2026-11-01 修：API 只认 content 数组；旧写法 {"text": …} 会返回 null 且**静默丢弃**
    # 2026-11-06 步 3：控制面走门面（bus 优先，三态不重放），match 校验收到的回执
    def _direct_send():
        st_, pl_ = _req("POST", f"/api/v1/sessions/{sid}/prompts",
                        {"content": [{"type": "text", "text": text}]})
        d_ = _data(pl_) or {}
        stored_ = ""
        if isinstance(d_, dict):
            for c in (d_.get("content") or []):
                if isinstance(c, dict) and c.get("type") == "text":
                    stored_ = c.get("text") or ""
                    break
        return bool(d_), {"http": st_, "raw": d_,
                          "prompt_id": d_.get("prompt_id") if isinstance(d_, dict) else None,
                          "match": stored_ == text}

    ok, detail, via = BUSD.ctl("send", sid, caller="sess.py send",
                               payload={"text": text}, direct_fn=_direct_send)
    st = detail.get("http") if isinstance(detail, dict) else None
    data = detail.get("raw") if isinstance(detail, dict) else None
    if not isinstance(data, dict):
        data = {k: v for k, v in (detail or {}).items()
                if k in ("prompt_id", "match", "state", "code", "_rid")}
    # 回执里的 match 是 daemon 侧权威判据（它拿到的是上游存档文本）；
    # 不要用本地变量覆盖，否则 bus 路会误报 match=False（2026-11-06 实测踩到）
    if isinstance(detail, dict) and detail.get("match") is not None:
        data["match"] = detail["match"]
    if isinstance(detail, dict) and detail.get("state") == "indeterminate":
        # 已受理/不确定：**绝不重发**，如实告知调用方
        print(f"send: 不确定（可能已投递，禁止重发） 请求 {detail.get('_rid')}")
        print(f"  回执：{json.dumps(detail, ensure_ascii=False)[:220]}")
        return 3
    ok = bool(ok)
    # 2026-11-05：回读服务端实际存档的 text，让「到底投进去没有/内容对不对」可验证
    stored = ""
    if ok:
        try:
            for c in (data.get("content") or []):
                if isinstance(c, dict) and c.get("type") == "text":
                    stored = c.get("text") or ""
                    break
        except AttributeError:
            stored = ""
    # 优先用回执里的 match（daemon 侧权威）；没有才用本地比对
    same = data.get("match")
    if same is None:
        same = (stored == text)
    print(f"queued http={st} ok={ok} match={same} [{via}] "
          f"{json.dumps(data, ensure_ascii=False)[:160]}")
    if not ok:
        # ISS-35：未送达必须诚实报错。此前这条路径只打「queued ok=False」，
        # 读的人容易当成已投；现在把原因与结论写死（不重发，不猜）。
        code = (detail or {}).get("code")
        if code == 40401:
            print(f"send: 未送达 —— 服务端不认识该会话（code=40401，sid={sid[:16]}…）")
            # 2026-11-14（PM 第十任实测，supervisor 裁「顺手修」）：与 abort 路径同口径。
            # 实测时间线：服务端先起（14:37:41），随后 1–2 分钟内 send 返回 40401，
            # 窗口过后同工具同 sid 即可投达 ⇒ 这是「新进程尚未加载该会话」的瞬态，
            # 不是端口错、不是 daemon 陈旧、也不需要换工具。
            print("  可能原因：kimi 刚重启，服务端尚未加载该会话（瞬态）。")
            print("  处置：等 60–120 秒重投；若持续 404 ⇒ 会话真不存在。")
            print("  自助判据：KIMI_BUS_MODE=direct python -m super_agent.console status "
                  f"{sid[:16]}…   # 绕过 bus 现读端口复验")
        elif code:
            print(f"send: 未送达 —— 上游拒绝（code={code}）")
        else:
            print(f"send: 未送达 —— 回执 ok=False（state={(detail or {}).get('state')}），"
                  f"该轮没有进入会话")
        print("  未重发（投递语义 at-most-once）；请人工核对后再决定是否重投。")
        return 1
    if ok and not same:
        print("  ⚠ 服务端存档内容与发送内容不一致（可能被 shell 或转义改动）")
    if "`" in text:
        print("  提示：正文含反引号。本脚本已完成投递；若怀疑被 shell 吃掉，"
              "下次用 --body-file 或单引号包正文（§7.0i）")
    if expect_confirm is None:
        return 0
    # ---- ISS-35：发送后的独立观测（不重发）--------------------------------
    prompt_id = str(data.get("prompt_id") or "")
    if not prompt_id:
        print("确收失败：无法确收（上游未给 prompt_id）"
              "——回执里没有 prompt_id，无从比对 wire")
        return EXIT_UNCONFIRMED
    timeout = max(0.0, expect_confirm)
    verdict, info = confirm_delivery(sid, prompt_id, timeout)
    if verdict == "confirmed":
        print(f"确收 turnId={info.get('turn_id')} 行号={info.get('line')} "
              f"用时={info.get('elapsed')}s  该轮={info.get('turn_reason')}  "
              f"promptId={prompt_id}")
        print(f"  wire={info.get('wire')}")
        return 0
    if verdict == "confirmed_failed":
        # 投递成功 ≠ 执行成功：wire 有这一行，但该轮立刻 turn.ended reason=failed。
        print(f"DELIVERED-BUT-FAILED turnId={info.get('turn_id')} "
              f"行号={info.get('line')} 用时={info.get('elapsed')}s  "
              f"promptId={prompt_id}")
        print(f"  ⇒ 消息**已进**目标会话（wire 有该 turn.prompt），但本轮以失败结束："
              f"{info.get('error_code') or '?'} {info.get('error_message') or ''}".rstrip())
        print("  常见原因：该会话没配模型（model.not_configured）。"
              "处置：sess.py profile <会话> --model <别名> 后重投；本次未重发。")
        print(f"  wire={info.get('wire')}")
        return EXIT_UNCONFIRMED
    if verdict == "no_wire":
        print(f"UNCONFIRMED 目标会话 wire 不存在（用时 {info.get('elapsed')}s）："
              f"daemon 已受理 promptId={prompt_id}，但本地找不到该 sid 的 "
              f"agents/main/wire.jsonl")
        print("  ⇒ 无法判定是否唤醒；**不是**送达证据，也不是未送达证据。")
        return EXIT_UNCONFIRMED
    print(f"UNCONFIRMED daemon 已受理但目标会话 wire 未见该 promptId"
          f"（等待 {info.get('elapsed')}s 超时，promptId={prompt_id}）")
    print("  ⇒ 可能未唤醒。未重发（at-most-once）；请 dispatch --session 或人工确认。")
    return EXIT_UNCONFIRMED


def cmd_tasks(args):
    """后台任务列表。bus 优先（/tasks/<sid>），直连降级。

    来源必须可判（P1-1）：空列表同样是合法结果，所以走 `tasks_src()` 拿标记，
    而不是靠「返回是否 None」猜。
    """
    sid = resolve(args[0])
    ts, src_kind = BUSD.tasks_src(sid, caller="sess.py tasks")
    if ts is None:
        ts = kimi_api.tasks(sid)
        src_kind = "direct"
    print(json.dumps(ts, ensure_ascii=False)[:1500])
    print(f"  来源：{'bus(/tasks)' if src_kind == 'bus' else 'direct(tasks)'}")
    return 0


def cmd_cancel_task(args):
    """取消一个后台任务。

    2026-11-05 实测：旧代端点**必须带 body**（空 body 会 50001 "Body cannot be empty"），
    这就是旧版恒打 null 的原因。带 {} 后返回 {"cancelled":true}。
    注意：cancelled=true 只表示服务端受理，任务状态要几秒后才转 cancelled。
    2026-11-06（issue #6）：新代改走 WS `task.stop`，分支在 `kimi_api.cancel_task`。
    """
    sid, tid = resolve(args[0]), args[1]
    ok, detail, via = BUSD.ctl(
        "cancel-task", sid, caller="sess.py cancel-task",
        payload={"tid": tid},
        direct_fn=lambda: kimi_api.cancel_task(sid, tid))
    code = detail.get("code") if isinstance(detail, dict) else None
    print(f"cancel ok={ok} code={code} [{via}]")
    return 0 if ok else 1


def cmd_pending(args):
    """待批交互（bus 优先）。与 `approvals` 的区别：**只读列出**，不答复。

    判据（步 2 对比表要求）：必须能区分「确实没有待批」与「有待批但拿不到明细」。
    """
    sid = resolve(args[0])
    # **必须用 pending_src**：空列表是合法结果，靠 None 判会永远停在 bus 分支，
    # 于是 KIMI_BUS_MODE=direct 也显示 bus(...)（2026-11-06 步 2 返工 P0-1）。
    items, src_kind = BUSD.pending_src(sid, caller="sess.py pending")
    if items is None:
        items = kimi_api.pending_interactions(sid)
        src_kind = "direct"
    src = f"bus(/pending)" if src_kind == "bus" else "direct(pending_interactions)"
    if not items:
        print(f"没有待批交互  来源：{src}")
        return 0
    for it in items:
        if not it.get("approval_id"):
            print(f"未知（有 pending 但无明细）  {it.get('action')}  来源：{src}")
            continue
        print(f"{it.get('kind')} {it.get('approval_id')} {it.get('tool_name')} "
              f"{str(it.get('action'))[:80]}  来源：{src}")
    return 0


def cmd_messages(args):
    """会话消息。**保持直连**——daemon 只存状态/事件，不存消息正文。"""
    sid = resolve(args[0])
    n = args[1] if len(args) > 1 else "5"
    st, payload = _req("GET", f"/api/v1/sessions/{sid}/messages?page_size={n}")
    items = _data(payload)
    if isinstance(items, dict):
        items = items.get("items") or []
    for it in (items or [])[:int(n)]:
        role = it.get("role")
        text = "".join(c.get("text", "") for c in it.get("content", []) if isinstance(c, dict))
        print(f"[{role}] {text[:300]}")
    return 0


def cmd_simple(args, path, method="POST", key=None):
    sid = resolve(args[0])
    st, payload = _req(method, path.format(sid=sid))
    print(f"http={st} {json.dumps(_data(payload), ensure_ascii=False)[:400]}")
    return 0


def _request_verdict(st, payload) -> tuple[bool, str]:
    """服务端「受理」判据；返回 (是否受理, 走了哪条分支的说明)。

    两种响应形状：

      · 单动作（`archive`/`takeover`）失败时**信封顶层 `code` 非零**（如 40401）；
      · v2 批量（`restore` 走 `/api/v2/sessions:restore`）**外层 code 恒为 0**，
        逐条结果在 `data` 里：`{"code": 0, "data": {"results": [{"ok": false,
        "error": {"code": …}}], "succeeded": 0, "failed": 1}}`。只认顶层 code
        就会把「一条都没成功」当成功，于是写出假的 `session.restored`。

    批量判据（**失败必不记**）：`failed` 必须为 0／缺省，每条 `results[].ok`
    必须为真且无 `error`，成功条数 ≥1；部分成功也判失败（restore 只发一条 id，
    出现混合结果说明服务端状态不明，宁可不记事件）。

    最前一层是 HTTP：`st >= 400` 一律判失败——4xx/5xx 的错误页常带空 body，
    走到下面会因 `code is None` 被当成受理。
    """
    if not st:
        return False, "无 HTTP 状态"
    if st >= 400:
        return False, f"HTTP {st}"
    if not isinstance(payload, dict):
        return False, "响应非对象"
    body = _data(payload)
    results = body.get("results") if isinstance(body, dict) else None
    if isinstance(results, list):
        code = payload.get("code")
        if code not in (0, None):
            return False, f"批量响应外层 code={code}"
        ok_n = len([r for r in results if isinstance(r, dict) and r.get("ok")])
        bad = [r for r in results if not isinstance(r, dict) or not r.get("ok")
               or r.get("error")]
        failed = body.get("failed")
        succeeded = body.get("succeeded")
        if failed not in (0, None) or (isinstance(succeeded, int) and succeeded < ok_n):
            return False, f"批量响应 succeeded={succeeded} failed={failed}"
        if bad or ok_n < 1:
            return False, (f"批量响应 succeeded={ok_n} "
                           f"failed={failed if failed is not None else 0}")
        return True, f"批量响应 succeeded={ok_n} failed=0"
    code = payload.get("code")
    if code in (0, None):
        return True, f"顶层 code={code}"
    return False, f"顶层 code={code}"


def _request_ok(st, payload) -> bool:
    """服务端「受理」判据（`_request_verdict` 的布尔投影，给 `profile` 等调用者）。"""
    return _request_verdict(st, payload)[0]


def cmd_lifecycle(args, path, event_type, body=None, post_hook=None):
    """archive / restore / takeover：动作成功后记一条生命周期事件（ISS-27）。

    `post_hook` 只给 `archive` 用（ISS-76）：归档成功后报出该会话名下仍存活的
    cron。它**不改变退出码、不阻断归档**——归档本身已成功。`takeover`／`restore`
    不传它，行为逐字不变。

    ISS-76 P1-2（2026-11-17 QA 返工）：`takeover` 不再复用 `session.restored`。
    两者语义不同：restore 是「归档被撤销」，takeover 只是「接管正在用的会话」。
    复用同一事件类型让本地旁证把 takeover 读成「未归档」——实测会把归档会话
    误判成活的。takeover 现在写独立类型 `session.taken_over`（见 `EV_TAKEN_OVER`）。
    `session_events.replay` 遇未知类型是 no-op，所以 safkc 侧读取不受影响。

    ISS-76 P1-1（2026-11-17 QA 第四轮）：事件写失败**不再抛源码异常**。
    此前是裸 `EV.emit`，事件闸等 15 秒锁超时后抛出去，命令以 traceback 退 1——
    方向安全（没人会当成功），但退出码与服务端失败同值，调用方分不清
    「归档没成」与「归档成了、闸门降级了」。现在与 `sessions.py cmd_archive`
    同口径：动作成功但事件未落 ⇒ `EXIT_EVENT_LOST`（rc=8）＋后果与补救提示。
    **不回滚动作**：服务端归档不可撤销，假装失败会误导。
    """
    sid = resolve(args[0])
    payload_in = body(sid) if callable(body) else body
    st, payload = _req("POST", path.format(sid=sid), payload_in)
    ok, why = _request_verdict(st, payload)
    print(f"http={st} {json.dumps(_data(payload), ensure_ascii=False)[:400]}")
    if not ok:
        print(f"未记事件（服务端未受理 {why}）")
        return 0
    lost = False
    try:
        EV.emit(event_type, sid=sid)
    except Exception as exc:  # noqa: BLE001  动作已成功，事件失败只影响闸门
        lost = True
        # 措辞按事件类型分开：只有 `session.archived` 会让**归档闸**退化，
        # restore／takeover 漏写只是旁证缺失。任务书要求的三个要素必须逐字出现。
        done_word = "归档已生效" if event_type == EV_ARCHIVED else "动作已生效"
        gate_word = "归档闸对该会话将退化" if event_type == EV_ARCHIVED else "旁证缺这一条"
        print(f"警告：{event_type} 未写入（{type(exc).__name__}: {exc}）", file=sys.stderr)
        print(f"      {done_word}（服务端不可撤销），但本地事件未落 ⇒ {gate_word}。",
              file=sys.stderr)
        print(f"      补救：python -m super_agent.sess_events emit {event_type} --sid {sid} "
              f"--state-dir {EV.resolve_root(None)}", file=sys.stderr)
    if post_hook is not None:
        post_hook(sid)
    return EXIT_EVENT_LOST if lost else 0


def cmd_cron(args):
    """跨会话 cron：`list` 报出存活集，`rm` 一律拒绝。

    ISS-76 事实：**服务端既无 cron 读接口也无写接口**（`/api/v1/cron`、
    `/api/v1/crons`、`/api/v1/cron/jobs`、`/api/v1/sessions/<sid>/cron` 全 404；
    `GET /api/v1/sessions/<sid>` 的 data 里没有 cron 字段）。cron 是 Kimi 客户端
    工具（CronCreate／CronList／CronDelete），只作用于**当前会话**。

    所以：
      · `list` 读该会话的 wire 复算 `cron.add − cron.delete`，rc=0；
        wire 不存在或读不出 ⇒ 「未确认」，rc=2（**不许报 0 条**）；
      · `rm` 一律 rc=7 且**不执行任何写操作**。

    原话（supervisor 要求写进 docstring）：**跨会话删除 cron 在服务端根本做不到**
    ——服务端既无 cron 读接口也无写接口，CronDelete 只在会话自身内可用。
    本工具只提供报出，不提供删除。

    用法：
      sess.py cron list <sid|名字>
      sess.py cron rm <sid|名字> <jobId>
    """
    if not args:
        print("用法: sess.py cron list <sid|名字> | sess.py cron rm <sid|名字> <jobId>")
        return 2
    sub, rest = args[0], args[1:]
    if sub == "list":
        if not rest:
            print("用法: sess.py cron list <sid|名字>")
            return 2
        sid = resolve(rest[0])
        print(f"sid={sid}")
        lines, summary = CRON.describe(sid)
        for line in lines:
            print(line)
        if summary["state"] == CRON.STATE_UNKNOWN:
            return 2
        for line in CRON.clear_hint(sid):
            print(line)
        return 0
    if sub == "rm":
        if len(rest) < 2:
            print("用法: sess.py cron rm <sid|名字> <jobId>（本命令一律拒绝执行）")
            return 2
        sid, job_id = resolve(rest[0]), rest[1]
        print(f"cron rm: 拒绝执行 —— 跨会话删除 cron 在服务端根本做不到"
              f"（sid={sid} jobId={job_id}）")
        print("  服务端既无 cron 读接口也无写接口，CronDelete 只在会话自身内可用。")
        print("  本工具只提供报出，不提供删除。本次未执行任何写操作。")
        for line in CRON.clear_hint(sid):
            print(f"  {line}")
        return 7
    print(f"未知 cron 子命令：{sub}（只支持 list / rm）")
    return 2


def _parse_flags(rest):
    """极简 `--k v` 解析（本文件的其它子命令也是位置参数风格）。"""
    out, i = {}, 0
    while i < len(rest):
        if not rest[i].startswith("--"):
            i += 1
            continue
        key = rest[i][2:]
        out[key] = rest[i + 1] if i + 1 < len(rest) else ""
        i += 2
    return out


def cmd_profile(args):
    """改会话 profile（model / effort / permission）。

    此前没有统一入口——改模型只能直接拼 `POST /sessions/<sid>/profile`
    （`tools/revive.py:74 switch_model()`），于是「谁在什么时候换了模型」
    在事件流里查不到。本子命令把那一步收成入口并记 `session.profile_changed`。

    用法：sess.py profile <sid|名字> [--model M] [--effort high] [--permission auto]
    """
    sid = resolve(args[0])
    flags = _parse_flags(args[1:])
    cfg: dict = {}
    profile: dict = {}
    if flags.get("model"):
        cfg["model"] = profile["model"] = flags["model"]
    if flags.get("effort"):
        cfg["thinking"] = profile["effort"] = flags["effort"]
    if flags.get("permission"):
        cfg["permission_mode"] = profile["permission"] = flags["permission"]
    if not cfg:
        print("profile: 至少给 --model / --effort / --permission 之一")
        return 2

    def _direct_profile():
        st, payload = _req("POST", f"/api/v1/sessions/{sid}/profile",
                           {"agent_config": cfg})
        return _request_ok(st, payload), (_data(payload) or {})

    ok, detail, via = BUSD.ctl(
        "profile", sid, caller="sess.py profile",
        payload={"model": flags.get("model") or ""},
        direct_fn=_direct_profile)
    print(f"profile ok={ok} [{via}] {json.dumps(detail, ensure_ascii=False)[:200]}")
    if not ok:
        return 1
    EV.emit("session.profile_changed", sid=sid, profile=profile)
    return 0


def sid_ok_or_explain(arg, allow_short: bool) -> bool:
    """ISS-74 B：`session_` 前缀但形态不完整 ⇒ 这是残号，不是台账键。

    放在 `main()` 一处而不是逐个子命令里做：`sess.py` 的 15 个子命令都收
    `<sid|名字>`，逐个加校验必然漏一个。键名（`qa`／`pm-<项目>`）不以
    `session_` 开头，所以这条闸不会误拦正常用法。
    """
    text = str(arg or "")
    if not text.startswith("session_"):
        return True                     # 是台账键或别的形态，交给 resolve()
    if EV.check_sid(text):
        return True
    if allow_short:
        print(f"警告：{text!r} 不是完整 sid 形态，按 --allow-short-sid 放行",
              file=sys.stderr)
        return True
    print("拒绝执行：sid 不是完整形态")
    print(f"  {EV.sid_problem(text)}")
    print("  可用 python -m super_agent.console list 取完整 id；确要试残号请加 --allow-short-sid")
    return False


def main(argv=None):
    argv = EV.apply_state_dir(argv or sys.argv[1:])
    # #98 反例 5：坏的私有配置必须在**做任何事之前**失败，且打印路径。
    # 转 rc=2 由本函数统一收口（`agent_config` 抛的是 `config.ConfigError`）。
    try:
        EV.resolve_root()
    except config_mod.ConfigError as exc:
        print(f"[config] {exc}", file=sys.stderr)
        return 2
    # ISS-74 B：逃生门两处都认——子命令前（`sess.py --allow-short-sid restore …`）
    # 与子命令后（`sess.py restore … --allow-short-sid`）。
    allow_short = "--allow-short-sid" in argv
    argv = [a for a in argv if a != "--allow-short-sid"]
    if not argv:
        print(__doc__.strip())
        return 2
    cmd, rest = argv[0], argv[1:]
    table = {
        "list": cmd_list, "status": cmd_status, "abort": cmd_abort, "send": cmd_send,
        "approvals": cmd_approvals,
        "pending": cmd_pending,
        "tasks": cmd_tasks, "cancel-task": cmd_cancel_task, "stop": cmd_stop, "steer": cmd_steer, "messages": cmd_messages,
        "snapshot": lambda a: cmd_simple(a, "/api/v1/sessions/{sid}/snapshot", "GET"),
        "takeover": lambda a: cmd_lifecycle(a, "/api/v1/sessions/{sid}:takeover",
                                           EV_TAKEN_OVER),
        "archive": lambda a: cmd_lifecycle(a, "/api/v1/sessions/{sid}:archive",
                                          EV_ARCHIVED,
                                          post_hook=CRON.report),
        # v2 restore 只认 `{"ids": [...]}`；不带 body 会 50001（实测）
        "restore": lambda a: cmd_lifecycle(a, "/api/v2/sessions:restore",
                                          EV_RESTORED,
                                          body=lambda sid: {"ids": [sid]}),
        "profile": cmd_profile,
        "cron": cmd_cron,
    }
    fn = table.get(cmd)
    if not fn:
        print(f"未知子命令：{cmd}")
        return 2
    if rest and not sid_ok_or_explain(rest[0], allow_short):
        return 2
    # `cron list <sid>` / `cron rm <sid> <jobId>` 的 sid 在第二位。
    if cmd == "cron" and len(rest) > 1 and not sid_ok_or_explain(rest[1], allow_short):
        return 2
    return fn(rest)


if __name__ == "__main__":
    sys.exit(main())
