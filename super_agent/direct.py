"""Kimi Code 直连协议：**唯一直连实现**（daemon 与各工具都走这里）。

为什么单独成一个文件（2026-11-06，issue #8 步 1 返工，QA P0-1）：
  上一版 `wss_bus.py` 里 13 处 `import kimi_api` / `kimi_api.*`，造成**递归依赖**：
  daemon 本该是「唯一框架」，却在内部调用自己的降级层。QA 的 grep 硬条款直接判红。
  现在：
    · `kimi_direct.py` —— 协议层，只依赖标准库，**不认识 daemon**；
    · `kimi_api.py`    —— 兼容薄壳，保持原函数签名与 CLI，内部转调本文件；
    · `wss_bus.py`     —— 只 import 本文件，**永不 import kimi_api**（有断言钉死）。

职责边界（重要）：
  · 本文件**不读 daemon、不碰 state/wss_bus\***，也不做重试与降级决策；
  · 「已受理要不要重发」这类**业务判据**属于 daemon 的 journal，不属于这里；
  · 本文件只回答「怎么把这一条请求发出去、怎么解读回复」。

协议要点（2.5.2 实测，写死在代码里以免再踩）：
  · WS 订阅 `transcript` 必须 `{"main":"turn"}`，空对象或 off 时服务端只 ack 不推数据；
  · **必须回 `pong`**（服务端每 10s 发 `ping` 带 nonce，不回即 ~20s 后 close 1001）；
  · `task.stop` 用顶层 `request_id` 关联、回复 `type="response"`；
    `abort` 用 `id` 关联、回复 `type="ack"`。两套不能混；
  · 2.5.x 帧字段是下划线（`context_tokens`），2.4.x 是驼峰。

路径：一切 kimi 目录经 `tools/kimi_paths.py`（`KIMI_CODE_HOME`）。
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.append(str(BASE / "tools"))     # 追加不插入（tools/queue.py 会遮蔽标准库）
from . import paths as kimi_paths  # noqa: E402

SERVER = os.environ.get("KIMI_SERVER", "http://127.0.0.1:58627")
WS_PATH = "/api/v1/ws"
AGENT = "main"
TRANSCRIPT_GRADE = "turn"          # 必须非 off，否则服务端不推初始快照
NEW_API_SINCE = (2, 5, 0)

_version_cache: str | None = None


# --------------------------------------------------------------------- 传输
def token() -> str:
    """token 原文（去空白）。读不到返回空串。**值永不打印/落日志。**"""
    return kimi_paths.token()


def _request(method: str, path: str, body=None, timeout: float = 20.0):
    """返回 (http_status, payload)。异常折成 (0, {"_err": …})，不向调用方抛。"""
    import urllib.error
    import urllib.request

    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {token()}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{SERVER}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw
    except Exception as exc:  # noqa: BLE001 - 看门狗不许因单次请求死掉
        return 0, {"_err": f"{type(exc).__name__}: {exc}"}


def get(path: str, timeout: float = 20.0):
    return _request("GET", path, None, timeout)


def post(path: str, body=None, timeout: float = 30.0):
    return _request("POST", path, {} if body is None else body, timeout)


def data_of(payload):
    """剥掉 {code,msg,data} 信封；不是信封就原样返回。"""
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def code_of(payload):
    return payload.get("code") if isinstance(payload, dict) else None


# --------------------------------------------------------------------- 判代
def version(refresh: bool = False) -> str:
    global _version_cache
    if _version_cache and not refresh:
        return _version_cache
    st, payload = get("/api/v1/meta", timeout=5)
    d = data_of(payload)
    v = (d or {}).get("server_version") if isinstance(d, dict) else ""
    if st == 200 and isinstance(v, str) and v:
        _version_cache = v
        return v
    return ""


def _ver_tuple(v: str) -> tuple:
    out = []
    for part in (v or "").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out) or (0,)


def is_new() -> bool:
    v = version()
    return bool(v) and _ver_tuple(v) >= NEW_API_SINCE


def source() -> str:
    v = version()
    return f"ws(>=2.5, server={v or '?'})" if is_new() else f"rest(<2.5, server={v or '?'})"


# --------------------------------------------------------------------- WS
def _import_websockets():
    """导入 websockets，绕开 `tools/queue.py` 对标准库 `queue` 的遮蔽。"""
    import sysconfig

    saved = list(sys.path)
    try:
        stdlib = Path(sysconfig.get_paths()["stdlib"]).resolve()
        shadows = []
        for p in sys.path:
            if not p:
                continue
            path = Path(p)
            if not (path / "queue.py").is_file():
                continue
            if (path / "queue" / "__init__.py").is_file():
                continue
            try:
                path.resolve().relative_to(stdlib)
                continue
            except ValueError:
                pass
            shadows.append(p)
        sys.path[:] = [p for p in sys.path if p not in shadows]
        import websockets.sync.client as ws_client
        return ws_client
    finally:
        sys.path[:] = saved


def _ws_url() -> str:
    return SERVER.replace("http://", "ws://").replace("https://", "wss://") + WS_PATH


def ping_reply(frame: dict) -> dict | None:
    """服务端 ping → 应回的 pong 帧（非 ping 返回 None）。

    **这条不写下来必再踩**：不回 pong 时服务端约 20 秒后以 close 1001 踢连接，
    表现是「WS 循环正常结束、日志无异常」，极难定位（2026-11-06 实测 5 次重连）。
    """
    if (frame.get("type") or "") != "ping":
        return None
    nonce = (frame.get("payload") or {}).get("nonce") or ""
    return {"type": "pong", "payload": {"nonce": nonce}}


class WSProbe:
    """一次性 WS 探针：连上 → hello → subscribe_v2 → 收帧 → 关。

    **订阅必须带 `transcript={"main":"turn"}`**，否则服务端只 ack 不推初始状态。
    """

    def __init__(self, sid: str, timeout: float = 2.0, transcript: str = TRANSCRIPT_GRADE):
        self.sid = sid
        self.timeout = timeout
        self.transcript = transcript

    def collect(self, want=(), budget: int = 10):
        """收帧直到 want 全部到手 / 帧数预算用完 / 超时。返回 (frames, err)。"""
        frames: list = []
        try:
            connect = _import_websockets().connect
        except Exception as exc:  # noqa: BLE001
            return frames, f"websockets unavailable: {exc}"
        try:
            with connect(_ws_url(), open_timeout=max(3.0, self.timeout),
                         additional_headers={"Authorization": f"Bearer {token()}"}) as ws:
                ws.send(json.dumps({"type": "client_hello", "id": uuid.uuid4().hex,
                                    "payload": {"client_id": "kimi-direct-probe"}}))
                ws.send(json.dumps({
                    "type": "subscribe_v2", "id": uuid.uuid4().hex,
                    "payload": {"session_id": self.sid,
                                "transcript": {AGENT: self.transcript}},
                }))
                for _ in range(max(1, budget)):
                    try:
                        raw = ws.recv(timeout=self.timeout)
                    except Exception:  # noqa: BLE001
                        break
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    pong = ping_reply(msg)
                    if pong:
                        ws.send(json.dumps(pong))     # 不回 pong 会被踢
                        continue
                    frames.append(msg)
                    if want and all(_has(frames, w) for w in want):
                        break
        except Exception as exc:  # noqa: BLE001
            return frames, f"{type(exc).__name__}: {exc}"
        return frames, ""


def _has(frames: list, type_name: str) -> bool:
    return any(_is(m, type_name) for m in frames)


def _is(msg: dict, type_name: str) -> bool:
    return (msg.get("type") or "") == type_name


def payload_of(msg: dict) -> dict:
    p = msg.get("payload")
    return p if isinstance(p, dict) else {}


def flat(msg: dict) -> dict:
    """2.5.x 帧是扁平的，2.4.x 在 payload 下；统一摊平。"""
    out = {}
    out.update(payload_of(msg))
    for k, v in msg.items():
        if k != "payload":
            out[k] = v
    info = payload_of(msg).get("info")
    if isinstance(info, dict):
        out.update(info)
    return out


def _ws_status(sid: str, timeout: float = 2.0) -> dict:
    frames, err = WSProbe(sid, timeout=timeout).collect(want=("agent", "usage"), budget=12)
    out: dict = {"agent": {}, "usage": {}, "tasks": [], "err": err}
    for msg in frames:
        t = msg.get("type") or ""
        f = flat(msg)
        if t == "agent":
            out["agent"] = f
        elif t == "usage":
            out["usage"] = f
        elif t == "task.list":
            out["tasks"] = f.get("tasks") or []
    if not out["err"] and not out["agent"] and not out["usage"]:
        out["err"] = "no agent/usage frame (subscribe_v2 需要 transcript={main:turn})"
    return out


def _send_ws(frame: dict, timeout: float = 8.0) -> dict:
    """发一个 WS 请求帧并等对应回复，失败返回 {}。

    两套关联字段不能混：`abort` 用 `id` + `ack`；`task.stop` 用顶层
    `request_id` + `response`。帧里带了 `request_id` 键就走后者。
    """
    try:
        connect = _import_websockets().connect
    except Exception:  # noqa: BLE001
        return {}
    rid = uuid.uuid4().hex
    frame = dict(frame)
    if "request_id" in frame:
        frame["request_id"] = rid
    else:
        frame["id"] = rid
    try:
        with connect(_ws_url(), open_timeout=timeout,
                     additional_headers={"Authorization": f"Bearer {token()}"}) as ws:
            ws.send(json.dumps({"type": "client_hello", "id": uuid.uuid4().hex,
                                "payload": {"client_id": "kimi-direct-cmd"}}))
            ws.send(json.dumps(frame))
            for _ in range(12):
                try:
                    msg = json.loads(ws.recv(timeout=timeout))
                except Exception:  # noqa: BLE001
                    break
                pong = ping_reply(msg)
                if pong:
                    ws.send(json.dumps(pong))
                    continue
                if msg.get("request_id") == rid or msg.get("id") == rid:
                    return msg
    except Exception:  # noqa: BLE001
        return {}
    return {}


# --------------------------------------------------------------------- 读
def cached(sid: str, max_age: float = 90.0, cache_file: Path | None = None) -> dict:
    """读 `kimi_ws_cache.py` 落的缓存；过期/不存在返回 {}。

    注意：daemon 有**自己的** state-dir 缓存，不要拿这份冒充实时。
    """
    import time as _time

    path = cache_file or (BASE / "state" / "kimi_ws_cache.json")
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rec = (d.get("sessions") or {}).get(sid) or {}
    if not isinstance(rec, dict) or not rec:
        return {}
    if max_age and _time.time() - int(rec.get("ts") or 0) > max_age:
        return {}
    return rec


def session_info(sid: str) -> dict:
    """会话对象（两代都在）。busy/main_turn_active/pending_interaction/archived。"""
    st, payload = get(f"/api/v1/sessions/{sid}", timeout=10)
    d = data_of(payload)
    if st == 200 and isinstance(d, dict) and code_of(payload) in (0, None):
        return d
    return {"_err": f"http={st} {str(payload)[:120]}", "_source": "rest"}


# status 的**键集契约**：bus 与 direct 两条路必须返回完全相同的键集合。
# 2026-11-06 步 2 返工 P0-2：daemon 侧原样透出 rec["status"]，而该 dict 只在
# 对应帧推过之后才有键 ⇒ 安静会话缺 archived/main_turn_active 等。定义在这里，
# 两侧共用（wss_bus 的 _norm_status 也用这份）。
STATUS_KEYS = (
    "busy", "main_turn_active", "pending_interaction", "last_turn_reason",
    "archived", "model", "thinking_level", "permission", "plan_mode",
    "swarm_mode", "tower_mode", "context_tokens", "max_context_tokens",
    "context_usage", "agent_status",
)


def status_template() -> dict:
    """空模板：所有键都在，值为 None。两侧据此补齐键集。"""
    return {k: None for k in STATUS_KEYS}


def status(sid: str) -> dict:
    """会话运行状态。旧代走 `/status`；新代 = `/sessions/{id}` + WS agent/usage。"""
    info = session_info(sid)
    out: dict = status_template()          # 键集恒定，值可为 None
    if isinstance(info, dict) and "_err" not in info:
        out.update({
            "busy": info.get("busy"),
            "main_turn_active": info.get("main_turn_active"),
            "pending_interaction": info.get("pending_interaction"),
            "last_turn_reason": info.get("last_turn_reason"),
            "archived": info.get("archived"),
        })

    if not is_new():
        st, payload = get(f"/api/v1/sessions/{sid}/status", timeout=10)
        d = data_of(payload)
        if st == 200 and isinstance(d, dict) and code_of(payload) in (0, None):
            d.setdefault("_source", "rest:/status")
            return d

    wsres = _ws_status(sid)
    agent, usage = wsres.get("agent") or {}, wsres.get("usage") or {}
    if agent or usage:
        cap = usage.get("max_context_tokens")
        tok = usage.get("context_tokens")
        out.update({
            "model": agent.get("model"),
            "thinking_level": agent.get("thinking"),
            "permission": agent.get("permission_mode"),
            "plan_mode": agent.get("plan_mode"),
            "swarm_mode": agent.get("swarm_mode"),
            "tower_mode": agent.get("tower_mode"),
            "context_tokens": tok,
            "max_context_tokens": cap,
            "context_usage": (tok / cap) if (isinstance(tok, int) and isinstance(cap, int) and cap) else None,
        })
        if agent.get("status"):
            out["agent_status"] = agent["status"]
        out["_source"] = "ws:agent+usage"
        return out

    rec = cached(sid, max_age=180)
    st_rec = rec.get("status") if isinstance(rec.get("status"), dict) else {}
    if st_rec:
        out.update({k: v for k, v in st_rec.items() if v is not None})
        out["_source"] = f"ws_cache(age={int(__import__('time').time()) - int(rec.get('ts') or 0)}s)"
        return out

    out["_source"] = (f"degraded(sessions/{{id}}; ws:{wsres.get('err') or 'no frame'}; "
                      f"cache:miss)")
    return out


def active_prompt(sid: str) -> str:
    """正在跑的那一轮 prompt id；没有则空串。`GET /prompts` 两代都在。"""
    st2, payload2 = get(f"/api/v1/sessions/{sid}/prompts?page_size=10", timeout=10)
    d2 = data_of(payload2)
    if isinstance(d2, dict):
        active = d2.get("active") or {}
        pid = active.get("prompt_id") or active.get("id")
        if pid:
            return str(pid)
        queued = d2.get("queued") or []
        if queued:
            return str(queued[0].get("prompt_id") or queued[0].get("id") or "")
    if is_new():
        return ""
    st, payload = get(f"/api/v1/sessions/{sid}/snapshot", timeout=10)
    d = data_of(payload) or {}
    turn = (d.get("in_flight_turn") or {}) if isinstance(d, dict) else {}
    pid = turn.get("current_prompt_id") or turn.get("prompt_id") or ""
    if pid:
        return str(pid)
    queued = (d.get("queued") or []) if isinstance(d, dict) else []
    return str(queued[0].get("prompt_id") or "") if queued else ""


def has_pending(sid: str, info: dict | None = None) -> bool:
    """廉价预判：有没有待批交互（一个 REST 就够，别为它开 WS 探针）。"""
    d = info if isinstance(info, dict) else session_info(sid)
    return bool(d) and d.get("pending_interaction") not in (None, "none", "")


def pending_interactions(sid: str) -> list:
    """待批交互。旧代 `/snapshot`；新代 WS `interaction` 帧 + 缓存。"""
    if not is_new():
        st, payload = get(f"/api/v1/sessions/{sid}/snapshot", timeout=10)
        d = data_of(payload) or {}
        out = []
        if isinstance(d, dict):
            for it in d.get("pending_approvals") or []:
                out.append({"approval_id": it.get("approval_id"),
                            "tool_name": it.get("tool_name"),
                            "action": it.get("action"),
                            "kind": "approval", "raw": it})
            for it in d.get("pending_questions") or []:
                out.append({"approval_id": it.get("approval_id") or it.get("interaction_id"),
                            "tool_name": "",
                            "action": json.dumps(it, ensure_ascii=False)[:200],
                            "kind": "question", "raw": it})
        return out

    frames, _err = WSProbe(sid, timeout=1.5).collect(want=("interaction",), budget=10)
    out, seen = [], set()
    for msg in frames:
        if not _is(msg, "interaction"):
            continue
        p = flat(msg)
        if (p.get("status") or "pending") != "pending":
            continue
        aid = p.get("interaction_id") or p.get("approval_id")
        if not aid or aid in seen:
            continue
        seen.add(aid)
        req = p.get("request") or {}
        out.append({"approval_id": aid,
                    "tool_name": req.get("tool_name") or p.get("tool_name") or "",
                    "action": req.get("action") or json.dumps(req, ensure_ascii=False)[:200],
                    "kind": p.get("kind") or "approval",
                    "raw": p})
    rec = cached(sid, max_age=180)
    for it in (rec.get("pending") or []):
        aid = it.get("approval_id") if isinstance(it, dict) else None
        if aid and aid not in seen:
            out.append(it)
            seen.add(aid)
    if not out and session_info(sid).get("pending_interaction") not in (None, "none"):
        return [{"approval_id": "", "tool_name": "",
                 "action": "有 pending_interaction 但无明细",
                 "kind": "unknown", "raw": {}}]
    return out


def tasks(sid: str) -> list:
    """后台任务列表。旧代 REST；新代 WS `task.list` + 缓存。"""
    if not is_new():
        st, payload = get(f"/api/v1/sessions/{sid}/tasks", timeout=10)
        d = data_of(payload) or {}
        items = (d.get("items") if isinstance(d, dict) else None) or []
        return [{"id": t.get("id") or t.get("taskId"),
                 "status": t.get("status"),
                 "run_in_background": t.get("run_in_background", t.get("detached")),
                 "command": t.get("command"), "raw": t} for t in items]

    frames, _err = WSProbe(sid, timeout=1.5).collect(want=("task.list",), budget=10)
    table: dict = {}
    for msg in frames:
        t = msg.get("type") or ""
        f = flat(msg)
        if t == "task.list":
            for it in f.get("tasks") or []:
                if isinstance(it, dict) and it.get("task_id"):
                    table[it["task_id"]] = norm_task(it)
        elif t == "task" and f.get("task_id"):
            table[f["task_id"]] = norm_task(f)
    rec = cached(sid, max_age=300)
    for tid, it in ((rec.get("tasks") or {}) if isinstance(rec, dict) else {}).items():
        if tid in table or not isinstance(it, dict):
            continue
        if it.get("status") == "running":
            table[tid] = it
    return list(table.values())


def norm_task(it: dict) -> dict:
    """2.5.x task 结构 → 本仓一贯字段名（status 保留 running/ended 语义）。"""
    return {
        "id": it.get("task_id") or it.get("id"),
        "status": it.get("status"),
        "run_in_background": it.get("detached"),
        "command": it.get("command") or it.get("description"),
        "raw": it,
    }


# --------------------------------------------------------------------- 写
def send(sid: str, text: str) -> tuple:
    """投一轮消息。返回 (ok, detail)。detail 含 prompt_id 与 match（回读一致性）。"""
    st, payload = post(f"/api/v1/sessions/{sid}/prompts",
                       {"content": [{"type": "text", "text": text}]})
    d = data_of(payload) or {}
    stored = "".join(c.get("text", "") for c in (d.get("content") or [])
                     if isinstance(c, dict) and c.get("type") == "text")
    return bool(d), {"http": st, "prompt_id": d.get("prompt_id"),
                     "match": stored == text, "code": code_of(payload),
                     "raw": d}


def steer(sid: str, text: str) -> tuple:
    """把一条新指令**并入当前 turn**（等同 Ctrl-S）。返回 (ok, detail)。

    正确编排（QA P0-2，照 `tools/sess.py:210-221`）：
      1. `POST /sessions/{sid}/prompts` 取**新的** prompt_id；
      2. `POST /sessions/{sid}/prompts/{pid}:steer`（**带 pid 的路径**）。
    **禁止**用 `/prompts:steer` 聚合端点——那是错的（上一版 `wss_bus.py:585-587`）。
    """
    pid = active_prompt(sid)
    if not pid:
        return False, {"err": "no active turn"}
    st, payload = post(f"/api/v1/sessions/{sid}/prompts",
                       {"content": [{"type": "text", "text": text}]})
    d = data_of(payload) or {}
    new_pid = d.get("prompt_id") or ""
    if not new_pid:
        return False, {"err": "submit failed", "http": st, "raw": d}
    st2, payload2 = post(f"/api/v1/sessions/{sid}/prompts/{new_pid}:steer", {})
    d2 = data_of(payload2) or {}
    ok = bool(d2.get("steered"))
    return ok, {"prompt_id": new_pid, "active_before": pid, "steered": ok,
                "http": st2, "code": code_of(payload2), "raw": d2}


def abort_prompt(sid: str, pid: str) -> tuple:
    """中止指定 prompt。REST 优先，失败且新代则 WS `abort` 帧兜底。"""
    st, payload = post(f"/api/v1/sessions/{sid}/prompts/{pid}:abort", {}, timeout=20)
    d = data_of(payload) or {}
    ok = bool(d.get("aborted")) if isinstance(d, dict) else False
    code = code_of(payload)
    if ok or not is_new():
        return ok, code
    msg = _send_ws({"type": "abort", "payload": {"session_id": sid, "prompt_id": pid}})
    if msg:
        return bool(payload_of(msg).get("aborted")), msg.get("code")
    return False, code if code not in (None, 0) else None


def abort_session(sid: str) -> tuple:
    """停当前这一轮，不必先知道 pid。`POST /sessions/{sid}:abort` 2.5.2 仍可用。"""
    st, payload = post(f"/api/v1/sessions/{sid}:abort", {}, timeout=20)
    d = data_of(payload) or {}
    ok = bool(d.get("aborted")) if isinstance(d, dict) else False
    code = code_of(payload)
    if ok:
        return True, code
    pid = active_prompt(sid)
    if not pid:
        return False, code if code not in (None, 0) else 40402
    return abort_prompt(sid, pid)


def cancel_task(sid: str, tid: str, agent_id: str = AGENT) -> tuple:
    """取消后台任务。旧代 REST `:cancel`；新代 WS `task.stop`（request_id 关联）。"""
    if not is_new():
        st, payload = post(f"/api/v1/sessions/{sid}/tasks/{tid}:cancel", {}, timeout=20)
        d = data_of(payload) or {}
        ok = bool(d.get("cancelled")) if isinstance(d, dict) else False
        return ok, code_of(payload)
    msg = _send_ws({"type": "task.stop", "request_id": "", "session_id": sid,
                    "agent_id": agent_id, "task_id": tid})
    if not msg:
        return False, None
    return msg.get("code") == 0, msg.get("code")


def answer_interaction(sid: str, aid: str, decision: str, kind: str = "approval") -> tuple:
    """答复待批交互。`POST /interactions/{aid}` 两代都在。"""
    st, payload = post(f"/api/v1/sessions/{sid}/interactions/{aid}",
                       {"kind": kind, "decision": decision}, timeout=20)
    d = data_of(payload)
    ok = isinstance(d, dict) and d.get("outcome") == decision
    return ok, code_of(payload)


def create_session(title: str, cwd: str = "", model: str = "") -> tuple:
    """建会话。返回 (ok, data)。**只给 `--probe` 用，消费者不许调。**"""
    body: dict = {"title": title}
    if cwd:
        body["metadata"] = {"cwd": cwd}
    st, payload = post("/api/v1/sessions", body, timeout=30)
    d = data_of(payload) or {}
    sid = d.get("id") if isinstance(d, dict) else None
    if not sid:
        return False, {"http": st, "raw": d}
    if model:
        post(f"/api/v1/sessions/{sid}/profile",
             {"agent_config": {"model": model, "permission": "auto"}}, timeout=20)
    return True, d


def set_profile(sid: str, model: str = "", permission: str = "") -> tuple:
    cfg: dict = {}
    if model:
        cfg["model"] = model
    if permission:
        cfg["permission"] = permission
    st, payload = post(f"/api/v1/sessions/{sid}/profile", {"agent_config": cfg}, timeout=20)
    return (st == 200), (data_of(payload) or {})


def archive_session(sid: str) -> tuple:
    """真归档（v2）。v1 的 `:archive` 常返 null 不生效。"""
    st, payload = post("/api/v2/sessions:archive", {"ids": [sid]}, timeout=20)
    d = data_of(payload) or {}
    ok = False
    for r in (d.get("results") or []):
        if isinstance(r, dict) and r.get("ok"):
            ok = True
    return ok, d


def restore_session(sid: str) -> tuple:
    st, payload = post("/api/v2/sessions:restore", {"ids": [sid]}, timeout=20)
    return (st == 200), (data_of(payload) or {})


def takeover_session(sid: str) -> tuple:
    st, payload = post(f"/api/v1/sessions/{sid}:takeover", {}, timeout=20)
    return (st == 200), (data_of(payload) or {})


def list_sessions(busy_only: bool = False, archived_only: bool = False,
                  page_size: int = 50, after_id: str = "") -> tuple:
    """会话列表（**支持翻页**）。返回 (items, next_after_id, err)。

    QA P1-3：上一版只取 `page_size=50` 一页就完事，会话超过 50 个时会漏。
    这里按 `after_id` 游标翻页，由调用方循环直到 `next_after_id` 为空。
    """
    q = f"?page_size={page_size}"
    if busy_only:
        q += "&busy=true"
    if archived_only:
        q += "&archived_only=true"
    if after_id:
        q += f"&after_id={after_id}"
    st, payload = get(f"/api/v1/sessions{q}", timeout=10)
    if st != 200:
        return [], "", f"http={st}"
    d = data_of(payload)
    items = d
    if isinstance(d, dict):
        items = d.get("items") or d.get("sessions") or []
    items = items if isinstance(items, list) else []
    nxt = ""
    if items and isinstance(items[-1], dict):
        nxt = str(items[-1].get("id") or "")
    return items, (nxt if len(items) >= page_size else ""), ""


def list_all_sessions(page_size: int = 50, max_pages: int = 20) -> tuple:
    """翻完所有页，按 id 去重。返回 (items, err)。"""
    out: dict = {}
    after = ""
    for _ in range(max_pages):
        items, nxt, err = list_sessions(page_size=page_size, after_id=after)
        if err:
            return list(out.values()), err
        for it in items:
            if isinstance(it, dict) and it.get("id"):
                out[it["id"]] = it
        if not nxt or nxt == after:
            break
        after = nxt
    return list(out.values()), ""


if __name__ == "__main__":
    print("version =", version() or "(unreachable)", "|", source())
    if len(sys.argv) > 1:
        sid = sys.argv[1]
        print("session_info =", json.dumps(session_info(sid), ensure_ascii=False)[:300])
        print("status       =", json.dumps(status(sid), ensure_ascii=False)[:400])
