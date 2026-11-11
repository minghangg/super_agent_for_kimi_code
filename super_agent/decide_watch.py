"""Injectable decision/delivery watch port (ISS-25 item 7).

The workbench implementation is preserved verbatim as a legacy script. This
package port keeps the same state machine, cursor, lock, delivery rules and
22-case self-test. Every private dependency is supplied by the caller through
``DecideWatchConfig``: state root, individual files, event/heartbeat bridge,
session probing, recipients, send transport, health/alarm modules and the
autostart spec.

With no injection the module reports a parameter error or an explicit WARN;
it never guesses a production root and never imports a workbench module.

Usage:
    python -m super_agent.decide_watch --self-test
    configure(DecideWatchConfig(...))
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from . import autostart as autostart
from . import session_alarm as alarm_mod
from . import session_health as health_mod
from . import paths as kimi_paths


@dataclass(frozen=True)
class DecideWatchConfig:
    """Caller-supplied boundary. None means not injected, never guessed."""

    state_root: Path | None = None
    workspace_root: Path | None = None
    sessions_path: Path | None = None
    cursor_path: Path | None = None
    log_path: Path | None = None
    lock_path: Path | None = None
    health_state_path: Path | None = None
    delivery_state_path: Path | None = None
    delivery_stats_path: Path | None = None
    delivery_log_path: Path | None = None
    undelivered_path: Path | None = None
    bind_path: Path | None = None
    ledger_path: Path | None = None
    supervisor_notice_ledger_path: Path | None = None
    supervisor_notice_last_path: Path | None = None
    session_script: Path | None = None
    supervisor_key: str = "supervisor"
    alarm_state_file: str = "session_alarm_state.json"
    event_source: object | None = None
    heartbeat: object | None = None
    inject_event: object | None = None
    ps_json: object | None = None
    windowless_python: object | None = None
    probe_session: object | None = None
    send_transport: object | None = None
    alarm_module: object | None = None
    health_module: object | None = None
    autostart_name: str = "DecisionWatch"
    startup_script: Path | None = None
    startup_workdir: Path | None = None
    autostart_spec: object | None = None
    self_test_root: Path | None = None
    fixture_sessions_path: Path | None = None
    fixture_bind_path: Path | None = None
    fixture_ledger_path: Path | None = None


DEFAULT_CONFIG = DecideWatchConfig()
_LAST_HEALTH_STATE = ""
DELIVERY_SINK = None


def configure(config: DecideWatchConfig | None = None, **overrides) -> DecideWatchConfig:
    global DEFAULT_CONFIG
    base = config or DEFAULT_CONFIG
    if overrides:
        base = replace(base, **overrides)
    DEFAULT_CONFIG = base
    return base


def missing_injections(config: DecideWatchConfig | None = None) -> list[str]:
    cfg = config or DEFAULT_CONFIG
    required = ("state_root", "workspace_root", "event_source", "heartbeat",
                "inject_event", "ps_json", "windowless_python", "probe_session",
                "send_transport", "session_script")
    return [n for n in required if getattr(cfg, n) is None]


def self_check() -> int:
    missing = missing_injections()
    if missing:
        print("decide_watch: 未注入 " + ", ".join(missing), file=sys.stderr)
        return 2
    print("decide_watch: 注入完整")
    return 0


def _cfg() -> DecideWatchConfig:
    return DEFAULT_CONFIG


def _state_root() -> Path:
    cfg = _cfg()
    if cfg.state_root is None:
        raise RuntimeError("未注入 state_root，拒绝静默连生产")
    return Path(cfg.state_root).expanduser()


def _workspace_root() -> Path:
    cfg = _cfg()
    if cfg.workspace_root is None:
        raise RuntimeError("未注入 workspace_root，拒绝静默连生产")
    return Path(cfg.workspace_root).expanduser()


def _path(name: str, filename: str) -> Path:
    cfg = _cfg()
    value = getattr(cfg, name)
    if value is not None:
        return Path(value).expanduser()
    return _state_root() / filename


SESSIONS = lambda: _path("sessions_path", "sessions.json")
CURSOR = lambda: _path("cursor_path", "decide_watch_cursor.json")
LOG = lambda: _path("log_path", "decision_watch.log")
LOCK = lambda: _path("lock_path", "decide_watch.lock")
BASE = _workspace_root
STATE_DIR = _state_root


def _delivery_log_path() -> Path:
    return _path("delivery_log_path", "delivery_watch.log")


def _delivery_state_path() -> Path:
    return _path("delivery_state_path", "delivery_watch_state.json")


def _delivery_paths() -> dict:
    return {
        "state": _delivery_state_path(),
        "stats": _path("delivery_stats_path", "delivery_watch_stats.jsonl"),
        "log": _delivery_log_path(),
        "undelivered": _path("undelivered_path", "delivery_undelivered.log"),
        "sessions": SESSIONS(),
        "bind": _path("bind_path", "issue_sessions.json"),
        "ledger": _path("ledger_path", "ledger.jsonl"),
    }


def AUTOSTART_NAME() -> str:
    return _cfg().autostart_name


def wss_bus():
    cfg = _cfg()
    if cfg.event_source is None:
        raise RuntimeError("未注入 event_source，拒绝静默连生产")
    return cfg.event_source


def _event_get(path: str, *, timeout: float, port: int = 0):
    return wss_bus().bus_get(path, timeout=timeout, port=port)


def _event_health(port: int = 0):
    return wss_bus().bus_health(port=port)


def _event_post(path: str, body: dict, *, port: int = 0):
    cfg = _cfg()
    if cfg.inject_event is None:
        raise RuntimeError("未注入 inject_event，拒绝静默连生产")
    return cfg.inject_event(path, body, port=port)


def _write_heartbeat(name: str, extra: dict | None = None) -> None:
    cfg = _cfg()
    if cfg.heartbeat is None:
        print("decide_watch: 未注入心跳 callable（heartbeat）", flush=True)
        return
    cfg.heartbeat(name, extra)


def _ps_bridge(script: str, timeout: float = 60.0):
    cfg = _cfg()
    if cfg.ps_json is None:
        raise RuntimeError("未注入 PowerShell 桥接（ps_json）")
    return cfg.ps_json(script, timeout)


def _windowless():
    cfg = _cfg()
    if cfg.windowless_python is None:
        raise RuntimeError("未注入 windowless_python")
    return cfg.windowless_python()


def _send(sid: str, text: str, *, timeout: float = 60.0):
    cfg = _cfg()
    if cfg.send_transport is None:
        raise RuntimeError("未注入 send_transport，拒绝静默连生产")
    return cfg.send_transport(sid, text, timeout=timeout)


def _probe_session(sid: str):
    probe = _cfg().probe_session
    if probe is None:
        return None, "未注入会话探测函数（probe_session）"
    return probe(sid)


HANDOFF_EVENT = "event.file.handoff_created"
DECIDE_EVENT = "event.file.pm_decide_changed"
WATCH_TYPES = {HANDOFF_EVENT, DECIDE_EVENT}

MAX_AGE_S = 1800          # 超过这么久的事件不再通知（防重放历史）
BACKOFF_MIN, BACKOFF_MAX = 1.0, 30.0


def log(msg: str) -> None:
    line = f"{datetime.now().strftime('%F %T')} [DECIDE] {msg}"
    print(line, flush=True)
    try:
        STATE_DIR().mkdir(parents=True, exist_ok=True)
        with LOG().open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def supervisor_sid(explicit: str = "") -> str:
    """每轮现读（§7.0f：id 会换血，不写死）。"""
    if explicit:
        return explicit
    try:
        data = json.loads(SESSIONS().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    val = data.get("supervisor")
    if isinstance(val, dict):
        val = val.get("session") or val.get("id") or ""
    return val if isinstance(val, str) else ""


def load_cursor() -> dict:
    try:
        d = json.loads(CURSOR().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cursor(seq: int, boot_id: str) -> None:
    try:
        CURSOR().parent.mkdir(parents=True, exist_ok=True)
        CURSOR().write_text(json.dumps({"seq": int(seq), "boot_id": boot_id,
                                      "updated": datetime.now().isoformat(timespec="seconds")},
                                     ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        log(f"游标写盘失败（不影响本进程）：{exc}")


def _pid_alive(pid: int) -> bool:
    """pid 是否活着（Windows/Unix 通用，只用 stdlib）。"""
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, text=True, timeout=15)
            return str(pid) in (out.stdout or "")
        os.kill(pid, 0)
        return True
    except Exception:  # noqa: BLE001
        return False


def _cmdline_of(pid: int) -> str:
    """取进程命令行（用于确认这个 pid 真的是 decide_watch，而不是 pid 复用）。"""
    try:
        if os.name == "nt":
            ps = ("(Get-CimInstance Win32_Process -Filter \"ProcessId=%d\")"
                  ".CommandLine") % pid
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                 capture_output=True, text=True, timeout=25)
            return (out.stdout or "").strip()
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read().decode("utf-8", "replace").replace("\0", " ").strip()
    except Exception:  # noqa: BLE001
        return ""


def acquire_lock(force: bool = False) -> tuple[bool, str]:
    """单实例锁（issue #17 退修 P1-2）。返回 `(拿到没, 说明)`。

    为什么需要：QA 实测**并发启动两个 decide_watch ⇒ 同一事件双发 2 条通知**
    （游标只在正常轮次间推进，两个进程各自从同一 since 拉）。
    判据：锁存在 + pid 活着 + **命令行含 `decide_watch`** 才算「已有实例」——
    单看 pid 会被 pid 复用骗到；单看锁文件会被陈旧锁永久挡住。
    陈旧锁（pid 已死/命令行不符）⇒ 自动清理接管。
    """
    try:
        raw = LOCK().read_text(encoding="utf-8")
        rec = json.loads(raw)
    except (OSError, ValueError):
        rec = {}
    if rec:
        pid = int(rec.get("pid") or 0)
        cmd = _cmdline_of(pid) if _pid_alive(pid) else ""
        if cmd and "decide_watch" in cmd:
            msg = (f"已有实例 pid={pid} 在跑（started_at={rec.get('started_at')}）"
                   f"⇒ 拒绝启动，避免同一事件双发")
            if force:
                log(f"[lock] ⚠⚠ --force 越过单实例锁：{msg}")
            else:
                print(f"[decide_watch] {msg}", file=sys.stderr)
                log(f"[lock] {msg}")
                return False, msg
        else:
            why = "pid 已不存在" if not _pid_alive(pid) else "命令行不符（疑似 pid 复用）"
            log(f"[lock] 清理陈旧锁（pid={pid}：{why}）并接管")
    try:
        STATE_DIR().mkdir(parents=True, exist_ok=True)
        tmp = LOCK().with_suffix(".lock.tmp")
        tmp.write_text(json.dumps({
            "pid": os.getpid(),
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "boot_id": str((_event_health(wss_bus().PORT) or {}).get("boot_id") or ""),
        }, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, LOCK())
    except OSError as exc:
        log(f"[lock] 写锁失败（不阻断启动）：{exc}")
    return True, ""


def release_lock() -> None:
    """只删自己那把锁（别误删别人接管后的锁）。"""
    try:
        rec = json.loads(LOCK().read_text(encoding="utf-8"))
        if int(rec.get("pid") or 0) == os.getpid():
            LOCK().unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def _state_file() -> Path:
    """跨进程的去重状态文件（与 STATE_DIR 同步，便于隔离测试）。"""
    return _path("health_state_path", "supervisor_health_state.json")


def _last_state_read() -> str:
    """上一轮 supervisor 状态：**优先读状态文件**（跨进程/跨重启都有效）。

    issue #16 退修 P2-4：原实现只在进程内存里记 `_LAST_SUPERVISOR_STATE`，
    `--once` 每轮都是新进程 ⇒ 每轮都认为「状态变了」⇒ 每轮发一条事件。
    """
    try:
        d = json.loads(_state_file().read_text(encoding="utf-8"))
        return str(d.get("state") or "")
    except (OSError, ValueError):
        return ""


def _last_state_write(st: str) -> None:
    try:
        _path("health_state_path", "supervisor_health_state.json").parent.mkdir(parents=True, exist_ok=True)
        tmp = _state_file().with_suffix(".tmp")
        tmp.write_text(json.dumps({"state": st, "ts": time.time()}, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, _state_file())
    except OSError:
        pass


def check_supervisor_health(port: int = 0) -> dict:
    """supervisor 键健康核（issue #16）：dead/unknown 走**双通道**告警。

    通道①：写本消费者日志（`decision_watch.log`），一条话说清「send to the supervisor key would be lost」。
    通道②：往 daemon 发一个 `event.session.supervisor_key_dead` 事件（复用 `/events` 面），
            这样 `/healthz` 与 注入观察端 都能看到，不依赖有人去翻日志。

    **绝不 send 给失效收件人本人**——那条腿已经坏了；谁把它「优化」成发同一收件人，
    就等于把告警也扔进黑洞。
    `ok` 恢复后写一条恢复日志，告警状态自动消失（不保留粘滞标志）。
    """
    global _LAST_SUPERVISOR_STATE
    probe = _cfg().probe_session
    if probe is None:
        log("[supervisor-health] probe_session not injected")
        return {"state": "unknown", "reason": "probe_missing"}
    hm = _cfg().health_module or health_mod
    res = hm.check_key(_cfg().supervisor_key,
                       sessions=str(SESSIONS()) if SESSIONS().is_file() else "",
                       probe=probe)
    st = res.get("state")
    prev = _last_state_read() or _LAST_SUPERVISOR_STATE      # 文件优先（跨进程），内存兜底
    _LAST_SUPERVISOR_STATE = st
    _last_state_write(st)

    if st == "ok":
        if prev and prev != "ok":
            log(f"[supervisor-health] supervisor 键已恢复：{res.get('sid')} 活着且未归档")
        return res

    if st in ("dead", "missing", "invalid"):
        # 日志与**事件**都按状态去重（issue #16 退修 P2-4：上一版事件不去重，
        # 接上消费者后会每 20s 重复发一条，把事件环刷满）。
        # 状态没变 ⇒ 两条腿都不重复；状态变化或恢复 ⇒ 再发。
        if prev != st:
            log(f"[supervisor-health] ⚠ supervisor 键={res.get('sid') or '（空）'}，"
                f"状态={st}，**send to the supervisor key would be lost**：{res.get('reason')}")
            _emit_supervisor_dead_event(res, port)
        return res

    # unknown：只记一次「无法判定」，不当 dead
    if prev != "unknown":
        log(f"[supervisor-health] supervisor 键无法判定（{res.get('reason')}）——不告警、不当 dead")
    return res


def _emit_supervisor_dead_event(res: dict, port: int) -> None:
    """往 daemon 发事件（失败**记一条明确日志**，不静默跳过 —— issue #16 退修 P1-3）。

    daemon 挂了就是「事件腿不可用」，必须在日志里说清：否则事后看日志会以为
    「既没告警也没问题」，而真相是「告警发不出去」。
    """
    try:
        body = {
            "type": "file",
            "event": "event.session.supervisor_key_dead",
            "payload": {
                "key": res.get("key"),
                "old_sid": res.get("sid"),
                "state": res.get("state"),
                "reason": str(res.get("reason") or "")[:300],
                "checked_at": datetime.now().isoformat(timespec="seconds"),
                "effect": "send to the supervisor key would be lost（收件人不存在）",
            },
        }
        _event_post("/events/inject", body, port=port or wss_bus().PORT)
    except Exception as exc:  # noqa: BLE001
        log(f"[supervisor-health] 事件腿不可用（daemon down：{type(exc).__name__}）"
            f"——日志那条腿已写，但 /healthz 与 注入观察端 看不到本次告警")


def notify(rsid: str, text: str, dry: bool) -> bool:
    """发一条提醒；`dry` 只打印。返回是否成功。"""
    if dry:
        log(f"[dry-run] 将发给 {rsid or '<无 supervisor>'}: {text}")
        return True
    if not rsid:
        log("无 supervisor 会话 id（state/sessions.json 没有 supervisor 键），通知丢弃")
        return False
    try:
        r = _send(rsid, text)
    except Exception as exc:  # noqa: BLE001
        log(f"send 失败：{type(exc).__name__}: {exc}")
        return False
    if r.returncode != 0:
        log(f"send 失败 rc={r.returncode}：{(r.stderr or r.stdout or '').strip()[:160]}")
        return False
    log(f"已通知 supervisor {rsid[:14]}…：{text[:60]}")
    return True


def describe(ev: dict) -> str:
    """把事件翻成给主管的一句提醒（**只放元数据**，不带正文）。"""
    p = ev.get("payload") or {}
    t = ev.get("type") or ""
    if t == HANDOFF_EVENT:
        return (f"【拍板哨】新交接档 {p.get('name')}（role={p.get('role')}）："
                f"{p.get('path')}。请查收并执行交接。")
    if t == DECIDE_EVENT:
        return (f"【拍板哨】state/pm/{p.get('project')}.md 的「待拍板」段有变化"
                f"（md5={str(p.get('md5'))[:8]}…）。请尽快裁定（按注入的处置选项），别让用户再等。")
    return f"【拍板哨】{t} {json.dumps(p, ensure_ascii=False)[:160]}"


def fetch_events(since: int, wait: float, port: int) -> dict | None:
    """`/events?since=&wait=` 长轮询；daemon 不可用返回 None。"""
    try:
        return _event_get(f"/events?since={int(since)}&wait={wait}&limit=200",
                               timeout=wait + 10, port=port)
    except Exception as exc:  # noqa: BLE001
        log(f"/events 取数失败：{type(exc).__name__}: {exc}")
        return None


def health(port: int) -> dict | None:
    try:
        return _event_health(port=port)
    except Exception:  # noqa: BLE001
        return None


def handle(events: list[dict], rsid: str, dry: bool, max_age: float) -> int:
    """处理一批事件，返回「实际通知条数」。"""
    n = 0
    now = time.time()
    for ev in events:
        if (ev.get("type") or "") not in WATCH_TYPES:
            continue
        age = now - float(ev.get("ts") or 0)
        if max_age and age > max_age:
            log(f"跳过过期事件 seq={ev.get('seq')}（{int(age)}s 前，超过 {int(max_age)}s 上限）")
            continue
        if notify(rsid, describe(ev), dry):
            n += 1
    return n


# --------------------------------------------------------------------- 主管投递哨（ISS-55）
# `event.agent.supervisor` 的**投递消费端**：事件由 engine 注入，本段把它真投进
# `payload.sid` 指定的会话。三条硬约束（ISS-55 任务书）：
#   ① 独立一腿，**不进 `WATCH_TYPES`**——`handle()` 用「此刻的 supervisor 键」作目标，
#      而 `payload.sid` 是发件那一刻解析的，换血后两者会指向不同会话；
#   ② at-most-once 靠**事件指纹台账**，seq 游标与拍板哨共用（`/events` 排他：
#      只有 `seq > since` 才会返回，所以「本轮不投且不退游标」＝永久漏投）；
#   ③ `retry_later` 必须**台账驱动**重试，且台账存**完整 text**（截断会发半句话）。
SUPERVISOR_NOTICE_EVENT = "event.agent.supervisor"
SUPERVISOR_FAILED_EVENT = "event.session.supervisor_delivery_failed"
SUPERVISOR_NOTICE_HEARTBEAT = "supervisor_notice"
NOTICE_RETRY_LIMIT = 3          # 连续这么多轮仍 unknown ⇒ probe_unknown 终态
EXIT_NOTICE_UNREGISTERED = 12   # 心跳名未注册（启动即报错，绝不带病起跑）
_NOTICE_LAST_KEYS = ("last_ok_at", "last_ok_seq", "last_fail_at", "last_fail_state",
                     "last_fail_seq", "pending_indeterminate", "delta")


def _notice_ledger_path() -> Path:
    return _path("supervisor_notice_ledger_path", "supervisor_notice_ledger.jsonl")


def _notice_last_path() -> Path:
    return _path("supervisor_notice_last_path", "supervisor_notice_last.json")


def notice_fingerprint(ev: dict) -> str:
    """事件指纹：`sha1(seq|ts|type|md5(payload 规范 JSON))[:16]`。

    `sort_keys=True` 是硬要求：同一 payload 只要键序不同就该是同一条，
    否则 daemon 重启重扫会把「重放」当「新事件」再投一遍。
    """
    payload = ev.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    canon = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":"))
    digest = hashlib.md5(canon.encode("utf-8")).hexdigest()
    raw = f"{int(ev.get('seq') or 0)}|{ev.get('ts')}|{ev.get('type')}|{digest}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _notice_rows() -> dict:
    """台账**最新态**：同一指纹后写的行覆盖先写的行（追加式，只读时折叠）。"""
    out: dict = {}
    try:
        text = _notice_ledger_path().read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("fp"):
            out[str(row["fp"])] = row
    return out


def _notice_append(row: dict) -> None:
    try:
        p = _notice_ledger_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        log(f"[supervisor-notice] 台账写盘失败：{exc}")


def _notice_last_read() -> dict:
    try:
        d = json.loads(_notice_last_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _notice_last_write(**fields) -> None:
    st = _notice_last_read()
    for key in _NOTICE_LAST_KEYS:
        st.setdefault(key, None)
    st.update(fields)
    st["updated"] = datetime.now().isoformat(timespec="seconds")
    try:
        p = _notice_last_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except OSError as exc:
        log(f"[supervisor-notice] last.json 写盘失败：{exc}")


def _probe_notice_target(sid: str) -> tuple[bool | None, str]:
    """三态探测，**查不到 ≠ 死了**：`exists is None` 一律 unknown（走 retry_later）。

    探针异常也只降成 unknown：一次网络抖动不该把通知判死。这层 catch 只包**单次探测**，
    不是新腿的业务 try（业务异常必须冲出 `_loop`，否则腿死了而进程心跳照旧 ok）。
    """
    try:
        exists, detail = _probe_session(sid)
    except Exception as exc:  # noqa: BLE001
        return None, f"probe 异常 {type(exc).__name__}: {exc}"
    if exists is None:
        why = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)[:200]
        return None, why or "无法判定目标会话"
    if exists is False:
        return False, "会话不存在（probe=False）"
    if isinstance(detail, dict) and detail.get("archived"):
        return False, "会话已归档（archived=True）"
    return True, ""


# `tools/sess.py:72-75` 的退出码契约，**逐档照抄**——消费端只能与它对齐：
#   0 = 已投递；1 = 上游明确拒绝（未投，可重试）；2 = 参数错（重试无意义）；
#   3 = 不确定（**可能已投、禁止重发**）；4 = 退役清单拦下；5 = daemon 已受理但
#   wire 未见该 promptId（**可能已投**）。
# 3 与 5 都是「可能已经投出去了」：再发一次就可能双发，直接破坏本腿的 at-most-once。
# 所以它们落 `indeterminate`——发过一次、不再重试、终态并出声，把不确定性交给人和
# 心跳去发现，而不是用重发去「赌」。
SEND_RC_DELIVERED = 0
SEND_RC_UPSTREAM_REFUSED = 1
SEND_RC_BAD_ARGS = 2
SEND_RC_INDETERMINATE = (3, 5)
SEND_RC_RETIRED = 4


def _attempt_notice(row: dict, dry: bool) -> tuple[str, str, bool]:
    """尝试投递一行台账，返回 `(state, detail, unknown)`。**绝不使用事件外层 sid**。

    `send` 的退出码按 `tools/sess.py:72-75` 逐档映射（见上方常量）：
    `0`→delivered、`1`→retry_later、`2`→refused_payload、`3|5`→indeterminate、
    `4`→refused_retired。**rc=3/5 绝不重试**：契约写着「可能已投」。

    第三个返回值 `unknown` 只为 `retry_later` 服务：只有「**无法判定**」才该在
    连续 N 轮后升级成 `probe_unknown`。`rc=1` 是**原因明确**的上游拒绝，它也待重试，
    但把它算进 probe 未知会让台账把「对方说不行」记成「我们看不清」。
    """
    sid = str(row.get("sid") or "")
    text = str(row.get("text") or "")
    if not sid.strip() or not text.strip():
        return "refused_payload", "payload 缺 sid/text（事件外层 sid 恒为 file，绝不用它）", False
    exists, why = _probe_notice_target(sid)
    if exists is None:
        return "retry_later", why, True          # 探测无法判定
    if exists is False:
        return "refused_archived", why, False
    if dry:
        log(f"[dry-run] 将投递给 {sid[:14]}…：{text[:60]}")
        return "delivered", "(dry-run 未真发)", False
    try:
        res = _send(sid, text)
    except Exception as exc:  # noqa: BLE001  send 异常＝本轮没投出去，留台账下轮再试
        return "retry_later", f"send 异常 {type(exc).__name__}: {exc}", True
    rc = int(getattr(res, "returncode", 0) or 0)
    tail = (getattr(res, "stderr", "") or getattr(res, "stdout", "") or "").strip()
    if rc == SEND_RC_DELIVERED:
        return "delivered", "", False
    if rc == SEND_RC_RETIRED:
        # sess.py 的退役清单拦截（tools/sess.py:614-623）：拒投**不是** delivered
        return "refused_retired", "send rc=4：退役清单拦截", False
    if rc == SEND_RC_BAD_ARGS:
        # 参数错／正文为空：同样的入参再发一次还是错，不可重试
        return "refused_payload", f"send rc=2：参数错，不可重试 {tail[:160]}", False
    if rc in SEND_RC_INDETERMINATE:
        # **禁止重发**：可能已投。终态 indeterminate，交给人／心跳发现。
        return "indeterminate", (f"send rc={rc}：可能已投、按 sess.py 契约禁止重发 "
                                 f"{tail[:160]}"), False
    if rc == SEND_RC_UPSTREAM_REFUSED:
        return "retry_later", f"send rc=1：上游明确拒绝，可重试 {tail[:160]}", False
    # 契约外的码：不认识就不敢当「已投」也不敢当「没投」——同样按不确定处理
    return "indeterminate", f"send rc={rc}（契约外）：按不确定处理 {tail[:160]}", False


def _inject_notice_failure(row: dict, port: int, detail: str) -> None:
    """出声通道 a1：往 daemon 注入 `event.session.supervisor_delivery_failed`。

    复用 `_emit_supervisor_dead_event` 的同款调用；注入失败要**留一行日志**，
    否则事后看日志会以为「既没告警也没问题」。
    """
    try:
        body = {
            "type": "file",
            "event": SUPERVISOR_FAILED_EVENT,
            "payload": {
                "sid": row.get("sid"),
                "seq": row.get("seq"),
                "kind": row.get("kind"),
                "assignment": row.get("assignment"),
                "state": row.get("state"),
                "reason": str(detail or "")[:300],
                "checked_at": datetime.now().isoformat(timespec="seconds"),
            },
        }
        _event_post("/events/inject", body, port=port or wss_bus().PORT)
    except Exception as exc:  # noqa: BLE001
        log(f"[supervisor-notice] 事件腿不可用（daemon down：{type(exc).__name__}）"
            f"——日志那条腿已写，但 /healthz 看不到本次投递失败")


def _notice_row_from_event(ev: dict) -> dict:
    payload = ev.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    text = payload.get("text")
    text = text if isinstance(text, str) else ""
    return {
        "fp": notice_fingerprint(ev),
        "seq": int(ev.get("seq") or 0),
        "ts": ev.get("ts"),
        "sid": str(payload.get("sid") or ""),
        # 未知形状按 A（可关单）处理：kind 只影响文案，不影响投不投
        "kind": str(payload.get("kind") or "close_ok"),
        "assignment": str(payload.get("assignment") or ""),
        "text": text,             # **完整正文**：重试要发全文
        "text_head": text[:100],  # 给人看的前缀，重试绝不用它
        "attempts": 0,
    }


def _run_notice_row(row: dict, *, dry: bool, port: int, rows: dict) -> str:
    state, detail, unknown = _attempt_notice(row, dry)
    attempts = int(row.get("attempts") or 0) + 1
    if state == "retry_later" and unknown and attempts >= NOTICE_RETRY_LIMIT:
        state = "probe_unknown"
        detail = f"连续 {attempts} 轮无法判定 ⇒ 终态，不再重试：{detail}"
    out = {**row, "state": state, "attempts": attempts,
           "detail": detail,
           "state_at": datetime.now().isoformat(timespec="seconds")}
    _notice_append(out)
    rows[row["fp"]] = out
    if state == "delivered":
        _notice_last_write(last_ok_at=out["state_at"], last_ok_seq=out["seq"])
        log(f"[supervisor-notice] 已投 seq={out['seq']} kind={out['kind']}"
            f" sid={str(out['sid'])[:14]}…")
    elif state != "retry_later":
        _notice_last_write(last_fail_at=out["state_at"], last_fail_state=state,
                           last_fail_seq=out["seq"])
        _inject_notice_failure(out, port, detail)
        log(f"[supervisor-notice] ⚠ seq={out['seq']} 终态 {state}：{detail}")
    else:
        log(f"[supervisor-notice] seq={out['seq']} 本轮未投（{detail}）"
            f"⇒ 存台账，第 {attempts}/{NOTICE_RETRY_LIMIT} 轮")
    return state


def handle_supervisor_notices(evs, now: float | None = None, *,
                              dry: bool = False, port: int = 0,
                              attempted: set | None = None) -> int:
    """消费本轮的 `event.agent.supervisor`，返回实际投出的条数。

    指纹已在台账里 ⇒ 跳过（同 seq 重放、daemon 重启重扫都不重发）。
    `attempted` 是**本轮**已试过的指纹集合，与 `_retry_pending` 共用一个，
    这样一轮只算一轮（`NOTICE_RETRY_LIMIT` 数的是轮，不是调用次数）。
    **不要给它包 try**：腿异常必须冲出 `_loop`，否则「腿死而进程心跳照旧 ok」。
    """
    now = time.time() if now is None else now
    attempted = set() if attempted is None else attempted
    rows = _notice_rows()
    delivered = 0
    for ev in evs or []:
        if str(ev.get("type") or "") != SUPERVISOR_NOTICE_EVENT:
            continue
        fp = notice_fingerprint(ev)
        if fp in rows:
            continue
        row = _notice_row_from_event(ev)
        if dry:
            log(f"[dry-run] 将投递 seq={row['seq']} kind={row['kind']}：{row['text'][:60]}")
            continue
        attempted.add(fp)
        if _run_notice_row(row, dry=dry, port=port, rows=rows) == "delivered":
            delivered += 1
    _notice_tick_tail(rows, delivered)
    return delivered


def _retry_pending(*, dry: bool = False, port: int = 0,
                   attempted: set | None = None) -> int:
    """台账驱动重试：取所有 `state == "retry_later"` 的行逐条重判并重投。

    `/events` 是排他的（只返回 `seq > since`）且 `_loop` 每轮无条件推进游标，
    所以**等事件重投等于永远等不到**——必须从台账里拿回完整 text 再发一次。

    `attempted` 里已有的指纹本轮跳过：同一进程轮里「消费＋重试」各试一次
    会偷偷把 3 轮折成 2 轮就升级。
    """
    attempted = set() if attempted is None else attempted
    rows = _notice_rows()
    delivered = 0
    for fp, row in list(rows.items()):
        if row.get("state") != "retry_later" or fp in attempted:
            continue
        if dry:
            log(f"[dry-run] 台账重试 seq={row.get('seq')}：{str(row.get('text'))[:60]}")
            continue
        attempted.add(fp)
        if _run_notice_row(row, dry=dry, port=port, rows=rows) == "delivered":
            delivered += 1
    _notice_tick_tail(rows, delivered)
    return delivered


def _notice_tick_tail(rows: dict, delivered: int) -> None:
    """收尾：pending_indeterminate 落 last.json（a2）＋ 腿心跳（a3）。"""
    pending = sum(1 for r in rows.values() if r.get("state") == "retry_later")
    # 终态 indeterminate（rc=3/5，可能已投）与「等重试」是两件事：前者已经不能再发，
    # 后者还会再试。心跳分两个计数，免得把「已定的悬案」误读成「还在排队」。
    stuck = sum(1 for r in rows.values() if r.get("state") == "indeterminate")
    _notice_last_write(pending_indeterminate=pending)
    # 未注册时 wss_bus 的 write_heartbeat 直接 return ⇒ 心跳文件不生成；
    # 那种情况已被 notice_registration_problem() 在启动时拦死。
    _write_hb(SUPERVISOR_NOTICE_HEARTBEAT, {"pending_indeterminate": pending,
                                            "indeterminate": stuck,
                                            "delivered": delivered})


def notice_cursor_rollback(cursor_seq: int, daemon_seq: int) -> int | None:
    """游标超前 daemon ⇒ 回退。返回 `delta = cursor - daemon`，否则 None。

    判据用 `cursor > data["seq"]`，**不用 `oldest_seq`**：事件环不持久，重启后为 0。
    恢复动作在 `_loop` 里做：把 `since` 退回 0 做**全量重扫**——**不是**退到
    `daemon_seq`，那反而一条都取不到（`/events?since` 排他，窗口事件的 seq 恰好
    ≤ 现 daemon 的 `seq`）。重扫时已投的靠指纹判重不再发，未投的正好补上。
    只记 `delta` 不重扫＝把窗口内的事件永久丢掉。
    """
    if daemon_seq and int(cursor_seq) > int(daemon_seq):
        delta = int(cursor_seq) - int(daemon_seq)
        log(f"[supervisor-notice] ⚠ 游标回退：cursor={cursor_seq} > "
            f"daemon_seq={daemon_seq}（delta={delta}）——游标归零做全量重扫")
        _notice_last_write(delta=delta,
                           rollback_at=datetime.now().isoformat(timespec="seconds"))
        return delta
    return None


def _extra_guardian_names() -> dict:
    """事件源没有 `guardian_thresholds()` 时的兜底：直接读同语义的注册文件。"""
    try:
        data = json.loads((_state_root() / "guardian_extra.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def notice_registration_problem() -> str:
    """启动断言：心跳名必须已注册，否则**启动即报错**。

    未注册时 `write_heartbeat` 直接 return ⇒ 心跳文件不生成 ⇒ `_guardian_summary()`
    只遍历注册表里的名字 ⇒ `/healthz` 里连 `missing` 都不报，整条腿彻底不可见。
    """
    name = SUPERVISOR_NOTICE_HEARTBEAT
    fn = getattr(_cfg().event_source, "guardian_thresholds", None)
    try:
        names = fn() if callable(fn) else _extra_guardian_names()
    except Exception as exc:  # noqa: BLE001
        return f"读心跳注册表失败（{type(exc).__name__}: {exc}）⇒ 无法确认 {name!r} 可见"
    if isinstance(names, dict) and name in names:
        return ""
    return (f"心跳名 {name!r} 未注册（guardian_thresholds 里没有它）⇒ 该腿心跳不会生成、"
            f"/healthz 里连 missing 都不报；拒绝启动。请先在 state/guardian_extra.json 注册")


# --------------------------------------------------------------------- 收单事件化（issue #15 第一笔）
# 信号源＝the injected ledger file 增量读（字节 offset ＋ 半行缓冲）；消费者＝本进程 `_loop()` 每轮
# 调 `delivery_tick()`。**不动 wss_bus 事件核心**：只往 `/events/inject` 投一条可观测事件，
# `WATCH_TYPES` 一行不改（避免自己消费自己造成双发）。
#
# 状态文件 `delivery_watch_state.json` 必带 `baseline_established`：缺它／读失败／offset<=0／
# 文件缩水 ⇒ **一律重建基线（推 EOF）**，绝不按 offset=0 回放历史（那会重演 2026-11-07 11:08
# 那次「5 个真 PM 同时被刷」的事故）。
DELIVERY_EVENT = "event.delivery_ready"
DELIVERY_HEARTBEAT_S = 600.0        # 每 10 分钟一行心跳（第一笔只保证「可查」，「会叫」在第二笔）
DELIVERY_COOLDOWN_S = 1800.0        # 同 (repo,issue) 冷却
DELIVERY_SUMMARY_DELAY_S = 1800.0   # 同单当日第 2 次起合并，30 分钟后汇总一条
DELIVERY_SEEN_DAYS = 7              # 精确键保留窗口
DELIVERY_MAX_PER_DAY = 3            # 当日 nudge > 3 ⇒ **只停该单**
DELIVERY_ESCALATE_S = 3600.0        # 1h 内无 pm 收据 ⇒ 升级
DELIVERY_PENDING_CHECK_S = 60.0     # 收据/汇总/升级的检查节流
EXIT_FIXTURE_INVALID = 6            # 夹具前置校验失败（--send-mode 的 unknown 预留给 8，勿混）
EXIT_SEND_MODE_UNKNOWN = 8         # --send-mode 无法判定（issue #18 本体，预留位正式启用）
EXIT_SELFTEST_FAILED = 7            # --self-test 有用例失败
EXIT_LOCKED = 2                     # 已有实例在跑（与老址 tools/decide_watch_legacy.py 同值）
DELIVERY_SINK = None                # 自检用：非 None 时只记录不发送（生产恒 None）

# --------------------------------------------------------------------------------------
# 三道夹具闸（issue #15 第一笔退修 U1）
#
# 2026-11-07 14:17 事故：本文件**声称**实现了这三道闸，实际一个都没有（grep 零命中）
# ⇒ QA 的「隔离跑」根本没隔离，一条【收单】真发进了live supervisor session（wire `turnId=206`）。
# 教训：**安全闸必须自己能被验**——`delivery_gate_selftest()` 就是那条反向断言。
#
# 语义（Q1 结论）：**默认通、显式断**
#   · 未设任何变量          ⇒ 生产真发（默认通；绝不因为「某个变量没设」而静默）
#   · DELIVERY_FIXTURE=1    ⇒ 夹具模式：收件人强制换成假 sid，且**必须**同时给 SINK，否则拒绝运行
#   · DELIVERY_SEND_SINK=p  ⇒ 每条本会发出的消息先写 p（sid/ts/via/正文）
#   · DELIVERY_ALLOW_SEND=1 ⇒ sink 存在时，只有显式给这个变量才真发；否则只写 sink
# --------------------------------------------------------------------------------------
DELIVERY_FAKE_PREFIX = "session_5e55a000-"


def gate_fixture() -> bool:
    return (os.environ.get("DELIVERY_FIXTURE") or "").strip() == "1"


def gate_sink() -> str:
    return (os.environ.get("DELIVERY_SEND_SINK") or "").strip()


def gate_allow_send() -> bool:
    return (os.environ.get("DELIVERY_ALLOW_SEND") or "").strip() == "1"


def gate_check() -> str:
    """启动即校验；返回错误描述（空串＝可运行）。"""
    if gate_fixture() and not gate_sink():
        return ("DELIVERY_FIXTURE=1 但未设 DELIVERY_SEND_SINK —— 夹具而不落 sink 等于白测，"
                "且会退化成真发。拒绝运行。")
    return ""


def _fake_sid(name: str) -> str:
    import hashlib
    h = hashlib.md5(str(name).encode("utf-8")).hexdigest()[:12]
    return f"{DELIVERY_FAKE_PREFIX}0000-4000-8000-{h}"


def gate_rewrite(sid: str, name: str = "") -> str:
    """夹具模式下把任何 sid 换成假 sid（**不只是 stub 判活**）。"""
    if not gate_fixture():
        return sid
    if sid.startswith(DELIVERY_FAKE_PREFIX):
        return sid
    return _fake_sid(name or sid)




def delivery_log(msg: str) -> None:
    """收单哨自己的日志（**不写 `decision_watch.log`、不写 `monitor.log`**）。"""
    line = f"{datetime.now().strftime('%F %T')} [DELIVERY] {msg}"
    print(line, flush=True)
    try:
        p = _delivery_log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _norm_dt(value) -> "datetime | None":
    """台账时间戳归一为 **naive 本地时间**。

    台账里有约 5% 的行带 `+08:00`（实测 80 行）；naive 与 aware 直接比较会 `TypeError`，
    而 `pm_watch.parse_iso()` 只认 4 种无时区格式、遇 `+08:00` 返回 `None` ⇒ **静默漏判**
    （比崩更糟）。所以这里统一走 `fromisoformat` 再去掉 tzinfo。
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip()).replace(tzinfo=None)
    except ValueError:
        return None


def _date_of(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _default_state() -> dict:
    return {
        "v": 1,
        "baseline_established": False,
        # 方案 A（issue #15 V1 补修）：默认 **None** 而非 0。
        # 旧写法预填 0 ⇒ `"offset" not in st` 恒为假（死代码）⇒ state 只写
        # `{"baseline_established": true}` 时会**从 0 读、回放全部历史**。
        # 用 None 才能区分「默认值（从未写过）」与「显式写入的 0（空台账，合法）」。
        "offset": None,
        "line_no": 0,
        "pending_partial": "",
        "last_partial": "",
        "seen": {},
        "last_nudge": {},
        "day_nudges": {},
        "breakers": {},
        "pending": {},
        "summaries": {},
        "stats": {},
        "last_heartbeat": 0.0,
        "last_pending_check": 0.0,
        "undelivered_offset": 0,
    }


def load_delivery_state() -> dict:
    st = _default_state()
    try:
        raw = json.loads(_delivery_paths()["state"].read_text(encoding="utf-8"))
    except FileNotFoundError:
        st["_baseline_reason"] = "no_state"
        return st
    except (OSError, ValueError):
        st["_baseline_reason"] = "bad_json"
        return st
    if not isinstance(raw, dict):
        st["_baseline_reason"] = "bad_json"
        return st
    st.update(raw)
    return st


def save_delivery_state(st: dict) -> None:
    p = _delivery_state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        clean = {k: v for k, v in st.items() if not k.startswith("_")}
        tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
    except OSError as exc:
        delivery_log(f"状态写盘失败（不影响本轮）：{exc}")


def _stats(st: dict, now: float) -> dict:
    today = _date_of(now)
    s = st.get("stats") or {}
    if s.get("date") != today:
        if s.get("date"):
            try:
                p = _delivery_paths()["stats"]
                p.parent.mkdir(parents=True, exist_ok=True)
                with p.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(s, ensure_ascii=False) + "\n")
            except OSError:
                pass
        s = {"date": today, "hits": 0, "nudges": 0, "summaries": 0, "dedup_skips": 0,
             "escalations": 0, "undelivered": 0, "false_pos": 0, "missed": 0,
             "parse_errors": 0}
        st["stats"] = s
    return s


def stats_inc(st: dict, key: str, n: int = 1, now: float = 0.0) -> None:
    s = _stats(st, now or time.time())
    s[key] = int(s.get(key) or 0) + n


def stats_roll(st: dict, now: float) -> list[str]:
    """日切：旧日 stats 落盘 ＋ **跨日自动解除全部熔断**（issue #15 N3）。"""
    today = _date_of(now)
    old = (st.get("stats") or {}).get("date")
    if old == today:
        return []
    _stats(st, now)                      # 触发旧日归档 + 新建当日
    tripped = [k for k, v in (st.get("breakers") or {}).items() if not v.get("enabled", True)]
    st["breakers"] = {}
    st["day_nudges"] = {}
    st["summaries"] = {}
    if tripped:
        delivery_log(f"delivery breaker reset (date={today}, was={len(tripped)} issues)")
    return tripped


def _sessions() -> dict:
    try:
        d = json.loads(_delivery_paths()["sessions"].read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _sid_of(db: dict, key: str) -> str:
    val = db.get(key)
    if isinstance(val, dict):
        val = val.get("id") or val.get("session") or ""
    return val.strip() if isinstance(val, str) else ""


def _project_of_repo(repo: str) -> str:
    """`<owner>/<仓名>` → 小写仓名（与 `pm_watch.repo_to_project()` 同口径）。"""
    if not isinstance(repo, str) or "/" not in repo:
        return ""
    return repo.rsplit("/", 1)[-1].strip().lower()


def _bind_name(repo: str, issue) -> tuple[str, str]:
    try:
        db = json.loads(_delivery_paths()["bind"].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unknown", ""
    rec = (db or {}).get(f"{_project_of_repo(repo)}#{issue}") or {}
    w = rec.get("worker") if isinstance(rec, dict) else None
    if not isinstance(w, dict):
        return "unknown", ""
    return str(w.get("name") or "unknown"), str(w.get("session") or "")


def _load_alarm_module():
    """Load the injected alarm recipient module; never import a workbench module."""
    cfg = _cfg()
    mod = cfg.alarm_module or alarm_mod
    if hasattr(mod, "configure"):
        mod.configure(state_root=_state_root(), sessions=SESSIONS(), log=LOG(),
                      state_file=_path("health_state_path", cfg.alarm_state_file),
                      live_root=_state_root(), probe=cfg.probe_session,
                      sess_script=cfg.session_script)
    return mod

def resolve_recipients(repo: str) -> list[tuple[str, str]]:
    """收件人（四级降级链，**每一级都真的能发出去**）。

    ① `pm-<project>` ⇒ ② `supervisor` ⇒ ③ `supervisor_alarm.live_pm_sids()` 的全部活 `pm-*`
    ⇒ ④ 返回空（调用方落 `delivery_undelivered.log` 并由本进程每轮重试，不是死信）。
    """
    db = _sessions()
    out: list[tuple[str, str]] = []
    proj = _project_of_repo(repo)
    pm_key = f"pm-{proj}" if proj else ""
    sid = _sid_of(db, pm_key) if pm_key else ""
    if sid:
        out.append((pm_key, gate_rewrite(sid, pm_key)))
    key = _cfg().supervisor_key
    rsid = _sid_of(db, key)
    if rsid:
        out.append((key, gate_rewrite(rsid, key)))
    if not out:
        try:
            supervisor_alarm = _load_alarm_module()
            out.extend((n, gate_rewrite(s, n)) for n, s in supervisor_alarm.live_pm_sids())
        except Exception as exc:  # noqa: BLE001
            delivery_log(f"[降级] supervisor_alarm 收件人逻辑不可用：{type(exc).__name__}: {exc}")
    return out


def _sink_append(record: dict) -> None:
    """把「本会发出的消息」落到 sink 文件（U1 第二道闸）。

    夹具模式下这是**唯一**的落地方式；生产模式下若也给了 sink，则在真发之前
    先写一行审计（sid/ts/via/正文），便于事后核对「发了什么」。
    """
    path = gate_sink()
    if not path:
        return
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        delivery_log(f"[sink] 写失败（不阻断发送决策）：{exc}")


def delivery_notify(sid: str, text: str, dry: bool) -> bool:
    sid = gate_rewrite(sid)                 # 最后一道保险：任何路径都不许带真 sid 出去
    record = {"ts": time.time(), "sid": sid, "via": "injected send transport", "text": text,
              "dry": bool(dry), "fixture": gate_fixture()}
    if DELIVERY_SINK is not None:            # 自检内注入的内存 sink
        DELIVERY_SINK.append({"sid": sid, "text": text, "dry": dry})
        _sink_append(record)
        return bool(sid)
    if dry:
        _sink_append({**record, "sent": False, "why": "dry"})
        delivery_log(f"[dry-run] 将发给 {sid or '<无收件人>'}：{text}")
        return True
    if not sid:
        delivery_log("无收件人 ⇒ 丢弃")
        return False
    # 夹具模式：只写 sink，绝不 spawn
    if gate_fixture():
        _sink_append({**record, "sent": False, "why": "fixture"})
        delivery_log(f"[fixture] 只写 sink，不发送：{sid[:16]}…")
        return True
    # 生产 + 给了 sink 但没给 ALLOW_SEND ⇒ 只写 sink，不发
    if gate_sink() and not gate_allow_send():
        _sink_append({**record, "sent": False, "why": "sink_without_allow_send"})
        delivery_log(f"[sink] 已记录但未发送（缺 DELIVERY_ALLOW_SEND=1）：{sid[:16]}…")
        return True
    _sink_append({**record, "sent": True})
    try:
        r = _send(sid, text)
    except Exception as exc:  # noqa: BLE001
        delivery_log(f"send 失败：{type(exc).__name__}: {exc}")
        return False
    if r.returncode != 0:
        delivery_log(f"send 失败 rc={r.returncode}：{(r.stderr or r.stdout or '')[:160]}")
        return False
    delivery_log(f"已发 {sid[:14]}…：{text[:60]}")
    return True


def render_delivery_text(rec: dict) -> str:
    return (f"【收单】{rec['project']}#{rec['issue']} 有交付："
            f"worker={rec['worker_name']}，证据={rec['evidence']}"
            f"（ledger 第 {rec['ledger_line']} 行），"
            f"{datetime.fromtimestamp(rec['detected_ts']).strftime('%H:%M:%S')} 落盘。"
            f"请收：python tools/gh_issue.py show --project {rec['project']} --issue {rec['issue']}")


def _inject(etype: str, payload: dict) -> None:
    """可观测（进 daemon 事件环）；**不承担触发**，失败只记日志。"""
    if DELIVERY_SINK is not None or os.environ.get("DELIVERY_NO_INJECT"):
        return
    try:
        _event_post("/events/inject", {"type": "file", "event": etype, "payload": payload},
                   port=wss_bus().PORT)
    except Exception as exc:  # noqa: BLE001
        delivery_log(f"[可观测] 注入事件失败（daemon down？{type(exc).__name__}）：{etype}")


def _trip_breaker(st: dict, key: str, now: float, dry: bool) -> None:
    st.setdefault("breakers", {})[key] = {"date": _date_of(now), "count": DELIVERY_MAX_PER_DAY + 1,
                                          "enabled": False}
    delivery_log(f"breaker tripped: {key} 当日第 {DELIVERY_MAX_PER_DAY + 1} 次交付提醒 ⇒ 该单停发至次日 0 点")
    text = (f"【收单·熔断】{key} 今日交付提醒已超 {DELIVERY_MAX_PER_DAY} 次，"
            f"该单自动停发至次日 0 点（跨日自动解除）。")
    for _name, sid in resolve_recipients(key.split("#")[0]):
        if delivery_notify(sid, text, dry):
            break


def _summary_add(st: dict, key: str, rec: dict, now: float) -> None:
    buf = st.setdefault("summaries", {}).setdefault(key, {"flush_at": now + DELIVERY_SUMMARY_DELAY_S,
                                                          "items": [], "date": _date_of(now)})
    buf["items"].append({"evidence": rec["evidence"], "line": rec["ledger_line"],
                         "worker": rec["worker_name"]})
    delivery_log(f"合并进摘要 {key}：{rec['evidence']}（当日第 2 次起不单独打扰）")


def _send_delivery(st: dict, rec: dict, dry: bool, now: float) -> int:
    key = f"{rec['repo']}#{rec['issue']}"
    text = render_delivery_text(rec)
    targets = resolve_recipients(rec["repo"])
    ok = False
    for _name, sid in targets:
        if delivery_notify(sid, text, dry):
            ok = True
            break
    if not ok:
        stats_inc(st, "undelivered", now=now)
        _append_undelivered(st, rec, now)
        delivery_log(f"未投递（无可用收件人）⇒ 落 undelivered 队列，下轮重试：{key} {rec['evidence']}")
        return 0
    st.setdefault("last_nudge", {})[key] = now
    stats_inc(st, "nudges", now=now)
    st.setdefault("pending", {})[rec["dl_id"]] = {
        "repo": rec["repo"], "issue": rec["issue"], "project": rec["project"],
        "evidence": rec["evidence"], "worker_name": rec["worker_name"],
        "nudge_ts": now, "escalated": False,
    }
    _inject(DELIVERY_EVENT, {**{k: rec[k] for k in
                              ("v", "project", "repo", "issue", "worker_name",
                               "worker_session", "pm_key", "pm_sid", "kind",
                               "evidence", "ledger_line")},
                            "detected_at": datetime.fromtimestamp(rec["detected_ts"]).isoformat(timespec="seconds"),
                            "nudged": True, "err": ""})
    return 1


def _append_undelivered(st: dict, rec: dict, now: float) -> None:
    try:
        p = _delivery_paths()["undelivered"]
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"t": now, "enqueued_ts": now, "rec": rec},
                                ensure_ascii=False) + "\n")
    except OSError:
        pass


def _ledger_rows() -> list[dict]:
    """全量台账行（供收据判定）；按 (size, mtime) 缓存，避免每分钟读 11MB。"""
    p = _delivery_paths()["ledger"]
    try:
        stat = p.stat()
    except OSError:
        return []
    cache = _LEDGER_CACHE
    if cache.get("key") == (stat.st_size, stat.st_mtime_ns):
        return cache.get("rows") or []
    rows: list[dict] = []
    try:
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    rows.append(rec)
    except OSError:
        return []
    _LEDGER_CACHE.update({"key": (stat.st_size, stat.st_mtime_ns), "rows": rows})
    return rows


_LEDGER_CACHE: dict = {}


def has_pm_receipt(repo: str, issue, after_ts: float, within_s: float = DELIVERY_ESCALATE_S) -> bool:
    """收据判据（可机判）：该单有 `role=pm` 且 `act ∈ {comment,label,close}` 的行落在
    `(after_ts, after_ts+within_s]`。

    **排除 `bind`**：实测 bind 占「首次动作」约 30%，把它算作收据会让升级永远响不了
    （收单哨退化成「只会更吵、不会更响」）。
    """
    for rec in _ledger_rows():
        if str(rec.get("role") or "").lower() != "pm":
            continue
        if rec.get("repo") != repo or rec.get("issue") != issue:
            continue
        if str(rec.get("act") or "").lower() not in {"comment", "label", "close"}:
            continue
        t = _norm_dt(rec.get("t"))
        if t is None:
            continue
        delta = t.timestamp() - after_ts
        if 0 < delta <= within_s:
            return True
    return False


def _prune(st: dict, now: float) -> None:
    cutoff = now - DELIVERY_SEEN_DAYS * 86400
    seen = st.get("seen") or {}
    st["seen"] = {k: v for k, v in seen.items() if float(v or 0) >= cutoff}


def enqueue_delivery(st: dict, rec: dict, *, dry: bool, now: float,
                     no_cooldown: bool = False, no_merge: bool = False,
                     breaker_off: bool = False) -> int:
    key = f"{rec['repo']}#{rec['issue']}"
    skey = f"{key}#{rec['evidence']}"
    stats_inc(st, "hits", now=now)
    if skey in (st.get("seen") or {}):
        stats_inc(st, "dedup_skips", now=now)
        return 0
    st.setdefault("seen", {})[skey] = now
    _prune(st, now)

    today = _date_of(now)
    day = (st.setdefault("day_nudges", {}).get(key) or {})
    if day.get("date") != today:
        day = {"date": today, "count": 0}
        st["day_nudges"][key] = day

    br = (st.get("breakers") or {}).get(key) or {}
    tripped = br.get("date") == today and br.get("enabled") is False
    if tripped and not breaker_off:
        stats_inc(st, "dedup_skips", now=now)
        delivery_log(f"熔断中，跳过 {key} {rec['evidence']}")
        return 0

    day["count"] = int(day.get("count") or 0) + 1
    if day["count"] > DELIVERY_MAX_PER_DAY and not breaker_off:
        _trip_breaker(st, key, now, dry)
        stats_inc(st, "dedup_skips", now=now)
        return 0

    last = float((st.get("last_nudge") or {}).get(key) or 0)
    if not no_cooldown and last and now - last < DELIVERY_COOLDOWN_S:
        _summary_add(st, key, rec, now)
        stats_inc(st, "dedup_skips", now=now)
        return 0

    if not no_merge and int(day["count"]) > 1:
        _summary_add(st, key, rec, now)
        stats_inc(st, "dedup_skips", now=now)
        return 0

    return _send_delivery(st, rec, dry, now)


def scan_deliveries(st: dict, *, dry: bool, now: float, force_replay: bool = False,
                    no_cooldown: bool = False, no_merge: bool = False,
                    breaker_off: bool = False) -> int:
    """增量读台账，产出/发送「交付」提醒。返回本轮**实际发送**条数。"""
    p = _delivery_paths()["ledger"]
    if not p.is_file():
        return 0
    try:
        size = p.stat().st_size
    except OSError:
        return 0

    # ---- U2（issue #15 第一笔退修）：把「是否需要重建基线」与「offset 是否为 0」**解耦**
    #
    # 旧实现把 `offset <= 0` 当「状态坏了」⇒ 空台账（或 logrotate 截断到 0）建完基线后
    # offset 恒为 0 ⇒ **每一轮都重建、期间新行全吞**，且重启也不修（状态文件里
    # baseline_established 一直是 true，但判据每次都先命中 offset_zero）。这是「永久失聪」。
    #
    # 新判据：
    #   · 缺 `baseline_established`／JSON 坏／文件不存在     ⇒ 重建（真·损坏）
    #   · `offset` 键**缺失**或不是非负整数                  ⇒ 重建（真·损坏）
    #   · `size < offset`（文件缩水／被截断）                ⇒ 重建**一次**，之后 offset=size 正常增量
    #   · `offset == 0` 且 `size == 0`（空台账）             ⇒ **正常状态**，不重建
    #   · `offset == 0` 且 `size > 0`（首部署后追加了行）     ⇒ 正常从 0 读
    #
    # 「首次部署空文件」与「状态损坏」的区分靠的是 **offset 键在不在**，不是它的值。
    reason = ""
    off_raw = st.get("offset")
    # V1 补修（方案 A）：`offset` 默认为 **None**，所以 `is None` 能真正区分
    # 「从未写过 offset」与「显式写入的 0（空台账，合法）」。旧写法 `"offset" not in st`
    # 是死代码——`_default_state()` 预填了 0，该键恒存在。
    off_ok = (isinstance(off_raw, int) and not isinstance(off_raw, bool) and off_raw >= 0)
    if not st.get("baseline_established"):
        reason = st.get("_baseline_reason") or "no_flag"
    elif off_raw is None or not off_ok:
        reason = "offset_invalid"
    elif size < int(off_raw):
        reason = "shrunk"

    if reason:
        st["offset"] = size
        st["baseline_established"] = True
        st["pending_partial"] = ""
        st["seen"] = {}
        st["pending"] = {}
        delivery_log(f"baseline rebuilt (reason={reason}) offset={size}")
        return 0

    off = 0 if force_replay else int(off_raw)
    if size == off and not force_replay:
        return 0
    if size < off:
        return 0
    try:
        with p.open("rb") as fh:
            fh.seek(off)
            chunk = fh.read(size - off)
    except OSError:
        return 0
    text = ("" if force_replay else (st.get("pending_partial") or "")) + chunk.decode("utf-8", "replace")
    pieces = text.split("\n")
    partial = pieces.pop()
    st["pending_partial"] = partial
    if partial and partial != (st.get("last_partial") or ""):
        st["last_partial"] = partial
        stats_inc(st, "parse_errors", now=now)     # 尾部半行（未成行）——计数但**不消费**
    if force_replay:
        st["line_no"] = 0
    st["offset"] = size

    sent = 0
    for i, raw in enumerate(pieces):
        st["line_no"] = int(st.get("line_no") or 0) + 1
        line_no = st["line_no"]
        raw = raw.strip()
        if not raw:
            continue
        try:
            row = json.loads(raw)
        except ValueError:
            stats_inc(st, "parse_errors", now=now)
            continue
        if not isinstance(row, dict):
            continue
        if str(row.get("role") or "").lower() != "worker":
            continue
        if str(row.get("act") or "").lower() != "comment":
            continue
        if row.get("ok") is not True:
            continue
        repo = str(row.get("repo") or "")
        issue = row.get("issue")
        if not repo or issue is None:
            continue
        note = str(row.get("note") or "")
        evidence = note if note.startswith("cid:") else f"ledger:{line_no}"
        worker_name, worker_sid = _bind_name(repo, issue)
        proj = _project_of_repo(repo)
        pm_sid = _sid_of(_sessions(), f"pm-{proj}") if proj else ""
        rec = {
            "v": 1, "project": proj, "repo": repo, "issue": issue,
            "worker_name": worker_name, "worker_session": worker_sid,
            "pm_key": f"pm-{proj}" if proj else "", "pm_sid": pm_sid,
            "kind": "comment", "evidence": evidence, "ledger_line": line_no,
            "detected_ts": now, "dl_id": f"dl-{line_no}",
        }
        sent += enqueue_delivery(st, rec, dry=dry, now=now, no_cooldown=no_cooldown,
                                 no_merge=no_merge, breaker_off=breaker_off)
    return sent


def check_pending(st: dict, *, dry: bool, now: float) -> dict:
    """① 汇总（同单当日第 2 次起）② 1h 无收据 ⇒ 升级。"""
    out = {"summaries": 0, "escalations": 0}
    for key, buf in list((st.get("summaries") or {}).items()):
        if float(buf.get("flush_at") or 0) > now or not buf.get("items"):
            continue
        items = buf.get("items") or []
        text = (f"【收单·汇总】{key} 今日另有 {len(items)} 条交付未单独提醒："
                + "、".join(f"{it.get('evidence')}" for it in items[:5])
                + ("…" if len(items) > 5 else "") + "。请一并收取。")
        for _name, sid in resolve_recipients(key.split("#")[0]):
            if delivery_notify(sid, text, dry):
                out["summaries"] += 1
                stats_inc(st, "summaries", now=now)
                break
        st["summaries"].pop(key, None)

    for dl_id, rec in list((st.get("pending") or {}).items()):
        if rec.get("escalated"):
            continue
        nudge_ts = float(rec.get("nudge_ts") or 0)
        if now - nudge_ts < DELIVERY_ESCALATE_S:
            continue
        if has_pm_receipt(rec["repo"], rec["issue"], nudge_ts):
            rec["escalated"] = True          # 已收，标记后不再检查
            continue
        text = (f"【收单·升级】{rec['project']}#{rec['issue']} 的交付提醒（{rec['evidence']}，"
                f"{datetime.fromtimestamp(nudge_ts).strftime('%H:%M:%S')} 发出）"
                f"60 分钟内该单无任何 pm comment/label/close 台账行，疑似空转，请处置。")
        for _name, sid in resolve_recipients(rec["repo"]):
            if delivery_notify(sid, text, dry):
                out["escalations"] += 1
                stats_inc(st, "escalations", now=now)
                break
        rec["escalated"] = True
        delivery_log(f"升级：{rec['project']}#{rec['issue']} {rec['evidence']}（1h 无收据）")
    return out


def retry_undelivered(st: dict, *, dry: bool, now: float) -> int:
    """`delivery_undelivered.log` 的**读取方**（不是死信）：每轮重试未投递的记录。

    **P1-1（退修）**：同一 tick 内刚落队列的条目不参与本轮重试——否则
    `scan_deliveries` 落队列后本函数立刻又读又发，同一轮发两次
    （实测 `stats.undelivered=2`、日志 2 条、队列 2 行）。
    判据：只取 `tick_started` **之前**入队的条目。
    """
    p = _delivery_paths()["undelivered"]
    try:
        size = p.stat().st_size
    except OSError:
        return 0
    off = int(st.get("undelivered_offset") or 0)
    if size <= off:
        return 0
    try:
        with p.open("rb") as fh:
            fh.seek(off)
            chunk = fh.read(size - off)
    except OSError:
        return 0
    pieces = chunk.decode("utf-8", "replace").split("\n")
    partial = pieces.pop()
    tick_started = float(st.pop("_tick_started", now))
    sent = 0
    for raw in pieces:
        raw = raw.strip()
        if not raw:
            continue
        try:
            item = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        if float(item.get("enqueued_ts") or item.get("t") or 0) >= tick_started:
            continue                       # 本轮才入队 ⇒ 下一轮再试
        rec = (item or {}).get("rec") or {}
        if not isinstance(rec, dict) or not rec.get("repo"):
            continue
        if _send_delivery(st, {**rec, "detected_ts": rec.get("detected_ts") or now}, dry, now):
            sent += 1
    st["undelivered_offset"] = size - len(partial.encode("utf-8"))
    return sent


def _write_hb(name: str, extra: dict | None = None) -> None:
    """写**本模块专属**的守护心跳（G-2/G-3：一路一个文件，唯一写者）。

    只在本进程未隔离时写——隔离实例不该污染生产心跳目录；
    隔离态下写到自己 state root 里也无妨（the injected heartbeat bridge 指向同一 root）。
    """
    try:
        _write_heartbeat(name, extra)
    except Exception as exc:  # noqa: BLE001  心跳失败不拖垮收单主流程
        print(f"decide_watch: heartbeat failed: {type(exc).__name__}: {exc}", flush=True)


def delivery_tick(*, dry: bool, now: float | None = None) -> dict:
    """主循环每轮调用：日切 → 扫描 → 汇总/升级 → 未投递重试 → 心跳。"""
    now = time.time() if now is None else now
    st = load_delivery_state()
    st["_tick_started"] = now                 # P1-1：本轮入队的不参与本轮 retry
    stats_roll(st, now)
    sent = scan_deliveries(st, dry=dry, now=now)
    pend = {"summaries": 0, "escalations": 0}
    if now - float(st.get("last_pending_check") or 0) >= DELIVERY_PENDING_CHECK_S:
        st["last_pending_check"] = now
        pend = check_pending(st, dry=dry, now=now)
    retried = retry_undelivered(st, dry=dry, now=now)
    if now - float(st.get("last_heartbeat") or 0) >= DELIVERY_HEARTBEAT_S:
        st["last_heartbeat"] = now
        delivery_log(f"scan ok offset={st.get('offset')} pending={len(st.get('pending') or {})}")
    # G-2：收单哨自己的心跳（与 decide_watch 进程心跳分开，各自阈值、独立告警）
    _write_hb("delivery_watch", {"offset": st.get("offset"),
                                 "pending": len(st.get("pending") or {}),
                                 "sent": sent})
    if not dry:
        save_delivery_state(st)
    return {"sent": sent, **pend, "retried": retried,
            "offset": st.get("offset"), "pending": len(st.get("pending") or {})}


def send_mode(dry_run_hint: bool = False) -> tuple[str, str, int]:
    """三态自证（issue #18 本体）：返回 `(mode, 说明, 退出码)`。

    **只读**：不 mkdir、不写 state、不走 `log()`（R3 约束）。
    **必须能被活体持锁现场调用**：因此它排在 `main()` 最前，先于 `acquire_lock()`。

    三态判据（与生产语义一一对应）：
      fixture —— `DELIVERY_FIXTURE=1`（假 sid ＋ sink，绝不外发）
      dry     —— 给了 sink 但没给 `DELIVERY_ALLOW_SEND=1`，**或**命令行 `--dry-run`
      real    —— 其它（生产默认真发）
      unknown —— 读环境异常 / 三态无法判定（**不静默当 real**）
    """
    try:
        fx = gate_fixture()
        sink = gate_sink()
        allow = gate_allow_send()
    except Exception as exc:  # noqa: BLE001
        return "unknown", f"读环境失败：{type(exc).__name__}: {exc}", EXIT_SEND_MODE_UNKNOWN
    if fx:
        return "fixture", f"(fixture=1, sink={'已设' if sink else '未设'}) ⇒ 假 sid＋只写 sink", 0
    if dry_run_hint:
        return "dry", "(--dry-run) ⇒ 只打印不发送", 0
    if sink and not allow:
        return "dry", "(sink 已设但未设 ALLOW_SEND=1) ⇒ 只写 sink 不发送", 0
    if sink and allow:
        return "real", "(sink＋ALLOW_SEND=1) ⇒ 真发并留审计", 0
    return "real", "(未设 fixture/sink) ⇒ 生产真发", 0


def print_send_mode(dry_run_hint: bool = False) -> int:
    mode, why, rc = send_mode(dry_run_hint)
    print(f"send_mode={mode}")
    print(why)
    return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="跑收单哨 A–O 十五项自检（隔离 TemporaryDirectory，全程 dry，绝不外发）")
    ap.add_argument("--send-mode", dest="send_mode", action="store_true",
                    help="只读打印发送模式（real|fixture|dry|unknown），不加锁、不写盘")
    ap.add_argument("--port", type=int, default=0, help="daemon 端口（默认取 WSS_BUS_PORT/58800）")
    ap.add_argument("--wait", type=float, default=20.0, help="长轮询等待秒数")
    ap.add_argument("--once", action="store_true", help="只跑一轮就退出")
    ap.add_argument("--dry-run", action="store_true", help="只打印不发送")
    ap.add_argument("--no-cursor", action="store_true",
                    help="忽略游标（反例用：证明首轮不设 since 会刷屏）")
    ap.add_argument("--supervisor", default="", help="覆盖 supervisor sid（测试用）")
    ap.add_argument("--self-check", dest="self_check", action="store_true",
                    help="只检查调用方注入是否完整，不跑轮询")
    ap.add_argument("--max-age", type=float, default=MAX_AGE_S)
    ap.add_argument("--force", action="store_true",
                    help="越过单实例锁（救场用；会打印醒目警告）")
    ap.add_argument("--install-autostart", dest="install_autostart", action="store_true",
                    help="注册登录自启（默认 startup 后端＝零提权；--backend scheduled 需 UAC）")
    ap.add_argument("--uninstall-autostart", dest="uninstall_autostart", action="store_true",
                    help="卸载登录自启（回读确认）")
    ap.add_argument("--backend", default=autostart.DEFAULT_BACKEND,
                    choices=list(autostart.BACKENDS),
                    help="自启后端：startup（默认，零提权，写启动文件夹）| scheduled（需 UAC）")
    args = ap.parse_args(argv)
    if args.self_check:
        return self_check()
    if args.send_mode:
        # issue #18 本体：只读查询，**不加锁**，且必须在任何 state 操作之前
        return print_send_mode(dry_run_hint=bool(args.dry_run))
    if args.self_test:
        return self_test()
    missing = missing_injections()
    if missing:
        print(f"[decide_watch] 拒绝运行：未注入 {', '.join(missing)}", file=sys.stderr)
        return 9
    # 夹具闸：**任何入口**（含 --once）都要先过，绝不静默退化成真发
    gate_err = gate_check()
    if gate_err:
        print(f"[decide_watch] 拒绝运行：{gate_err}", file=sys.stderr)
        delivery_log(f"拒绝运行：{gate_err}")
        return EXIT_FIXTURE_INVALID
    if args.install_autostart:
        return install_autostart(dry_run=args.dry_run, backend=args.backend)
    if args.uninstall_autostart:
        return uninstall_autostart(backend=args.backend, dry_run=args.dry_run)
    # ISS-55 ③：心跳名未注册 ⇒ 这条腿在 `/healthz` 里彻底不可见（连 missing 都不报）。
    # 必须启动即报错，不带病起跑。注册动作由主管在 state/guardian_extra.json 亲手做。
    reg_problem = notice_registration_problem()
    if reg_problem:
        print(f"[decide_watch] 拒绝运行：{reg_problem}", file=sys.stderr)
        delivery_log(f"拒绝运行：{reg_problem}")
        return EXIT_NOTICE_UNREGISTERED
    port = args.port or wss_bus().PORT

    cur = {} if args.no_cursor else load_cursor()
    since = int(cur.get("seq") or 0)
    first = since <= 0
    boot_now = ""
    h = health(port)
    if not h:
        log(f"daemon 不可用（port={port}）⇒ 拍板哨主路径不可用，"
            f"请用调用方注入的 fallback poller")
    else:
        boot_now = str(h.get("boot_id") or "")
        if first:
            # 首轮不发送：把游标推到「当前末尾」，不为存量事件补发通知
            since = int(h.get("seq") or 0)
            log(f"首轮：游标起点 seq={since}（存量事件不补发）")
            save_cursor(since, boot_now)
        elif cur.get("boot_id") and boot_now and cur["boot_id"] != boot_now:
            log(f"daemon 重启（boot {cur['boot_id']} → {boot_now}）⇒ 全量重扫一次并告警")
            since = 0
    ok, why = acquire_lock(force=args.force)
    if not ok:
        return EXIT_LOCKED
    log(f"decide_watch 起：port={port} since={since} wait={args.wait}s"
        f"{' [dry-run]' if args.dry_run else ''}")

    backoff = BACKOFF_MIN
    try:
        return _loop(args, port, since, boot_now, backoff)
    finally:
        release_lock()


def _loop(args, port: int, since: int, boot_now: str, backoff: float) -> int:
    """主消费循环（抽出以便 main 用 try/finally 统一释放锁）。"""
    while True:
        # issue #16 退修 P1-3：健康核必须在**循环入口**跑——上一版挂在 fetch_events
        # 之后，daemon 不可达时直接 continue，永远走不到核（QA 用 --port 59999 实测零行）。
        # 「daemon 挂了」恰恰是最需要知道 supervisor 键死没死的时候。
        check_supervisor_health(port)
        # G-2：decide_watch **进程本身**的心跳（与收单哨分开，各自阈值、独立告警）
        _write_hb("decide_watch", {"loop": True})
        try:                       # issue #15 第一笔：收单哨（异常不拖垮拍板哨主循环）
            delivery_tick(dry=bool(getattr(args, "dry_run", False)))
        except Exception as exc:   # noqa: BLE001
            delivery_log(f"delivery_tick 异常（不阻断主循环）：{type(exc).__name__}: {exc}")
        data = fetch_events(since, args.wait, port)
        if data is None:
            # 退避序列 1,2,4,8,16,30：**先睡当前值再翻倍**（先翻倍会让第一轮睡 2s，
            # 与验收要求的起点 1 不符）。
            if args.once:
                return 1
            log(f"daemon 不可达，{backoff:.1f}s 后重试；"
                f"若长期不可用请用 the injected fallback poller fallback")
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)
            continue
        backoff = BACKOFF_MIN
        # ISS-55 P1-A：游标超前 daemon ⇒ **必须真的退回去重扫**，只记 delta 会把
        # 回退窗口里的事件永久丢掉。
        #
        # 退回的**不是** `daemon_seq`：`/events?since` 是排他的（只给 `seq > since`），
        # 而窗口里那些事件（`daemon_seq+1 .. old_cursor`）的 seq 恰恰 ≤ 现 daemon 的
        # `seq`，以 `daemon_seq` 为下界一条都取不到——那等于换个姿势再丢一次。
        # 所以退回 0 做**全量重扫**（与上面「早于环形缓冲起点 ⇒ since = 0」同一套处置）：
        # 环只留最近 N 条，重扫成本有限；已投过的靠**指纹判重**一条都不会重发，
        # 没投过的正好补上。这就是 P1-A 要的「既不重发、也不丢」。
        delta = notice_cursor_rollback(since, int(data.get("seq") or 0))
        if delta is not None:
            since = 0          # 不落盘：进程若在此重启，main() 会再判一次回退，不丢事件
            continue
        oldest = int(data.get("oldest_seq") or 0)
        dropped = data.get("dropped") or []
        if since and oldest and since + 1 < oldest:
            log(f"游标 seq={since} 早于环形缓冲起点 {oldest} ⇒ 有事件被淘汰，全量重扫一次")
            since = 0
            continue
        if dropped:
            log(f"daemon 报了丢失区间（{dropped[-1]}）⇒ 已越过，继续")
        evs = data.get("events") or []
        rsid = supervisor_sid(args.supervisor)
        handle(evs, rsid, args.dry_run, args.max_age)
        # ISS-55：主管投递哨独立一腿。**落在上面那个 try 之外**（方案 A8）——
        # 它抛异常就该冲出 `_loop` → `finally: release_lock()` → 进程退出 → 心跳消失，
        # 这样「腿死了」可被发现；再包一层 try 就重演「腿死而进程心跳照旧 ok」。
        # 回退已在上面处理（退回 daemon_seq 重扫）；已投过的靠指纹判重，绝不会因回退重发。
        attempted: set = set()          # 一轮 = 一次「消费＋重试」，升级按轮数算
        handle_supervisor_notices(evs, dry=args.dry_run, port=port,
                                  attempted=attempted)
        _retry_pending(dry=args.dry_run, port=port, attempted=attempted)
        nxt = int(data.get("next_seq") or (since + 1))
        if evs:
            since = max(int(e.get("seq") or since) for e in evs)
        else:
            since = max(since, nxt - 1)
        boot_now = str(data.get("boot_id") or boot_now)
        save_cursor(since, boot_now)
        if args.once:
            return 0




# --------------------------------------------------------------------- 自启（issue #17 项 7）
# 复用 the injected autostart implementation（994ca72 的 pythonw + 0bc089e 的回读验证）：
#   · 动作程序必须 `pythonw.exe`（无窗口）；拿不到就降级并**明确告警**，不假装。
#   · 注册后 `Get-ScheduledTask` 回读 Execute/Arguments/WorkingDirectory，
#     不符即非零退出（不能只信自己打印的 registered）。
AUTOSTART_NAME = lambda: _cfg().autostart_name
EXIT_REGISTER_FAILED = 3      # 注册动作本身失败（多为权限不足，需 UAC）
EXIT_VERIFY_FAILED = 4        # 注册返回成功但回读校验不通过
EXIT_UNINSTALL_FAILED = 5     # 卸载后仍能查到任务
EXIT_OK = 0                    # 成功（issue #18：与 autostart 模块同口径）
EXIT_STARTUP_INSTALL_FAILED = autostart.EXIT_STARTUP_INSTALL_FAILED   # 10（issue #18，QA18-3：8 让给 --send-mode）
EXIT_STARTUP_UNINSTALL_FAILED = autostart.EXIT_STARTUP_UNINSTALL_FAILED  # 11


def _this_spec() -> autostart.Spec:
    """本进程的自启定义（startup 后端用；两个后端共用同一份 name/script/workdir）。"""
    exe, note = _windowless()
    return autostart.Spec(name=AUTOSTART_NAME(), script=Path(__file__).resolve(),
                          args=[], workdir=_workspace_root(), execute_note=note)


def _norm_backend(backend: str) -> str:
    """归一化后端名；空＝默认 startup（issue #18）。"""
    b = (backend or "").strip().lower()
    if not b:
        return autostart.DEFAULT_BACKEND
    if b not in autostart.BACKENDS:
        print(f"[autostart] 未知后端 {backend!r}（可选：{'/'.join(autostart.BACKENDS)}）",
              file=sys.stderr)
        return autostart.DEFAULT_BACKEND
    return b


def startup_install(dry_run: bool = False) -> int:
    """startup 后端安装（幂等 ＋ 回读校验 ＋ 失败非零）。"""
    spec = _this_spec()
    exe, note = _windowless()
    if note:
        print(f"[autostart] 警告：{note}", file=sys.stderr)
    text = autostart.render_vbs(spec, exe)
    path = spec.startup_file
    if dry_run:
        print(f"[autostart] backend=startup（干跑，未落盘）")
        print(f"  target : {path}")
        print(f"  encode : UTF-16LE + BOM")
        print("  content:")
        for ln in text.splitlines():
            print("    " + ln)
        exists = path.is_file()
        print(f"[autostart] 当前状态：{'已存在' if exists else '未安装'}")
        return EXIT_OK

    if path.is_file():
        try:
            old = autostart.read_vbs(path)
        except OSError as exc:
            old = ""
            print(f"[autostart] 读取现有文件失败（将覆盖）：{exc}", file=sys.stderr)
        if old == text:
            print("[autostart] 已安装（内容一致，跳过）")
            return EXIT_OK
        print("[autostart] 已安装但内容不一致 ⇒ 覆盖，变更点：")
        for d in autostart.diff_summary(old, text):
            print("  " + d)

    try:
        autostart.write_vbs(path, text)
    except OSError as exc:
        print(f"[autostart] 写盘失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_STARTUP_INSTALL_FAILED

    fails = autostart.verify_vbs(spec, exe)
    if fails:
        print("[autostart] 写入后回读校验失败：", file=sys.stderr)
        for f in fails:
            print("  x " + f, file=sys.stderr)
        return EXIT_STARTUP_INSTALL_FAILED
    print(f"[autostart] 安装成功并已回读验证：{path}")
    return EXIT_OK


def startup_uninstall(dry_run: bool = False) -> int:
    """startup 后端卸载（幂等 ＋ 回读查无）。

    QA18-4：`--uninstall-autostart --dry-run` 曾**仍真删**。现在 dry_run
    只打印将删除的路径，不落盘。
    """
    spec = _this_spec()
    path = spec.startup_file
    if dry_run:
        print("[autostart] backend=startup（干跑，不删除）")
        print(f"  would remove : {path}")
        print(f"[autostart] 当前状态：{'存在（真跑会删）' if path.is_file() else '不存在（无需删）'}")
        return EXIT_OK
    if not path.is_file():
        print("[autostart] 未安装（文件不存在），无需卸载")
        return EXIT_OK
    try:
        path.unlink()
    except OSError as exc:
        print(f"[autostart] 删除失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_STARTUP_UNINSTALL_FAILED
    if path.is_file():
        print(f"[autostart] 卸载后文件仍存在：{path}", file=sys.stderr)
        return EXIT_STARTUP_UNINSTALL_FAILED
    print(f"[autostart] 已卸载（回读确认查无）：{path}")
    return EXIT_OK


def _ps_json(script: str, timeout: float = 60.0):
    """跑 PowerShell 并把 stdout 解析成 JSON（复用 wss_bus 的实现）。"""
    return _ps_bridge(script, timeout)


def windowless_python() -> tuple[str, str]:
    return _windowless()


def task_spec() -> dict:
    """本消费者的计划任务定义（单一来源：注册与回读校验都用它）。"""
    exe, note = _windowless()
    cfg = _cfg()
    script = str(cfg.startup_script or Path(__file__).resolve())
    return {
        "name": cfg.autostart_name,
        "execute": exe,
        "arguments": '"%s"' % script,
        "workdir": str(BASE),
        "script": script,
        "execute_note": note,
        "execute_is_windowless": not note,
    }


def query_task():
    """回读本任务；不存在返回 None。"""
    ps = (
        "$ErrorActionPreference='Stop';"
        "$t = Get-ScheduledTask -TaskName '%s' -ErrorAction SilentlyContinue;"
        "if (-not $t) { Write-Output 'null'; exit 0 };"
        "$a = $t.Actions[0];"
        "$o = [ordered]@{ name=$t.TaskName; state=[string]$t.State;"
        " execute=$a.Execute; arguments=$a.Arguments; workdir=$a.WorkingDirectory };"
        "Write-Output ($o | ConvertTo-Json -Compress)"
    ) % AUTOSTART_NAME()
    rc, obj, _raw = _ps_json(ps)
    return obj if rc == 0 and isinstance(obj, dict) else None


def verify_task(spec: dict) -> list[str]:
    """校验已注册任务等于 spec；返回失败项（空＝全过）。WorkingDirectory 不可读时降级。"""
    actual = query_task()
    if not actual:
        return ["任务未注册（Get-ScheduledTask 查无）"]
    fails = []
    if str(actual.get("execute") or "").strip().lower() != spec["execute"].strip().lower():
        fails.append("Execute 不符：期望 %r，实际 %r" % (spec["execute"], actual.get("execute")))
    args = str(actual.get("arguments") or "")
    if spec["script"] not in args:
        fails.append("Arguments 缺脚本路径：期望含 %r，实际 %r" % (spec["script"], args))
    wd = actual.get("workdir")
    if wd is None or str(wd).strip() == "":
        fails.append("__workdir_unverifiable__")
    elif str(wd).strip().lower() != spec["workdir"].strip().lower():
        fails.append("WorkingDirectory 不符：期望 %r，实际 %r" % (spec["workdir"], wd))
    return fails


def print_spec(spec: dict) -> None:
    """干跑输出（第 7 项要求：不注册也要能看清将建什么）。"""
    print("[autostart] 计划任务定义（干跑，未注册）：")
    print("  name       : %s" % spec["name"])
    print("  Execute    : %s" % spec["execute"])
    print("  Arguments  : %s" % spec["arguments"])
    print("  WorkingDir : %s" % spec["workdir"])
    print("  Trigger    : AtLogOn（登录时）")
    print("  Settings   : AllowStartIfOnBatteries / DontStopIfGoingOnBatteries / Restart×3")
    if spec.get("execute_note"):
        print("  ⚠ %s" % spec["execute_note"], file=sys.stderr)


def install_autostart(dry_run: bool = False, backend: str = "") -> int:
    """注册登录自启；`dry_run=True` 只打印定义（不落盘）。

    issue #18：**双后端**。`startup`（默认，零提权，写启动文件夹 `.vbs`）
    或 `scheduled`（原 UAC 计划任务路径，行为一行不改）。
    """
    backend = _norm_backend(backend)
    if backend == "startup":
        return startup_install(dry_run=dry_run)
    spec = task_spec()
    if spec.get("execute_note"):
        print("[autostart] 警告：%s" % spec["execute_note"], file=sys.stderr)
    if dry_run:
        print_spec(spec)
        cur = query_task()
        print("[autostart] 当前注册状态：%s" % ("已存在" if cur else "未注册"))
        print("[autostart] 真实注册需 UAC 提升权限的终端（由调用方授权），本命令到此为止。")
        return 0

    ps = (
        "$ErrorActionPreference='Stop';"
        "try {"
        " $a = New-ScheduledTaskAction -Execute '%s' -Argument '%s' -WorkingDirectory '%s';"
        " $t = New-ScheduledTaskTrigger -AtLogOn;"
        " $s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries"
        " -DontStopIfGoingOnBatteries -RestartCount 3"
        " -RestartInterval (New-TimeSpan -Minutes 1);"
        " Register-ScheduledTask -TaskName '%s' -Action $a -Trigger $t -Settings $s -Force | Out-Null;"
        " Write-Output '{\"ok\":true}'"
        "} catch {"
        " Write-Output ('{\"ok\":false,\"err\":' + (ConvertTo-Json ([string]$_.Exception.Message)) + '}')"
        "}"
    ) % (spec["execute"], spec["arguments"], spec["workdir"], spec["name"])
    rc, obj, raw = _ps_json(ps)
    if rc != 0 or not isinstance(obj, dict) or not obj.get("ok"):
        err = (obj or {}).get("err") if isinstance(obj, dict) else raw
        print("[autostart] 注册失败：%s" % err, file=sys.stderr)
        if "denied" in str(err).lower():
            print("[autostart] 提示：注册计划任务需要管理员权限（UAC 由调用方处理）。", file=sys.stderr)
        return EXIT_REGISTER_FAILED

    fails = verify_task(spec)
    hard = [f for f in fails if f != "__workdir_unverifiable__"]
    if hard:
        print("[autostart] 注册返回成功，但回读校验失败：", file=sys.stderr)
        for f in hard:
            print("  x %s" % f, file=sys.stderr)
        print("[autostart] 请手工清理：schtasks /Delete /TN %s /F" % spec["name"], file=sys.stderr)
        return EXIT_VERIFY_FAILED
    actual = query_task() or {}
    print("[autostart] 注册成功并已回读验证：")
    for k in ("name", "state", "execute", "arguments", "workdir"):
        print("  %-11s: %s" % (k, actual.get(k)))
    return 0


def uninstall_autostart(backend: str = "", dry_run: bool = False) -> int:
    """卸载自启（issue #18：双后端；QA18-4 / R2-1：两个后端都支持 dry-run）。"""
    backend = _norm_backend(backend)
    if backend == "startup":
        return startup_uninstall(dry_run=dry_run)
    # R2-1（QA 二轮）：scheduled 分支此前**没有** dry_run 判断，`--dry-run` 仍会
    # 执行 `Unregister-ScheduledTask` —— 而目标 the injected task name 是**在役任务**，
    # 一旦误用等于把正在跑的自启拆掉。干跑必须早退，绝不能走到 _ps_json。
    if dry_run:
        print("[autostart] backend=scheduled（干跑，未卸载）："
              "将执行 Unregister-ScheduledTask -TaskName '%s'" % AUTOSTART_NAME())
        print("[autostart] 当前注册状态：%s" % ("已存在" if query_task() else "未注册"))
        return EXIT_OK
    ps = ("$ErrorActionPreference='Stop';"
          "Unregister-ScheduledTask -TaskName '%s' -Confirm:$false -ErrorAction SilentlyContinue;"
          "Write-Output '{}'") % AUTOSTART_NAME()
    _ps_json(ps)
    if query_task():
        print("[autostart] 卸载后仍能查到任务 %s" % AUTOSTART_NAME()(), file=sys.stderr)
        return EXIT_UNINSTALL_FAILED
    print("[autostart] 已卸载（回读确认查无）")
    return 0


# --------------------------------------------------------------------- 自检（issue #15 第一笔，A–O 十五项）
DELIVERY_SELFTEST_SRC = r'''
# 自检靶场：全部在 TemporaryDirectory 隔离目录，全程 dry，绝不外发
# 覆盖：A–O 十五项
'''


def _self_fixture(root: Path) -> dict:
    """造一个干净靶场：ledger 副本 + sessions + issue_sessions。"""
    import shutil
    root.mkdir(parents=True, exist_ok=True)
    cfg = _cfg()
    for name, source in (("sessions.json", cfg.fixture_sessions_path),
                         ("issue_sessions.json", cfg.fixture_bind_path)):
        if source is None:
            raise RuntimeError(f"未注入自检夹具 {name}，拒绝读生产状态")
        shutil.copyfile(source, root / name)
    # 假 sid：绝不指向真会话（自检全程 dry，但宁可多一道）
    db = json.loads((root / "sessions.json").read_text(encoding="utf-8"))
    fake = {}
    wanted = ("pm-" + _project_of_repo("owner/neutral_project"),
              _cfg().supervisor_key, "steward")
    for i, (k, v) in enumerate(sorted(db.items()), start=1):
        if k not in wanted:
            continue
        rec = dict(v) if isinstance(v, dict) else {"id": v}
        rec["id"] = "session_5e55a000-0000-4000-8000-%012d" % i
        fake[k] = rec
    (root / "sessions.json").write_text(json.dumps(fake, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
    return fake


def _eligible_count(ledger: Path) -> int:
    n = 0
    try:
        with ledger.open("r", encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if (isinstance(r, dict) and str(r.get("role")).lower() == "worker"
                        and str(r.get("act")).lower() == "comment" and r.get("ok") is True
                        and r.get("repo") and r.get("issue") is not None):
                    n += 1
    except OSError:
        pass
    return n


def _fresh(root: Path, ledger_lines: list[dict] | None = None,
           copy_full_ledger: bool = False, baseline: bool = True) -> dict:
    """重置靶场状态，返回 env 快照（调用方负责设/恢复）。

    `baseline=True`（默认）先写一份「已建基线」的状态（offset=当前文件末尾）——
    否则每轮首跑都会走「五路重建 ⇒ 推 EOF ⇒ 消费 0 条」，
    反例 A–O 会**全部哑火却显示"通过"**（这正是 S1 那类洞的变体）。
    """
    import shutil
    for name in ("delivery_watch_state.json", "delivery_watch.log",
                 "delivery_undelivered.log", "delivery_watch_stats.jsonl"):
        try:
            (root / name).unlink()
        except OSError:
            pass
    target = root / "ledger.jsonl"
    if copy_full_ledger:
        source = _cfg().fixture_ledger_path
        if source is None:
            raise RuntimeError("未注入自检夹具 ledger，拒绝读生产状态")
        shutil.copyfile(source, target)
    else:
        with target.open("w", encoding="utf-8") as fh:
            for row in (ledger_lines or []):
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    configure(delivery_state_path=root / "delivery_watch_state.json",
              delivery_stats_path=root / "delivery_watch_stats.jsonl",
              delivery_log_path=root / "delivery_watch.log",
              undelivered_path=root / "delivery_undelivered.log",
              sessions_path=root / "sessions.json",
              bind_path=root / "issue_sessions.json",
              ledger_path=target,
              health_state_path=root / "supervisor_health_state.json",
              cursor_path=root / "decide_watch_cursor.json",
              log_path=root / "decision_watch.log",
              lock_path=root / "decide_watch.lock")
    os.environ["DELIVERY_NO_INJECT"] = "1"
    if baseline:
        st = _default_state()
        st["baseline_established"] = True
        # 空台账（size==0）是**正常状态**：U2 退修后 offset==0 不再触发重建，
        # 所以这里**不垫任何 seed 行**，反例 P 直接测这条路径。
        st["offset"] = target.stat().st_size
        st["last_heartbeat"] = time.time()      # 心跳压住，免得干扰 H 的日志计数
        st["last_pending_check"] = time.time()
        save_delivery_state(st)
    return {"root": str(root), "ledger": str(target)}


def _worker_row(issue=999, ok=True, **kw):
    row = {"t": datetime.now().isoformat(timespec="seconds"), "role": "worker",
           "act": "comment", "repo": "owner/neutral_project",
           "issue": issue, "ok": ok, "note": ""}
    row.update(kw)
    return row


def _append(ledger: Path, row: dict) -> None:
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _run_tick(dry=True, now=None, **kw) -> dict:
    return delivery_tick(dry=dry, now=now)


def self_test() -> int:
    """A–O 十五项。全绿 rc=0；有用例失败 rc=EXIT_SELFTEST_FAILED。"""
    global DELIVERY_SINK
    root = _cfg().self_test_root or Path(tempfile.mkdtemp(prefix="decide_watch_selftest_"))
    started = time.time()
    results: list[tuple[str, bool, str]] = []
    saved_config = _cfg()
    os.environ["DELIVERY_NO_INJECT"] = "1"
    saved_sink = DELIVERY_SINK

    def check(tag: str, ok: bool, detail: str = "") -> None:
        results.append((tag, bool(ok), detail))

    try:
        root = Path(root)
        _self_fixture(root)
        # 前置校验（S1 病灶的机判落点）
        if not (root / "sessions.json").is_file():
            delivery_log(f"夹具失败：{root / 'sessions.json'} 不存在（漏了 cp sessions.json）")
            return EXIT_FIXTURE_INVALID

        # ---------- A：ok=false 不算交付
        env = _fresh(root, [])
        _append(Path(env["ledger"]), _worker_row(999, ok=False))
        DELIVERY_SINK = []
        _run_tick()
        check("A", len(DELIVERY_SINK) == 0, f"sink={len(DELIVERY_SINK)}")

        # ---------- B：正常 worker comment ⇒ 恰好 1 条
        # 注意：用 dry=False 走**生产路径**（DELIVERY_SINK 非 None ⇒ 只记录，绝不外发）；
        # dry=True 按设计不落盘，会让 C 的精确键无从生效（M 反例守的正是这个语义）。
        env = _fresh(root, [])
        _append(Path(env["ledger"]), _worker_row(999, ok=True))
        DELIVERY_SINK = []
        _run_tick(dry=False)
        check("B", len(DELIVERY_SINK) == 1, f"sink={len(DELIVERY_SINK)}")

        # ---------- C：再跑一轮 ⇒ 0 条（精确键）
        _run_tick(dry=False)
        check("C", len(DELIVERY_SINK) == 1, f"sink={len(DELIVERY_SINK)}")

        # ---------- D：pm 自己的 comment 不算交付
        env = _fresh(root, [])
        _append(Path(env["ledger"]), {"t": datetime.now().isoformat(timespec="seconds"),
                                      "role": "pm", "act": "comment",
                                      "repo": "owner/neutral_project",
                                      "issue": 999, "ok": True, "note": ""})
        DELIVERY_SINK = []
        _run_tick()
        check("D", len(DELIVERY_SINK) == 0, f"sink={len(DELIVERY_SINK)}")

        # ---------- E：no-cooldown + no-merge + 熔断关 ⇒ 等于 eligible
        _fresh(root, copy_full_ledger=True)
        elig = _eligible_count(root / "ledger.jsonl")
        DELIVERY_SINK = []
        st = load_delivery_state()
        sent = scan_deliveries(st, dry=True, now=time.time(), force_replay=True,
                               no_cooldown=True, no_merge=True, breaker_off=True)
        check("E", sent == elig, f"sent={sent} eligible={elig}")

        # ---------- F：1h 无收据 ⇒ 升级
        _fresh(root, [])
        DELIVERY_SINK = []
        st = load_delivery_state()
        st["baseline_established"] = True
        st["pending"] = {"dl-f": {"repo": "owner/neutral_project", "issue": 999,
                                  "project": "neutral_project",
                                  "evidence": "cid:1", "worker_name": "wk-f",
                                  "nudge_ts": time.time() - 7200, "escalated": False}}
        res = check_pending(st, dry=True, now=time.time())
        check("F", res["escalations"] == 1, f"escalations={res['escalations']}")

        # ---------- G：有 pm comment 收据 ⇒ 0 升级
        now = time.time()
        _fresh(root, [{"t": datetime.fromtimestamp(now - 3600).isoformat(timespec="seconds"),
                       "role": "pm", "act": "comment",
                       "repo": "owner/neutral_project", "issue": 999,
                       "ok": True, "note": ""}])
        DELIVERY_SINK = []
        st = load_delivery_state()
        st["baseline_established"] = True
        st["pending"] = {"dl-g": {"repo": "owner/neutral_project", "issue": 999,
                                  "project": "neutral_project",
                                  "evidence": "cid:2", "worker_name": "wk-g",
                                  "nudge_ts": now - 7200, "escalated": False}}
        res = check_pending(st, dry=True, now=now)
        check("G", res["escalations"] == 0, f"escalations={res['escalations']}")

        # ---------- H：空跑 3 轮 ⇒ 0 条、非心跳日志 0 行
        env = _fresh(root, [])
        DELIVERY_SINK = []
        logp = root / "delivery_watch.log"
        before = logp.read_text(encoding="utf-8").count("[DELIVERY]") if logp.is_file() else 0
        for _ in range(3):
            _run_tick()
        after = logp.read_text(encoding="utf-8").count("[DELIVERY]") if logp.is_file() else 0
        noisy = after - before
        check("H", len(DELIVERY_SINK) == 0 and noisy == 0, f"sink={len(DELIVERY_SINK)} log+={noisy}")

        # ---------- I：尾半行不算一行、不崩、parse_errors+1
        env = _fresh(root, [])
        led = Path(env["ledger"])
        _append(led, _worker_row(998))
        with led.open("a", encoding="utf-8") as fh:
            fh.write('{"role": "worker", "act": "com')     # 无换行 ⇒ 半行
        DELIVERY_SINK = []
        st = load_delivery_state()
        sent = scan_deliveries(st, dry=True, now=time.time())
        partial_ok = (st.get("pending_partial") or "") == '{"role": "worker", "act": "com'
        errs = int((st.get("stats") or {}).get("parse_errors") or 0)
        check("I", sent == 1 and partial_ok and errs >= 1,
              f"sent={sent} partial={partial_ok} parse_errors={errs}")

        # ---------- J：空状态文件 {} ⇒ 0 条（五路重建）
        _fresh(root, copy_full_ledger=True)
        (root / "delivery_watch_state.json").write_text("{}", encoding="utf-8")
        DELIVERY_SINK = []
        _run_tick()
        check("J", len(DELIVERY_SINK) == 0, f"sink={len(DELIVERY_SINK)}")

        # ---------- K：只有 pm bind ⇒ 仍升级（bind 不算收据）
        now = time.time()
        _fresh(root, [{"t": datetime.fromtimestamp(now - 3600).isoformat(timespec="seconds"),
                       "role": "pm", "act": "bind",
                       "repo": "owner/neutral_project", "issue": 999,
                       "ok": True, "note": ""}])
        DELIVERY_SINK = []
        st = load_delivery_state()
        st["baseline_established"] = True
        st["pending"] = {"dl-k": {"repo": "owner/neutral_project", "issue": 999,
                                  "project": "neutral_project",
                                  "evidence": "cid:3", "worker_name": "wk-k",
                                  "nudge_ts": now - 7200, "escalated": False}}
        res = check_pending(st, dry=True, now=now)
        check("K", res["escalations"] == 1, f"escalations={res['escalations']}")

        # ---------- L：+08:00 行能正确判定（不抛 TypeError）
        now = time.time()
        ts_plus = (datetime.fromtimestamp(now - 3600).strftime("%Y-%m-%dT%H:%M:%S") + "+08:00")
        _fresh(root, [{"t": ts_plus, "role": "pm", "act": "comment",
                       "repo": "owner/neutral_project", "issue": 999,
                       "ok": True, "note": ""}])
        DELIVERY_SINK = []
        st = load_delivery_state()
        st["baseline_established"] = True
        st["pending"] = {"dl-l": {"repo": "owner/neutral_project", "issue": 999,
                                  "project": "neutral_project",
                                  "evidence": "cid:4", "worker_name": "wk-l",
                                  "nudge_ts": now - 7200, "escalated": False}}
        try:
            res = check_pending(st, dry=True, now=now)
            ok = res["escalations"] == 0
            detail = f"escalations={res['escalations']}"
        except TypeError as exc:
            ok, detail = False, f"TypeError: {exc}"
        check("L", ok, detail)

        # ---------- M：dry-run 不落盘 ⇒ 之后再跑仍发 1 条
        env = _fresh(root, [])
        _append(Path(env["ledger"]), _worker_row(997))
        DELIVERY_SINK = []
        _run_tick(dry=True)
        first = len(DELIVERY_SINK)
        _run_tick(dry=False)
        second = len(DELIVERY_SINK)
        check("M", first == 1 and second == 2, f"dry={first} after-real={second}")

        # ---------- N：熔断停该单，其它单照发
        now = time.time()
        _fresh(root, [])
        DELIVERY_SINK = []
        st = load_delivery_state()
        st["baseline_established"] = True
        st["breakers"] = {"owner/neutral_project#999":
                          {"date": _date_of(now), "count": 4, "enabled": False}}
        r_blocked = {"v": 1, "project": "neutral_project",
                     "repo": "owner/neutral_project", "issue": 999,
                     "worker_name": "w", "worker_session": "", "pm_key": "pm-x",
                     "pm_sid": "", "kind": "comment", "evidence": "cid:n1",
                     "ledger_line": 1, "detected_ts": now, "dl_id": "dl-n1"}
        r_other = {**r_blocked, "issue": 1000, "evidence": "cid:n2", "dl_id": "dl-n2"}
        s1 = enqueue_delivery(st, r_blocked, dry=True, now=now)
        s2 = enqueue_delivery(st, r_other, dry=True, now=now)
        check("N", s1 == 0 and s2 == 1, f"blocked={s1} other={s2}")

        # ---------- O：跨日自动解除熔断并写日志
        now = time.time()
        _fresh(root, [])
        st = load_delivery_state()
        st["breakers"] = {"a#1": {"date": "2000-01-01", "count": 4, "enabled": False}}
        st["stats"] = {"date": "2000-01-01"}
        tripped = stats_roll(st, now)
        check("O", tripped == ["a#1"] and st["breakers"] == {},
              f"tripped={tripped} breakers={st['breakers']}")

        # ---------- P：空 ledger 建基线 → 追加一行 ok=true ⇒ 必须发 1 条
        # （U2 退修：空台账是**正常状态**，不得每轮重建）
        env = _fresh(root, [])
        led = Path(env["ledger"])
        DELIVERY_SINK = []
        r1 = _run_tick(dry=False)                      # 空台账：建基线、0 条
        b1 = Path(root / "delivery_watch.log").read_text(encoding="utf-8").count("baseline rebuilt") \
            if (root / "delivery_watch.log").is_file() else 0
        _append(led, _worker_row(996, ok=True))
        r2 = _run_tick(dry=False)
        b2 = Path(root / "delivery_watch.log").read_text(encoding="utf-8").count("baseline rebuilt") \
            if (root / "delivery_watch.log").is_file() else 0
        check("P", r1["sent"] == 0 and r2["sent"] == 1 and b2 == b1,
              f"empty={r1['sent']} after_append={r2['sent']} rebuilt={b2 - b1}")

        # ---------- Q：正常跑几轮 → 截断为 0 → 追加一行 ⇒ 必须发 1 条且只重建一次
        env = _fresh(root, [])
        led = Path(env["ledger"])
        DELIVERY_SINK = []
        _append(led, _worker_row(995, ok=True))
        _run_tick(dry=False)                            # 正常消费 1 条
        _run_tick(dry=False)                            # 空跑
        logp = root / "delivery_watch.log"
        before = logp.read_text(encoding="utf-8").count("baseline rebuilt") if logp.is_file() else 0
        led.write_text("", encoding="utf-8")            # 模拟 logrotate 截断
        _run_tick(dry=False)                            # 这一轮重建一次
        after1 = logp.read_text(encoding="utf-8").count("baseline rebuilt") if logp.is_file() else 0
        DELIVERY_SINK = []
        _append(led, _worker_row(994, ok=True))
        r = _run_tick(dry=False)
        after2 = logp.read_text(encoding="utf-8").count("baseline rebuilt") if logp.is_file() else 0
        check("Q", (after1 - before) == 1 and (after2 - after1) == 0 and r["sent"] == 1,
              f"rebuilt={after1 - before}→{after2 - after1} sent={r['sent']}")

        # ---------- R：DELIVERY_FIXTURE=1 不设 SINK ⇒ 拒绝运行（rc=6）
        # P2-1（V1 补修）：不只验 gate_check() 非空，**真跑 main() 断言 rc==6**。
        import io
        from contextlib import redirect_stderr
        os.environ["DELIVERY_FIXTURE"] = "1"
        os.environ.pop("DELIVERY_SEND_SINK", None)
        err = gate_check()
        buf = io.StringIO()
        try:
            with redirect_stderr(buf):
                rc_main = main(["--once", "--wait", "1"])
        finally:
            os.environ.pop("DELIVERY_FIXTURE", None)
        rc_ok = (rc_main == EXIT_FIXTURE_INVALID == 6)
        check("R", bool(err) and rc_ok,
              f"gate_check={'有错' if err else '空'} main_rc={rc_main}（期望 6）")

        # ---------- S：FIXTURE=1 + SINK ⇒ resolve 出假 sid，真发 0
        sink_path = root / "sent_gate.jsonl"
        try:
            sink_path.unlink()
        except OSError:
            pass
        os.environ["DELIVERY_FIXTURE"] = "1"
        os.environ["DELIVERY_SEND_SINK"] = str(sink_path)
        os.environ.pop("DELIVERY_ALLOW_SEND", None)
        saved_mem_sink = DELIVERY_SINK
        try:
            # 用**真实** sessions.json（含真 sid）来证明"夹具强制换假"
            source = _cfg().fixture_sessions_path
            if source is None:
                raise RuntimeError("未注入自检夹具 sessions，拒绝读生产状态")
            shutil.copyfile(source, root / "sessions.json")
            recips = resolve_recipients("owner/neutral_project")
            all_fake = bool(recips) and all(s.startswith(DELIVERY_FAKE_PREFIX) for _n, s in recips)
            fake_rewrite_ok = all_fake
            # 关掉内存 sink，强制走**真实的闸路径**（fixture ⇒ 只写 sink，不 spawn）
            DELIVERY_SINK = None
            spawned = []
            orig_run = subprocess.run

            def spy_run(*a, **kw):
                spawned.append(a[0] if a else kw.get("args"))
                return orig_run(*a, **kw)
            subprocess.run = spy_run
            try:
                sent_ok = delivery_notify(recips[0][1] if recips else "", "【收单】selftest-S", False) \
                    if recips else False
            finally:
                subprocess.run = orig_run
            sink_lines = 0
            if sink_path.is_file():
                sink_lines = len([l for l in sink_path.read_text(encoding="utf-8").splitlines() if l.strip()])
            spawned_sess = [c for c in spawned if "injected transport" in str(c)]
        finally:
            DELIVERY_SINK = saved_mem_sink
            for k in ("DELIVERY_FIXTURE", "DELIVERY_SEND_SINK"):
                os.environ.pop(k, None)
            _self_fixture(root)                       # 恢复假 sessions
        check("S", fake_rewrite_ok and sent_ok and sink_lines >= 1 and not spawned_sess,
              f"fake={fake_rewrite_ok} sink_lines={sink_lines} notify={sent_ok} spawned={len(spawned_sess)}")

        # ---------- T（V1 补修）：state 缺 `offset` 键 ＋ 台账 5 行 ok=true
        # ⇒ **必须重建基线、发 0 条**（不许回放 5 条）。
        # 旧写法 `"offset" not in st` 是死代码（_default_state 预填 0），T 会红。
        env = _fresh(root, [])
        led = Path(env["ledger"])
        for i in range(5):
            _append(led, _worker_row(993, ok=True, note=f"cid:v1-{i}"))
        (root / "delivery_watch_state.json").write_text(
            json.dumps({"baseline_established": True}), encoding="utf-8")
        DELIVERY_SINK = []
        logp = root / "delivery_watch.log"
        before = logp.read_text(encoding="utf-8").count("baseline rebuilt") if logp.is_file() else 0
        r = _run_tick(dry=False)
        after = logp.read_text(encoding="utf-8").count("baseline rebuilt") if logp.is_file() else 0
        check("T", r["sent"] == 0 and len(DELIVERY_SINK) == 0 and (after - before) == 1,
              f"sent={r['sent']} sink={len(DELIVERY_SINK)} rebuilt={after - before}（期望 0/0/1）")

        # ---------- 红线断言：三道闸真能被读到（U1 的反向断言）
        # 这一步**必须**放在最后，且必须能在闸缺失时变红。
        os.environ["DELIVERY_FIXTURE"] = "1"
        os.environ["DELIVERY_SEND_SINK"] = str(root / "gate_probe.jsonl")
        os.environ["DELIVERY_ALLOW_SEND"] = "1"
        try:
            gates_read = (gate_fixture() and gate_sink() and gate_allow_send())
            err_with_sink = gate_check()               # 有 sink ⇒ 应无错
        finally:
            for k in ("DELIVERY_FIXTURE", "DELIVERY_SEND_SINK", "DELIVERY_ALLOW_SEND"):
                os.environ.pop(k, None)
        gates_default = (not gate_fixture() and not gate_sink() and not gate_allow_send())
        check("GATE", gates_read and not err_with_sink and gates_default,
              f"读到={gates_read} 有sink时无错={not err_with_sink} 默认全关={gates_default}")

        # ---------- SM（issue #18 本体）：--send-mode 三态 ＋ 只读 ＋ 退出码
        for k in ("DELIVERY_FIXTURE", "DELIVERY_SEND_SINK", "DELIVERY_ALLOW_SEND"):
            os.environ.pop(k, None)
        m_real, _w, rc_real = send_mode(False)
        os.environ["DELIVERY_FIXTURE"] = "1"
        os.environ["DELIVERY_SEND_SINK"] = str(root / "sm.jsonl")
        m_fix, _w2, rc_fix = send_mode(False)
        for k in ("DELIVERY_FIXTURE", "DELIVERY_SEND_SINK"):
            os.environ.pop(k, None)
        os.environ["DELIVERY_SEND_SINK"] = str(root / "sm2.jsonl")
        m_dry, _w3, rc_dry = send_mode(False)
        os.environ.pop("DELIVERY_SEND_SINK", None)
        m_dryflag, _w4, _rc4 = send_mode(True)
        # 只读性：调用 --send-mode 不得产生任何 state 文件写入
        before = {p.name: p.stat().st_mtime_ns for p in root.glob("*.json*") if p.is_file()}
        print_send_mode(False)
        after = {p.name: p.stat().st_mtime_ns for p in root.glob("*.json*") if p.is_file()}
        check("SM", (m_real == "real" and m_fix == "fixture" and m_dry == "dry"
                     and m_dryflag == "dry" and rc_real == rc_fix == rc_dry == 0
                     and before == after),
              f"real={m_real} fixture={m_fix} dry={m_dry} dryflag={m_dryflag} 只读={before == after}")

    except Exception as exc:  # noqa: BLE001
        delivery_log(f"自检异常：{type(exc).__name__}: {exc}")
        results.append(("EXC", False, f"{type(exc).__name__}: {exc}"))
    finally:
        DELIVERY_SINK = saved_sink
        configure(saved_config)
        os.environ.pop("DELIVERY_NO_INJECT", None)

    passed = sum(1 for _t, ok, _d in results if ok)
    total = 22          # A–T ＋ GATE ＋ T（V1）＋ SM（#18 --send-mode）
    for tag, ok, detail in results:
        print(f"[self-test] {tag} {'通过' if ok else '失败'}" + (f"（{detail}）" if detail else ""))
    elapsed = time.time() - started
    print(f"--self-test: A–T＋SM {passed}/{total} 通过 · 耗时 {elapsed:.1f}s")
    if elapsed > 30:
        print("[self-test] 超时（预算 30s）", file=sys.stderr)
        return EXIT_SELFTEST_FAILED
    return 0 if passed == total else EXIT_SELFTEST_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
