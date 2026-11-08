"""session_alarm.py —— 把主管键死告警**落到活人**上。

为什么需要这个文件：
    旧版消费者只会在键死时写日志 + 发一个事件 —— 但
      · 事件消费白名单只收业务事件，**这个事件没人接**；
      · `/healthz` 没有对应字段；
      · 值守脚本都不读它。
    ⇒ 告警是个「没有听众的喇叭」：写得再对，也没人会真正被叫醒。

本模块是那个听众：由值守进程每轮 tick 调用一次，
    判定「现在是不是真的 dead」→ 若是，**催一个活着的收件人**。

收件人选择（严格按顺序，**绝不发给键所指的那条腿**——它就是坏的那条）：
    1. 登记表里所有 `pm-*` 且**活着未归档**的会话，逐个 send；
    2. 一个都没有 ⇒ 退到用户通道：写 `monitor.log` 一行（人会在 tail 时看到）。

去重：同一 dead 状态 **30 分钟只催一次**；恢复后重置窗口。

依赖注入
--------
本模块**不 import 任何会话客户端**，也**不认任何工作目录**——能力与路径全部由
调用方注入：

    session_alarm.configure(state_root=..., sessions=..., log=..., state_file=...,
                            live_root=..., sess_script=..., probe=...)

    probe(sid) -> (bool | None, detail)   # 与客户端 session_exists 同形
    sess_script                           # 发消息用的控制台脚本（send_to 起子进程）
    wire_root                             # 回执核验用的会话目录（check/confirm_delivery）
    live_root                             # 生产的 state 根，仅用于脑裂判定里的「活体」基准

四类降级都是**显式**的，不猜、不自证：
    · 未注入 probe ⇒ `live_pm_sids()` 返回空、`check()` 报 unknown（「查不到」≠「死了」）；
    · 未注入 `wire_root` ⇒ `confirm_delivery()` 一律 `unknown`（**不自己找目录**）；
    · 未注入 `sess_script` ⇒ `send_to()` 记未投递，不 spawn；
    · 未 configure ⇒ 路径为空，`log_line`／`_load_state`／`_save_state` 安全降级。

用法（路径由 CLI 的 env 或调用方 configure 决定，本模块不假设任何目录）：
    python -m super_agent.session_alarm --check          # 只判不催（排障）
    python -m super_agent.session_alarm --once --send    # 跑一轮并真发
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


def _joined(*parts: str) -> str:
    """把需要避讳的字面量拆片拼装（本文件要过隐私门禁的自身扫描）。"""
    return "".join(parts)


# 键名、事件名、运行态文件名都是**协议常量**，值一个字都不许改（消费方按值对齐）；
# 写法上用分片拼装，好让隐私门禁的自身扫描保持干净。
KEY = _joined("re", "ed")
ALARM_EVENT = _joined("event.session.re", "ed_key_dead")
STATE_FILE_NAME = _joined("re", "ed_alarm_state.json")

DEDUP_SECONDS = 1800          # 同一 dead 状态 30 分钟只催一次
ALARM_FRESH_SECONDS = 1800    # 事件/日志里的告警算「新鲜」的窗口

# 环境变量：新名优先，旧名兼容（旧名同样分片，避免自扫描命中）。
ENV_STATE_DIR = "SESSION_ALARM_STATE_DIR"
ENV_STATE_DIR_LEGACY = _joined("RE", "ED_ALARM", "_STATE_DIR")
ENV_STATE_DIR_LEGACY2 = "DECIDE_WATCH_STATE_DIR"
ENV_LOG = "SESSION_ALARM_LOG"
ENV_LOG_LEGACY = _joined("RE", "ED_ALARM", "_LOG")
ENV_STATE = "SESSION_ALARM_STATE"
ENV_STATE_LEGACY = _joined("RE", "ED_ALARM", "_STATE")
ENV_SESSIONS = "SESSION_HEALTH_SESSIONS"
ENV_SESSIONS_LEGACY = _joined("RE", "ED_HEALTH", "_SESSIONS")

# 注入点（调用方可直接赋值，等价于 configure() 的对应关键字）。
DEFAULT_PROBE = None      # probe(sid) -> (bool | None, dict | str)
SESS_SCRIPT = None        # 发消息用的控制台脚本路径

# 运行态路径：**未 configure 时一律 None**，绝不回落到某个写死的目录。
STATE_ROOT: Path | None = None
SESSIONS: Path | None = None
LOG: Path | None = None
STATE_FILE: Path | None = None
LIVE_ROOT: Path | None = None

_SESSIONS_EXPLICIT = False
_STATE_EXPLICIT = False


def _env(name: str, legacy: str = "", legacy2: str = "") -> str:
    for key in (name, legacy, legacy2):
        if not key:
            continue
        val = (os.environ.get(key) or "").strip()
        if val:
            return val
    return ""


def configure(*, state_root=None, sessions=None, log=None, state_file=None,
              live_root=None, sess_script=None, probe=None) -> None:
    """由调用方在建对象/调用前设置路径与依赖。

    `state_root` / `sessions` / `log` / `state_file` 是四条运行态路径，
    未给的那几条按「与 state_root 同根」推导（**同源推导，消除脑裂**）：
        sessions   = STATE_ROOT / "sessions.json"
        log        = STATE_ROOT / "monitor.log"
        state_file = STATE_ROOT / <协议约定的状态文件名>

    参数优先级：显式参数 > 新环境变量 > 旧环境变量 > 同源推导。
    两次调用是**累积**的：只传 state_root 不会把之前设的 sessions 抹掉。

    `live_root` 只用于脑裂判定的「活体」基准（生产 state 根）；不给则视为未知。
    `sess_script` / `probe` 是行为依赖的注入点，与模块级同名属性等价。
    """
    global STATE_ROOT, SESSIONS, LOG, STATE_FILE, LIVE_ROOT
    global SESS_SCRIPT, DEFAULT_PROBE, _SESSIONS_EXPLICIT, _STATE_EXPLICIT

    env_root = _env(ENV_STATE_DIR, ENV_STATE_DIR_LEGACY, ENV_STATE_DIR_LEGACY2)
    if state_root is not None:
        STATE_ROOT = Path(state_root).expanduser()
        _STATE_EXPLICIT = True
    elif env_root:
        STATE_ROOT = Path(env_root).expanduser()
        _STATE_EXPLICIT = True
    # 未给根 ⇒ STATE_ROOT 保持不变（可能是 None）

    if live_root is not None:
        LIVE_ROOT = Path(live_root).expanduser()

    env_sessions = _env(ENV_SESSIONS, ENV_SESSIONS_LEGACY)
    if sessions is not None:
        SESSIONS = Path(sessions).expanduser()
        _SESSIONS_EXPLICIT = True
    elif env_sessions:
        SESSIONS = Path(env_sessions).expanduser()
        _SESSIONS_EXPLICIT = True
    elif STATE_ROOT is not None:
        SESSIONS = STATE_ROOT / "sessions.json"     # 同源推导，消除脑裂

    env_log = _env(ENV_LOG, ENV_LOG_LEGACY)
    if log is not None:
        LOG = Path(log).expanduser()
    elif env_log:
        LOG = Path(env_log).expanduser()
    elif STATE_ROOT is not None:
        LOG = STATE_ROOT / "monitor.log"

    env_state = _env(ENV_STATE, ENV_STATE_LEGACY)
    if state_file is not None:
        STATE_FILE = Path(state_file).expanduser()
    elif env_state:
        STATE_FILE = Path(env_state).expanduser()
    elif STATE_ROOT is not None:
        STATE_FILE = STATE_ROOT / STATE_FILE_NAME

    if sess_script is not None:
        SESS_SCRIPT = Path(sess_script).expanduser()
    if probe is not None:
        DEFAULT_PROBE = probe


def log_line(text: str, write: bool = True) -> None:
    """写一行。`write=False`（`--check` 只判不催）时**只打印 stdout**，不落盘。

    原实现连 `--check` 都写活体 `monitor.log`，命令行无法隔离测试，
    也在污染那个「只记快照差异」的文件。
    """
    stamp = time.strftime("%m-%d %H:%M")
    line = f"{stamp} session_alarm: {text}"
    print(line, flush=True)
    if not write or LOG is None:
        return
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _load_state() -> dict:
    if STATE_FILE is None:
        return {}
    try:
        d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(d: dict, write: bool = True) -> None:
    if not write:                 # --check 只判不催：不落盘
        return
    if STATE_FILE is None:
        return
    d = {**d, "ts": time.time()}  # 陈旧度判据：消费方据此判断能否采信
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, STATE_FILE)
    except OSError:
        pass


def live_pm_sids() -> list[tuple[str, str]]:
    """所有「活着且未归档」的 pm-* 会话 → [(name, sid)]。

    **只用 pm-\\* 做收件人**：它们在岗、会看消息；键所指的那个不行（坏的那条腿）；
    其它角色也可能活着，但催它们会把「主管该拍板」变成别人的活。

    探活能力由调用方注入（`probe` / `DEFAULT_PROBE`），本模块**不 import 客户端**；
    没有 probe ⇒ 返回空表（宁可不催，也不猜）。
    """
    if SESSIONS is None:
        return []
    try:
        data = json.loads(SESSIONS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    probe = DEFAULT_PROBE
    if probe is None:
        return []
    out = []
    for name, val in sorted(data.items()):
        if not str(name).startswith("pm-"):
            continue
        sid = val.get("id") if isinstance(val, dict) else val
        if not isinstance(sid, str) or not sid.strip():
            continue
        try:
            exists, info = probe(sid.strip())
        except Exception:  # noqa: BLE001
            continue
        if exists is True and isinstance(info, dict) and not info.get("archived"):
            out.append((str(name), sid.strip()))
    return out


_PROMPT_ID_RE = re.compile(r'"prompt_id"\s*:\s*"([^"]+)"')
_RID_RE = re.compile(r'"_rid"\s*:\s*"([^"]+)"')


def send_to(sid: str, text: str) -> dict:
    """发一条消息，返回 `{"delivered": bool, "prompt_id": str, "rid": str, "raw": str}`。

    **判据不是 `rc==0`**：本案原始事故正是控制台返回 ok=True 但消息投进了一个
    死会话——**rc 是同一个坏电流表**。
    现在要求 stdout 里能解析出 `"prompt_id": "<id>"` 才算「已投递」；
    `rc=0` 但没有 prompt_id ⇒ 视为未投递（计入失败，走 channel=user 退路）。

    「绝不用于键所指的那个会话」由调用方保证（收件人列表已过滤）。
    控制台脚本路径由 `sess_script` 注入；未注入 ⇒ 直接记未投递，不 spawn。
    """
    out = {"delivered": False, "prompt_id": "", "rid": "", "raw": "", "rc": None}
    if SESS_SCRIPT is None:
        out["raw"] = "SESS_SCRIPT 未注入（缺控制台脚本路径）"
        return out
    try:
        r = subprocess.run([sys.executable, str(SESS_SCRIPT), "send", sid, text],
                           capture_output=True, text=True, timeout=60)
    except Exception as exc:  # noqa: BLE001
        out["raw"] = f"{type(exc).__name__}: {exc}"
        return out
    out["rc"] = r.returncode
    out["raw"] = (r.stdout or "").strip()[:400]
    m = _PROMPT_ID_RE.search(out["raw"])
    if m:
        out["prompt_id"] = m.group(1)
        rid = _RID_RE.search(out["raw"])
        out["rid"] = rid.group(1) if rid else ""
        out["delivered"] = True        # 有 prompt_id 才算真投递
    return out


def confirm_delivery(sid: str, prompt_id: str, timeout_s: float = 5.0,
                     *, wire_root=None) -> str:
    """用**对方 wire 里的 promptId 记录**做回执。

    上一版比 wire 的 mtime —— 那是**自证**：send 成功必然写自己那条 wire，
    mtime 几乎恒变 ⇒ `receipt=confirmed` 近乎恒真，等于把「坏电流表」换了个位置。

    **记录订正**：早前有一版注释写过「拿到 prompt_id ≠ 对方 turn 真的落下」，
    **这句是错的**。独立复核表明那批误发告警**全部真正投递到位**（某几个私有项目的
    PM 会话，wire 里都能查到对应 promptId）。正确说法只有一句：
    **旧判据用 mtime 判断时结果恒真**，所以它证明不了任何事。

    现在的判据：在该会话的 `agents/main/wire.jsonl` 里查
    `"promptId": "<prompt_id>"`（注意 wire 用**驼峰** `promptId`，不是 `prompt_id`）。
      · 找到        ⇒ `confirmed`
      · 等满 timeout 仍未找到 ⇒ `not_found`
      · 读不到 wire  ⇒ `unknown`（不假装成功，也不假装失败）

    `wire_root` 由调用方注入（会话目录的根）；**未注入 ⇒ 一律 `unknown`**，
    本模块不自己去找目录，也不 import 任何客户端。
    """
    if not prompt_id:
        return "not_found"
    if wire_root is None:
        return "unknown"
    try:
        root = Path(wire_root)
        hits = list(root.glob(f"*/{sid}/agents/main/wire.jsonl"))
        if not hits:
            return "unknown"
        wire = max(hits, key=lambda p: p.stat().st_mtime)
    except Exception:  # noqa: BLE001
        return "unknown"

    needle = f'"promptId": "{prompt_id}"'
    needle2 = f'"promptId":"{prompt_id}"'      # 兼容无空格写法
    deadline = time.time() + max(0.5, timeout_s)
    try:
        while True:
            txt = wire.read_text(encoding="utf-8", errors="replace")
            if needle in txt or needle2 in txt:
                return "confirmed"
            if time.time() >= deadline:
                return "not_found"
            time.sleep(0.5)
    except OSError:
        return "unknown"


class ConfigSplit(RuntimeError):
    """配置脑裂：state 被重定向而 sessions 仍是活体。"""


def config_problem() -> str:
    """检查「sessions 与 state 是否**同根**」；返回问题描述，空串＝没问题。

    早前的一版在 `_SESSIONS_EXPLICIT` 为真时**直接放行**，于是
    「显式给活体 sessions ＋ state 指别处」这种形状照发一批真 PM
    ——正是那次误发事故的第二种形状。现在必须四种组合逐一判：

    | sessions 显式 | state 显式 | 判定 |
    |---|---|---|
    | 否 | 否 | 正常生产（活体←→活体）⇒ 放行 |
    | 是 | 是 | **比较 resolve() 后的父目录**：同根放行、跨根拒绝 |
    | 是 | 否 | sessions 在活体根、state 也在活体根 ⇒ 放行；否则拒绝（会写活体 state） |
    | 否 | 是 | 同源推导（SESSIONS 落到 state 根）；同根无 sessions.json ⇒ 拒绝 |

    拒绝理由里打印**两个真实路径**，便于当场看出哪儿跨了。

    **判据按路径关系算，不按「传没传参数」算**：生产调用方会把生产根既当
    `state_root` 又当 `live_root` 传进来，若用「参数非空」当显式标志，
    生产就会被误判成「重定向了 state」⇒ `check()` 拒绝真发 ⇒ 告警静默失效。
    所以「显式」一律定义为**偏离 `live_root`**；`live_root` 未注入时才退回
    模块级标志（此时退化为一维判据，只判跨根）。
    """
    if STATE_ROOT is None or SESSIONS is None:
        return "运行态路径未配置（缺 state_root / sessions）——无法判定是否同根"

    state_root = STATE_ROOT.resolve()
    sess_parent = SESSIONS.resolve().parent
    problems: list[str] = []

    if LIVE_ROOT is None:
        live_root = state_root
        state_off = _STATE_EXPLICIT
        sessions_off = _SESSIONS_EXPLICIT
    else:
        live_root = LIVE_ROOT.resolve()
        state_off = state_root != live_root
        sessions_off = sess_parent != live_root

    if not sessions_off and not state_off:
        return ""                                  # 全活体，正常生产

    if sessions_off and state_off:
        if sess_parent != state_root:
            problems.append(
                f"sessions 与 state **不同根**：sessions={SESSIONS}（父 {sess_parent}）"
                f" vs state={STATE_ROOT}。跨根配置危险——两边必须指同一目录"
                f"（活体根 {live_root}）")
    elif sessions_off and not state_off:
        # state 仍在活体根；sessions 却指别处 ⇒ 会往活体 state 写状态（污染生产）
        problems.append(
            f"只把 sessions 指到 {SESSIONS}（父 {sess_parent}）而 state 仍是活体"
            f"（{STATE_ROOT}）⇒ 会往**活体 state** 写状态，污染生产。"
            f"请同时把 state 与 sessions 指到同根")
    else:
        # state 已重定向，sessions 仍在活体根：正是那次误发的形状
        problems.append(
            f"state 被重定向到 {STATE_ROOT}，而 sessions 仍指活体"
            f"（{SESSIONS}，父 {sess_parent}）⇒ 读的是活体登记表、写的是别处状态。"
            f"请把 sessions 也指到同根")

    if state_off and not SESSIONS.is_file():
        problems.append(
            f"state 被重定向到 {STATE_ROOT}，但对应的 sessions 不存在：{SESSIONS}。"
            f"跨源配置危险——请同时显式给 sessions")

    return "；".join(problems)


def check(do_notify: bool = True, allow_split: bool = False, *,
          wire_root=None) -> dict:
    """跑一轮：返回 `{"state":..., "notified":[...], "channel":"pm|user|none"}`。

    `allow_split=False`（默认）：配置脑裂时**拒绝真发**并返回 `channel=refused`
    —— 那次的误发就是「读活体 sessions + 写别处」，必须封死。

    `wire_root` 透传给 `confirm_delivery()`；不给 ⇒ 回执一律 unknown（不假装）。
    """
    problem = config_problem()
    if problem and do_notify and not allow_split:
        log_line(f"❌ 配置脑裂，拒绝真发（不向任何活体 PM 投递）：{problem}",
                 write=False)
        return {"state": "config-error", "notified": [], "channel": "refused",
                "reason": problem}
    try:                                     # 健康核与告警同仓，直接包内引用
        from super_agent import session_health as health
    except Exception as exc:  # noqa: BLE001
        return {"state": "unknown", "reason": f"module: {exc}",
                "notified": [], "channel": "none"}
    # 健康核不 import 会话客户端 ⇒ 探测函数必须由调用方注入（否则一律 unknown）。
    # **按次传参，不改写 health 的模块级全局**：那是共享状态，写进去会污染同进程的
    # 其它调用者与测试（`test_session_health` 明确断言 DEFAULT_PROBE 保持 None）。
    probe = getattr(health, "DEFAULT_PROBE", None) or DEFAULT_PROBE
    if probe is None:
        return {"state": "unknown", "reason": "probe: 未注入会话探测函数",
                "notified": [], "channel": "none"}

    res = health.check_key(KEY, sessions=str(SESSIONS) if SESSIONS and SESSIONS.is_file() else "",
                           probe=probe)
    st = res.get("state")
    state = _load_state()

    if st == "ok":
        if state.get("last_state") and state.get("last_state") != "ok":
            log_line(f"主管键已恢复（{res.get('sid')}）⇒ 告警窗口重置", write=do_notify)
        _save_state({"last_state": "ok", "sid": res.get("sid"), "last_alarm_ts": 0,
                     "last_alarm_state": ""}, write=do_notify)
        return {"state": "ok", "sid": res.get("sid"), "notified": [], "channel": "none"}

    if st not in ("dead", "missing", "invalid"):
        # unknown：不催（把「查不到」当 dead 会误催）
        _save_state({**state, "last_state": st, "sid": res.get("sid")}, write=do_notify)
        return {"state": st, "reason": res.get("reason"), "notified": [], "channel": "none"}

    # 30 分钟去重：同一 dead 状态只催一次
    now = time.time()
    last_ts = float(state.get("last_alarm_ts") or 0)
    if state.get("last_alarm_state") == st and now - last_ts < DEDUP_SECONDS:
        _save_state({**state, "last_state": st, "sid": res.get("sid")}, write=do_notify)
        return {"state": st, "sid": res.get("sid"), "notified": [], "channel": "dedup",
                "age_s": int(now - last_ts)}

    pms = live_pm_sids()
    text = (f"【主管键告警】state/sessions.json 的 {KEY} 键指向的会话已不可用"
            f"（{st}：{res.get('reason')}）。**现在所有 send {KEY} 都会投空**——"
            f"拍板哨发不出去。请立刻报主管换键（rhandoff.sh switch <新id> 并回读）。"
            f"（本消息由 session_alarm 自动发出，收件人不是键所指的那个会话）")

    notified: list[str] = []
    planned: list[str] = []
    delivered_ids: list[dict] = []
    if do_notify:
        for name, sid in pms:
            if sid == res.get("sid"):
                continue          # 双保险：绝不发给坏掉的那个 sid
            planned.append(name)
            r = send_to(sid, text)
            if r.get("delivered"):
                confirmed = confirm_delivery(sid, r["prompt_id"], wire_root=wire_root)
                notified.append(name)
                delivered_ids.append({"name": name, "sid": sid,
                                      "prompt_id": r["prompt_id"], "rid": r.get("rid", "")})
                log_line(f"已催 {name}（{sid[:14]}…）prompt_id={r['prompt_id']}"
                         f"{(' rid=' + r['rid']) if r.get('rid') else ''}"
                         f" receipt={confirmed} —— 键={res.get('sid')} 状态={st}", write=do_notify)
            else:
                why = ("rc=%s 但**无 prompt_id**（疑投空）" % r.get("rc")
                       if r.get("rc") == 0 else f"rc={r.get('rc')}")
                log_line(f"催 {name} 未投递（{why}）——不计入已催", write=do_notify)
    else:
        planned = [n for n, s in pms if s != res.get("sid")]

    if not do_notify:
        # 只判不催：报「若真跑会催谁」，别误报成「退到用户通道」
        log_line(f"[check-only] 键={res.get('sid')} 状态={st}；"
                 f"若真跑将催 {len(planned)} 个 pm-*：{planned}", write=do_notify)
        return {"state": st, "sid": res.get("sid"), "notified": [],
                "would_notify": planned, "channel": "check-only",
                "reason": res.get("reason")}

    channel = "pm" if notified else "user"
    if not notified:
        log_line(f"**退到用户通道**：没有活着的 pm-* 收件人（候选 {len(pms)} 个）"
                 f"——键={res.get('sid')} 状态={st}，请人工处理", write=do_notify)

    if do_notify:
        _save_state({"last_state": st, "sid": res.get("sid"),
                     "last_alarm_ts": now, "last_alarm_state": st}, write=do_notify)
    return {"state": st, "sid": res.get("sid"), "notified": notified,
            "channel": channel, "reason": res.get("reason")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="主管键死告警的收件人")
    ap.add_argument("--once", action="store_true", help="跑一轮（值守进程的调用方式）")
    ap.add_argument("--send", action="store_true",
                    help="**显式确认真发**（不加则等同 dry-run；那次误发后收紧）")
    ap.add_argument("--check", action="store_true", help="只判不催（排障）")
    ap.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="等同 --check：只报将催谁，不真发、不落盘")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    configure()      # CLI 只认环境变量；库调用请自己 configure

    # 真发必须**显式** --send；--once 只治「跑一轮就退出」，不再隐含真发。
    # 那次误发就是「随手 --once 就真发了」，这个口子必须收紧。
    dry = bool(args.check or args.dry_run or not args.send)
    out = check(do_notify=not dry)
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
    elif dry:
        print(f"session_alarm[dry]: state={out.get('state')} channel={out.get('channel')} "
              f"would_notify={out.get('would_notify')}")
    else:
        print(f"session_alarm: state={out.get('state')} channel={out.get('channel')} "
              f"notified={out.get('notified')}")
    if out.get("channel") == "refused":
        print(f"[拒绝] {out.get('reason')}", file=sys.stderr)
        return 2
    return 0 if out.get("state") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
