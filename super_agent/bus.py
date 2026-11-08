"""消费者统一入口：**bus 优先，直连降级**（issue #8 步 2）。

所有读侧消费者（ctx / sess / monitor / 看门狗 / auto_approve / revive / dispatch）
都经由这里取数，不再各自判断用哪条路。

三种模式（`KIMI_BUS_MODE`，供 bus/direct 一致性对照用）：
    auto   （默认）先问 daemon，不可用才直连
    bus    强制走 daemon；不可用**报错**（对照实验用，不用于生产）
    direct 强制直连，完全不碰 daemon（对照实验用）

降级判据（`bus_health()`）：
    · HTTP 2 秒内不通 ⇒ 直连
    · 通了但 `ready=false`（未连上上游 / 0 个成功订阅）⇒ 直连
      —— 这条是为了避免「daemon 活着但数据面是空的」被当成实时
    · 端点 404 / 该会话没有上下文数据 ⇒ 直连（同一语义）

留痕：每次降级调 `note_degraded(caller, reason)`，**同一 caller+reason 60 秒内只记一次**
（看门狗每轮都跑，不节流会刷屏）。

来源标注：返回值的 `_source` 一定以 `bus(` 或 `direct(` 开头，
消费者原样透出，便于对照表与排障。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.append(str(BASE / "tools"))

from . import direct as K  # noqa: E402

ENV_MODE = "KIMI_BUS_MODE"
_DEGRADE_LOG: dict[str, float] = {}      # "caller|reason" -> last logged ts
_DEGRADE_WINDOW = 60.0


# --------------------------------------------------------------------- 模式
def mode() -> str:
    m = (os.environ.get(ENV_MODE) or "auto").strip().lower()
    return m if m in ("auto", "bus", "direct") else "auto"


# --------------------------------------------------------------------- daemon
def _bus():
    """延迟导入 wss_bus（它依赖 aiohttp；纯 direct 模式不该被迫加载）。"""
    import wss_bus
    return wss_bus


def health(timeout: float = 2.0) -> dict | None:
    """daemon 健康。`direct` 模式直接返回 None（不碰它）。

    `auto`/`bus` 模式要求 `ready=true` 才算可用：`ok` 只说明 HTTP 面活着，
    订阅为 0 时数据面是空的，拿它当实时就是「旧缓存伪装实时」。
    """
    if mode() == "direct":
        return None
    try:
        h = _bus().bus_health(timeout=timeout)
    except Exception:  # noqa: BLE001
        return None
    if not h:
        return None
    if not h.get("ready", h.get("ok")):
        return None
    return h


def note_degraded(caller: str, reason: str) -> None:
    """降级留痕（同 caller+reason 60 秒一条）。"""
    key = f"{caller}|{reason}"
    now = time.time()
    if now - _DEGRADE_LOG.get(key, 0.0) < _DEGRADE_WINDOW:
        return
    _DEGRADE_LOG[key] = now
    try:
        _bus().note_degraded(f"{caller}: {reason}")
    except Exception:  # noqa: BLE001
        pass


def get(path: str, caller: str, timeout: float = 3.0, quiet_404: bool = True):
    """读 daemon 端点。不可用/404 返回 None（调用方转直连）。

    `quiet_404=True`（默认）时 **404 不记降级**：daemon 只订阅未归档会话，
    查已归档会话必然 404，而「回落到直连」本来就是正确路径，记 warning 只会
    刷屏（2026-11-06 批 4 实测：session_watch 一轮刷了两条无意义告警）。
    """
    if not health():
        note_degraded(caller, "daemon 不可用或未 ready")
        return None
    try:
        return _bus().bus_get(path, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "code", None) or getattr(exc, "status", None)
        if not (quiet_404 and code == 404):
            note_degraded(caller, f"{path} {type(exc).__name__}")
        return None


# --------------------------------------------------------------------- 读动作
def status(sid: str, caller: str = "status") -> dict | None:
    """会话状态（bus 优先）。bus 拿不到上下文时返回 None，交调用方直连。"""
    if mode() != "direct":
        d = get(f"/state/{sid}", caller)
        if d and d.get("ok"):
            st = dict(d.get("status") or {})
            if st.get("context_tokens") is not None:
                h = health() or {}
                meta = d.get("_meta") or {}
                age = meta.get("age_s")
                st["_source"] = (f"bus(port={h.get('port') or '?'} "
                                 f"age={age if age is not None else '?'}s "
                                 f"boot={h.get('boot_id') or '?'}"
                                 + (",gap" if meta.get("gap") else "") + ")")
                return st
            note_degraded(caller, f"{sid} 无 context_tokens")
        elif mode() == "bus":
            return None                 # bus 模式下不偷偷直连
    if mode() == "bus":
        return None
    d2 = K.status(sid) or {}
    if not d2 or d2.get("_err"):
        return None
    d2["_source"] = f"direct({d2.get('_source') or '?'})"
    return d2


def session_info(sid: str, caller: str = "session_info") -> dict | None:
    """会话对象（busy/main_turn_active/pending_interaction/archived）。"""
    if mode() != "direct":
        d = get(f"/state/{sid}", caller)
        if d and d.get("ok"):
            st = dict(d.get("status") or {})
            meta = d.get("_meta") or {}
            st["_source"] = f"bus(age={meta.get('age_s')}s)"
            st["archived"] = bool(d.get("archived"))
            return st
        if mode() == "bus":
            return None
    if mode() == "bus":
        return None
    info = K.session_info(sid)
    if not info or "_err" in info:
        return None
    info = dict(info)
    info["_source"] = "direct(sessions/{id})"
    return info


def tasks_src(sid: str, caller: str = "tasks") -> tuple[list | None, str]:
    """返回 (任务列表, 来源标记)。来源一定是 "bus" 或 "direct"。

    为什么要单独返回来源（2026-11-06 步 2 返工 P0-1）：
    **空列表是合法结果**，调用方无法从 `[]` 判断走的是哪条路。若只返回 list，
    direct 对照模式会因为 bus 先命中（哪怕结果为空）而永远走不到直连分支，
    对照实验在「无 pending / 无任务」这种最常见状态上直接失效。
    """
    if mode() != "direct":
        d = get(f"/tasks/{sid}", caller)
        if d and d.get("ok"):
            return d.get("tasks") or [], "bus"
        if mode() == "bus":
            return None, "bus"
    if mode() == "bus":
        return None, "bus"
    return K.tasks(sid), "direct"


def tasks(sid: str, caller: str = "tasks") -> list | None:
    """兼容旧签名：只要列表。需要区分来源请用 `tasks_src()`。"""
    return tasks_src(sid, caller)[0]


def pending_src(sid: str, caller: str = "pending") -> tuple[list | None, str]:
    """返回 (待批列表, 来源标记)。理由同 `tasks_src()`。"""
    if mode() != "direct":
        d = get(f"/pending/{sid}", caller)
        if d and d.get("ok"):
            return d.get("pending") or [], "bus"
        if mode() == "bus":
            return None, "bus"
    if mode() == "bus":
        return None, "bus"
    return K.pending_interactions(sid), "direct"


def pending(sid: str, caller: str = "pending") -> list | None:
    """兼容旧签名：只要列表。需要区分来源请用 `pending_src()`。"""
    return pending_src(sid, caller)[0]


def sessions(caller: str = "sessions", busy_only: bool = False) -> list | None:
    """会话列表。bus 侧的 `/state` 是「已订阅会话」，**不等于服务端全量**，
    所以这里**永远直连**取列表（daemon 不持有完整目录）。"""
    items, _err = K.list_all_sessions(page_size=50)
    return items


def list_envelopes(caller: str = "sess.list") -> list | None:
    """`sess.py list` 用：带 code/msg/data 信封的原始条目。

    daemon 不持全量目录 ⇒ 直连；但只要 daemon 可用，就把它已经知道的
    busy 覆盖上去（更新鲜）。返回 [{id,title,busy,...}]。
    """
    items, _err = K.list_all_sessions(page_size=50)
    if _err:
        return None
    live: dict = {}
    if mode() != "direct":
        d = get("/state", caller)
        if d and d.get("ok"):
            live = d.get("sessions") or {}
            note = "bus"
        else:
            note = "direct"
    else:
        note = "direct"
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        sid = it.get("id") or ""
        busy = it.get("busy")
        if sid in live:
            st = (live[sid] or {}).get("status") or {}
            if st.get("busy") is not None:
                busy = st["busy"]
        out.append({"id": sid, "title": it.get("title"), "busy": busy,
                    "archived": it.get("archived"), "_src": note})
    return out


def events(since: int = 0, sid: str = "", limit: int = 200,
           caller: str = "events") -> dict | None:
    """事件增量。bus 不可用返回 None（调用方自行处理）。"""
    if mode() != "direct":
        q = f"/events?since={since}&limit={limit}"
        if sid:
            q += f"&sid={sid}"
        return get(q, caller)
    return None


def source_of(d: dict | None) -> str:
    """取来源前缀（bus/direct/…），没有则 '?'。"""
    return str((d or {}).get("_source") or "?")


def is_bus(d: dict | None) -> bool:
    return source_of(d).startswith("bus(")


# --------------------------------------------------------------------- 控制面
def _new_rid(prefix: str = "") -> str:
    """生成调用方 request_id（非幂等控制必须带，daemon 靠它判「已受理不重发」）。"""
    import uuid
    return f"{prefix}{uuid.uuid4().hex[:16]}"


def _control(verb: str, sid: str, caller: str, payload: dict | None = None,
             rid: str = "", timeout: float = 25.0) -> tuple[dict | None, str]:
    """发控制请求。返回 (daemon 回执 | None, 来源标记)。

    **本函数不做重试**。三态由 daemon 的 journal 判，调用方只在
    `state=="not_accepted"` / 明确未受理时才允许补一次直连（见 `ctl()`）。
    """
    if mode() == "direct":
        return None, "direct"
    if not health():
        note_degraded(caller, "daemon 不可用，控制走直连")
        return None, "direct"
    body = {"sid": sid, "request_id": rid or _new_rid()}
    body.update(payload or {})
    try:
        return _bus().bus_post(f"/control/{verb}", body, timeout=timeout), "bus"
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "code", None)
        note_degraded(caller, f"/control/{verb} {type(exc).__name__}")
        return {"ok": False, "state": "indeterminate", "http_error": code,
                "err": f"{type(exc).__name__}: {exc}"}, "bus"


def ctl(verb: str, sid: str, caller: str, direct_fn, payload: dict | None = None,
        rid: str = "", timeout: float = 25.0) -> tuple[bool, dict, str]:
    """控制面统一入口（bus 优先，直连降级，**三态不重放**）。

    参数
      verb      : daemon 的动词名（send/abort/steer/interaction/cancel-task/…）
      direct_fn : 直连实现，签名为 `fn() -> (ok, detail)`；只在允许时才调用
    返回 (ok, detail, 来源)

    三态（QA 步 1 P0-3 判据，**这里是调用方侧的对偶**）：
      · daemon 回执 `state="indeterminate"` ⇒ **绝不**补直连（可能已经生效）
      · daemon 回执 `state="accepted"`      ⇒ 成功，不重发
      · daemon 回执 `state="not_accepted"`  ⇒ daemon 明确未受理，**允许**直连一次
      · daemon 不可达 / bus 模式失败        ⇒ 直连一次（但**先查 journal 语义**：
        不可达时 daemon 不可能受理过本次请求——请求根本没发出去）
      · `KIMI_BUS_MODE=direct`              ⇒ 直接直连
    """
    use_rid = rid or _new_rid(f"{verb[:4]}-")
    if mode() == "direct":
        ok, detail = direct_fn()
        return ok, dict(detail or {}), "direct"

    resp, via = _control(verb, sid, caller, payload=payload, rid=use_rid, timeout=timeout)
    if resp is None:
        ok, detail = direct_fn()
        return ok, dict(detail or {}), "direct"

    state = resp.get("state")
    if state == "accepted":
        return bool(resp.get("ok")), {**resp, "_rid": use_rid}, "bus(indeterminate-safe)"
    if state == "indeterminate":
        # **不重试、不补直连**——可能已经生效，重放就是双发
        return False, {**resp, "_rid": use_rid}, "bus(indeterminate)"
    if state == "not_accepted":
        # daemon 明确没受理 ⇒ 允许直连一次
        ok, detail = direct_fn()
        out = dict(detail or {})
        out["_rid"] = use_rid
        out["_fallback"] = "not_accepted→direct"
        return ok, out, "direct(after not_accepted)"
    if state == "rejected":
        # 业务/4xx 拒绝：原样返回，不降级不重放
        return False, {**resp, "_rid": use_rid}, "bus(rejected)"
    if resp.get("http_error"):
        # 连接层失败（daemon 挂了/超时）⇒ 请求未送达，可安全直连一次
        ok, detail = direct_fn()
        out = dict(detail or {})
        out["_rid"] = use_rid
        out["_fallback"] = f"bus_error→direct({resp.get('http_error')})"
        return ok, out, "direct(after bus error)"

    ok, detail = direct_fn()
    return ok, dict(detail or {}), "direct"
