"""The liveness network: is a bound worker/QA still alive? (moved out in #138)

Why a module of its own: this is one subject, 690 lines long - classify a session
``busy``/``idle``/``unknown``, keep that verdict per thread in a state file, and
raise **one** exception frame to the supervisor when a bound worker goes quiet
while its thread sees no new comment. ``worker.md`` §3.1.1 asks that a change to
:mod:`..decide_watch` leave it smaller and extract a real module; this domain has
the cleanest boundary there, because it never touches the delivery cursor, the
single-instance lock or the notice ledger.

The body below is **moved verbatim** - a cut and a paste, not a rewrite - so a
reviewer can diff it against the old region and see only the domain move. The
semantics are unchanged and load-bearing:

* ``busy`` is the only liveness signal: lifecycle carries created/archived only,
  so a crash or a kill leaves no event at all;
* ``unknown`` never counts towards "idle" - one network wobble must not report a
  working session as dead;
* one frame per death, reset on revival, re-reportable after a second death.

Host seams arrive through :func:`bind` (the pattern ``parts.state`` and
``parts.gates`` already use): the host owns the config resolution order, the log
and the event post, and a second resolution here would let an isolated run reach
production. Every host name the body touches is listed in :data:`SEAM_NAMES`.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

#: Host-owned names the moved body reads. They are **forwarders** resolved against
#: the host module on every call, so ``mock.patch.object(decide_watch, ...)`` keeps
#: reaching the moved code - an eager copy of a function object would freeze the
#: pre-patch value and silently change what nine existing tests prove.
SEAM_NAMES = (
    "_path", "_cfg", "_probe_session", "_event_post", "_workspace_root",
    "supervisor_sid", "log", "wss_bus", "_as_stamp", "_issue_of",
)

_HOST = None


def bind(host) -> None:
    """Install the host module whose names this one borrows. Idempotent."""
    global _HOST
    _HOST = host


def _host_attr(name: str):
    if _HOST is None:
        raise RuntimeError(f"decide_watch_parts.liveness: 未 bind 宿主，拒绝猜 {name}")
    return getattr(_HOST, name)


def _path(name: str, filename: str):
    return _host_attr("_path")(name, filename)


def _cfg():
    return _host_attr("_cfg")()


def _probe_session(sid: str):
    return _host_attr("_probe_session")(sid)


def _event_post(path: str, body: dict, *, port: int = 0):
    return _host_attr("_event_post")(path, body, port=port)


def _workspace_root():
    return _host_attr("_workspace_root")()


def supervisor_sid(explicit: str = ""):
    return _host_attr("supervisor_sid")(explicit)


def log(msg: str) -> None:
    _host_attr("log")(msg)


def wss_bus():
    return _host_attr("wss_bus")()


def _as_stamp(value, default: float):
    return _host_attr("_as_stamp")(value, default)


def _issue_of(thread: str):
    return _host_attr("_issue_of")(thread)


# --------------------------------------------------------------------- 活性网（ISS-69 / E61）
# 用户 2026-11-14 定案：**busy 推断只出「生死」，不出「完工」**；完工仍靠 verdict 契约。
#
# 生命周期流只有 created/archived，崩溃/被杀不留事件 ⇒ busy 是唯一活性信号。
# 判据：bound worker/QA 的 sid 连续 N 轮不忙，且该 thread 期间无新评论／verdict
# ⇒ 投**一次**例外帧给 supervisor。状态不变不重发；复活后重置，再死可再报。
WORKER_LIVENESS_EVENT = "event.agent.supervisor"
LIVENESS_KIND = "liveness_lost"
LIVENESS_ROLES = ("worker", "qa")
LIVENESS_DEFAULT_ROUNDS = 2
LIVENESS_DEFAULT_INTERVAL_S = 60.0
# busy 三态：busy=忙；idle=闲（计数 +1）；unknown=看不清（**计数不动**）。
# unknown **绝不能**算不忙——一次网络抖动会把活会话报成死的
# （与 `_probe_notice_target` 的 unknown 语义同源：查不到 ≠ 死了）。
LIVENESS_BUSY = "busy"
LIVENESS_IDLE = "idle"
LIVENESS_UNKNOWN = "unknown"


def _liveness_state_path() -> Path:
    return _path("liveness_state_path", "worker_liveness_state.json")


def _engine_config_path() -> Path | None:
    """找到引擎自己的配置文件（返工 P1-③ 的真断点）。

    断点在**调用方**：lwa 薄壳 `tools/decide_watch.py` 构造 `DecideWatchConfig`
    时只传了 state/sessions/ledger 那几个路径，没传配置路径，于是这里的
    `config.load(None)` 只拿到 `DEFAULTS`——照 `new_workbench/engine.json` 配了
    也永远不生效。薄壳不在本单允许改动范围，所以**从 safkc 侧自己找**：

      ① 显式注入的 `DecideWatchConfig.config_path`（调用方愿意传就用它）；
      ② `<workspace_root>/new_workbench/engine.json`（新轨工作台的标准落点）；
      ③ 空——退回 DEFAULTS。

    只认这两处，**不做目录爬取**：猜路径猜错会让引擎读到别人的配置，
    比读不到更危险。
    """
    explicit = _cfg().config_path
    if explicit:
        return Path(explicit).expanduser()
    try:
        candidate = _workspace_root() / "new_workbench" / "engine.json"
    except RuntimeError:
        return None
    return candidate if candidate.is_file() else None


def _engine_config() -> dict:
    """引擎配置面（ISS-69 返工 P1-③）。

    `config.load()` 是引擎自己读配置的唯一入口。**必须走这一处**——直接读
    `config.DEFAULTS` 会得到「照 sample 配了却不生效」的结果（上一版的缺陷）。
    路径来源见 `_engine_config_path()`。

    读失败只降级成 DEFAULTS 并留一行日志：配置坏不该让整条腿炸掉。
    """
    try:
        from .. import config as config_mod
    except Exception as exc:  # noqa: BLE001
        log(f"[liveness] 配置模块不可用（{type(exc).__name__}: {exc}）⇒ 用空配置")
        return {}
    path = _engine_config_path()
    loaded = None
    overrides: dict | None = None
    try:
        for _ in range(len(_LIVENESS_CONFIG_KEYS) + 1):
            try:
                loaded = config_mod.load(str(path) if path else None, overrides)
                break
            except config_mod.ConfigError as exc:
                field = _liveness_config_field(str(exc))
                if field is None:
                    raise
                # P1-D：一个写错的开关不许把同份配置里的收件人一起带走。
                # 只把这个字段钉回缺省值再试，其余字段（`supervisor_sid`／
                # `supervisor_keys`）照旧生效。
                log(f"[liveness] ⚠ 配置字段 engine.{field} 不合法（{exc}）"
                    f"⇒ 只该字段用缺省值，收件人等其余配置照旧")
                overrides = _pin_config_default(
                    overrides, field, config_mod.DEFAULTS["engine"][field])
        if loaded is None:
            raise config_mod.ConfigError("活性三键反复放宽后仍不合法")
    except Exception as exc:  # noqa: BLE001  ConfigError 等：降级，不炸腿
        log(f"[liveness] 配置读取失败（{path}：{type(exc).__name__}: {exc}）"
            f"⇒ 回退 DEFAULTS")
        loaded = config_mod.DEFAULTS
    return loaded if isinstance(loaded, dict) else {}


_LIVENESS_CONFIG_KEYS = ("worker_liveness_enabled", "worker_liveness_rounds",
                         "worker_liveness_interval_s")


def _liveness_config_field(message: str) -> str | None:
    """`ConfigError` 文本开头的字段名，且必须是活性三键之一。

    不是活性三键就返回 None：别的字段坏掉仍按原样整份回退 DEFAULTS。放宽范围
    只到本单这三个键，别处的校验（`creation_mode` 等）不许被这里绕过。
    """
    head = message.split(" ", 1)[0]
    if not head.startswith("engine."):
        return None
    name = head[len("engine."):]
    return name if name in _LIVENESS_CONFIG_KEYS else None


def _pin_config_default(overrides: dict | None, field: str, value) -> dict:
    out = dict(overrides or {})
    engine = dict(out.get("engine") or {})
    engine[field] = value
    out["engine"] = engine
    return out


def _liveness_settings() -> dict:
    """配置面（ISS-69 三键）。缺省即关：没人配就不跑，绝不静默生效。

    优先级：注入的 `DecideWatchConfig.liveness` → `config.load()` 的 `engine` 段
    → 内置缺省。注入优先是为测试与隔离靶场，生产走 `config.load()`。
    """
    raw = _cfg().liveness
    if not isinstance(raw, dict):
        raw = (_engine_config().get("engine") or {})
    enabled = raw.get("worker_liveness_enabled", False)
    if not isinstance(enabled, bool):
        # P1-D：`bool("false")` 是 True。一个字符串开关不许把整张活性网打开。
        log(f"[liveness] ⚠ worker_liveness_enabled 不是布尔（原值={enabled!r}）"
            f"⇒ 按关处理")
        enabled = False
    rounds = raw.get("worker_liveness_rounds", LIVENESS_DEFAULT_ROUNDS)
    if not isinstance(rounds, int) or isinstance(rounds, bool) or rounds < 1:
        rounds = LIVENESS_DEFAULT_ROUNDS
    interval = raw.get("worker_liveness_interval_s", LIVENESS_DEFAULT_INTERVAL_S)
    if not isinstance(interval, (int, float)) or isinstance(interval, bool) or interval <= 0:
        interval = LIVENESS_DEFAULT_INTERVAL_S
    return {"enabled": enabled, "rounds": rounds, "interval_s": float(interval)}


def _liveness_state_read() -> dict:
    """读状态文件。**坏文件不许打断腿**（P1-④）。

    与「腿坏」区分开：腿（探针／投帧）异常必须冲出 `_loop` 让心跳消失，但**数据坏**
    是另一码事——一份写坏的 JSON 不该让整个 decide_watch 死掉，那样连拍板哨和收单哨
    都跟着停。处置：改名留证（`<file>.corrupt.<ts>`）＋ WARN ＋ 用空态继续。

    整份坏（JSON 不合法／顶层不是对象）改名留证。**单行坏**（P1-B）只丢那一行：
    `idle_rounds` 转 int、`first_idle_at` 转 float，转不动就丢弃该行并 WARN 一行
    （含原值）。不抛异常——上一版把 `int("abc")` 留在转型点上，一次坏字段就让
    整个进程退出。
    """
    p = _liveness_state_path()
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        _quarantine_liveness_state(p, f"JSON 解析失败：{exc}")
        return {}
    if not isinstance(data, dict):
        _quarantine_liveness_state(p, f"顶层不是对象（{type(data).__name__}）")
        return {}
    rows = data.get("workers")
    if isinstance(rows, dict):
        data["workers"] = _liveness_rows_clean(rows)
    return data


def _liveness_rows_clean(rows: dict) -> dict:
    """逐行清洗 `workers`：坏行丢弃并留 WARN，好行原样保留。"""
    clean: dict = {}
    for sid, row in rows.items():
        if not isinstance(row, dict):
            _liveness_row_drop(sid, "不是对象", row)
            continue
        fixed = _liveness_row_clean(sid, row)
        if fixed is not None:
            clean[sid] = fixed
    return clean


def _liveness_row_clean(sid, row: dict) -> dict | None:
    """单行清洗。返回清洗后的行；坏到不能计数则返回 None（丢弃）。"""
    out = dict(row)
    raw_rounds = out.get("idle_rounds", 0)
    try:
        if isinstance(raw_rounds, bool):
            raise ValueError("布尔不是轮数")
        out["idle_rounds"] = int(raw_rounds if raw_rounds not in (None, "") else 0)
    except (TypeError, ValueError):
        _liveness_row_drop(sid, "idle_rounds", raw_rounds)
        return None
    if out["idle_rounds"] < 0:
        _liveness_row_drop(sid, "idle_rounds（负数）", raw_rounds)
        return None
    raw_first = out.get("first_idle_at")
    if raw_first is None:
        out.pop("first_idle_at", None)
        return out
    try:
        if isinstance(raw_first, bool):
            raise ValueError("布尔不是时间戳")
        out["first_idle_at"] = float(raw_first)
    except (TypeError, ValueError):
        _liveness_row_drop(sid, "first_idle_at", raw_first)
        return None
    return out


def _liveness_row_drop(sid, field_name: str, value) -> None:
    log(f"[liveness] ⚠ 状态行 {str(sid)[:14]} 的 {field_name} 不可用"
        f"（原值={value!r}）⇒ 丢弃该行，本轮不计入 idle 计数")


def _quarantine_liveness_state(p: Path, why: str) -> None:
    """坏状态文件改名留证，再留一行 WARN。改名失败也不抛——下一轮照旧用空态。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = p.with_name(f"{p.name}.corrupt.{stamp}")
    try:
        os.replace(p, target)
        kept = f"已改名留证 {target.name}"
    except OSError as exc:
        kept = f"改名失败（{exc}）"
    log(f"[liveness] ⚠ 状态文件损坏（{why}）⇒ {kept}；本轮用空态继续，不退出")


def _liveness_state_write(st: dict) -> None:
    try:
        p = _liveness_state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
    except OSError as exc:
        log(f"[liveness] 状态写盘失败（不影响本进程）：{exc}")


def probe_liveness(sid: str) -> tuple[str, str]:
    """三态探测，复用既有 `probe_session` 注入（不新起 daemon、不加注入键）。

    返回 `(state, why)`，state ∈ {busy, idle, unknown}。探针异常只降成 unknown：
    一次网络抖动不该把活会话判死。这层只包**单次探测**——腿的业务异常必须冲出
    `_loop`，否则「腿死了而进程心跳照旧 ok」。

    **等人工交互的会话归 unknown**（返工 P1-⑥）：`pending_interaction` 非 `none`
    表示它在等人批，不是死了。算成「不忙」会在 N 轮后把**活着的**会话报成死亡，
    而这条腿的全部价值就是「报得准」；误报一次，之后就没人信了。
    与 `unknown` 同侧：计数不动、不报、不清零。

    注意：`archived` **仍算 idle**。归档是 #69 任务书认可的「真杀」手段
    （`sess.py stop` 或 API archive），不能当正常收尾放过——否则「真杀一个
    bound worker ⇒ 恰好一帧」这条验收就不成立了。关单时 `gh_issue.close` 会归档
    本单 worker/QA，所以关单前必须先解绑（`_bound_sids` 里没有就不再看护）。

    **2.7（ISS-103 §1.4③）：判据是 `activity` 三态，不再是 `busy` 布尔。**
    `busy` 已从会话对象消失 ⇒ 旧写法落到末行恒 `unknown`，而 `unknown` 既不算死
    也不算活 ⇒ 「真杀一个 bound worker ⇒ 恰好一帧」不再成立（在岗巡查整体失效）。
    三态映射：`turn` → busy；`background` → busy（主轮之外的活也算在跑）；
    `idle` → 再按 `pending_interaction`／`archived` 细分，两者都为假才算真 idle。

    ⚠ 这处经 import 转发壳**同时影响 lwa 值守腿**（`tools/decide_watch.py` 转发
    本模块的 `main`）：lwa 文件 0 字节改动，但死亡判定会开始真的触发，历史上被
    `unknown` 漏掉的死 worker 会有一批「迟到」的 `liveness_lost` 帧。回滚单点：
    把本函数的 `activity` 分支改回读 `busy`（即回到恒 `unknown`），无需动 lwa。
    """
    try:
        exists, detail = _probe_session(sid)
    except Exception as exc:  # noqa: BLE001
        return LIVENESS_UNKNOWN, f"probe 异常 {type(exc).__name__}: {exc}"
    if exists is None:
        return LIVENESS_UNKNOWN, "探针无法判定（exists=None）"
    if exists is False:
        return LIVENESS_IDLE, "会话不存在（probe=False）"
    act = detail.get("activity") if isinstance(detail, dict) else None
    if act is not None:
        if act in ("turn", "background"):
            return LIVENESS_BUSY, "" if act == "turn" else "activity=background"
        pending = detail.get("pending_interaction")
        if pending is not None and str(pending).strip().lower() not in ("", "none",
                                                                       "null", "false"):
            return LIVENESS_UNKNOWN, f"等人工交互（pending_interaction={pending}）"
        if detail.get("archived"):
            # 归档仍是 idle（#69 验收把 archive 当「真杀」），但**要带 why**：
            # 首轮播种只豁免「活着且空闲」的正常收尾，不能把归档一起放过。
            return LIVENESS_IDLE, "会话已归档（archived=True）"
        return LIVENESS_IDLE, ""
    # 兼容层：2.7 之前的服务端仍有 `busy`。两边都没有 ⇒ 不判，宁 unknown 不误报。
    busy = detail.get("busy") if isinstance(detail, dict) else None
    if busy is True:
        return LIVENESS_BUSY, ""
    if busy is False:
        pending = detail.get("pending_interaction") if isinstance(detail, dict) else None
        if pending is not None and str(pending).strip().lower() not in ("", "none",
                                                                       "null", "false"):
            return LIVENESS_UNKNOWN, f"等人工交互（pending_interaction={pending}）"
        if isinstance(detail, dict) and detail.get("archived"):
            return LIVENESS_IDLE, "会话已归档（archived=True）"
        return LIVENESS_IDLE, ""
    return LIVENESS_UNKNOWN, "会话对象既无 activity 也无 busy 字段"


def _bound_sids() -> dict:
    """读 bindings：`{thread: {role: sid}}`，只取 worker/qa 两角色。

    `issue_sessions.json[thread][role]` 是 `{bound, name, session}`（写者
    `gh_issue.bind`）。形状不认识就跳过该条目——宁可少报，不可误报。
    """
    try:
        data = json.loads(_path("bind_path", "issue_sessions.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict = {}
    for thread, roles in data.items():
        if not isinstance(roles, dict):
            continue
        for role, entry in roles.items():
            if str(role).lower() not in LIVENESS_ROLES:
                continue
            sid = ""
            if isinstance(entry, dict):
                sid = str(entry.get("session") or entry.get("sid") or "")
            elif isinstance(entry, str):
                sid = entry
            if sid:
                out.setdefault(str(thread), {})[str(role).lower()] = sid
    return out


def _liveness_thread_index(bindings: dict) -> tuple[dict, dict]:
    """绑定键的两种写法都建索引，供台账行反查 thread。

    `owner/repo#N`（引擎自己的 thread 键）与 `项目名#N`（`gh_issue.bind` 写的是
    `f"{project}#{issue}"`，项目名只有仓名、没有 owner）。台账 `repo` 恒是
    `owner/repo`。只按全键比对时 `owner/x#69` 拼不出 `x#69` ⇒ 清零永不生效。
    """
    exact = {str(thread).lower(): thread for thread in bindings}
    by_repo_name: dict = {}
    for thread in bindings:
        name, _, number = str(thread).rpartition("#")
        if not number.isdigit():
            continue
        by_repo_name[(name.rsplit("/", 1)[-1].lower(), number)] = thread
    return exact, by_repo_name


def _liveness_row_thread(row: dict, exact: dict, by_repo_name: dict) -> str | None:
    """台账行 → 命中的 thread（认不出就 None，不猜）。"""
    issue = row.get("issue")
    repo = str(row.get("repo") or "")
    if issue is None or not repo:
        return None
    hit = exact.get(f"{repo}#{issue}".lower())
    if hit:
        return hit
    return by_repo_name.get((repo.rsplit("/", 1)[-1].lower(), str(issue)))


def _liveness_signal_key(row: dict, exact: dict, by_repo_name: dict) -> tuple | None:
    """台账行 → `(thread, role)` 信号键（认不出就 None）。

    ISS-69 返工 P0-②：清计数与解除武装必须到 **`(thread, role)`**，不能到 thread。
    台账行带 `role`（`pm`／`worker`／`qa`／主管…）。同 thread 里 PM 贴派单书、
    QA 贴结论，都不是「被看护的那个会话动了」——按 thread 判会把 worker 的计数
    一并清掉，于是真死的 worker 永远等不到 N 轮（漏报）。

    `role` 缺失或不是被看护的角色 ⇒ None：不猜、不当信号。宁可少数一轮，不可误清。
    """
    hit = _liveness_row_thread(row, exact, by_repo_name)
    if not hit:
        return None
    role = str(row.get("role") or "").strip().lower()
    if role not in LIVENESS_ROLES:
        return None
    return hit, role


def _liveness_last_comment_at(bindings: dict) -> dict:
    """每个 `(thread, role)` 台账里**最近一条评论**的时间戳（epoch 秒）。

    首轮不能只看「游标之后」的增量：那会把「已经交完活的会话」当成「从没动过」，
    于是它在 N 轮后被误报成死亡。存量评论不追溯**清零**，但必须用来判定「这个
    `(thread, role)` 交付过」⇒ 首轮直接进 settled 态（正常收尾，不计数不报）。

    键是 `(thread, role)` 而非 thread：PM 的评论不能替 worker 播种 settled。
    """
    exact, by_repo_name = _liveness_thread_index(bindings)
    latest: dict = {}
    try:
        with _path("ledger_path", "ledger.jsonl").open(
                "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"comment"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or row.get("ok") is not True:
                    continue
                if str(row.get("act") or "") != "comment":
                    continue
                key = _liveness_signal_key(row, exact, by_repo_name)
                if not key:
                    continue
                try:
                    ts = datetime.fromisoformat(str(row.get("t") or "")).timestamp()
                except ValueError:
                    continue
                if ts > float(latest.get(key) or 0.0):
                    latest[key] = ts
    except OSError:
        pass
    return latest


def _liveness_hot_signals(bindings: dict, offset: int) -> tuple[set, int]:
    """台账 `offset` 之后有「新评论」的 `(thread, role)` 集合，返回 `(hot, new_offset)`。

    用**字节偏移**而不是时间戳：台账 `t` 只精确到秒，同一秒内的两轮会互相看不
    见（新评论被当成「没来过」⇒ 误报死）。偏移是追加流水的天然游标，只前进不回头。
    首轮 offset 由调用方初始化成当前文件大小；存量评论的用途见
    `_liveness_last_comment_at`（播种 settled），这里只管增量。

    粒度是 `(thread, role)`，不是 thread（P0-②）：同 thread 的 PM 派单书不该让
    worker 的计数清零。`_liveness_signal_key` 认得谁在说话。
    """
    hot: set = set()
    exact, by_repo_name = _liveness_thread_index(bindings)
    path = _path("ledger_path", "ledger.jsonl")
    try:
        size = path.stat().st_size
    except OSError:
        return hot, offset
    if size <= offset:
        return hot, size if size < offset else offset
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(offset)
            chunk = fh.read(size - offset)
    except OSError:
        return hot, offset
    for line in chunk.splitlines():
        line = line.strip()
        if not line or '"comment"' not in line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("ok") is not True:
            continue
        if str(row.get("act") or "") != "comment":
            continue
        key = _liveness_signal_key(row, exact, by_repo_name)
        if key:
            hot.add(key)
    return hot, size


def _report_recipient_missing(st: dict, *, port: int = 0, dry: bool = False) -> None:
    """收件人解析不到时的**唯一**出声点（P0-① 第 3 条）。

    上一版在 `for` 循环里逐 sid 打一行「无 supervisor sid ⇒ …」，一轮 12 行一字不差，
    既没信息量也没人看。改成：

      ① 一条汇总 WARN（本轮 0 帧、去哪儿配）；
      ② 往 daemon 落一个 `[user]` 可见事件，且**同一原因只发一次**——
         去重键落状态文件（跨进程有效），不像日志每轮重刷；
      ③ 只在**首次**或**原因变化**时出声，之后安静保持，收件人恢复后再清标记。

    绝不投给 `""`：那等于把告警扔进黑洞。
    """
    now = time.time()
    prev = st.get("recipient_missing") if isinstance(st.get("recipient_missing"), dict) else {}
    reason = ("supervisor_sid() 解析为空：engine.supervisor_sid／配置面 supervisor_key／"
              "sessions.json['supervisor']／兜底键表 都未命中")
    first = not prev
    changed = bool(prev) and str(prev.get("reason") or "") != reason
    if not (first or changed):
        return

    stamp = datetime.now().isoformat(timespec="seconds")
    if dry:
        # 干跑：只打印，不发事件、不写状态（与 P1-⑤ 同一条理由）
        log(f"[dry-run] 活性网无收件人（本轮 0 帧）：{reason}。"
            f"（干跑不发事件、不写状态）")
        return
    st["recipient_missing_at"] = stamp
    st["recipient_missing"] = {"reason": reason, "at": stamp,
                               "notified_at": stamp if first or changed else ""}
    log(f"[liveness] ⚠ 活性网无收件人（本轮 0 帧）：{reason}。"
        f"请配 engine.supervisor_sid，或把真实键名写进 "
        f"engine.supervisor_keys／配置面 supervisor_key")
    try:
        _event_post("/events/inject", {
            "type": "file",
            "event": "event.session.supervisor_key_dead",
            "payload": {
                "key": "supervisor",
                "state": "missing",
                "reason": f"[user] {reason}",
                "leg": "worker_liveness",
                "frames_this_round": 0,
                "checked_at": stamp,
                "effect": "活性网本轮 0 帧：收件人解析不到，例外帧无处可投",
            },
        }, port=_liveness_port(port))
    except Exception as exc:  # noqa: BLE001  事件腿不可用：日志那条腿已经写过
        log(f"[liveness] 无收件人事件注入失败（{type(exc).__name__}: {exc}）"
            f"——日志那条腿已写，但 /healthz 看不到本次告警")


def _liveness_text(thread: str, role: str, sid: str, idle_rounds: int,
                   waited_s: float, now_iso: str) -> str:
    return (f"【活性异常】{thread} 的 {role} 会话连续 {idle_rounds} 轮不忙，"
            f"期间无新评论/verdict，已等待 {int(waited_s // 60)} 分钟"
            f"（探测时刻 {now_iso}）。\n"
            f"sid={sid}\n"
            f"只报生死、不报完工：完工仍以 verdict 契约判。此帧同一死亡态只发一次。")


def _liveness_port(port: int) -> int:
    """注入端口优先；没有事件源时不猜生产端口，用 0 让调用方自己兜。"""
    if port:
        return int(port)
    try:
        return int(wss_bus().PORT)
    except Exception:  # noqa: BLE001  未注入 event_source ⇒ 不静默连生产
        return 0


def worker_liveness_tick(*, dry: bool = False, port: int = 0,
                         now: float | None = None,
                         supervisor: str = "") -> int:
    """一轮活性巡检，返回本轮**投出**的例外帧条数。

    去重键＝`sid`：同一 sid 的死亡态只发一次；复活（重新 busy 或有新评论）后
    删键，再死可再报一次。状态落盘 ⇒ 跨进程/`--once` 每轮新进程也不会重发。
    """
    settings = _liveness_settings()
    if not settings["enabled"]:
        return 0
    now = time.time() if now is None else now
    bindings = _bound_sids()
    if not bindings:
        return 0
    st = _liveness_state_read()
    rows = st.get("workers") if isinstance(st.get("workers"), dict) else {}
    # 首轮把游标推到台账末尾：存量评论不追溯为「刚来过」（否则一启动就全体清零）。
    # 但存量评论仍要用来播种 settled：交过活的会话空闲是正常收尾，不是死亡。
    last_off = st.get("ledger_offset")
    first_tick = not (isinstance(last_off, int) and not isinstance(last_off, bool)
                      and last_off >= 0)
    if first_tick:
        try:
            last_off = _path("ledger_path", "ledger.jsonl").stat().st_size
        except OSError:
            last_off = 0
    seeded = _liveness_last_comment_at(bindings) if first_tick else {}
    hot, new_off = _liveness_hot_signals(bindings, last_off)
    alive: set = set()
    sent = 0
    sup = supervisor_sid(supervisor)
    if not sup:
        # P0-① 第 3 条：解析不到收件人必须**大声**，但**只喊一次**。
        # 上一版在循环里逐 sid 打，一轮 12 行同样的字，等于没人会看见。
        # 这是配置/数据问题（不是腿坏）⇒ 不炸 `_loop`，但要留痕：
        #   ① 一条汇总 WARN，说清「本轮 0 帧」；
        #   ② 往 daemon 落一个 `[user]` 可见事件，且**同一原因只发一次**。
        # `dry` 传给 `_report_recipient_missing`：干跑**不许**发事件、也不许写状态
        # （P1-⑤ 的同一条理由——干跑一次就把「已告警」落盘，等于把真实告警静音）。
        _report_recipient_missing(st, port=port, dry=dry)
    else:
        # 收件人恢复：清掉标记，之后再丢能重新出声一次
        st.pop("recipient_missing_at", None)
        st.pop("recipient_missing", None)
    now_iso = datetime.now().isoformat(timespec="seconds")
    for thread, roles in bindings.items():
        for role, sid in roles.items():
            alive.add(sid)
            state, why = probe_liveness(sid)
            row = rows.get(sid) if isinstance(rows.get(sid), dict) else {}
            if (thread, role) in hot:
                # **该 sid 自己**（同 thread 同角色）期间来了新评论/verdict ⇒ 计数清零，
                # 且**解除武装**直到它重新忙。交付评论之后会话保持空闲是正常收尾，
                # 不是死亡：不解除武装就会在交付后第 N 轮误报。
                #
                # 粒度到 `(thread, role)`（P0-②）：PM 在同一 thread 贴派单书不算
                # worker 的信号。按 thread 判会把 worker 的计数一并清掉，
                # 真死的 worker 于是永远攒不满 N 轮（漏报）。
                if row:
                    log(f"[liveness] {thread} {role} 有新评论 ⇒ {sid[:14]} 计数清零并解除武装")
                rows[sid] = {"sid": sid, "thread": thread, "role": role,
                             "settled": True, "settled_at": now}
                continue
            if not row and (thread, role) in seeded and state == LIVENESS_IDLE and not why:
                # 首轮播种：该 `(thread, role)` 台账里交过评论，且会话**仍在服务端、
                # 只是空闲**（`why` 非空表示「会话不存在」等明确死讯）⇒ 交付后的
                # 正常收尾，直接进 settled。
                #
                # 不能把 `exists=False`／`archived` 一并播种：那两种是**明确的死亡
                # 信号**，历史上有过评论不代表现在还活着。否则「三天前交过活、
                # 之后被杀」的会话会永远不报——漏报比误报更致命。
                rows[sid] = {"sid": sid, "thread": thread, "role": role,
                             "settled": True, "settled_at": seeded[(thread, role)]}
                continue
            if state == LIVENESS_BUSY:
                if row:
                    log(f"[liveness] {sid[:14]} 复活（busy）⇒ 去重重置")
                rows.pop(sid, None)
                continue
            if row.get("settled"):
                continue                     # 交付后空闲：正常收尾，不计数不报
            if state == LIVENESS_UNKNOWN:
                # 看不清 ≠ 死了：计数不动、不报
                if row:
                    row["last_unknown"] = why
                continue
            row = row or {"sid": sid, "thread": thread, "role": role,
                          "idle_rounds": 0, "first_idle_at": now, "reported": False}
            row["thread"] = thread
            row["role"] = role
            # P1-B 的第二道闸：清洗已把坏值挡住，这里仍按缺省兜底。`_loop` 刻意
            # 不给这条腿包 try（腿死要让心跳消失），所以转型异常一次都不许漏出去。
            row["idle_rounds"] = _as_count(row.get("idle_rounds")) + 1
            row["last_probe_at"] = now
            rows[sid] = row
            if row.get("reported"):
                continue                     # 同一死亡态只发一次
            if row["idle_rounds"] < settings["rounds"]:
                continue
            waited = max(0.0, now - _as_stamp(row.get("first_idle_at"), now))
            text = _liveness_text(thread, role, sid, row["idle_rounds"], waited, now_iso)
            if dry:
                # P1-⑤：dry 只许打印，**一个持久态字段都不许改**。
                # 上一版在这里写 `row["reported"] = True` 并落盘 ⇒ dry 跑一次就把
                # 该 sid 永久静音，之后真实模式再也不报（漏报，且现场无痕）。
                # `idle_rounds`／`first_idle_at` 同样不属于 dry 的权限：dry 不该
                # 改变后续真实判定的起点。
                log(f"[dry-run] 将投活性例外帧 {thread} {role} {sid[:14]}：{text[:60]}")
                sent += 1
                continue
            if not sup:
                # 收件人缺失已在循环**之前**汇总喊过一次（`_report_recipient_missing`）。
                # 这里只保证「不投、不标已报」——下一轮能解析了照样能报。
                continue
            try:
                _event_post("/events/inject", {
                    "type": "file",
                    "event": WORKER_LIVENESS_EVENT,
                    "payload": {"sid": sup, "thread": thread,
                                "issue": _issue_of(thread), "assignment": "",
                                "kind": LIVENESS_KIND, "role": role, "dead_sid": sid,
                                "idle_rounds": row["idle_rounds"],
                                "waited_s": int(waited), "probed_at": now_iso,
                                "text": text},
                }, port=_liveness_port(port))
            except Exception as exc:  # noqa: BLE001  注入失败：留日志，下轮再试
                log(f"[liveness] 例外帧注入失败（{type(exc).__name__}: {exc}）⇒ 下轮重试")
                continue
            row["reported"] = True
            row["reported_at"] = now_iso
            sent += 1
            log(f"[liveness] ⚠ 已投例外帧：{thread} {role} {sid[:14]}"
                f" 连续 {row['idle_rounds']} 轮不忙")
    for sid in list(rows):
        if sid not in alive:
            rows.pop(sid, None)              # binding 已解绑 ⇒ 不再看护
    if dry:
        # P1-⑤：dry-run **不落盘**。上面的循环虽然在内存里动过 rows，但不写回，
        # 下一轮真实模式读到的仍是干跑前的状态——dry 与真实态彻底隔离。
        log(f"[dry-run] 活性巡检：本轮将投 {sent} 帧（未写任何状态文件）")
        return sent
    st["workers"] = rows
    st["last_tick_ts"] = now
    st["ledger_offset"] = new_off
    _liveness_state_write(st)
    return sent


def _as_count(value) -> int:
    """轮数兜底：转不动就当 0（不抛）。"""
    if isinstance(value, bool):
        return 0
    try:
        got = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return got if got >= 0 else 0
