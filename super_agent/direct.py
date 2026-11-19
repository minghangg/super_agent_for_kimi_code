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
import re
import sys
import time
import uuid
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.append(str(BASE / "tools"))     # 追加不插入（tools/queue.py 会遮蔽标准库）
from . import paths as kimi_paths  # noqa: E402

WS_PATH = "/api/v1/ws"
AGENT = "main"
TRANSCRIPT_GRADE = "turn"          # 必须非 off，否则服务端不推初始快照
NEW_API_SINCE = (2, 5, 0)

_version_cache: str | None = None


def server_base() -> str:
    return kimi_paths.server_base()


def __getattr__(name: str):
    if name == "SERVER":
        return server_base()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
    req = urllib.request.Request(f"{server_base()}{path}", data=data, method=method, headers=headers)
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
    return server_base().replace("http://", "ws://").replace("https://", "wss://") + WS_PATH


def ping_reply(frame: dict) -> dict | None:
    """服务端 ping → 应回的 pong 帧（非 ping 返回 None）。

    **这条不写下来必再踩**：不回 pong 时服务端约 20 秒后以 close 1001 踢连接，
    表现是「WS 循环正常结束、日志无异常」，极难定位（2026-11-06 实测 5 次重连）。

    2.6 的 ping 是裸的 ``{"type":"ping"}``（无 payload.nonce），pong 也只回
    ``{"type":"pong"}``；2.5.x 带 nonce 并原样回传。有 nonce 就带，没有就不带。
    """
    if (frame.get("type") or "") != "ping":
        return None
    nonce = (frame.get("payload") or {}).get("nonce") or ""
    if nonce:
        return {"type": "pong", "payload": {"nonce": nonce}}
    return {"type": "pong"}


class WSProbe:
    """一次性 WS 探针：连上 → subscribe → 收帧 → 关。

    **2.6 协议（实测 2.6.2）**：帧是平铺的，命令带 `request_id`，应答是
    `{"type":"response","request_id",code}`；订阅命令叫 `subscribe`，参数是
    `agent_ids` 数组。2.5.x 的 `client_hello` + `subscribe_v2` + `transcript`
    对象在 2.6 上**完全无回应**（连 hello 都不回），这是 #94 之后整条 WS 路径
    静默失效的根因。两条都保留：老服务端仍回 `ack`。
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
                # 2.6：平铺帧 + subscribe + agent_ids 数组
                ws.send(json.dumps({
                    "type": "subscribe", "request_id": uuid.uuid4().hex,
                    "session_id": self.sid, "agent_ids": [AGENT],
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
    frames, err = WSProbe(sid, timeout=timeout).collect(want=("agent", "context"), budget=12)
    out: dict = {"agent": {}, "usage": {}, "tasks": [], "err": err}
    for msg in frames:
        t = msg.get("type") or ""
        f = flat(msg)
        if t == "agent":
            out["agent"] = f
        elif t == "usage":
            out["usage"] = f
        elif t == "context":
            # 2.6 把上下文从 `usage` 拆成独立的 `context` 帧，字段名不变
            # （context_tokens / max_context_tokens），只是帧名换了。
            out["usage"] = f
        elif t == "task.list":
            out["tasks"] = f.get("tasks") or []
    if not out["err"] and not out["agent"] and not out["usage"]:
        out["err"] = ("no agent/context frame (subscribe 已带 agent_ids=[main]，"
                      "服务端未推送 agent/context 帧)")
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
    """Read the cache the daemon writes; a miss **says why** instead of ``{}``.

    The daemon has **its own** state-dir cache, so this is never live data - and
    this module no longer guesses where that cache is: the caller knows its own
    ``BASE``, so it passes the path (#92 §F.1).

    Three misses, three answers, all carrying ``_degraded``:

    * ``no_cache_file`` - nobody said where the cache is;
    * ``missing`` - the path does not exist or does not parse;
    * ``stale`` - the record is older than ``max_age``, with ``ts`` attached.

    A bare ``{}`` used to mean all three, and every consumer read it as "no
    data available anywhere" - invisible, un-machine-checkable, and impossible
    to tell from a healthy session with an empty record (#92 §F.1 route ③).
    """
    import time as _time

    path = cache_file
    if path is None:
        print("direct.cached: 未指定 cache_file，跳过读缓存（缓存位于调用方 state 目录；"
              "本模块已迁移，不再推断默认路径）", file=sys.stderr)
        return {"_degraded": "no_cache_file"}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"_degraded": "missing"}
    rec = (d.get("sessions") or {}).get(sid) or {}
    if not isinstance(rec, dict) or not rec:
        return {"_degraded": "missing"}
    ts = int(rec.get("ts") or 0)
    if max_age and _time.time() - ts > max_age:
        return {"_degraded": "stale", "ts": ts}
    return rec


def session_info(sid: str) -> dict:
    """会话对象（两代都在）。busy/main_turn_active/pending_interaction/archived。"""
    st, payload = get(f"/api/v1/sessions/{sid}", timeout=10)
    d = data_of(payload)
    if st == 200 and isinstance(d, dict) and code_of(payload) in (0, None):
        return d
    return {"_err": f"http={st} {str(payload)[:120]}", "_source": "rest"}


# --------------------------------------------------------------------- 上下文可判性
# 一个数字能不能拿去判线，取决于它有多旧、窗口知不知道。这两件事原来都不在
# 返回值里，于是 47 天前的旧值被当成现值判 60% 交接线（#94）。本段补三件事：
# 值产生时刻（`value_ts`）、年龄（`age_s`）、可判性（`ctx_state`）。
#
# `ctx_state` 三值，只读语义：
#   live   —— 有值、`age_s <= CTX_STALE_S`、窗口已知 ⇒ 可判
#   stale  —— 有值但过期 ⇒ 数字仍由 `context_tokens_raw` 承载，判据字段不可判
#   absent —— 无值（含 sid 不存在、wire 无记录、窗口未知）
CTX_STALE_S = 300
WIRE_BLOCK = 64 * 1024

_MODEL_SECTION = re.compile(r'^\[models\.(?:"([^"]+)"|([A-Za-z0-9_\-]+))\]', re.M)
_MODEL_MAX = re.compile(r'^max_context_size\s*=\s*(\d+)', re.M)
_MODEL_INNER = re.compile(r'^model\s*=\s*"([^"]*)"', re.M)

_WINDOWS_CACHE: dict = {}


def _wire_root() -> Path:
    """wire 根：`SESS_WIRE_ROOT`（隔离自测）> `kimi_paths.sessions_dir()`。"""
    raw = (os.environ.get("SESS_WIRE_ROOT") or "").strip()
    return Path(os.path.expanduser(raw)) if raw else kimi_paths.sessions_dir()


def _wire_files(sid: str) -> list:
    try:
        return list(_wire_root().glob(f"*/{sid}/agents/main/wire.jsonl"))
    except OSError:
        return []


def _iter_wire_records(path: Path, block: int = WIRE_BLOCK):
    """Yield parsed records newest-first, reading tail-out in ``block`` chunks.

    A line straddles a block boundary: its head sits in the older chunk. The
    head-less fragment of the newer chunk is carried over and glued back, so no
    line is lost and none is parsed twice.
    """
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            pos, pending = size, b""
            while pos > 0:
                step = min(block, pos)
                pos -= step
                fh.seek(pos)
                data = fh.read(step) + pending
                lines = data.split(b"\n")
                if pos > 0:
                    pending = lines[0]
                    body = lines[1:]
                else:
                    pending, body = b"", lines
                for raw in reversed(body):
                    if not raw.strip():
                        continue
                    try:
                        rec = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(rec, dict):
                        yield rec
    except OSError:
        return


def _model_windows(config_file: Path | None = None) -> dict:
    """`[models.*]` 段 → {键名: 窗口, 段内 model 名: 窗口}。

    `config.toml` 有 4 个**不带引号**的段（`[models.cb-hy4-preview]` 之类），
    只匹配引号段会漏，故两条分支都要。Python 3.10 无 `tomllib`，用 20 行正则
    代替第三个依赖；取不到就是取不到，不填默认窗口。
    """
    path = Path(config_file) if config_file is not None else kimi_paths.config_file()
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return {}
    key = (str(path), stamp)
    cached = _WINDOWS_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    out: dict = {}
    marks = [(m.start(), m.group(1) or m.group(2))
             for m in _MODEL_SECTION.finditer(text)]
    for idx, (start, name) in enumerate(marks):
        end = marks[idx + 1][0] if idx + 1 < len(marks) else len(text)
        body = text[start:end]
        got = _MODEL_MAX.search(body)
        if not got:
            continue
        cap = int(got.group(1))
        out.setdefault(name, cap)
        inner = _MODEL_INNER.search(body)
        if inner and inner.group(1):
            out.setdefault(inner.group(1), cap)
    _WINDOWS_CACHE.clear()
    _WINDOWS_CACHE[key] = out
    return out


def _window_of(model: str) -> int | None:
    if not model:
        return None
    return _model_windows().get(model)


def _ctx_from_usage(usage: dict) -> int:
    """一轮的真实上下文：四加数。漏 `output` 会把占用算低（#94 已核 39/87）。"""
    return sum(int(usage.get(k) or 0) for k in
               ("inputOther", "inputCacheRead", "inputCacheCreation", "output"))


def wire_ctx(sid: str) -> dict:
    """从该会话 wire 尾部取上下文，并给出可判性。

    只认末条 `type=="usage.record" and usageScope=="turn"`：`session` 级记录是
    全会话累计，取值会偏大。`usage.record.time` 是**毫秒**。

    第 5 条路（`token_counting.turn_recorded`）只在第 4 条无可用记录时使用；
    **不许**用它把一个已判 `stale` 的第 4 条值洗新——旧值换个来源还是旧值。
    两条都无 ⇒ `absent`。
    """
    now = time.time()
    hits = _wire_files(sid)
    if not hits:
        return {"ctx_state": "absent", "value_ts": None, "age_s": None,
                "context_tokens_raw": None, "max_context_tokens": None,
                "_source": "wire:no_file"}

    try:
        path = max(hits, key=lambda p: p.stat().st_mtime)
    except OSError:
        path = hits[0]

    turn_usage = None
    fallback = None
    model_hint = ""
    scanned_whole = True
    for rec in _iter_wire_records(path):
        kind = rec.get("type") or ""
        if kind == "usage.record":
            if not model_hint:
                # 模型是会话级事实，不是值：用它定窗口不会把旧值洗新。
                model_hint = rec.get("model") or ""
            if rec.get("usageScope") == "turn":
                turn_usage = rec
                scanned_whole = False      # 命中即停，没读到文件头
                break
        elif kind == "token_counting.turn_recorded" and fallback is None:
            fallback = rec

    scan = "full" if scanned_whole else "tail"
    if turn_usage is not None:
        usage = turn_usage.get("usage") or {}
        total = _ctx_from_usage(usage if isinstance(usage, dict) else {})
        value_ts = float(turn_usage.get("time") or 0) / 1000.0
        route = "usage.record"
        model = turn_usage.get("model") or ""
        cap = _window_of(model)
        raw = total
    elif fallback is not None:
        raw = int(fallback.get("tokens") or 0)
        value_ts = float(fallback.get("time") or 0) / 1000.0
        route = "turn_recorded"
        # 该记录不带模型名，窗口只能取本会话最近一条 usage.record 的模型；
        # 取不到就是不知道，不填默认值。
        model, cap = model_hint, _window_of(model_hint)
    else:
        return {"ctx_state": "absent", "value_ts": None, "age_s": None,
                "context_tokens_raw": None, "max_context_tokens": None,
                "_source": f"wire:no_record(scan={scan})"}

    if not value_ts:
        return {"ctx_state": "absent", "value_ts": None, "age_s": None,
                "context_tokens_raw": None, "max_context_tokens": None,
                "_source": f"wire:{route}(no_time, scan={scan})"}

    age = int(now - value_ts)
    window = f"window={cap}" if cap else "window=unknown"
    if cap is None:
        state = "absent"          # 无窗口 ⇒ 占用率算不出，值仍由 _raw 承载
    elif age > CTX_STALE_S:
        state = "stale"
    else:
        state = "live"
    return {"ctx_state": state, "value_ts": int(value_ts), "age_s": age,
            "context_tokens_raw": raw, "max_context_tokens": cap,
            "_source": f"wire({route},age={age}s,{window},scan={scan})"}


# status 的**键集契约**：bus 与 direct 两条路必须返回完全相同的键集合。
# 2026-11-06 步 2 返工 P0-2：daemon 侧原样透出 rec["status"]，而该 dict 只在
# 对应帧推过之后才有键 ⇒ 安静会话缺 archived/main_turn_active 等。定义在这里，
# 两侧共用（wss_bus 的 _norm_status 也用这份）。
STATUS_KEYS = (
    "busy", "main_turn_active", "pending_interaction", "last_turn_reason",
    "archived", "model", "thinking_level", "permission", "plan_mode",
    "swarm_mode", "tower_mode", "context_tokens", "max_context_tokens",
    "context_usage", "agent_status",
    # 可判性三键（#94）：不进模板就会被 `_norm_status` 丢掉，bus 路径于是恒无
    # `ctx_state`，一旦 WS 修好就 WARN 刷屏。
    "ctx_state", "value_ts", "age_s",
)

# 判据之外的原始值：**不进 STATUS_KEYS**。它经过 `json.dumps` 要看得见（只读属性
# 会全丢），所以是普通 dict 键；「非判据」由 `ctx_gate.usage_of()` 不读它保证，
# 并有两个自测断言钉住（`direct` 侧查它不进模板，`ctx_gate` 侧查守卫先看状态）。
NON_JUDGE_KEYS = ("context_tokens_raw",)


def status_template() -> dict:
    """空模板：所有键都在，值为 None。两侧据此补齐键集。"""
    return {k: None for k in STATUS_KEYS}


def _apply_ctx(out: dict, ctx: dict) -> dict:
    """把 wire 侧的上下文结论落到 status 上，判据字段与原始值分开。

    `ctx_state != "live"` 时判据三字段一起置 None（纵深防御）：只清
    `context_usage` 不够，`context_tokens`／`max_context_tokens` 还在，
    `usage_of()` 的第二分支会立刻把一个过期值复活成占用率。
    原值不丢，改由 `context_tokens_raw` 承载。
    """
    state = ctx.get("ctx_state") or "absent"
    out["ctx_state"] = state
    out["value_ts"] = ctx.get("value_ts")
    out["age_s"] = ctx.get("age_s")
    out["context_tokens_raw"] = ctx.get("context_tokens_raw")
    cap = ctx.get("max_context_tokens")
    if state == "live":
        tok = ctx.get("context_tokens_raw")
        out["context_tokens"] = tok
        out["max_context_tokens"] = cap
        out["context_usage"] = (tok / cap) if (isinstance(tok, (int, float))
                                               and isinstance(cap, int) and cap) else None
    else:
        out["context_tokens"] = None
        out["max_context_tokens"] = None
        out["context_usage"] = None
    return out


def status(sid: str, cache_file: Path | None = None) -> dict:
    """会话运行状态。旧代走 `/status`；新代 = `/sessions/{id}` + WS agent/usage。

    ``cache_file`` is threaded down to the cache read, because the caller is the
    only part that knows where its state directory is (#92 §F.2 F-3).

    上下文的**可判性**（#94）：每条出口都要给 `ctx_state`／`value_ts`／`age_s`，
    否则「有没有值」与「值还能不能用来判线」又被混成同一件事。
    """
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
    elif isinstance(info, dict) and "40401" in str(info.get("_err") or ""):
        # sid 不存在：无值，不是「读坏了」。判据要给 absent，不能落到 none(...)。
        out["ctx_state"] = "absent"
        out["_source"] = "absent(http=40401)"
        return out

    if not is_new():
        st, payload = get(f"/api/v1/sessions/{sid}/status", timeout=10)
        d = data_of(payload)
        if st == 200 and isinstance(d, dict) and code_of(payload) in (0, None):
            d.setdefault("_source", "rest:/status")
            # 旧代没有值时刻，只能按「刚取到」算；窗口与占比齐备才算 live。
            tok, cap = d.get("context_tokens"), d.get("max_context_tokens")
            if isinstance(tok, (int, float)) and isinstance(cap, int) and cap:
                d["ctx_state"] = "live"
                d["value_ts"] = int(time.time())
                d["age_s"] = 0
                d["context_tokens_raw"] = tok
            else:
                d["ctx_state"] = "absent"
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
        })
        if isinstance(tok, (int, float)) and isinstance(cap, int) and cap:
            # 帧刚到：值就是此刻的，窗口由帧给出。
            _apply_ctx(out, {"ctx_state": "live", "value_ts": int(time.time()),
                             "age_s": 0, "context_tokens_raw": tok,
                             "max_context_tokens": cap})
        else:
            _apply_ctx(out, wire_ctx(sid))     # 帧里没数字 ⇒ 退到 wire，不假装有值
        if agent.get("status"):
            out["agent_status"] = agent["status"]
        out["_source"] = "ws:agent+usage"
        return out

    rec = cached(sid, max_age=180, cache_file=cache_file)
    st_rec = rec.get("status") if isinstance(rec.get("status"), dict) else {}
    if st_rec:
        out.update({k: v for k, v in st_rec.items() if v is not None
                    and k not in NON_JUDGE_KEYS})
        # 缓存记录没有值时刻，`ts` 是「最后一次被碰」而不是「值产生时」，
        # 拿它当 `value_ts` 会把旧值洗新 —— 正是 #94 的病。故一律另算；
        # wire 也说不出可判性时，缓存里的判据数字一并清掉（原值留在 `_raw`）。
        wire = wire_ctx(sid)
        _apply_ctx(out, wire)
        if wire.get("ctx_state") == "absent":
            out["context_tokens_raw"] = st_rec.get("context_tokens")
        out["_source"] = f"ws_cache(age={int(time.time()) - int(rec.get('ts') or 0)}s)"
        return out

    wire = wire_ctx(sid)
    _apply_ctx(out, wire)
    if wire.get("ctx_state") != "absent":
        out["_source"] = f"direct({wire.get('_source') or 'wire'})"
        return out

    out["_source"] = (f"degraded(sessions/{{id}}; ws:{wsres.get('err') or 'no frame'}; "
                      f"cache:{rec.get('_degraded') or 'miss'}; "
                      f"wire:{wire.get('_source') or 'none'})")
    if rec.get("_degraded"):
        out["_degraded"] = rec["_degraded"]
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


def pending_interactions(sid: str, cache_file: Path | None = None) -> list:
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
    rec = cached(sid, max_age=180, cache_file=cache_file)
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


def tasks(sid: str, cache_file: Path | None = None) -> list:
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
    rec = cached(sid, max_age=300, cache_file=cache_file)
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

    2.6（实测 2.6.2）：``POST /sessions/{sid}/steer`` 一次请求即可，
    请求体与 ``POST /prompts`` 相同。旧的「先提交取 prompt_id、再
    ``/prompts/{pid}:steer``」两步法在 2.6 上会失败——提交回执里的 id 是
    ``msg_`` 前缀，路由按 ``{prompt_id}:{action}`` 解析时把 ``msg_…:steer``
    当成未知动作，返回 ``40001 unsupported action``。

    先试单请求端点；失败再退回两步法，兼容 2.5.x。
    """
    pid = active_prompt(sid)
    if not pid:
        return False, {"err": "no active turn"}

    # 2.6：单请求端点
    st, payload = post(f"/api/v1/sessions/{sid}/steer",
                       {"content": [{"type": "text", "text": text}]})
    d = data_of(payload) or {}
    if code_of(payload) in (0, None) and isinstance(d, dict) and d.get("prompt_id"):
        return True, {"prompt_id": d.get("prompt_id"), "active_before": pid,
                      "steered": True, "via": "sessions/steer",
                      "http": st, "code": code_of(payload), "raw": d}

    # 2.5.x 退路：先提交取 prompt_id，再按 id steer
    st1, payload1 = post(f"/api/v1/sessions/{sid}/prompts",
                         {"content": [{"type": "text", "text": text}]})
    d1 = data_of(payload1) or {}
    new_pid = d1.get("prompt_id") or ""
    if not new_pid:
        return False, {"err": "submit failed", "http": st1, "raw": d1}
    st2, payload2 = post(f"/api/v1/sessions/{sid}/prompts/{new_pid}:steer", {})
    d2 = data_of(payload2) or {}
    ok = bool(d2.get("steered"))
    return ok, {"prompt_id": new_pid, "active_before": pid, "steered": ok,
                "via": "prompts/{pid}:steer",
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
