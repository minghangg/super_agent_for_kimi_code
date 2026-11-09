"""WSS event bus daemon —— the single observation/control plane over Kimi Code.

Architecture
------------
    Kimi Code server ──WS(subscribe_v2)──> [producer] ──absorb──> STATE.sessions
                                                     └──> events ring + jsonl
                                                                    │
                                        [HTTP 127.0.0.1:<port>]  <───┘
                                                    │
                              sess/ctx/monitor/watchdogs …
                                                    │（daemon 挂了）
                                                    ▼
                                        super_agent.direct（直连）

Dependency direction (hard rule): this module imports ``super_agent.direct``
and ``super_agent.paths`` only. It never imports a workbench tool module.

Injection
---------
Every private dependency is supplied by the caller through ``WssBusConfig``:
workspace root, state roots, sessions registry, retired-sid store, alarm and
health modules, session probe, autostart spec, role prefixes, supervisor key,
watch defaults. With no injection the module raises a parameter error or
prints an explicit WARN; it never guesses a production root and never imports
a workbench module.

    python -m super_agent.wss_bus --self-test
    configure(WssBusConfig(...))

Sub-commands
------------
    --daemon               run in the foreground
    --install-autostart    register login autostart, then read back and verify
                           (0=ok 3=register failed 4=verify failed 5=uninstall failed)
    --uninstall-autostart  unregister, read back, confirm gone
    --status               is the daemon alive
    --once --wait N        one short subscription, then exit (smoke test)
    --self-test            offline assertions (no network, no running daemon)
    --probe                end-to-end probe: create a throw-away session, talk
                           only to it, reclaim it, verify
    --state-dir DIR        isolated run directory (all state/* lands here)

File event source
-----------------
Besides WS frames the daemon watches local files (new handoff documents, the
"awaiting a decision" section of PM project docs). File events are structurally
identical to WS events and carry ``sid = "file"`` (never null), so
``/events?sid=file`` filters them and ``/events?since=`` mixes both.

The watch list lives in ``<state root>/wss_bus_watch.json``; editing it does not
need a restart (re-read on mtime change; a bad config keeps the previous one and
reports ``/healthz.watch_config_error``). The first round only seeds a baseline —
no events — so a restart never replays the whole directory history.

Section matching is deliberately permissive ("a false positive costs one extra
ping; a false negative means the feature silently stops working"). Known false
positive: a retrospective heading that happens to put a request word next to the
decision word still matches. Configure ``section_pattern`` when precision matters.

Control plane
-------------
Non-idempotent actions use three-state acceptance backed by a durable journal:
  1. accepted (journal has the request id **and** an upstream id) ⇒
     ``"indeterminate"``, never resend;
  2. definitely not accepted (no journal record) ⇒ one direct attempt;
  3. journal unreadable ⇒ ``"indeterminate"``, never retry.
4xx and business error codes are returned verbatim: no downgrade, no replay.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from . import direct as K
from . import paths as kimi_paths

HOST = "127.0.0.1"
RING_SIZE = 5000
EVENTS_MAX_BYTES = 20 * 1024 * 1024
RECONNECT_MIN, RECONNECT_MAX = 1.0, 60.0
HEALTH_TIMEOUT = 2.0

# --probe may only touch targets it created itself
PROBE_TITLE_PREFIX = "wk-probe-"

AGENT = K.AGENT
TRANSCRIPT_GRADE = K.TRANSCRIPT_GRADE


def _joined(*parts: str) -> str:
    """Assemble a literal that must not appear whole in this file."""
    return "".join(parts)


@dataclass(frozen=True)
class WssBusConfig:
    """Caller-supplied boundary. ``None`` means "not injected", never guessed."""

    workspace_root: Path | None = None
    state_root: Path | None = None
    production_state_root: Path | None = None
    sessions_path: Path | None = None
    state_envs: tuple[str, ...] = ("WSS_BUS_STATE_DIR", "DECIDE_WATCH_STATE_DIR")
    port: int = 0

    supervisor_key: str = "supervisor"
    role_prefixes: tuple[str, ...] = (
        "steward", "pm", "qa", "worker", "scout", "auditor", "planner", "reviewer")
    decide_section: str = "## 待拍板"
    watch_defaults: tuple[dict, ...] = ()
    watch_config: Path | None = None

    alarm_state_file: str = "session_alarm_state.json"
    alarm_event: str = ""
    guardian_extra_file: str = "guardian_extra.json"
    guardian_state_file: str = "wss_bus_guardian_state.json"
    guardian_warn_every: float = 300.0
    guardian_alert_dedup_s: float = 1800.0
    supervisor_state_fresh_s: float = 600.0

    bind_file: str = "issue_sessions.json"
    bind_lookup: object | None = None
    repo_full_of_project: object | None = None

    autostart_name: str = "WssBus"
    autostart_script: Path | None = None
    autostart_workdir: Path | None = None

    retired_module: object | None = None
    alarm_module: object | None = None
    health_module: object | None = None
    probe_session: object | None = None
    send_transport: object | None = None
    ps_json: object | None = None
    windowless_python: object | None = None
    self_test_root: Path | None = None
    fake_stdout: object | None = None


DEFAULT_CONFIG = WssBusConfig()
LAST_ANNOUNCE = {"ws_url": "", "note": ""}


def configure(config: WssBusConfig | None = None, **overrides) -> WssBusConfig:
    """Install the caller-supplied boundary and rebuild the state-root singleton."""
    global DEFAULT_CONFIG, PATHS, PORT
    base = config or DEFAULT_CONFIG
    if overrides:
        base = replace(base, **overrides)
    DEFAULT_CONFIG = base
    if base.state_root is not None:
        PATHS = Paths(base.state_root)
    if base.port:
        PORT = int(base.port)
    return base


def _cfg() -> WssBusConfig:
    return DEFAULT_CONFIG


def missing_injections(config: WssBusConfig | None = None) -> list[str]:
    """Names the daemon cannot run without. Empty ⇒ ready."""
    cfg = config or DEFAULT_CONFIG
    required = ("workspace_root", "state_root", "production_state_root",
                "sessions_path", "probe_session", "retired_module")
    return [n for n in required if getattr(cfg, n) is None]


def self_check() -> int:
    missing = missing_injections()
    if missing:
        print("wss_bus: 未注入 " + ", ".join(missing), file=sys.stderr)
        return 2
    print("wss_bus: 注入完整")
    return 0


def _workspace_root() -> Path:
    cfg = _cfg()
    if cfg.workspace_root is None:
        raise RuntimeError("未注入 workspace_root，拒绝静默连生产")
    return Path(cfg.workspace_root).expanduser().resolve()


def _production_root() -> Path:
    cfg = _cfg()
    if cfg.production_state_root is None:
        raise RuntimeError("未注入 production_state_root，拒绝静默连生产")
    return Path(cfg.production_state_root).expanduser().resolve()


def _sessions_path() -> Path:
    cfg = _cfg()
    if cfg.sessions_path is not None:
        return Path(cfg.sessions_path).expanduser()
    return _production_root() / "sessions.json"


def _retired():
    """Retired-sid store injected by the caller; ``None`` ⇒ fail-closed."""
    return _cfg().retired_module


# --------------------------------------------------------------------- paths
# Environment variables able to relocate the state root (same set as
# `current_state_root()` treats as an isolation signal).
def state_env_names() -> tuple[str, ...]:
    return tuple(_cfg().state_envs)


def env_state_root() -> Path | None:
    """State root named by an environment variable; None if none is set."""
    for name in state_env_names():
        val = (os.environ.get(name) or "").strip()
        if val:
            return Path(os.path.expanduser(val)).resolve()
    return None


class _UnconfiguredPaths:
    """Placeholder used before ``configure()``: every access is an explicit error."""

    def __getattr__(self, item: str):
        raise RuntimeError("未注入 state_root，拒绝静默连生产")


class Paths:
    """Every runtime file. ``--state-dir`` moves the whole set (isolation, tests).

    ``root`` fallback order: explicit argument > environment override > injected
    production root. All three are consulted here so an instance started with an
    environment variable cannot leak cache/events/journal/seq/lock into production.
    """

    def __init__(self, root: Path | str | None = None):
        if root:
            self.root = Path(root).expanduser().resolve()
        else:
            self.root = env_state_root() or _production_root()
        self.cache = self.root / "wss_bus_cache.json"
        self.events = self.root / "wss_bus_events.jsonl"
        self.log = self.root / "wss_bus.log"
        self.pid = self.root / "wss_bus.pid"
        self.lock = self.root / "wss_bus.lock"
        self.journal = self.root / "wss_bus_journal.jsonl"
        self.seq = self.root / "wss_bus_seq.json"
        self.watch = self.root / "wss_bus_watch.json"

    def mkdir(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)


PATHS = _UnconfiguredPaths()
PORT = 0


def log(msg: str) -> None:
    line = f"{datetime.now().strftime('%m-%d %H:%M:%S')} wss_bus: {msg}"
    print(line, flush=True)
    try:
        root = current_state_root()
        p = root / "wss_bus.log"
        root.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except (OSError, RuntimeError):
        pass


# --------------------------------------------------------------------- issue keys
def repo_full_of(repo: str) -> str:
    """Normalise ``owner/repo`` for key building: lowercase, slash-preserved."""
    raw = str(repo or "").strip()
    if "/" not in raw:
        return ""
    owner, _, name = raw.rpartition("/")
    owner = owner.strip().lower()
    name = name.strip().lower()
    if not owner or not name:
        return ""
    return f"{owner}/{name}"


def project_of_repo(repo: str) -> str:
    """``owner/repo`` → ``repo`` (lowercased). Used only for read-only compat."""
    full = repo_full_of(repo)
    return full.rsplit("/", 1)[-1] if full else ""


def issue_key(repo: str, issue) -> str:
    """Canonical, cross-repository-safe key: ``owner/repo#issue``."""
    full = repo_full_of(repo)
    num = str(issue if issue is not None else "").strip()
    if not full or not num:
        return ""
    return f"{full}#{num}"


def legacy_issue_keys(repo: str, issue) -> list[str]:
    """Short keys a pre-migration writer may have used — **read-only** compat."""
    num = str(issue if issue is not None else "").strip()
    out: list[str] = []
    full = repo_full_of(repo)
    proj = project_of_repo(repo)
    for short in (full, proj):
        if short and num:
            out.append(f"{short}#{num}")
    return out


def _record_repo_full(key: str, rec) -> str:
    """Best-effort ``owner/repo`` of a stored record (key or payload)."""
    if isinstance(rec, dict):
        val = rec.get("repo_full") or rec.get("repo") or ""
        if val:
            return repo_full_of(str(val))
    if isinstance(key, str) and "/" in key and "#" in key:
        return repo_full_of(key.split("#", 1)[0])
    return ""


def _normalised_bindings(db: dict) -> dict:
    """Index a binding table under case-folded keys (qualified and short alike)."""
    out: dict = {}
    for k, v in db.items():
        if not isinstance(k, str) or "#" not in k:
            continue
        head, _, num = k.rpartition("#")
        full = repo_full_of(head) if "/" in head else ""
        norm = f"{full}#{num.strip()}" if full else f"{head.strip().lower()}#{num.strip()}"
        out[norm] = v
    return out


def lookup_issue_record(db: dict | None, repo: str, issue) -> dict | None:
    """Binding lookup: fully-qualified key first, short keys only when unambiguous.

    A short key such as ``demo#12`` cannot tell two owners apart. It is therefore
    read **only** when no other repository could own it; a short key that is
    ambiguous, or that points at a different owner, returns ``None`` instead of
    silently matching the wrong repository. That is the anti-crosstalk rule.
    """
    if not isinstance(db, dict):
        return None
    norm = _normalised_bindings(db)
    full_key = issue_key(repo, issue)
    if full_key and isinstance(norm.get(full_key), dict):
        return norm[full_key]
    proj = project_of_repo(repo)
    num = str(issue if issue is not None else "").strip()
    if not proj or not num:
        return None
    short = f"{proj}#{num}"
    rec = norm.get(short)
    if not isinstance(rec, dict):
        return None
    # Every record that could own this short key: the bare spelling plus any
    # qualified spelling of the same repository name and issue number.
    owners: set[str] = set()
    suffix = f"/{short}"
    for k in norm:
        if k == short:
            continue
        if k.endswith(suffix):
            owners.add(k[: -len(suffix)])
    want = repo_full_of(repo)
    if owners:
        # Another owner claims the same repo name and issue number ⇒ ambiguous.
        return None
    return rec


def bind_worker(repo: str, issue, bind_path: Path | None = None) -> tuple[str, str]:
    """``(worker name, session)`` from the binding table, or ``("unknown", "")``."""
    cfg = _cfg()
    if cfg.bind_lookup is not None:
        try:
            res = cfg.bind_lookup(repo, issue)
        except Exception:                       # noqa: BLE001  injected, best effort
            return "unknown", ""
        if isinstance(res, tuple) and len(res) == 2:
            return str(res[0] or "unknown"), str(res[1] or "")
        return "unknown", ""
    path = Path(bind_path) if bind_path else current_state_root() / cfg.bind_file
    try:
        db = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unknown", ""
    rec = lookup_issue_record(db, repo, issue) or {}
    worker = rec.get("worker") if isinstance(rec, dict) else None
    if not isinstance(worker, dict):
        return "unknown", ""
    return str(worker.get("name") or "unknown"), str(worker.get("session") or "")


def delivery_key(repo: str, issue, evidence: str = "") -> str:
    """Key used by delivery bookkeeping (nudge / undelivered / pending)."""
    key = issue_key(repo, issue)
    ev = str(evidence or "").strip()
    return f"{key}#{ev}" if key and ev else key


def qualify_event_payload(payload: dict, repo: str = "", issue=None) -> dict:
    """Return a payload whose issue-space keys are fully qualified.

    The original ``project`` field is preserved; ``repo_full`` and ``key`` are
    added so two repositories that share a name can never collide.
    """
    if not isinstance(payload, dict):
        return {}
    out = dict(payload)
    raw_repo = str(repo or out.get("repo_full") or out.get("repo") or "")
    num = issue if issue is not None else out.get("issue")
    full = repo_full_of(raw_repo)
    if not full:
        proj = str(out.get("project") or "")
        mapper = _cfg().repo_full_of_project
        if proj and callable(mapper):
            try:
                full = repo_full_of(str(mapper(proj) or ""))
            except Exception:                   # noqa: BLE001  injected, best effort
                full = ""
    out.setdefault("project", project_of_repo(raw_repo) or str(out.get("project") or ""))
    out["repo_full"] = full
    if full and num not in (None, ""):
        out["key"] = issue_key(full, num)
    return out


# --------------------------------------------------------------------- seq/journal
class SeqStore:
    """Event sequence survives restarts, with boot id and loss ranges recorded."""

    def __init__(self, path: Path):
        self.path = path
        self.boot_id = uuid.uuid4().hex[:12]
        self.seq = 0
        self.dropped: list[dict] = []
        self._load()

    def _load(self) -> None:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            self.seq = int(d.get("seq") or 0)
            self.dropped = d.get("dropped") or []
        except (OSError, ValueError):
            self.seq = 0

    def next(self) -> int:
        self.seq += 1
        return self.seq

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"seq": self.seq, "boot_id": self.boot_id,
                                       "dropped": self.dropped[-20:]},
                                      ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    def note_drop(self, start: int, end: int, why: str) -> None:
        if end >= start:
            self.dropped.append({"from": start, "to": end, "why": why,
                                 "at": time.strftime("%F %T")})


class Journal:
    """Durable acceptance ledger for non-idempotent control actions."""

    def __init__(self, path: Path):
        self.path = path
        self.records: dict[str, dict] = {}
        self.ok = True
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists() and not self.path.is_file():
                # Path exists but is not a regular file ⇒ ledger is corrupt.
                # Must report ok=False: pretending it is a fresh journal makes
                # accepted records vanish and the same request id gets resent.
                self.ok = False
                return
            if not self.path.is_file():
                return
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                rid = rec.get("request_id")
                if rid:
                    self.records[rid] = rec
        except OSError:
            self.ok = False

    def get(self, rid: str) -> dict | None:
        if not self.ok:
            return None
        return self.records.get(rid)

    def put(self, rec: dict) -> None:
        rid = rec.get("request_id")
        if not rid:
            return
        self.records[rid] = rec
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            self.ok = False


# --------------------------------------------------------------------- state
class BusState:
    def __init__(self) -> None:
        self.started_at = time.time()
        self.sessions: dict[str, dict] = {}
        self.events: deque = deque(maxlen=RING_SIZE)
        self.subscribed: dict[str, dict] = {}
        self.connected = False
        self.reconnects = 0
        self.gap_started_at: float | None = None
        self.gaps: list[dict] = []
        self.degraded_calls = 0
        self.last_event_ts = 0.0
        self.last_snapshot_ts = 0.0
        self.seq = SeqStore(_seq_path())
        self.journal = Journal(_journal_path())
        self.probe_sids: set[str] = set()
        self.watchers: dict[str, dict] = {}
        self.watch_config = None
        self.watch_config_error = ""
        self.watch_file_watcher = None
        self.guardian_state: dict = {}
        self.retired_cache: set[str] = set()

    def session(self, sid: str) -> dict:
        return self.sessions.setdefault(
            sid, {"status": {}, "tasks": {}, "pending": {}, "prompt_id": "",
                  "ts": 0, "event_ts": 0, "snapshot_ts": 0, "archived": False})

    def add_event(self, sid: str, etype: str, payload: dict) -> dict:
        ev = {"seq": self.state_next(), "ts": time.time(), "sid": sid,
              "type": etype, "payload": payload, "boot_id": self.seq.boot_id}
        self.events.append(ev)
        self.last_event_ts = ev["ts"]
        return ev

    def state_next(self) -> int:
        return self.seq.next()


def _seq_path() -> Path:
    """Seq/journal files are unreadable before injection; keep module import cheap."""
    try:
        return Path(PATHS.seq)
    except RuntimeError:
        return Path(tempfile.gettempdir()) / "wss_bus_seq.json"


def _journal_path() -> Path:
    try:
        return Path(PATHS.journal)
    except RuntimeError:
        return Path(tempfile.gettempdir()) / "wss_bus_journal.jsonl"


STATE = BusState()
WS_REF: dict = {"ws": None, "loop": None}


# --------------------------------------------------------------------- absorb
def _flat(msg: dict) -> dict:
    return K.flat(msg)


def absorb(msg: dict) -> dict | None:
    """Merge one frame into state and return the event written (None if irrelevant)."""
    t = (msg.get("type") or "").strip()
    f = _flat(msg)
    sid = f.get("session_id") or f.get("sid") or ""
    if not t or not sid:
        return None
    rec = STATE.session(sid)

    if t == "agent":
        rec["status"].update({
            "model": f.get("model"), "thinking_level": f.get("thinking"),
            "permission": f.get("permission_mode"), "plan_mode": f.get("plan_mode"),
            "swarm_mode": f.get("swarm_mode"), "tower_mode": f.get("tower_mode"),
            "agent_status": f.get("status"),
            "busy": f.get("status") == "running",
        })
    elif t == "usage":
        tok, cap = f.get("context_tokens"), f.get("max_context_tokens")
        rec["status"].update({
            "context_tokens": tok, "max_context_tokens": cap,
            "context_usage": (tok / cap) if isinstance(tok, int)
            and isinstance(cap, int) and cap else None,
        })
    elif t in ("event.session.work_changed", "session.work_changed"):
        rec["status"].update({
            "busy": f.get("busy"), "main_turn_active": f.get("main_turn_active"),
            "pending_interaction": f.get("pending_interaction"),
            "last_turn_reason": f.get("last_turn_reason"),
        })
    elif t == "task.list":
        # Full replacement, never a merge.
        rec["tasks"] = {it.get("task_id"): it for it in (f.get("tasks") or [])
                        if isinstance(it, dict) and it.get("task_id")}
    elif t == "task":
        tid = f.get("task_id")
        if tid:
            rec["tasks"][tid] = f          # keep the last state, never delete
    elif t in ("interaction",):
        aid = f.get("interaction_id") or f.get("approval_id")
        if aid:
            if (f.get("status") or "pending") == "pending":
                rec["pending"][aid] = f
            else:
                rec["pending"].pop(aid, None)
    elif t in ("prompt.submitted", "turn.started"):
        pid = f.get("promptId") or f.get("prompt_id")
        if pid:
            rec["prompt_id"] = str(pid)
    elif t in ("prompt.completed", "prompt.aborted", "turn.ended"):
        rec["prompt_id"] = ""
    elif t in ("event.session.archived", "event.session.deleted"):
        rec["archived"] = True
        STATE.subscribed.pop(sid, None)
        return STATE.add_event(sid, t, f)
    elif t == "event.session.created":
        pass
    else:
        return None

    rec["ts"] = int(time.time())
    rec["event_ts"] = rec["ts"]
    return STATE.add_event(sid, t, f)


# --------------------------------------------------------------------- event log
def _append_event_file(ev: dict) -> None:
    try:
        PATHS.mkdir()
        if PATHS.events.exists() and PATHS.events.stat().st_size > EVENTS_MAX_BYTES:
            _rotate_events()
        with open(PATHS.events, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except (OSError, RuntimeError):
        pass


def _rotate_events() -> None:
    """Atomic rotation keeping two segments, recording the loss range."""
    try:
        second = PATHS.events.with_suffix(".jsonl.2")
        first = PATHS.events.with_suffix(".jsonl.1")
        if second.exists():
            second.unlink()
        if first.exists():
            os.replace(first, second)
        lo, hi = 0, 0
        try:
            with open(PATHS.events, "r", encoding="utf-8", errors="replace") as fh:
                head = fh.readline()
                fh.seek(max(0, PATHS.events.stat().st_size - 4096))
                tail = fh.read().strip().splitlines()[-1:]
            lo = json.loads(head).get("seq", 0) if head else 0
            hi = json.loads(tail[0]).get("seq", 0) if tail else 0
        except (OSError, ValueError, IndexError):
            pass
        os.replace(PATHS.events, first)
        if second.exists():
            try:
                with open(second, "r", encoding="utf-8", errors="replace") as fh:
                    h = fh.readline()
                lost = json.loads(h).get("seq", 0) if h else 0
                if lost:
                    STATE.seq.note_drop(1, max(0, lost - 1), "rotation")
            except (OSError, ValueError):
                pass
        log(f"事件日志轮转：{PATHS.events.name} → {first.name}（seq {lo}–{hi}）")
    except (OSError, RuntimeError) as exc:
        log(f"轮转失败：{exc}")


# --------------------------------------------------------------------- subscribe
async def _subscribe(ws, sid: str) -> str:
    """Send a subscribe frame and return the ack id the caller keys off."""
    ack_id = uuid.uuid4().hex
    await ws.send_json({
        "type": "subscribe_v2", "id": ack_id,
        "payload": {"session_id": sid, "transcript": {AGENT: TRANSCRIPT_GRADE}},
    })
    return ack_id


async def _handshake(ws) -> None:
    await ws.send_json({"type": "client_hello", "id": uuid.uuid4().hex,
                        "payload": {"client_id": "wss-bus"}})
    wanted = _targets()
    STATE.subscribed = {}                 # rebuilt after every reconnect
    for sid in wanted:
        ack_id = await _subscribe(ws, sid)
        STATE.subscribed[sid] = {"ok": False, "ack_id": ack_id,
                                 "reason": "pending ack", "ts": time.time()}


def _mark_ack(frame: dict) -> None:
    """Handle subscribe_v2 ack: success flips ok=True, failure records why."""
    if frame.get("type") not in ("ack", "error"):
        return
    rid = frame.get("id") or ""
    code = frame.get("code")
    p = frame.get("payload") or {}
    for sid, rec in STATE.subscribed.items():
        if rec.get("ack_id") != rid:
            continue
        if code not in (0, None):
            rec.update(ok=False, reason=f"code={code} {frame.get('msg') or ''}".strip(),
                       ts=time.time())
        else:
            accepted = p.get("accepted") or []
            not_found = p.get("not_found") or []
            if sid in accepted or (not accepted and not not_found):
                rec.update(ok=True, reason="", ts=time.time())
            elif sid in not_found:
                rec.update(ok=False, reason="not_found", ts=time.time())
            else:
                rec.update(ok=False, reason=f"accepted={accepted[:2]}", ts=time.time())
        return


def _targets() -> list[str]:
    """Sessions to subscribe: registry entries the server still has, plus live ones.

    Registry entries for deleted sessions used to produce ``code=40401`` on every
    handshake and a long ``subscribe_failed`` list on ``/healthz``. The server list
    (paged to exhaustion) is intersected first, so only live registry ids survive.
    """
    live: dict[str, dict] = {}
    items, _err = K.list_all_sessions(page_size=50)
    for it in items:
        sid = str(it.get("id") or "")
        if sid:
            live[sid] = it

    out: list[str] = []
    try:
        reg = json.loads(_sessions_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        reg = {}
    if isinstance(reg, dict):
        for val in reg.values():
            sid = val.get("id") if isinstance(val, dict) else val
            if not (isinstance(sid, str) and sid.startswith("session_")):
                continue
            if sid in out:
                continue
            if live and sid not in live:
                continue                      # server no longer has it
            out.append(sid)
    for sid, it in live.items():
        if sid in out or it.get("archived"):
            continue
        out.append(sid)
    # Retired sessions must not be subscribed. `unsubscribe` only drops the
    # current subscription; the server list still contains archived sessions, so
    # the 10 s re-subscribe would bring them straight back. The retired list has
    # to apply here as well, otherwise "cut off" only lasts ten seconds.
    #
    # When the list is unreadable `_retired_sids()` returns None (untrusted). Do
    # not treat that as "empty" (it would re-subscribe retired ids) and do not
    # guess: keep the current subscription set untouched. Better to subscribe too
    # few than to subscribe the wrong ones.
    retired = _retired_sids()
    if retired is None:
        log("退役清单不可信：本轮补订阅保持不变（不新增、不删除）")
        return list(STATE.subscribed)
    if retired:
        out = [sid for sid in out if sid not in retired]
    return out


def _retired_block(sid: str, allow_retired: bool) -> str:
    """Delivery gate: ``""`` passes, anything else is the refusal reason.

    Fail-closed: an unparsable list already yields a non-empty reason, so a bad
    list refuses delivery unless ``allow_retired``. An explicit bypass still
    leaves a log line.
    """
    mod = _retired()
    if mod is None:
        return "退役清单未注入（retired_module）；按 fail-closed 一律视为仍退役"
    try:
        reason, _err = mod.check_detail(sid, str(PATHS.root))
    except Exception as exc:                            # noqa: BLE001
        reason = (f"退役清单不可解析（{type(exc).__name__}: {exc}）；"
                  f"按 fail-closed 一律视为仍退役")
    if not reason:
        return ""
    if allow_retired:
        log(f"⚠ 绕过退役清单投递 {sid[:20]}…：{reason}")
        return ""
    return reason


def _retired_sids() -> set[str] | None:
    """Retired sid set; ``None`` = list untrustworthy (unreadable, no usable cache).

    Resolved against the daemon's own state root (``PATHS.root``, ``--state-dir``
    wins). Three states:
      * parsed ⇒ update the cache and return it (a bad list never overwrites it);
      * file missing ⇒ return an empty set and clear the cache (a fresh root);
      * unparsable ⇒ keep the previous cache and WARN; empty cache ⇒ ``None``.
    """
    mod = _retired()
    if mod is None:
        log("退役清单未注入（retired_module），沿用上一份")
        return set(STATE.retired_cache) if STATE.retired_cache else None
    try:
        sids = set(mod.load_strict(str(PATHS.root)))
    except Exception as exc:                            # noqa: BLE001
        log(f"退役清单读坏，沿用上一份：{exc}")
        return set(STATE.retired_cache) if STATE.retired_cache else None
    STATE.retired_cache = sids
    return sids


async def _watch_new_sessions(ws) -> None:
    """Periodically discover new sessions, subscribe, and fill gaps via REST."""
    while True:
        await asyncio.sleep(10)
        try:
            want = set(_targets())
            for sid in sorted(want - set(STATE.subscribed)):
                ack_id = await _subscribe(ws, sid)
                STATE.subscribed[sid] = {"ok": False, "ack_id": ack_id,
                                         "reason": "pending ack", "ts": time.time()}
                log(f"新增订阅 {sid[:20]}…（共 {len(STATE.subscribed)}）")
            for sid in list(STATE.subscribed):
                rec = STATE.sessions.get(sid)
                if rec and (rec.get("status") or {}).get("context_tokens") is not None:
                    continue
                info = await asyncio.to_thread(K.session_info, sid)
                if isinstance(info, dict) and "_err" not in info:
                    r = STATE.session(sid)
                    r["status"].update({
                        "busy": info.get("busy"),
                        "main_turn_active": info.get("main_turn_active"),
                        "pending_interaction": info.get("pending_interaction"),
                        "last_turn_reason": info.get("last_turn_reason"),
                    })
                    r["snapshot_ts"] = int(time.time())
                    r["ts"] = int(time.time())
                    STATE.last_snapshot_ts = time.time()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log(f"watch 异常：{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------- file events
# Event source: the local filesystem (new handoff documents, the
# "awaiting a decision" section of PM project docs).
# Contract: structurally identical to WS events (boot_id/seq/ts/sid/type/payload)
# with sid fixed to "file" (never null), so `/events?sid=file` filters them and
# `/events?since=` mixes both without a second null-check path in consumers.
FILE_EVENT_SID = "file"

DEFAULT_POLL_INTERVAL = 1.0
WATCH_WARN_EVERY = 600
WATCH_KINDS = {"dir_new_file", "file_section_md5"}


def default_watchers() -> list[dict]:
    """Configured defaults; the caller supplies the section heading and paths."""
    cfg = _cfg()
    if cfg.watch_defaults:
        return [dict(w) for w in cfg.watch_defaults]
    return [
        {"id": "handoff", "kind": "dir_new_file",
         "path": "state/asst_handoff", "event": "event.file.handoff_created"},
        {"id": "pm_decide", "kind": "file_section_md5",
         "path_glob": "state/pm/*.md", "section": cfg.decide_section,
         "event": "event.file.pm_decide_changed"},
    ]


def _role_of(name: str) -> str:
    """Infer a role from a handoff filename: ``steward_20261107-0024.md`` → ``steward``."""
    head = name.split("_", 1)[0].split("-", 1)[0].strip().lower()
    prefixes = tuple(_cfg().role_prefixes or ())
    return head if head in prefixes else "unknown"


_SECTION_NO = re.compile(r"^#+\s*")
_SECTION_LEAD_NUM = re.compile(r"^[\d]+(?:[.．]\d+)*[.．、)）]?\s*")
_SPACE_NORM = str.maketrans({"\u3000": " ", "\u00a0": " ", "\u200b": ""})
_DECIDE_HINTS = ("拍板",)


def _decide_taut() -> re.Pattern:
    """Position rule: a request word **immediately** next to the decision word.

    Orientation (deliberately permissive): one false positive costs one extra
    ping, a false negative means the feature silently stops working — the
    "awaiting a decision" section disappears and nobody notices.

    The supervisor key is interpolated from config, never hard-coded; an empty
    key simply drops that alternation.
    """
    key = re.escape((_cfg().supervisor_key or "").strip().lower())
    persons = "你|我|用户"
    if key:
        persons = f"{key}|{persons}"
    return re.compile(
        r"(?:待|需|要)\s*(?:" + persons + r")?\s*[:：]?\s*拍板"
        r"|未决[^\n]*拍板|拍板[^\n]*未决")


def _norm_heading(line: str) -> str:
    """Normalise a heading: drop ``#``/numbering/spaces, lowercase the key name."""
    s = (line or "").translate(_SPACE_NORM).strip()
    s = _SECTION_NO.sub("", s).strip()
    s = _SECTION_LEAD_NUM.sub("", s).strip()
    return re.sub(r"\s+", " ", s).strip().lower().replace("**", "")


def _norm_section_want(want: str) -> str:
    return _norm_heading(want)


def _heading_matches(line: str, want: str, pattern: str = "") -> bool:
    """Does this heading name the target section?

    Priority: ``pattern`` (configured regex on the normalised heading) > exact
    normalised match > the "request word next to the decision word" position
    rule. Known false positive: a retrospective heading that happens to put a
    request word next to the decision word. Configure ``section_pattern`` when
    precision matters; the fallback then does not participate.
    """
    head = _norm_heading(line)
    if not head:
        return False
    if pattern:
        try:
            return re.search(pattern, head) is not None
        except re.error:
            pass
    w = _norm_section_want(want)
    if w and head == w:
        return True
    if w and all(h in w for h in _DECIDE_HINTS):
        return _decide_taut().search(head) is not None
    return False


def _section_of(text: str, section: str, pattern: str = "") -> str | None:
    """Body of a markdown section (heading excluded); None when absent.

    ``##`` and ``###`` both count (level is not restricted), and the section runs
    until the next heading at the same or a higher level, so ``###`` subheadings
    inside ``##`` are not truncated.
    """
    if not (section or "").strip() and not pattern:
        return None
    lines = text.splitlines()
    out: list[str] = []
    inside = False
    level = 0
    for ln in lines:
        stripped = ln.strip()
        m = re.match(r"^(#{2,6})\s+\S", stripped)
        if m:
            if inside:
                if len(m.group(1)) <= level:
                    break
                out.append(ln)      # deeper heading is section content
                continue
            if _heading_matches(stripped, section, pattern):
                inside = True
                level = len(m.group(1))
            continue
        if inside:
            out.append(ln)
    return "\n".join(out) if inside else None


class WatchConfig:
    """Watch configuration (hot-reloadable). A bad load keeps the previous one."""

    def __init__(self, watchers=None, poll_interval=DEFAULT_POLL_INTERVAL, error="",
                 root=""):
        self.watchers = list(watchers if watchers is not None else default_watchers())
        self.poll_interval = float(poll_interval or DEFAULT_POLL_INTERVAL)
        self.error = error
        self.root = str(root or "")
        self.mtime = 0.0
        self.from_file = False

    @staticmethod
    def validate(data) -> tuple[list[dict], float, str]:
        """Validate and normalise; anything illegal raises ValueError."""
        if not isinstance(data, dict):
            raise ValueError("配置顶层必须是对象")
        raw = data.get("watchers")
        if not isinstance(raw, list) or not raw:
            raise ValueError("watchers 必须是非空数组")
        out: list[dict] = []
        seen: set[str] = set()
        for i, w in enumerate(raw):
            if not isinstance(w, dict):
                raise ValueError(f"watchers[{i}] 不是对象")
            wid = str(w.get("id") or "").strip()
            if not wid:
                raise ValueError(f"watchers[{i}] 缺 id")
            if wid in seen:
                raise ValueError(f"watchers id 重复：{wid}")
            seen.add(wid)
            kind = str(w.get("kind") or "").strip()
            if kind not in WATCH_KINDS:
                raise ValueError(f"watchers[{i}] kind 非法：{kind!r}")
            ev = str(w.get("event") or "").strip()
            if not ev:
                raise ValueError(f"watchers[{i}] 缺 event")
            item = {"id": wid, "kind": kind, "event": ev}
            if kind == "dir_new_file":
                if not str(w.get("path") or "").strip():
                    raise ValueError(f"watchers[{i}] dir_new_file 缺 path")
                item["path"] = str(w["path"]).strip()
            else:
                if not str(w.get("path_glob") or "").strip():
                    raise ValueError(f"watchers[{i}] file_section_md5 缺 path_glob")
                item["path_glob"] = str(w["path_glob"]).strip()
                item["section"] = str(w.get("section") or "").strip()
                pat = str(w.get("section_pattern") or "").strip()
                if pat:
                    try:
                        re.compile(pat)
                    except re.error as exc:
                        raise ValueError(
                            f"watchers[{i}] section_pattern 非法：{exc}") from None
                    item["section_pattern"] = pat
            out.append(item)
        try:
            poll = float(data.get("poll_interval_s") or DEFAULT_POLL_INTERVAL)
        except (TypeError, ValueError):
            raise ValueError("poll_interval_s 不是数字") from None
        if not (0.2 <= poll <= 60):
            raise ValueError(f"poll_interval_s 越界：{poll}")
        return out, poll, str(data.get("root") or "").strip()

    @classmethod
    def load(cls, path: Path, previous: "WatchConfig | None" = None) -> "WatchConfig":
        """Read the config file; a bad config returns a copy of the previous one."""
        prev = previous or cls()
        try:
            raw = path.read_text(encoding="utf-8")
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            cfg = cls(default_watchers(), DEFAULT_POLL_INTERVAL)
            cfg.error = ""
            cfg.from_file = False
            return cfg
        except OSError as exc:
            bad = cls(list(prev.watchers), prev.poll_interval, f"读配置失败：{exc}")
            bad.mtime, bad.from_file = 0.0, prev.from_file
            return bad
        try:
            watchers, poll, root = cls.validate(json.loads(raw))
        except (ValueError, TypeError) as exc:
            bad = cls(list(prev.watchers), prev.poll_interval,
                      f"{type(exc).__name__}: {exc}", prev.root)
            bad.mtime, bad.from_file = mtime, prev.from_file
            return bad
        cfg = cls(watchers, poll, root=root)
        cfg.mtime, cfg.from_file = mtime, True
        return cfg


class FileWatcher:
    """File event producer. ``poll_once`` is synchronous so it can be self-tested.

    "First round only seeds the baseline, emits nothing" is a hard requirement:
    otherwise a restart replays the whole directory history. The same reset
    happens after a successful hot reload — new watchers must not fire on files
    that already existed.
    """

    def __init__(self, config: WatchConfig, root: Path | None = None):
        self.config = config
        self._root = Path(root) if root else None
        self.baseline: dict[str, dict] = {}
        self.matched: dict[str, list[str]] = {}
        # Files with a heading but an empty body are not counted as matches;
        # they are kept only as observable evidence.
        self.empty_sections: dict[str, list[str]] = {}
        # Per-watcher seeding (by **id**, not globally): a global first round
        # would pause everyone when a hot reload adds one watcher, dropping files
        # created exactly then.
        self.seeded: set[str] = set()

    @property
    def root(self) -> Path:
        """Root used to resolve relative watch paths: config ``root`` or workspace."""
        return Path(self.config.root) if getattr(self.config, "root", "") else (
            self._root or _workspace_root())

    def reset_baseline(self, config: WatchConfig | None = None,
                       diff: bool = True) -> list[str]:
        """Reset baselines. ``diff=True`` is per-watcher-id:

        - same id and same criteria ⇒ keep the baseline;
        - new id or changed criteria ⇒ clear that id's baseline (rebuilt next
          round, so existing files never fire);
        - ids removed from the config ⇒ drop their tables.
        Returns the list of reset ids (for logging). ``diff=False`` restores the
        old global-reset semantics, which the counter-example test relies on.
        """
        old = self.config
        if config is not None:
            self.config = config
        if not diff:
            self.baseline = {}
            self.matched = {}
            self.empty_sections = {}
            self.seeded = set()
            return [w["id"] for w in self.config.watchers]
        if old is config:
            return []
        same = {w["id"]: w for w in old.watchers}
        keep: dict[str, dict] = {}
        reset_ids: list[str] = []
        for w in self.config.watchers:
            prev = same.get(w["id"])
            if prev is not None and _watcher_sig(prev) == _watcher_sig(w):
                if w["id"] in self.baseline:
                    keep[w["id"]] = self.baseline[w["id"]]
            else:
                reset_ids.append(w["id"])
        live = {w["id"] for w in self.config.watchers}
        self.baseline = keep
        self.matched = {k: v for k, v in self.matched.items() if k in live and k in keep}
        self.empty_sections = {k: v for k, v in self.empty_sections.items()
                               if k in live and k in keep}
        self.seeded &= (live - set(reset_ids))
        return reset_ids

    def _abs(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else (self.root / p)

    def _glob(self, pattern: str):
        """glob only accepts relative patterns; split an absolute one into base+tail."""
        p = Path(pattern)
        if p.is_absolute():
            parts = p.parts
            base = Path(parts[0])
            idx = 1
            for i, seg in enumerate(parts[1:], start=1):
                if any(ch in seg for ch in "*?["):
                    idx = i
                    break
                base = base / seg
                idx = i + 1
            tail = str(Path(*parts[idx:])) if idx < len(parts) else ""
            return list(base.glob(tail)) if tail else [base]
        return list(self.root.glob(pattern))

    def _poll_dir_new_file(self, w: dict) -> list[tuple[str, dict]]:
        d = self._abs(w["path"])
        try:
            files = [p for p in d.iterdir() if p.is_file()]
        except OSError:
            self.matched[w["id"]] = []
            return []
        cur = {}
        out: list[tuple[str, dict]] = []
        seen = self.baseline.get(w["id"], {})
        seeded = w["id"] in self.seeded
        for p in files:
            try:
                st = p.stat()
            except OSError:
                continue
            cur[p.name] = st.st_mtime
            if p.name in seen or not seeded:
                continue
            # Metadata only (name/path/time/role/size) — never the body.
            out.append((w["event"], {
                "name": p.name, "path": str(p), "mtime": st.st_mtime,
                "role": _role_of(p.name), "size": st.st_size,
            }))
        self.baseline[w["id"]] = cur
        self.matched[w["id"]] = sorted(cur)
        if not seeded:
            self.seeded.add(w["id"])
        return out

    def _poll_file_section(self, w: dict) -> list[tuple[str, dict]]:
        cur: dict[str, str] = {}
        out: list[tuple[str, dict]] = []
        seen = self.baseline.get(w["id"], {})
        matched: list[str] = []
        empty: list[str] = []
        seeded = w["id"] in self.seeded
        for p in sorted(self._glob(w["path_glob"])):
            if not p.is_file():
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            sect = _section_of(text, w.get("section") or "", w.get("section_pattern") or "")
            if sect is None:
                continue
            if not sect.strip():
                # Heading present, body empty ⇒ **not** a match. Counting it
                # would light up "matched N" on /healthz with nothing pending and
                # keep the zero-match warning from ever firing.
                empty.append(p.stem)
                continue
            matched.append(p.stem)
            md5 = hashlib.md5(sect.encode("utf-8")).hexdigest()
            cur[str(p)] = md5
            if not seeded or seen.get(str(p)) == md5:
                continue
            out.append((w["event"], qualify_event_payload({
                "project": p.stem, "md5": md5, "changed": True,
                "path": str(p),
            })))
        self.baseline[w["id"]] = cur
        self.matched[w["id"]] = matched
        self.empty_sections[w["id"]] = empty
        if not seeded:
            self.seeded.add(w["id"])
        return out

    def poll_once(self) -> list[tuple[str, dict]]:
        """One round: ``[(event_type, payload)]``. Each watcher's first round is silent."""
        events: list[tuple[str, dict]] = []
        for w in self.config.watchers:
            try:
                if w["kind"] == "dir_new_file":
                    events.extend(self._poll_dir_new_file(w))
                else:
                    events.extend(self._poll_file_section(w))
            except Exception as exc:  # noqa: BLE001  one bad watcher must not stop others
                log(f"watcher {w.get('id')} 异常：{type(exc).__name__}: {exc}")
        return events


def _watcher_sig(w: dict) -> tuple:
    """Criteria signature: any change means the baseline must be rebuilt."""
    return (w.get("kind"), w.get("path"), w.get("path_glob"),
            w.get("section"), w.get("section_pattern"))


# --------------------------------------------------------------------- guardian
# Topology (hard requirement): watchdog ← daemon ← scheduler. The daemon is the
# most reliable process on the machine, so it inspects three **guarded** parties;
# the monitor is demoted to a guarded party and carries no watchdog duty.
#
# Lesson baked in below: an isolated instance must never send real alerts. The
# isolation gate lives **inside** guardian_check (it does not depend on the caller
# passing a flag), and recipients are resolved from the **current** state root
# rather than from an alarm module's module-level constants.

# Each guarded party: its **own** heartbeat file + threshold + single writer.
# Two parties must never share a file: sharing means one dead party is masked by
# the other's fresh heartbeat.
GUARDIAN_THRESHOLDS = {
    # name           heartbeat (relative to state root)      threshold  single writer
    "delivery_watch": ("heartbeat/delivery.json", 900.0, "收单哨 delivery_tick"),
    "decide_watch":   ("heartbeat/decide.json",   900.0, "decide_watch 主循环"),
    "monitor":        ("heartbeat/monitor.json",  300.0, "monitor.py 的 tick（每轮无条件写）"),
}

GUARDIAN_EXTRA_WRITER = "(外部注册)"
_GUARDIAN_WARN_LAST: dict[str, float] = {}


def _guardian_warn(msg: str) -> None:
    """Degraded WARN for external registration: **stderr only, never a file**.

    Writing no file is deliberate: an isolated instance reads its own root, so any
    disk write would need an isolation check first — exactly the trap that leaked
    alerts before. A WARN is meant for a human anyway. The same line is emitted at
    most once per ``guardian_warn_every`` because ``_guardian_summary()`` runs on
    every patrol round and a bad file would otherwise flood the log.
    """
    now = time.time()
    every = float(_cfg().guardian_warn_every or 300.0)
    last = _GUARDIAN_WARN_LAST.get(msg)
    if last is not None and now - last < every:
        return
    if len(_GUARDIAN_WARN_LAST) > 32:
        _GUARDIAN_WARN_LAST.clear()
    _GUARDIAN_WARN_LAST[msg] = now
    sys.stderr.write(f"[wss_bus] WARN guardian_extra: {msg}\n")
    sys.stderr.flush()


def guardian_thresholds() -> dict[str, tuple[str, float, str]]:
    """Built-ins ∪ ``state_root/guardian_extra.json`` — external registration.

    Semantics (hard requirements):
      · file absent ⇒ built-ins only (**no WARN, no error**);
      · bad JSON / non-dict top level ⇒ built-ins plus **one WARN** (no crash);
      · a malformed entry ⇒ skip that entry + WARN, the rest still apply;
      · **same name ⇒ built-in wins** (external registration may never weaken
        an existing guard).

    The file is read on **every call** (never cached): the summary runs each round
    and an external edit must take effect immediately; the file is <1 KB, so the
    read costs far less than "changed but not applied".
    Which root is read comes from ``current_state_root()`` — an isolated instance
    only ever sees its own registration.
    """
    out: dict[str, tuple[str, float, str]] = dict(GUARDIAN_THRESHOLDS)
    path = current_state_root() / _cfg().guardian_extra_file
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return out                     # no file ⇒ no extras: not a warning
    except OSError as exc:
        _guardian_warn(f"{path} 读不出（{type(exc).__name__}: {exc}）"
                       f" ⇒ 只监护内置 {len(out)} 项")
        return out
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        _guardian_warn(f"{path} 坏 JSON（{type(exc).__name__}: {exc}）"
                       f" ⇒ 只监护内置 {len(out)} 项")
        return out
    if not isinstance(data, dict):
        _guardian_warn(f"{path} 顶层应为对象，实际 {type(data).__name__}"
                       f" ⇒ 只监护内置 {len(out)} 项")
        return out
    for name, spec in data.items():
        if not isinstance(spec, dict):
            _guardian_warn(f"{path} 条目 {name!r} 应为对象，实际 {type(spec).__name__} ⇒ 跳过")
            continue
        hb = spec.get("heartbeat")
        if not isinstance(hb, str) or not hb.strip():
            _guardian_warn(f"{path} 条目 {name!r} 的 heartbeat 应为非空字符串"
                           f"（实际 {hb!r}）⇒ 跳过")
            continue
        th = spec.get("threshold_s")
        if isinstance(th, bool) or not isinstance(th, (int, float)) or not th > 0:
            _guardian_warn(f"{path} 条目 {name!r} 的 threshold_s 应为 >0 的数字"
                           f"（实际 {th!r}）⇒ 跳过")
            continue
        writer = spec.get("writer", GUARDIAN_EXTRA_WRITER)
        if not isinstance(writer, str) or not writer.strip():
            writer = GUARDIAN_EXTRA_WRITER
        if name in GUARDIAN_THRESHOLDS:
            _guardian_warn(f"{path} 条目 {name!r} 与内置重名 ⇒ 内置优先，外部注册不生效")
            continue
        out[name] = (hb.strip(), float(th), writer)
    return out


def production_state_root() -> Path:
    """Production state root — the single baseline used to detect isolation."""
    return _production_root()


def state_override_envs() -> list[tuple[str, str]]:
    """Every environment variable that can relocate the state root (set ones only)."""
    out = []
    for name in state_env_names():
        val = (os.environ.get(name) or "").strip()
        if val:
            out.append((name, val))
    return out


def is_isolated_state() -> bool:
    """Is this process running against a non-production state root?

    Two historical holes are both closed here:
      ① only ``PATHS.root`` was checked, but ``PATHS`` is a module singleton built
        once from ``--state-dir``, so instances isolated by an **environment
        variable** sailed straight through;
      ② therefore every environment override is also tested.
    Paths are compared after ``resolve()``; any resolution failure counts as
    isolated (better not to send than to send wrongly).
    """
    prod = Path(production_state_root()).resolve()
    try:
        if Path(PATHS.root).resolve() != prod:
            return True
    except (OSError, RuntimeError):
        return True
    for _name, val in state_override_envs():
        try:
            if Path(os.path.expanduser(val)).resolve() != prod:
                return True
        except OSError:
            return True
    return False


def _guardian_state_path() -> Path:
    """Dedup state file — **same source** as the read side (``current_state_root()``).

    Using ``PATHS.root`` here was a P1: an isolated instance wrote
    ``wss_bus_guardian_state.json`` into production, production then read "just
    reported" and silenced a real alert for 30 minutes. Write and read must share
    one root.
    """
    return current_state_root() / _cfg().guardian_state_file


def _guardian_state_load() -> dict:
    """Read the dedup state from disk; unreadable means empty (patrol still runs)."""
    try:
        d = json.loads(_guardian_state_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _guardian_state_save(state: dict) -> None:
    try:
        p = _guardian_state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
    except OSError as exc:
        log(f"guardian: 状态落盘失败（不影响本轮）：{exc}")


def read_heartbeat(name: str) -> dict | None:
    """Read one guarded party's heartbeat; returns ``{ts, ...}``, None, or a marker.

    The three failures are kept apart on purpose:
      * ``FileNotFoundError`` ⇒ ``None`` (state=missing, the original meaning);
      * any other ``OSError`` (``PermissionError``, …) ⇒ ``{"_read_error": "<class>"}``
        (state=unknown — on Windows a concurrent read during ``os.replace`` must
        never be mistaken for "file does not exist");
      * ``ValueError`` (bad JSON) ⇒ ``None`` (still missing; unchanged here).
    """
    rel = guardian_thresholds().get(name, ("", 0.0, ""))[0]
    if not rel:
        return None
    try:
        d = json.loads((current_state_root() / rel).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except FileNotFoundError:
        return None
    except OSError as exc:
        return {"_read_error": type(exc).__name__}
    except ValueError:
        return None


def write_heartbeat(name: str, extra: dict | None = None) -> None:
    """Write a heartbeat (single writer per name, see the third column above).

    **Unconditional every round**: "no change" must not look stale.
    **Which root**: ``current_state_root()`` — same source as the read side,
    otherwise an isolated instance writes production and production reads
    production, looking "healthy" on someone else's heartbeat.
    """
    rel = guardian_thresholds().get(name, ("", 0.0, ""))[0]
    if not rel:
        return
    root = current_state_root()
    body = {"ts": time.time(), "iso": datetime.now().isoformat(timespec="seconds"),
            "pid": os.getpid(), "boot_id": STATE.seq.boot_id, "state_dir": str(root)}
    if extra:
        body.update(extra)
    try:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except OSError as exc:
        log(f"guardian: 写 {name} 心跳失败：{exc}")


def current_state_root() -> Path:
    """**Single source for runtime state**: reads and writes both come from here.

    Priority (an explicit ``--state-dir`` wins):
      ① ``PATHS.root`` ≠ production ⇒ the process was started with ``--state-dir``
         (or a self-test swapped PATHS); every runtime file lives there, so reads
         and writes must follow it — otherwise one side writes production while
         the other reads the isolated root;
      ② otherwise any environment override;
      ③ otherwise ``PATHS.root`` (production).

    History (do not undo): reads once honoured the environment while writes still
    used ``PATHS.root`` ⇒ isolated instances wrote the dedup state into production
    ⇒ production saw "just reported" and silenced a real alert for 30 minutes.
    Reads and writes diverging is the single root cause of that class of incident.
    """
    try:
        pats = Path(PATHS.root).resolve()
    except (OSError, RuntimeError):
        pats = Path(PATHS.root) if not isinstance(PATHS, _UnconfiguredPaths) else Path(
            tempfile.gettempdir())
    try:
        if pats != Path(production_state_root()).resolve():
            return pats
    except OSError:
        return pats
    for _name, val in state_override_envs():
        try:
            return Path(os.path.expanduser(val)).resolve()
        except OSError:
            return Path(os.path.expanduser(val))
    return pats


def _guardian_summary() -> dict:
    """``/healthz.guardians``: ``{state, age_s, threshold_s, heartbeat, writer}`` each.

    Four states (never pretend "ok"):
      ok       — heartbeat fresh (age <= threshold)
      stale    — heartbeat expired (age > threshold)
      missing  — heartbeat file absent / bad JSON (process never started, or never
                 wrote a heartbeat)
      unknown  — another OSError (transient read failure) ⇒ **no alert**, one log
                 line, and ``read_error`` surfaced on ``/healthz.guardians``.
                 On Windows a ``PermissionError`` from a concurrent read during
                 ``os.replace`` must never be treated as "file does not exist".

    **Which root**: ``current_state_root()`` (same source as the write side). An
    isolated instance only ever sees its own heartbeats and never borrows
    production's freshness as its own alibi.
    """
    out: dict[str, dict] = {}
    now = time.time()
    root = current_state_root()
    every = float(_cfg().guardian_warn_every or 300.0)
    for name, (rel, threshold, writer) in guardian_thresholds().items():
        hb = read_heartbeat(name)
        read_error = str((hb or {}).get("_read_error") or "")
        age = None
        if hb is not None and not read_error:
            ts = hb.get("ts")
            if isinstance(ts, (int, float)) and ts > 0:
                age = max(0.0, now - float(ts))
            else:
                try:
                    age = max(0.0, now - (root / rel).stat().st_mtime)
                except OSError:
                    age = None
        if read_error:
            state = "unknown"
            msg = f"guardian: {name} 心跳读取失败（{read_error}）⇒ state=unknown，不告警"
            last = _GUARDIAN_WARN_LAST.get(msg)
            if last is None or now - last >= every:
                if len(_GUARDIAN_WARN_LAST) > 32:
                    _GUARDIAN_WARN_LAST.clear()
                _GUARDIAN_WARN_LAST[msg] = now
                log(f"WARN {msg}")
        elif age is None:
            state = "missing"
        elif age > threshold:
            state = "stale"
        else:
            state = "ok"
        rec = {"state": state,
               "age_s": None if age is None else round(age, 1),
               "threshold_s": threshold, "heartbeat": rel, "writer": writer,
               "pid": (hb or {}).get("pid"),
               "state_dir": (hb or {}).get("state_dir", "")}
        if read_error:
            rec["read_error"] = read_error
        out[name] = rec
    return out


def _guardian_alert_due(summary: dict, state: dict) -> list[dict]:
    """Which parties need an alert (after dedup). ``state`` persists across rounds.

    **Dedup**: the same party in the same state alerts once per
    ``guardian_alert_dedup_s``; **recovery resets it** (state becomes ok ⇒ the
    record is dropped and a later break alerts again); **unknown is never
    delivered** (a transient read failure must not make noise) **and never
    consumes a dedup slot**.
    """
    now = time.time()
    dedup = float(_cfg().guardian_alert_dedup_s or 1800.0)
    due = []
    for name, rec in summary.items():
        st = rec["state"]
        if st in ("ok", "unknown"):
            state.pop(name, None)          # recovery resets; unknown neither sends nor reserves
            continue
        prev = state.get(name) or {}
        if prev.get("state") == st and now - float(prev.get("ts") or 0) < dedup:
            continue
        state[name] = {"state": st, "ts": now}
        due.append({"name": name, **rec})
    return due


def guardian_context() -> dict:
    """The three facts every alert must carry: which state-dir, which boot, when.

    Using ``PATHS.root`` here would print the production root even for an isolated
    run, making the alert look like production's — which defeats the point.
    """
    return {"generated_at": datetime.now().isoformat(timespec="seconds"),
            "state_dir": str(current_state_root()),
            "boot_id": str(STATE.seq.boot_id),
            "isolated": is_isolated_state()}


def _guardian_alert_text(items: list[dict], ctx: dict | None = None) -> str:
    """Alert text carrying ``generated_at`` / ``state_dir`` / ``boot_id``.

    Without that line the recipient cannot tell an isolated instance from
    production — the direct lesson of the false-alert incident.
    """
    ctx = ctx or guardian_context()
    parts = []
    for it in items:
        age = it.get("age_s")
        parts.append(f"{it['name']}={it['state']}"
                     + (f"（{int(age)}s 无心跳，阈值 {int(it['threshold_s'])}s）"
                        if age is not None else "（心跳文件不存在）"))
    flag = "【隔离实例·本不该发出】" if ctx.get("isolated") else ""
    return (f"【守护告警】{flag}以下被守护者心跳异常：" + "；".join(parts)
            + f"。\n来源：state_dir={ctx['state_dir']} boot_id={ctx['boot_id']}"
            f" generated_at={ctx['generated_at']}"
            f"\n如无在途任务，保持静默，请勿额外分析。")


def _guardian_live_pm_sids() -> list[tuple[str, str]]:
    """Recipients: resolved from **the current state root's** sessions registry.

    Deliberately does not borrow an alarm module's ``live_pm_sids()``: that reads
    production paths from its own module constants, so ``--state-dir`` never
    reaches it — the direct cause of the false-alert incident.
    """
    try:
        db = json.loads((current_state_root() / "sessions.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(db, dict):
        return []
    probe = _cfg().probe_session
    if probe is None:
        return []
    out = []
    for name, val in sorted(db.items()):
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


def _load_alarm_module():
    """Load the alarm recipient module: injected, else the package default.

    **No blanket Exception handler here**: a missing internal dependency of the
    injected module must propagate, otherwise "silently broken after migration"
    gets swallowed into one log line by an ``except Exception`` upstream.
    """
    cfg = _cfg()
    if cfg.alarm_module is not None:
        mod = cfg.alarm_module
    else:
        from . import session_alarm as mod      # same package, never a workbench module
    if hasattr(mod, "configure"):
        root = current_state_root()
        kwargs = dict(state_root=root, sessions=root / "sessions.json",
                      log=root / "monitor.log",
                      state_file=root / cfg.alarm_state_file,
                      live_root=production_state_root(), probe=_cfg().probe_session)
        if cfg.send_transport is None:
            kwargs["sess_script"] = None
        mod.configure(**kwargs)
    return mod


def _probe_session(sid: str):
    """Session liveness probe (injected; this module never imports a client)."""
    probe = _cfg().probe_session
    if probe is None:
        return None, "未注入会话探测函数（probe_session）"
    return probe(sid)


def _guardian_send(sid: str, text: str) -> bool:
    """Send one alert. Returns whether it was **really** delivered (prompt_id seen)."""
    cfg = _cfg()
    if cfg.send_transport is not None:
        res = cfg.send_transport(sid, text)
        if isinstance(res, dict):
            return bool(res.get("delivered"))
        return bool(res)
    alarm = _load_alarm_module()
    return bool(alarm.send_to(sid, text).get("delivered"))


def guardian_check(do_notify: bool = False) -> dict:
    """Run one patrol round: stale/missing ⇒ alert.

    **Default is not to notify.** A previous version defaulted to True, so any
    ``import wss_bus; guardian_check()`` really sent — that is how an import in
    production emitted fourteen messages. Only the production daemon's main loop
    passes True explicitly; every other caller owns that decision.

    Rationale for the opposite default from "real by default" elsewhere: this is
    an alarm, not business traffic. A false alarm pollutes everyone's context (80+
    messages in one day) while a missed one still leaves logs and
    ``/healthz.guardians``.

    **Isolation gate (hard requirement)**: inside this function, never relying on
    the caller. An isolated instance (including one isolated by environment
    variable) forces ``channel="isolated"`` — machine-checkable, not merely
    "did not send".

    **The guarded party is never a recipient** — it is the broken leg. Recipients
    come only from the current state root's registry; none ⇒ user channel.
    """
    isolated = is_isolated_state()
    # An isolated instance must never read production's dedup state; it starts
    # empty, so "just reported" records cannot silence its alerts.
    if isolated:
        STATE.guardian_state = {}
    elif not STATE.guardian_state:
        STATE.guardian_state = _guardian_state_load()
    summary = _guardian_summary()
    gstate = STATE.guardian_state
    before = {k: dict(v) for k, v in gstate.items()}
    due = _guardian_alert_due(summary, gstate)
    ctx = guardian_context()
    out = {"summary": {k: v["state"] for k, v in summary.items()},
           "due": [d["name"] for d in due], "channel": "none",
           "notified": [], **ctx}
    if not due:
        if gstate != before:
            _guardian_state_save(gstate)
        return out
    text = _guardian_alert_text(due, ctx)
    if isolated:
        # Hard gate: isolated instances never message a human and never write
        # production's monitor.log.
        out["channel"] = "isolated"
        out["text"] = text
        _guardian_state_save(gstate)
        log(f"guardian[ISOLATED]: due={out['due']} => 不投递（state_dir={ctx['state_dir']}）")
        return out
    if not do_notify:
        out["channel"] = "check-only"
        _guardian_state_save(gstate)
        return out
    for name, sid in _guardian_live_pm_sids():
        try:
            if _guardian_send(sid, text):
                out["notified"].append({"name": name, "sid": sid})
        except Exception as exc:  # noqa: BLE001
            log(f"guardian: 催 {name} 失败（{type(exc).__name__}）")
    if out["notified"]:
        out["channel"] = "pm"
    else:
        out["channel"] = "user"
        try:
            (current_state_root() / "monitor.log").open("a", encoding="utf-8").write(
                f"{datetime.now().strftime('%H:%M')} {text}\n")
        except OSError:
            pass
        log("guardian: 无活 pm-* => 退用户通道（state/monitor.log）")
    _guardian_state_save(gstate)
    log(f"guardian: {out['summary']} due={out['due']} channel={out['channel']}")
    return out


def _supervisor_key_summary() -> dict:
    """``/healthz`` three fields describing the supervisor key's health.

    Why not query liveness here: ``/healthz`` is a hot path (every watchdog round,
    plus humans). Querying REST would turn milliseconds into seconds, and the key
    state is a slow variable.

    Sources, in priority order:
      ① ``<state root>/<alarm_state_file>`` — written by the alarm module each
         tick (it carries the decision timestamp);
      ② the age of the most recent ``<alarm_event>`` frame in the event ring.

    **Staleness rule**: if the state file's ``ts`` (or its mtime) is older than
    ``supervisor_state_fresh_s`` ⇒ do not trust it, report ``stale``, and fill
    ``age`` with the **real** age (not None). A stale ``{last_state:dead}`` would
    glue an alert on forever, while a stale ``ok`` would falsely reassure; both
    must read as "don't know".
    Both unavailable ⇒ ``unknown`` (never pretend ok).
    """
    cfg = _cfg()
    out = {"supervisor_key_state": "unknown", "supervisor_key_sid": "",
           "last_supervisor_alarm_age_s": None}
    now = time.time()
    fresh = float(cfg.supervisor_state_fresh_s or 600.0)
    try:
        sp = current_state_root() / cfg.alarm_state_file
        d = json.loads(sp.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            age = None
            ts = d.get("ts")
            if isinstance(ts, (int, float)) and ts > 0:
                age = now - float(ts)
            else:
                try:
                    age = now - sp.stat().st_mtime
                except OSError:
                    age = None
            raw_state = str(d.get("last_state") or "unknown")
            out["supervisor_key_sid"] = str(d.get("sid") or "")
            if age is not None and age > fresh:
                out["supervisor_key_state"] = "stale"
                out["last_supervisor_alarm_age_s"] = round(age, 1)
                out["supervisor_key_sid_stale"] = out["supervisor_key_sid"]
                out["supervisor_key_state_raw"] = raw_state
            else:
                out["supervisor_key_state"] = raw_state
                if age is not None:
                    out["last_supervisor_alarm_age_s"] = round(age, 1)
    except (OSError, ValueError):
        pass
    # Age of the newest dead-key frame in the ring — the hardest evidence, since
    # it crosses processes and restarts.
    watcher_type = cfg.alarm_event
    if watcher_type:
        for ev in reversed(STATE.events):
            if ev.get("type") != watcher_type:
                continue
            ev_age = round(now - float(ev.get("ts") or now), 1)
            payload = ev.get("payload") or {}
            if out["last_supervisor_alarm_age_s"] is None:
                out["last_supervisor_alarm_age_s"] = ev_age
            if not out["supervisor_key_sid"]:
                out["supervisor_key_sid"] = str(payload.get("old_sid") or "")
            if out["supervisor_key_state"] == "unknown":
                out["supervisor_key_state"] = ("dead" if ev_age <= fresh else "stale")
            break
    return out


SUPERVISOR_STATE_FRESH_S = 600.0   # default freshness window for the key summary


def _watch_summary() -> dict:
    items = []
    any_zero = False
    fw = STATE.watch_file_watcher      # only for the empty_sections evidence (may be None)
    for wid, rec in sorted(STATE.watchers.items()):
        # Internal keys (e.g. `_warn_ts`) must never show up on /healthz.
        pub = {k: v for k, v in rec.items() if not str(k).startswith("_")}
        kind = str(pub.get("kind") or "")
        is_dir = kind == "dir_new_file"
        empties = list((fw.empty_sections.get(wid) if fw else []) or [])
        # A directory watcher reports file_count (an empty directory is normal and
        # carries no warning); a section watcher reports matched_count (zero means
        # the criteria are probably wrong).
        n = int(pub.get("matched_count") or 0)
        pub["file_count"] = n if is_dir else 0
        warn = ""
        if not is_dir and not n:
            warn = ("matched_files=0（该 watcher 没有命中任何**有内容**的段，"
                    "判据可能写错或待拍板项已清空）")
            any_zero = True
        pub.update(matched_files=list(pub.get("matched_files") or []),
                   empty_sections=empties, warning=warn)
        items.append(dict(pub, id=wid))
    return {
        "count": len(STATE.watchers),
        "poll_interval_s": getattr(STATE.watch_config, "poll_interval", None),
        "config_error": STATE.watch_config_error,
        "zero_match_warning": any_zero,
        "items": items,
    }


async def _watch_files() -> None:
    """File event producer: same lifetime as the WS producer."""
    cfg = WatchConfig.load(PATHS.watch)
    if not cfg.from_file:
        log(f"watch 配置不存在（{PATHS.watch.name}），用内置默认 {len(cfg.watchers)} 条")
    STATE.watch_config = cfg
    watcher = FileWatcher(cfg, _workspace_root())
    STATE.watch_file_watcher = watcher
    STATE.watchers = {w["id"]: {"kind": w["kind"], "event": w["event"],
                                "last_fire_ts": 0.0, "fires": 0}
                      for w in cfg.watchers}
    last_mtime = cfg.mtime
    while True:
        await asyncio.sleep(max(0.2, cfg.poll_interval))
        # Hot reload: re-read only when the config mtime changes; a bad config
        # keeps the previous one running.
        try:
            mtime = PATHS.watch.stat().st_mtime
        except OSError:
            mtime = 0.0
        if mtime != last_mtime:
            last_mtime = mtime
            newcfg = WatchConfig.load(PATHS.watch, previous=cfg)
            if newcfg.error:
                STATE.watch_config_error = newcfg.error
                log(f"watch 配置加载失败，沿用旧配置（{len(cfg.watchers)} 条）：{newcfg.error}")
            else:
                STATE.watch_config_error = ""
                prev_stats = {wid: dict(rec) for wid, rec in STATE.watchers.items()}
                reset_ids = watcher.reset_baseline(newcfg)
                cfg = newcfg
                STATE.watch_config = cfg
                kept = {}
                for w in cfg.watchers:
                    prev = prev_stats.get(w["id"]) or {}
                    keep_stats = w["id"] not in reset_ids
                    kept[w["id"]] = {
                        "kind": w["kind"], "event": w["event"],
                        "last_fire_ts": prev.get("last_fire_ts", 0.0) if keep_stats else 0.0,
                        "fires": prev.get("fires", 0) if keep_stats else 0,
                        "matched_count": prev.get("matched_count", 0) if keep_stats else 0,
                        "matched_files": prev.get("matched_files", []) if keep_stats else [],
                    }
                STATE.watchers = kept
                log(f"watch 配置已热加载：{len(cfg.watchers)} 条，"
                    f"poll={cfg.poll_interval:.1f}s，重建基线={reset_ids or '无'}")
        try:
            events = watcher.poll_once()
            for wid, rec in STATE.watchers.items():
                names = watcher.matched.get(wid, [])
                rec["matched_count"] = len(names)
                rec["matched_files"] = names[:20]
                # An empty directory is a normal state for a directory watcher.
                if not names and rec.get("kind") != "dir_new_file":
                    if time.time() - float(rec.get("_warn_ts") or 0) >= WATCH_WARN_EVERY:
                        rec["_warn_ts"] = time.time()
                        log(f"watcher {wid} 命中 0 个有内容的段（判据可能写错）")
            for etype, payload in events:
                ev = STATE.add_event(FILE_EVENT_SID, etype,
                                     qualify_event_payload(payload))
                _append_event_file(ev)
                wid = _watcher_id_of(etype)
                rec = STATE.watchers.get(wid)
                if rec is not None:
                    rec["last_fire_ts"] = ev["ts"]
                    rec["fires"] = int(rec.get("fires") or 0) + 1
                log(f"文件事件 seq={ev['seq']} {etype} "
                    f"{payload.get('name') or payload.get('project')}")
            # Guardian patrol: its own try, so a failure cannot stop file events.
            try:
                guardian_check(do_notify=True)
            except Exception as exc:  # noqa: BLE001
                log(f"guardian 巡检异常（不阻断）：{type(exc).__name__}: {exc}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log(f"watch 循环异常：{type(exc).__name__}: {exc}")


def _watcher_id_of(etype: str) -> str:
    for wid, rec in STATE.watchers.items():
        if rec.get("event") == etype:
            return wid
    return ""


def announce_upstream() -> str:
    """Print where the upstream came from (port / source / degraded) at startup.

    A degraded resolution is stated with its reason; startup argument semantics are
    unchanged. Returns the one-line summary for tests and logs.

    "Source" means **where the base URL came from**, not the protocol version:
      * ``env``       — an explicit ``KIMI_SERVER`` override;
      * ``instance``  — the newest readable instance registration;
      * ``default``   — neither, so the hard-coded default port is used and the
        daemon may be talking to the wrong server ⇒ degraded, with the reason.
    """
    try:
        url = K._ws_url()
    except Exception as exc:  # noqa: BLE001  reporting must never break startup
        url = f"<解析失败 {type(exc).__name__}: {exc}>"
    src = "default"
    reason = ""
    try:
        if kimi_paths._env_base() is not None:
            src = "env"
        else:
            rec = kimi_paths.latest_instance()
            if rec is not None:
                src = "instance"
            else:
                reason = ("实例注册不可用（instances 目录无有效记录）"
                          "⇒ 回落默认端口，可能连错服务")
    except Exception as exc:  # noqa: BLE001
        src = "unknown"
        reason = f"{type(exc).__name__}: {exc}"
    degraded = src != "instance"
    note = "" if not degraded else f"（降级：source={src}；{reason or '非实例注册'}）"
    line = f"上游解析：port={PORT} source={src} ws_url={url} degraded={degraded}{note}"
    LAST_ANNOUNCE["ws_url"] = str(url)
    LAST_ANNOUNCE["note"] = note
    print(line, flush=True)
    return line


# --------------------------------------------------------------------- producer
async def producer() -> None:
    import aiohttp

    backoff = RECONNECT_MIN
    while True:
        try:
            token = kimi_paths.token()
            if not token:
                log(f"读不到 token（{kimi_paths.token_file()}）；{backoff:.0f}s 后重试")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX)
                continue
            async with aiohttp.ClientSession() as sess:
                async with sess.ws_connect(
                        K._ws_url(), headers={"Authorization": f"Bearer {token}"},
                        heartbeat=20, timeout=aiohttp.ClientWSTimeout(ws_close=5)) as ws:
                    WS_REF["ws"] = ws
                    WS_REF["loop"] = asyncio.get_running_loop()
                    await _handshake(ws)
                    STATE.connected = True
                    await _note_recovered()
                    backoff = RECONNECT_MIN
                    log(f"已连接，请求订阅 {len(STATE.subscribed)} 个会话")
                    watcher = asyncio.create_task(_watch_new_sessions(ws))
                    try:
                        async for msg in ws:
                            if msg.type != aiohttp.WSMsgType.TEXT:
                                if msg.type in (aiohttp.WSMsgType.CLOSED,
                                                aiohttp.WSMsgType.ERROR):
                                    log(f"WS 关闭：{msg.type.name} code={ws.close_code}")
                                    break
                                continue
                            try:
                                frame = json.loads(msg.data)
                            except ValueError:
                                continue
                            t = frame.get("type") or ""
                            if t == "ping":
                                nonce = (frame.get("payload") or {}).get("nonce") or ""
                                await ws.send_json({"type": "pong",
                                                    "payload": {"nonce": nonce}})
                                continue
                            if t in ("pong", "server_hello"):
                                continue
                            if t in ("ack", "error"):
                                _mark_ack(frame)
                                continue
                            ev = absorb(frame)
                            if ev:
                                _append_event_file(ev)
                        log(f"WS 循环结束（无异常）：close_code={ws.close_code}")
                    finally:
                        watcher.cancel()
                        WS_REF["ws"] = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log(f"连接断开：{type(exc).__name__}: {exc}")

        STATE.connected = False
        STATE.reconnects += 1
        if STATE.gap_started_at is None:
            STATE.gap_started_at = time.time()
        wait = backoff + random.uniform(0, backoff * 0.25)
        log(f"重连中：{wait:.1f}s 后（第 {STATE.reconnects} 次）")
        await asyncio.sleep(wait)
        backoff = min(backoff * 2, RECONNECT_MAX)


async def _note_recovered() -> None:
    if STATE.gap_started_at:
        secs = time.time() - STATE.gap_started_at
        STATE.gaps.append({"from": STATE.gap_started_at, "to": time.time(),
                           "seconds": round(secs, 1)})
        STATE.gaps = STATE.gaps[-50:]
        STATE.gap_started_at = None
        log(f"断线间隙 {secs:.1f}s 已结束（该窗口事件不可补，已标 gap）")


# --------------------------------------------------------------------- persist
def _write_cache() -> None:
    PATHS.mkdir()
    payload = {
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "boot_id": STATE.seq.boot_id,
        "connected": STATE.connected,
        "seq": STATE.seq.seq,
        "sessions": STATE.sessions,
        "subscribed": STATE.subscribed,
    }
    tmp = PATHS.cache.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, PATHS.cache)
    STATE.seq.save()


# --------------------------------------------------------------------- control
def control(body: dict) -> tuple[int, dict]:
    """Perform a control action. Returns ``(http_status, payload)``.

    Three-state acceptance:
      · a request id already in the journal ⇒ 200 + ``state="indeterminate"``,
        **never resend**;
      · no request id, or no journal record ⇒ one direct attempt, then journal it;
      · journal unreadable ⇒ 200 + ``state="indeterminate"``, **never retry**.
    4xx and business error codes are returned verbatim (no downgrade, no replay).
    """
    verb = body.get("verb") or ""
    sid = body.get("sid") or ""
    rid = body.get("request_id") or ""

    if not sid and verb != "create":
        return 400, {"ok": False, "state": "rejected", "err": "missing sid"}
    # `unsubscribe` is a local idempotent action (drop the subscription, record the
    # retirement): no upstream call, so it is exempt from the journal. A broken
    # journal must not be able to block a rotation cut.
    if not STATE.journal.ok and verb != "unsubscribe":
        return 200, {"ok": False, "state": "indeterminate",
                     "err": "journal unreadable —— 不重试，请人工对账"}

    if rid:
        rec = STATE.journal.get(rid)
        if rec:
            return 200, {"ok": False, "state": "indeterminate",
                         "err": "request_id 已受理（有上游 id），禁止重发",
                         "accepted_at": rec.get("at"),
                         "upstream": {k: rec.get(k) for k in
                                      ("prompt_id", "approval_id", "task_id") if rec.get(k)}}

    try:
        return _control_dispatch(verb, sid, body, rid)
    except Exception as exc:  # noqa: BLE001
        return 500, {"ok": False, "state": "error",
                     "err": f"{type(exc).__name__}: {exc}"}


def _control_dispatch(verb: str, sid: str, body: dict, rid: str) -> tuple[int, dict]:
    if verb == "send":
        text = body.get("text") or ""
        # A retired sid takes no work. The daemon is the likeliest bypass (a caller
        # can write the old id straight into a payload), so check before sending.
        # An unreadable list refuses too: `_retired_sids()` returning None means
        # untrusted ⇒ treat everything as still retired.
        sid_check = _retired_block(sid, bool(body.get("allow_retired")))
        if sid_check:
            return 200, {"ok": False, "state": "rejected", "code": "retired_sid",
                         "err": sid_check}
        ok, detail = K.send(sid, text)
        code = detail.get("code")
        if code not in (0, None):            # business error verbatim
            return 200, {"ok": False, "state": "rejected", "code": code,
                         "raw": detail.get("raw")}
        if ok:
            _journal_put(rid, verb, sid, {"prompt_id": detail.get("prompt_id")})
        return 200, {"ok": ok, "state": "accepted" if ok else "not_accepted",
                     "prompt_id": detail.get("prompt_id"),
                     "match": detail.get("match"), "http": detail.get("http")}

    if verb == "abort":
        ok, code = K.abort_session(sid)
        if code not in (0, None):
            return 200, {"ok": ok, "state": "rejected", "code": code}
        if ok:
            _journal_put(rid, verb, sid, {})
        return 200, {"ok": ok, "state": "accepted" if ok else "not_accepted", "code": code}

    if verb == "steer":
        ok, detail = K.steer(sid, body.get("text") or "")
        if detail.get("code") not in (0, None) and not ok:
            return 200, {"ok": False, "state": "rejected", "code": detail.get("code")}
        if ok:
            _journal_put(rid, verb, sid, {"prompt_id": detail.get("prompt_id")})
        return 200, {"ok": ok, "state": "accepted" if ok else "not_accepted", **detail}

    if verb == "approve":
        aid = body.get("aid") or ""
        ok, code = K.answer_interaction(sid, aid, body.get("decision") or "approved",
                                        body.get("kind") or "approval")
        if ok:
            _journal_put(rid, verb, sid, {"approval_id": aid})
        return 200, {"ok": ok, "state": "accepted" if ok else "not_accepted", "code": code}

    if verb == "cancel-task":
        tid = body.get("tid") or ""
        ok, code = K.cancel_task(sid, tid)
        if ok:
            _journal_put(rid, verb, sid, {"task_id": tid})
        return 200, {"ok": ok, "state": "accepted" if ok else "not_accepted", "code": code}

    if verb == "interaction":
        # approval / question answer. `kind` must be explicit (decision alone is
        # silently ineffective).
        aid = body.get("aid") or ""
        kind = body.get("kind") or "approval"
        decision = body.get("decision") or "approved"
        if not aid:
            return 400, {"ok": False, "state": "rejected", "err": "missing aid"}
        ok, code = K.answer_interaction(sid, aid, decision, kind)
        if ok:
            _journal_put(rid, verb, sid, {"approval_id": aid})
        return 200, {"ok": ok, "state": "accepted" if ok else "not_accepted",
                     "code": code, "kind": kind, "decision": decision}

    if verb == "create":
        # No sid yet, so sid is not required; the upstream id is the **new sid**.
        title = body.get("title") or ""
        if not title:
            return 400, {"ok": False, "state": "rejected", "err": "missing title"}
        ok, d = K.create_session(title, cwd=body.get("cwd") or "",
                                 model=body.get("model") or "")
        new_sid = (d or {}).get("id") if isinstance(d, dict) else ""
        if ok:
            _journal_put(rid, verb, new_sid or "", {"prompt_id": ""})
        return 200, {"ok": ok, "state": "accepted" if ok else "not_accepted",
                     "new_sid": new_sid}

    if verb == "unsubscribe":
        # Rotation cut: drop the sid from the subscription set and record it as
        # retired so the 10 s re-subscribe does not bring it back. **Both steps are
        # needed**: popping alone is undone in 10 s, and recording alone leaves the
        # frames arriving on this connection until it reconnects.
        key = body.get("key") or ""
        replaced_by = body.get("replaced_by") or ""
        was = STATE.subscribed.pop(sid, None)
        _retire_sid_safe(sid, key=key, replaced_by=replaced_by)
        _send_unsubscribe(sid)
        return 200, {"ok": True, "state": "accepted",
                     "was_subscribed": was is not None,
                     "subscribed_now": len(STATE.subscribed),
                     "retired": sid}

    if verb == "profile":
        ok, d = K.set_profile(sid, model=body.get("model") or "",
                              permission=body.get("permission") or "")
        if not ok:
            return 200, {"ok": False, "state": "not_accepted"}
        # Read back rather than trusting a provider echo.
        info = K.session_info(sid)
        applied = ((info.get("agent_config") or {}).get("model")
                   if isinstance(info, dict) else None)
        matched = (not body.get("model")) or (applied == body.get("model"))
        if matched:
            _journal_put(rid, verb, sid, {})
        return 200, {"ok": matched, "state": "accepted" if matched else "not_accepted",
                     "applied_model": applied}

    if verb in ("archive", "restore", "takeover"):
        if verb == "archive":
            ok, d = K.archive_session(sid)
            archived = bool(K.session_info(sid).get("archived"))
            ok = ok and archived
            extra = {"archived": archived}
        elif verb == "restore":
            ok, d = K.restore_session(sid)
            extra = {}
        else:
            ok, d = K.takeover_session(sid)
            extra = {}
        if ok:
            _journal_put(rid, verb, sid, {})
        return 200, {"ok": ok, "state": "accepted" if ok else "not_accepted", **extra}

    return 400, {"ok": False, "state": "rejected", "err": f"unknown verb {verb!r}"}


def _retire_sid_safe(sid: str, *, key: str = "", replaced_by: str = "") -> None:
    """Record the retirement; a write failure only warns (the pop already happened).

    ``state_dir`` must be explicit: the store's default resolves from a different
    environment variable, while the daemon's isolated root is ``--state-dir``.
    Without it an isolated instance writes the retirement list into production.
    """
    mod = _retired()
    if mod is None:
        log("退役清单未注入（retired_module），仅摘订阅、不落盘")
        return
    try:
        mod.retire(sid, key=key, replaced_by=replaced_by,
                   reason="bus unsubscribe", state_dir=str(PATHS.root))
        STATE.retired_cache = set(mod.load_strict(str(PATHS.root)))
    except Exception as exc:  # noqa: BLE001
        log(f"退役清单写入失败（订阅已摘，但重连后会订回来）：{type(exc).__name__}: {exc}")


def _send_unsubscribe(sid: str) -> None:
    """Best effort: tell the server to unsubscribe; silently skip if unsupported."""
    ws = WS_REF.get("ws")
    loop = WS_REF.get("loop")
    if ws is None or loop is None:
        return
    frame = {"type": "unsubscribe_v2", "id": uuid.uuid4().hex,
             "payload": {"session_id": sid}}
    try:
        asyncio.run_coroutine_threadsafe(ws.send_json(frame), loop)
    except Exception:  # noqa: BLE001  best effort: a failed frame costs us nothing
        pass


def _journal_put(rid: str, verb: str, sid: str, ids: dict) -> None:
    if not rid:
        return
    rec = {"request_id": rid, "verb": verb, "sid": sid, "at": time.time(),
           "at_str": time.strftime("%F %T")}
    rec.update(ids)
    STATE.journal.put(rec)


# --------------------------------------------------------------------- HTTP
def make_app():
    from aiohttp import web

    app = web.Application()

    def _public_guardians() -> dict:
        """`/healthz.guardians`: neutral field names, same semantics and states."""
        out = {}
        for name, rec in _guardian_summary().items():
            item = dict(rec)
            item["supervisor"] = str(_cfg().supervisor_key or "")
            out[name] = item
        return out

    async def healthz(_req):
        ok_subs = sum(1 for r in STATE.subscribed.values() if r.get("ok"))
        # ready = "can serve reads": connected and at least one successful
        # subscription. Keeping ok (HTTP alive) apart from ready (data plane
        # usable) is what lets a consumer decide to degrade.
        ready = bool(STATE.connected and ok_subs > 0)
        return web.json_response({
            "ok": True, "ready": ready, "boot_id": STATE.seq.boot_id,
            "uptime": round(time.time() - STATE.started_at, 1),
            "connected": STATE.connected,
            "subscribed": ok_subs,
            "subscribe_failed": [s for s, r in STATE.subscribed.items() if not r.get("ok")],
            "seq": STATE.seq.seq, "reconnects": STATE.reconnects,
            "gaps": len(STATE.gaps), "degraded_calls": STATE.degraded_calls,
            "journal_ok": STATE.journal.ok,
            "watchers": _watch_summary(),
            "watch_config_error": STATE.watch_config_error,
            **_supervisor_key_summary(),
            "guardians": _public_guardians(),
            "state_dir": str(current_state_root()), "port": PORT,
        })

    def _norm_archived(sid: str, rec: dict) -> bool:
        """``archived`` really comes from the server; frames need not carry it.

        Order: an explicit frame value is used as-is; otherwise one REST query is
        cached for 60 s, because ``/state`` is a hot path and a REST round trip per
        session would turn milliseconds into seconds.
        """
        if rec.get("archived"):
            return True
        cached = rec.get("_archived_chk")
        if isinstance(cached, dict) and time.time() - cached.get("at", 0) < 60:
            return bool(cached.get("v"))
        try:
            info = K.session_info(sid)
            val = bool(info.get("archived")) if isinstance(info, dict) else False
        except Exception:  # noqa: BLE001
            return False
        rec["_archived_chk"] = {"v": val, "at": time.time()}
        if val:
            rec["archived"] = True
        return val

    def _norm_status(sid: str, rec: dict) -> dict:
        """Fill the sparse ``status`` up to the **same key set** ``direct`` emits.

        ``rec["status"]`` grows out of frames: ``main_turn_active`` only appears
        after a ``work_changed`` frame, ``archived`` only after an archive frame.
        Passing it through raw would leave quiet sessions with missing keys and
        force every consumer to write two sets of null checks. Start from
        ``K.status_template()`` and overlay known values; **missing keys become
        None, never a stale cache value**.
        """
        out = K.status_template()
        st = rec.get("status") or {}
        for k in list(out):
            if st.get(k) is not None:
                out[k] = st[k]
        # busy can be derived from agent_status (same meaning; frames carry one).
        if out["busy"] is None and st.get("agent_status") is not None:
            out["busy"] = st.get("agent_status") == "running"
        # main_turn_active can be derived from busy (they agree in terminal states).
        if out["main_turn_active"] is None and out["busy"] is not None:
            out["main_turn_active"] = out["busy"]
        if out["pending_interaction"] is None:
            out["pending_interaction"] = "approval" if (rec.get("pending") or {}) else "none"
        out["archived"] = _norm_archived(sid, rec)
        return out

    def _meta(rec: dict | None) -> dict:
        return {
            "source": "ws-bus",
            "last_event_ts": (rec or {}).get("event_ts") or STATE.last_event_ts,
            "last_snapshot_ts": (rec or {}).get("snapshot_ts") or STATE.last_snapshot_ts,
            "connected": STATE.connected,
            "gap": STATE.gaps[-1] if STATE.gaps else None,
            "age_s": round(time.time() - ((rec or {}).get("ts") or 0), 1) if rec else None,
        }

    async def state_one(req):
        sid = req.match_info["sid"]
        rec = STATE.sessions.get(sid)
        if rec is None:
            return web.json_response({"ok": False, "err": "unknown session"}, status=404)
        return web.json_response({"ok": True, "sid": sid, "_meta": _meta(rec),
                                  **{**rec, "status": _norm_status(sid, rec)}})

    async def state_all(_req):
        return web.json_response({
            "ok": True, "_meta": _meta(None),
            "sessions": {sid: {**rec, "status": _norm_status(sid, rec),
                               "_meta": _meta(rec)}
                         for sid, rec in STATE.sessions.items()}})

    async def events(req):
        try:
            since = int(req.query.get("since", "0"))
        except ValueError:
            since = 0
        sid = req.query.get("sid")
        limit = min(int(req.query.get("limit", "200")), RING_SIZE)
        wait = min(float(req.query.get("wait", "0")), 20.0)
        deadline = time.time() + wait
        while True:
            out = [e for e in STATE.events
                   if e["seq"] > since and (not sid or e["sid"] == sid)]
            if out or time.time() >= deadline or wait <= 0:
                break
            await asyncio.sleep(0.2)
        oldest = STATE.events[0]["seq"] if STATE.events else 0
        return web.json_response({
            "ok": True, "events": out[-limit:], "seq": STATE.seq.seq,
            "oldest_seq": oldest,
            "next_seq": (out[-1]["seq"] + 1) if out else STATE.seq.seq + 1,
            "boot_id": STATE.seq.boot_id,
            "dropped": STATE.seq.dropped[-5:],
            "gap": STATE.gaps[-1] if STATE.gaps else None,
        })

    async def tasks(req):
        rec = STATE.sessions.get(req.match_info["sid"]) or {}
        running = [t for t in (rec.get("tasks") or {}).values()
                   if t.get("status") == "running"]
        # Normalised to the **same field names** as ``direct.tasks()``
        # (id/status/run_in_background/command), otherwise bus and direct differ
        # in shape and every consumer needs two parsers.
        return web.json_response({
            "ok": True, "_meta": _meta(rec),
            "tasks": [K.norm_task(t) for t in running]})

    async def pending(req):
        rec = STATE.sessions.get(req.match_info["sid"]) or {}
        out = []
        for it in (rec.get("pending") or {}).values():
            reqd = it.get("request") or {}
            out.append({
                "approval_id": it.get("interaction_id") or it.get("approval_id"),
                "tool_name": reqd.get("tool_name") or it.get("tool_name") or "",
                "action": reqd.get("action") or "",
                "kind": it.get("kind") or "approval",
            })
        return web.json_response({"ok": True, "_meta": _meta(rec), "pending": out})

    async def inject_event(req):
        """Let a local consumer inject one file-class event.

        Used when a consumer detects a dead supervisor key and needs the event to
        land in the daemon's ring so both ``/healthz`` and watchdogs can see it.

        Structurally identical to WS events; ``sid`` is fixed to ``"file"`` so
        ``?sid=file`` filters it. **Local only** (the daemon binds 127.0.0.1) and
        the event name must start with ``event.``, so nobody can pollute the ring
        with arbitrary types.
        """
        try:
            body = await req.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"ok": False, "err": "bad json"}, status=400)
        etype = str((body or {}).get("event") or "").strip()
        payload = (body or {}).get("payload") or {}
        if not etype.startswith("event."):
            return web.json_response({"ok": False, "err": "event 名必须以 event. 开头"},
                                     status=400)
        if not isinstance(payload, dict):
            return web.json_response({"ok": False, "err": "payload 必须是对象"}, status=400)
        ev = STATE.add_event(FILE_EVENT_SID, etype, qualify_event_payload(payload))
        _append_event_file(ev)
        log(f"注入事件 seq={ev['seq']} {etype} "
            f"{json.dumps(payload, ensure_ascii=False)[:120]}")
        return web.json_response({"ok": True, "seq": ev["seq"], "type": etype})

    async def ctrl(req):
        verb = req.match_info["verb"]
        try:
            body = await req.json()
        except Exception:  # noqa: BLE001
            body = {}
        body["verb"] = verb
        status, payload = control(body)
        return web.json_response(payload, status=status)

    app.router.add_get("/healthz", healthz)
    app.router.add_get("/state", state_all)
    app.router.add_get("/state/{sid}", state_one)
    app.router.add_get("/events", events)
    app.router.add_get("/tasks/{sid}", tasks)
    app.router.add_get("/pending/{sid}", pending)
    app.router.add_post("/control/{verb}", control_handler := ctrl)
    app.router.add_post("/events/inject", inject_event)
    return app


# --------------------------------------------------------------------- lock
def _pid_alive(pid: int) -> bool:
    try:
        if os.name == "nt":
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  f"Get-Process -Id {pid} -ErrorAction SilentlyContinue"],
                                 capture_output=True, text=True, timeout=10)
            return bool(out.stdout.strip())
        os.kill(pid, 0)
        return True
    except Exception:  # noqa: BLE001
        return False


def _cmdline_of(pid: int) -> str:
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\")"
                              f".CommandLine"], capture_output=True, text=True, timeout=10)
        return (out.stdout or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def acquire_lock() -> bool:
    """Single-instance lock: pid alive **and** its command line names this script."""
    PATHS.mkdir()
    if PATHS.lock.is_file():
        try:
            d = json.loads(PATHS.lock.read_text(encoding="utf-8"))
            pid = int(d.get("pid") or 0)
            cmd = d.get("cmdline") or ""
            if pid and _pid_alive(pid):
                live = _cmdline_of(pid)
                if ("wss_bus" in live) or (not live and "wss_bus" in cmd):
                    print(f"已有实例在跑：pid={pid} port={d.get('port')} "
                          f"state_dir={d.get('state_dir')}")
                    return False
        except (OSError, ValueError):
            pass
    PATHS.lock.write_text(json.dumps({
        "pid": os.getpid(), "port": PORT, "state_dir": str(PATHS.root),
        "cmdline": " ".join(sys.argv), "at": time.strftime("%F %T"),
    }, ensure_ascii=False), encoding="utf-8")
    return True


def release_lock() -> None:
    try:
        d = json.loads(PATHS.lock.read_text(encoding="utf-8"))
        if int(d.get("pid") or 0) == os.getpid():
            PATHS.lock.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


# --------------------------------------------------------------------- autostart
AUTOSTART_EXIT_OK = 0
AUTOSTART_EXIT_REGISTER_FAILED = 3      # the registration itself failed
AUTOSTART_EXIT_VERIFY_FAILED = 4        # registered, but the read-back mismatched
AUTOSTART_EXIT_UNINSTALL_FAILED = 5     # still present after uninstall


def AUTOSTART_NAME() -> str:
    return str(_cfg().autostart_name or "WssBus")


def _ps_json(script: str, timeout: float = 60.0):
    """Run PowerShell and parse its JSON output. Returns ``(rc, obj, raw)``."""
    bridge = _cfg().ps_json
    if bridge is not None:
        return bridge(script, timeout)
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, text=True, timeout=timeout)
    raw = (r.stdout or "").strip()
    obj = None
    if raw:
        try:
            obj = json.loads(raw)
        except ValueError:
            obj = None
    return r.returncode, obj, raw or (r.stderr or "").strip()


def windowless_python() -> tuple[str, str]:
    """``pythonw.exe`` next to the current interpreter; returns ``(path, note)``.

    Registering the autostart with ``python.exe`` pops a console window whenever
    the trigger fires. ``pythonw.exe`` is the same interpreter with the GUI
    subsystem: identical behaviour, no console. Logs already go to
    ``<state root>/wss_bus.log``, so stdout is not needed.

    Degradation: when the sibling ``pythonw.exe`` is missing (very old or trimmed
    distributions) the function returns ``python.exe`` and puts the reason in the
    note — the caller **prints a warning**, nothing is hidden.
    """
    inject = _cfg().windowless_python
    if inject is not None:
        return inject()
    exe = Path(sys.executable).resolve()
    cand = exe.with_name("pythonw.exe")
    if cand.is_file():
        return str(cand), ""
    return str(exe), ("同目录未找到 pythonw.exe（%s），降级用 python.exe —— "
                      "触发时**会弹控制台窗口**" % cand)


def task_spec() -> dict:
    """Expected scheduled-task definition — one source for register and verify."""
    cfg = _cfg()
    script = Path(cfg.autostart_script) if cfg.autostart_script else Path(__file__).resolve()
    workdir = Path(cfg.autostart_workdir) if cfg.autostart_workdir else script.parent.parent
    exe, note = windowless_python()
    return {
        "name": AUTOSTART_NAME(),
        "execute": exe,
        "arguments": '"%s" --daemon' % Path(script).resolve(),
        "workdir": str(Path(workdir).resolve()),
        "script": str(Path(script).resolve()),
        "execute_note": note,
        "execute_is_windowless": not note,
    }


def query_task():
    """Read back the registered task; None when it does not exist."""
    ps = (
        "$ErrorActionPreference='Stop';"
        "$t = Get-ScheduledTask -TaskName '%s' -ErrorAction SilentlyContinue;"
        "if (-not $t) { Write-Output 'null'; exit 0 };"
        "$a = $t.Actions[0];"
        "$o = [ordered]@{ name=$t.TaskName; state=[string]$t.State;"
        " execute=$a.Execute; arguments=$a.Arguments; workdir=$a.WorkingDirectory };"
        "Write-Output ($o | ConvertTo-Json -Compress)"
    ) % AUTOSTART_NAME()
    rc, obj, raw = _ps_json(ps)
    if rc != 0 or not isinstance(obj, dict):
        return None
    return obj


def verify_task(spec: dict):
    """Compare the registered task with ``spec``; returns the failure list (empty ⇒ ok).

    ``WorkingDirectory`` is deliberately **not** a hard failure: some Windows
    versions do not echo it back after ``New-ScheduledTaskAction -WorkingDirectory``.
    When absent, verify only Execute + Arguments and say so with the sentinel
    ``__workdir_unverifiable__``.
    """
    actual = query_task()
    if not actual:
        return ["任务未注册（Get-ScheduledTask 查无）"]

    fails = []
    exe_ok = str(actual.get("execute") or "").strip().lower() == spec["execute"].strip().lower()
    if not exe_ok:
        fails.append("Execute 不符：期望 %r，实际 %r"
                     % (spec["execute"], actual.get("execute")))

    args = str(actual.get("arguments") or "")
    if spec["script"] not in args:
        fails.append("Arguments 缺脚本路径：期望含 %r，实际 %r"
                     % (spec["script"], args))
    elif "--daemon" not in args:
        fails.append("Arguments 缺 --daemon：实际 %r" % args)

    wd = actual.get("workdir")
    if wd is None or str(wd).strip() == "":
        fails.append("__workdir_unverifiable__")
    elif str(wd).strip().lower() != spec["workdir"].strip().lower():
        fails.append("WorkingDirectory 不符：期望 %r，实际 %r"
                     % (spec["workdir"], wd))
    return fails


def install_autostart() -> int:
    """Register login autostart, then read back and verify. Non-zero on failure.

    Exit codes (do not change): 0 registered+verified, 3 registration failed
    (permission/policy), 4 registered but verification failed.

    Why read back at all: the old implementation trusted its own
    ``registered`` print. ``Register-ScheduledTask`` throws ``Access is denied``
    when unelevated, but PowerShell defaults to ``Continue`` and the script
    still reached ``Write-Output 'registered'`` — success printed, exit 0, no
    task. ``$ErrorActionPreference='Stop'`` now turns the exception into JSON and
    ``Get-ScheduledTask`` verifies the fields.
    """
    spec = task_spec()
    if not spec.get("execute_is_windowless", True):
        print("[autostart] 警告：%s" % spec.get("execute_note"), file=sys.stderr)
    ps = (
        "$ErrorActionPreference='Stop';"
        "try {"
        " $a = New-ScheduledTaskAction -Execute '%s' -Argument '%s' -WorkingDirectory '%s';"
        " $t = New-ScheduledTaskTrigger -AtLogOn;"
        " $s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries"
        " -DontStopIfGoingOnBatteries -RestartCount 3"
        " -RestartInterval (New-TimeSpan -Minutes 1);"
        " Register-ScheduledTask -TaskName '%s' -Action $a -Trigger $t -Settings $s -Force"
        " | Out-Null;"
        " Write-Output '{\"ok\":true}'"
        "} catch {"
        " Write-Output ('{\"ok\":false,\"err\":'"
        " + (ConvertTo-Json ([string]$_.Exception.Message)) + '}')"
        "}"
    ) % (spec["execute"], spec["arguments"], spec["workdir"], spec["name"])

    rc, obj, raw = _ps_json(ps)
    if rc != 0 or not isinstance(obj, dict) or not obj.get("ok"):
        err = (obj or {}).get("err") if isinstance(obj, dict) else raw
        print("[autostart] 注册失败：%s" % err, file=sys.stderr)
        if "denied" in str(err).lower():
            print("[autostart] 提示：注册计划任务需要管理员权限，"
                  "请在**提升权限**的终端里执行本命令。", file=sys.stderr)
        return AUTOSTART_EXIT_REGISTER_FAILED

    fails = verify_task(spec)
    workdir_unverifiable = "__workdir_unverifiable__" in fails
    hard_fails = [f for f in fails if f != "__workdir_unverifiable__"]

    if hard_fails:
        print("[autostart] 注册返回成功，但回读校验失败：", file=sys.stderr)
        for f in hard_fails:
            print("  x %s" % f, file=sys.stderr)
        print("[autostart] 请手工清理：schtasks /Delete /TN %s /F" % spec["name"],
              file=sys.stderr)
        return AUTOSTART_EXIT_VERIFY_FAILED

    actual = query_task() or {}
    print("[autostart] 注册成功并已回读验证：")
    print("  name       : %s" % actual.get("name"))
    print("  state      : %s" % actual.get("state"))
    print("  Execute    : %s" % actual.get("execute"))
    print("  Arguments  : %s" % actual.get("arguments"))
    if workdir_unverifiable:
        print("  WorkingDir : (本机 PowerShell 不回显，无法校验；期望 %s)" % spec["workdir"])
    else:
        print("  WorkingDir : %s" % actual.get("workdir"))
    print("  Trigger    : AtLogOn")
    if spec.get("execute_is_windowless"):
        print("  窗口       : 无（pythonw.exe，不弹控制台）")
    else:
        print("  窗口       : **会弹控制台**（降级用 python.exe，见上方警告）")
    return AUTOSTART_EXIT_OK


def uninstall_autostart() -> int:
    """Unregister login autostart, then confirm it is gone. Non-zero on failure.

    Exit codes: 0 uninstalled or absent (idempotent), 5 still present.
    """
    if query_task() is None:
        print("[autostart] not installed（本来就没有该任务，无需卸载）")
        return AUTOSTART_EXIT_OK

    ps = (
        "$ErrorActionPreference='Stop';"
        "try {"
        " Unregister-ScheduledTask -TaskName '%s' -Confirm:$false;"
        " Write-Output '{\"ok\":true}'"
        "} catch {"
        " Write-Output ('{\"ok\":false,\"err\":'"
        " + (ConvertTo-Json ([string]$_.Exception.Message)) + '}')"
        "}"
    ) % AUTOSTART_NAME()

    rc, obj, raw = _ps_json(ps)
    if rc != 0 or not isinstance(obj, dict) or not obj.get("ok"):
        err = (obj or {}).get("err") if isinstance(obj, dict) else raw
        print("[autostart] 卸载命令失败：%s" % err, file=sys.stderr)
        return AUTOSTART_EXIT_UNINSTALL_FAILED

    if query_task() is not None:
        print("[autostart] 卸载后仍能查到任务 %s，判定失败" % AUTOSTART_NAME(),
              file=sys.stderr)
        return AUTOSTART_EXIT_UNINSTALL_FAILED
    print("[autostart] 已卸载并回读确认查无：%s" % AUTOSTART_NAME())
    return AUTOSTART_EXIT_OK


# --------------------------------------------------------------------- client
def bus_health(timeout: float = HEALTH_TIMEOUT, port: int | None = None) -> dict | None:
    """Is the daemon usable? None ⇒ **the caller should fall back to direct**."""
    import urllib.request

    p = port or PORT
    if not p:
        p = int(os.environ.get("WSS_BUS_PORT", "0") or 0)
    if not p:
        return None
    try:
        with urllib.request.urlopen(f"http://{HOST}:{p}/healthz", timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return None


def _port_of(port: int | None) -> int:
    p = port or PORT
    if not p:
        p = int(os.environ.get("WSS_BUS_PORT", "0") or 0)
    if not p:
        raise RuntimeError("未指定端口（configure(port=...) 或 WSS_BUS_PORT）")
    return p


def bus_get(path: str, timeout: float = 3.0, port: int | None = None):
    import urllib.request

    p = _port_of(port)
    with urllib.request.urlopen(f"http://{HOST}:{p}{path}", timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def bus_post(path: str, body: dict, timeout: float = 20.0, port: int | None = None):
    import urllib.request

    p = _port_of(port)
    req = urllib.request.Request(
        f"http://{HOST}:{p}{path}",
        data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def note_degraded(why: str = "") -> None:
    """Count one degradation (log only — no request; the dead "pretend down" code is gone)."""
    STATE.degraded_calls += 1
    log(f"degraded: {why}")


# --------------------------------------------------------------------- self-test
def self_test() -> int:
    """Offline assertions: no network, no running daemon. Returns 0 when all pass."""
    fails: list[str] = []
    total = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal total
        total += 1
        if not cond:
            fails.append(f"{name}{(' — ' + detail) if detail else ''}")

    src = Path(__file__).read_text(encoding="utf-8")

    # 1) Hard dependency rule: this file must never import a workbench tool module.
    #    The module name is assembled so the assertion is not itself a match.
    legacy = "kimi" + "_" + "api"
    body = "\n".join(ln for ln in src.splitlines()
                     if not ln.strip().startswith("#"))
    check("daemon 不得引用兼容薄壳模块",
          f"import {legacy}" not in body and f"{legacy}." not in body)

    # 2) Frame flattening.
    f = _flat({"type": "usage", "session_id": "s1", "context_tokens": 7})
    check("帧摊平", f.get("context_tokens") == 7 and f.get("session_id") == "s1")

    # 3) task.list is a full replacement; ended tasks are retained.
    STATE.sessions.clear()
    absorb({"type": "task.list", "session_id": "s1",
            "tasks": [{"task_id": "a", "status": "running"}]})
    absorb({"type": "task.list", "session_id": "s1",
            "tasks": [{"task_id": "b", "status": "running"}]})
    check("task.list 全量替换", set(STATE.sessions["s1"]["tasks"]) == {"b"})
    absorb({"type": "task", "session_id": "s1", "task_id": "b", "status": "ended",
            "outcome": "completed"})
    check("task ended 保留", STATE.sessions["s1"]["tasks"]["b"]["status"] == "ended")

    # 4) Interaction add/remove.
    absorb({"type": "interaction", "session_id": "s1", "interaction_id": "i1",
            "status": "pending"})
    check("interaction 增", "i1" in STATE.sessions["s1"]["pending"])
    absorb({"type": "interaction", "session_id": "s1", "interaction_id": "i1",
            "status": "ended", "outcome": "approved"})
    check("interaction 删", "i1" not in STATE.sessions["s1"]["pending"])

    # 5) Reconnect resets subscriptions; a failed ack never marks success.
    STATE.subscribed = {"s1": {"ok": True, "ack_id": "x"}, "s2": {"ok": True, "ack_id": "y"}}
    STATE.subscribed = {}
    check("subscribed 重连重置", STATE.subscribed == {})
    STATE.subscribed = {"s1": {"ok": False, "ack_id": "ack1"}}
    _mark_ack({"type": "ack", "id": "ack1", "code": 50001, "msg": "boom", "payload": {}})
    check("ack 失败不标成功", STATE.subscribed["s1"]["ok"] is False)
    STATE.subscribed = {"s1": {"ok": False, "ack_id": "ack2"}}
    _mark_ack({"type": "ack", "id": "ack2", "code": 0,
               "payload": {"accepted": ["s1"], "not_found": []}})
    check("ack 成功才标成功", STATE.subscribed["s1"]["ok"] is True)

    # 6) Paged listing deduplicates.
    pages = [([{"id": "a"}, {"id": "b"}], "b"), ([{"id": "b"}, {"id": "c"}], "c"),
             ([], "")]
    seen: dict = {}
    for items, _nxt in pages:
        for it in items:
            seen[it["id"]] = it
    check("分页去重", set(seen) == {"a", "b", "c"})

    tmpdir = Path(tempfile.mkdtemp(prefix="wss_selftest_"))
    try:
        # 7) Sequence survives restart; loss ranges are recorded.
        sp = tmpdir / "seq.json"
        s1 = SeqStore(sp)
        s1.next(); s1.next(); s1.save()
        s2 = SeqStore(sp)
        check("seq 跨重启连续", s2.seq == 2 and s2.next() == 3)
        s2.note_drop(1, 5, "rotation")
        s2.save()
        s3 = SeqStore(sp)
        check("淘汰 gap 记录", bool(s3.dropped) and s3.dropped[0]["to"] == 5)

        # 8) Journal tolerates damage and absence.
        jp = tmpdir / "journal.jsonl"
        jp.write_text('{"request_id":"r1","verb":"send"}\n'
                      'NOT JSON\n'
                      '{"request_id":"r2","verb":"abort"}\n', encoding="utf-8")
        j = Journal(jp)
        check("journal 损坏行跳过", j.ok and j.get("r1") and j.get("r2"))
        bad = Journal(tmpdir / "nope" / "x.jsonl")
        check("journal 缺失仍可用", bad.ok is True)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    # 9) Three-state acceptance: accepted ⇒ indeterminate, never resend.
    jdir = Path(tempfile.mkdtemp(prefix="wss_j_"))
    try:
        STATE.journal = Journal(jdir / "j1.jsonl")
        STATE.journal.put({"request_id": "dup", "verb": "send", "sid": "s",
                           "prompt_id": "p1"})
        st, pl = control({"verb": "send", "sid": "s", "text": "x", "request_id": "dup"})
        check("已受理不重发", st == 200 and pl.get("state") == "indeterminate",
              str(pl)[:80])
        check("已受理文案含禁止重发", "禁止重发" in str(pl.get("err")), str(pl.get("err"))[:60])
        old_j = STATE.journal
        STATE.journal = Journal(jdir / "j2.jsonl")
        STATE.journal.ok = False
        st2, pl2 = control({"verb": "send", "sid": "s", "text": "x", "request_id": "new"})
        check("journal 不可读不重试", st2 == 200 and pl2.get("state") == "indeterminate")
        STATE.journal = old_j
        check("三态-业务码原样返回", '"rejected"' in src or "state\": \"rejected\"" in src)
        # unsubscribe is exempt from the journal (a broken journal must not block a cut).
        STATE.journal.ok = False
        st3, pl3 = control({"verb": "unsubscribe", "sid": "session_x"})
        check("unsubscribe 不受坏 journal 阻断",
              st3 == 200 and pl3.get("state") == "accepted", str(pl3)[:80])
        STATE.journal.ok = True
    finally:
        shutil.rmtree(jdir, ignore_errors=True)

    # 10) Single-instance lock helpers exist (semantics only, no child process).
    check("单实例锁函数存在", callable(acquire_lock) and callable(release_lock))

    # 11) Control-plane verbs: every verb needs a branch in _control_dispatch.
    need = ("send", "abort", "steer", "interaction", "cancel-task",
            "create", "profile", "archive", "restore", "takeover", "unsubscribe")
    missing = [v for v in need if f'verb == "{v}"' not in src
               and not (v in ("archive", "restore", "takeover")
                        and 'verb in ("archive", "restore", "takeover")' in src)]
    check("控制面动词覆盖", not missing, f"缺 {missing}")

    # 12) create does not require a sid (it creates one).
    check("create 免 sid", 'verb != "create"' in src)

    # 13) Autostart read-back verification has a carrier.
    _orig_q = globals()["query_task"]
    try:
        globals()["query_task"] = lambda: None
        check("自启-任务不存在判失败", bool(verify_task(task_spec())))
        _spec = task_spec()
        globals()["query_task"] = lambda: {
            "name": AUTOSTART_NAME(), "state": "Ready",
            "execute": str(Path(_spec["execute"]).with_name("wrong.exe")),
            "arguments": '"C:/wrong/x.py" --daemon',
            "workdir": str(Path(_spec["workdir"]).parent / "wrong")}
        _f2 = verify_task(_spec)
        check("自启-字段不符判失败", len(_f2) >= 3, str(_f2)[:80])
        globals()["query_task"] = lambda: {
            "name": AUTOSTART_NAME(), "state": "Ready",
            "execute": _spec["execute"], "arguments": _spec["arguments"],
            "workdir": None}
        _f3 = verify_task(_spec)
        _hard = [x for x in _f3 if x != "__workdir_unverifiable__"]
        check("自启-workdir 不可验时降级不误判",
              "__workdir_unverifiable__" in _f3 and not _hard, str(_f3)[:80])
        globals()["query_task"] = lambda: {
            "name": AUTOSTART_NAME(), "state": "Ready",
            "execute": _spec["execute"], "arguments": _spec["arguments"],
            "workdir": _spec["workdir"]}
        check("自启-匹配时零失败", verify_task(_spec) == [])
    finally:
        globals()["query_task"] = _orig_q

    # 14) Exit codes stay distinct and include 0.
    _codes = {AUTOSTART_EXIT_OK, AUTOSTART_EXIT_REGISTER_FAILED,
              AUTOSTART_EXIT_VERIFY_FAILED, AUTOSTART_EXIT_UNINSTALL_FAILED}
    check("自启-退出码约定", len(_codes) == 4 and 0 in _codes, str(sorted(_codes)))

    # 15) The action program must be windowless when available.
    _spec_w = task_spec()
    _exe_name = Path(_spec_w["execute"]).name.lower()
    check("自启-动作程序非控制台版（或注明降级）",
          _exe_name == "pythonw.exe" or bool(_spec_w.get("execute_note")),
          "%r %r" % (_exe_name, _spec_w.get("execute_note")))

    # 16) The self-test never mutates production runtime state: PATHS points at a
    #     temporary root for the rest of this run.
    check("PATHS 非生产根（自检隔离）",
          Path(PATHS.root).resolve() != Path(production_state_root()).resolve(),
          f"{PATHS.root} vs {production_state_root()}")

    # 17) Section extraction only takes the target section; other bodies never count.
    doc = "# x\n\n" + _cfg().decide_section + "\n- A\n\n## 其它\n- B\n"
    sec = _section_of(doc, _cfg().decide_section)
    check("段提取只取目标段", sec is not None and "- A" in sec and "- B" not in sec,
          repr(sec))
    doc2 = doc.replace("## 其它\n- B\n", "## 其它\n- B2\n")
    check("改其它段 md5 不变",
          hashlib.md5(_section_of(doc, _cfg().decide_section).encode()).hexdigest()
          == hashlib.md5(_section_of(doc2, _cfg().decide_section).encode()).hexdigest())
    check("段不存在返回 None", _section_of("# y\n", _cfg().decide_section) is None)
    check("空段不算命中内容",
          not (_section_of(_cfg().decide_section + "\n\n## 其它\n- B\n",
                           _cfg().decide_section) or "").strip())

    # 18) Loose heading criteria: five real "awaiting decision" headings must hit.
    must_hit_5 = ["## 待拍板（历史遗留）", "## 需拍板：流程变更",
                  "## 待拍板：记录归档策略", "## 待拍板（流程类）",
                  "## 待拍板（已完成项之外的）"]
    check("五个真待拍板标题必须全 HIT",
          all(_heading_matches(h, _cfg().decide_section) for h in must_hit_5),
          str([h for h in must_hit_5 if not _heading_matches(h, _cfg().decide_section)]))
    real_cases = ["## 3. 待拍板", "## 待拍板", "## 5. 待你拍板",
                  "## 八、待拍板", "### 31.5 未决判定（待拍板）",
                  "## 风险 / 要拍板的", "## 5. 本单发现、需拍板的两件事"]
    check("现役真实标题全 HIT",
          all(_heading_matches(h, _cfg().decide_section) for h in real_cases),
          str([h for h in real_cases if not _heading_matches(h, _cfg().decide_section)]))
    check("已裁决回顾不误报",
          _heading_matches("## 第 15 批（用户拍板，四项）", _cfg().decide_section) is False)
    check("section_pattern 可精确化",
          not _heading_matches("## 需拍板的事项（历史）", _cfg().decide_section,
                               r"^待拍板$")
          and _heading_matches("## 3. 待拍板", _cfg().decide_section, r"^待拍板$"))
    check("排除词死代码不存在", "_DECIDE" + "_EXCLUDE" not in src)
    check("位置判据存在", "_decide_taut" in src)
    check("A3 默认间隔 ≤1.0s", DEFAULT_POLL_INTERVAL <= 1.0, f"{DEFAULT_POLL_INTERVAL}")

    # 19) File event source: offline tree, no running daemon.
    ftmp = Path(tempfile.mkdtemp(prefix="wss_watch_"))
    (ftmp / "asst_handoff").mkdir(parents=True, exist_ok=True)
    (ftmp / "pm").mkdir(parents=True, exist_ok=True)
    try:
        cfg = WatchConfig([
            {"id": "handoff", "kind": "dir_new_file",
             "path": "asst_handoff", "event": "event.file.handoff_created"},
            {"id": "pm_decide", "kind": "file_section_md5",
             "path_glob": "pm/*.md", "section": _cfg().decide_section,
             "event": "event.file.pm_decide_changed"},
        ], 0.2)
        (ftmp / "asst_handoff" / "steward_20261107-0000.md").write_text("旧档\n",
                                                                      encoding="utf-8")
        (ftmp / "pm" / "demo.md").write_text(doc, encoding="utf-8")
        w = FileWatcher(cfg, ftmp)
        check("首轮不发送", w.poll_once() == [])
        (ftmp / "asst_handoff" / "steward_new.md").write_text("新档\n", encoding="utf-8")
        evs = w.poll_once()
        hs = [p for t, p in evs if t == "event.file.handoff_created"]
        check("新交接档出事件", len(hs) == 1 and hs[0]["name"] == "steward_new.md")
        check("交接档 role 可推断", bool(hs) and hs[0]["role"] == "steward")
        check("交接档 payload 不含正文",
              bool(hs) and set(hs[0]) <= {"name", "path", "mtime", "role", "size",
                                          "repo_full", "project", "key"})
        check("非目标段变化不发", w.poll_once() == [])
        (ftmp / "pm" / "demo.md").write_text(doc.replace("- A", "- A2"), encoding="utf-8")
        ps = [p for t, p in w.poll_once() if t == "event.file.pm_decide_changed"]
        check("目标段变化必发", len(ps) == 1 and ps[0]["project"] == "demo"
              and ps[0]["changed"] is True)
        check("段事件带 repo_full 字段", "repo_full" in ps[0])

        # Hot reload: a bad config keeps the previous one, never raises.
        cp = ftmp / "watch.json"
        cp.write_text('{"watchers":[{"id":"a","kind":"dir_new_file",'
                      '"path":"asst_handoff","event":"e.a"}],"poll_interval_s":1}',
                      encoding="utf-8")
        good = WatchConfig.load(cp)
        check("配置可加载", not good.error and len(good.watchers) == 1)
        cp.write_text("{ NOT JSON", encoding="utf-8")
        badcfg = WatchConfig.load(cp, previous=good)
        check("坏配置保旧配置", bool(badcfg.error) and badcfg.watchers == good.watchers)
        for junk in ('{"watchers":[]}', '{"watchers":[{"id":"a","kind":"nope"}]}',
                     '{"watchers":[{"id":"a","kind":"dir_new_file","event":"e"}]}', "[]"):
            cp.write_text(junk, encoding="utf-8")
            b2 = WatchConfig.load(cp, previous=good)
            check(f"非法配置被拒 {junk[:24]}", bool(b2.error)
                  and b2.watchers == good.watchers)
        check("配置缺失用默认",
              len(WatchConfig.load(ftmp / "nope.json", previous=good).watchers)
              == len(default_watchers()))

        # Contract: file events are structurally identical to WS events, sid="file".
        ev = STATE.add_event(FILE_EVENT_SID, "event.file.test", {"k": 1})
        check("文件事件契约同构",
              set(ev) == {"seq", "ts", "sid", "type", "payload", "boot_id"}
              and ev["sid"] == "file")
        check("/events sid=file 可过滤",
              all(e["sid"] == "file" for e in STATE.events
                  if e["type"] == "event.file.test"))

        # Differential baseline reset: only new/changed watchers re-seed.
        (ftmp / "h2").mkdir(parents=True, exist_ok=True)
        cfgA = WatchConfig([
            {"id": "h", "kind": "dir_new_file", "path": str(ftmp / "h2"), "event": "e.h"},
        ], 0.2)
        (ftmp / "h2" / "old.md").write_text("x\n", encoding="utf-8")
        fw = FileWatcher(cfgA, ftmp)
        check("A2 首轮不发", fw.poll_once() == [])
        base_h = dict(fw.baseline.get("h", {}))
        cfgB = WatchConfig(list(cfgA.watchers) + [
            {"id": "x", "kind": "dir_new_file", "path": str(ftmp / "h2"), "event": "e.x"}],
            0.2)
        reset = fw.reset_baseline(cfgB)
        check("A2 差量只重置新增 id", reset == ["x"], str(reset))
        check("A2 未变 id 基线保留", fw.baseline.get("h") == base_h)
        (ftmp / "h2" / "brand_new.md").write_text("y\n", encoding="utf-8")
        evs = fw.poll_once()
        check("A2 热加载同时建文件不漏",
              any(p.get("name") == "brand_new.md" for _, p in evs), str(evs)[:120])
        check("A2 只改配置不误报", fw.poll_once() == [])
        fw.reset_baseline(cfgB, diff=False)
        check("A2 全量重置确实清空", fw.baseline == {} and not fw.seeded)

        # Empty section is not a hit (otherwise the zero-match warning never fires).
        etmp = Path(tempfile.mkdtemp(prefix="wss_empty_"))
        try:
            (etmp / "pm").mkdir(parents=True, exist_ok=True)
            (etmp / "pm" / "full.md").write_text(
                _cfg().decide_section + "\n- A\n", encoding="utf-8")
            (etmp / "pm" / "empty.md").write_text(
                _cfg().decide_section + "\n\n## 其它\n- B\n", encoding="utf-8")
            ecfg = WatchConfig([{"id": "pd", "kind": "file_section_md5",
                                 "path_glob": str(etmp / "pm" / "*.md"),
                                 "section": _cfg().decide_section, "event": "e.p"}], 0.2)
            efw = FileWatcher(ecfg, etmp)
            check("空段首轮不留事件", efw.poll_once() == [])
            check("空段不计命中", efw.matched.get("pd") == ["full"],
                  str(efw.matched.get("pd")))
            check("空段进 empty_sections", efw.empty_sections.get("pd") == ["empty"],
                  str(efw.empty_sections.get("pd")))
            (etmp / "pm" / "full.md").write_text(_cfg().decide_section + "\n\n",
                                                 encoding="utf-8")
            efw.poll_once()
            check("全空时命中为 0", efw.matched.get("pd") == [],
                  str(efw.matched.get("pd")))
            check("清空段不发事件", efw.poll_once() == [])
        finally:
            shutil.rmtree(etmp, ignore_errors=True)
    finally:
        shutil.rmtree(ftmp, ignore_errors=True)

    # 20) /healthz watcher summary semantics.
    saved = STATE.watchers, STATE.watch_file_watcher
    try:
        STATE.watch_file_watcher = None
        STATE.watchers = {
            "dirw": {"kind": "dir_new_file", "event": "e.d",
                     "last_fire_ts": 0.0, "fires": 0,
                     "matched_count": 0, "matched_files": [], "_warn_ts": 123.0},
            "secw": {"kind": "file_section_md5", "event": "e.s",
                     "last_fire_ts": 0.0, "fires": 0,
                     "matched_count": 0, "matched_files": [], "_warn_ts": 456.0},
            "sec2": {"kind": "file_section_md5", "event": "e.s2",
                     "last_fire_ts": 0.0, "fires": 0,
                     "matched_count": 2, "matched_files": ["a", "b"]},
        }
        s = _watch_summary()
        by = {i["id"]: i for i in s["items"]}
        check("目录型不出 warning", by["dirw"]["warning"] == "", repr(by["dirw"]["warning"]))
        check("目录型报 file_count", by["dirw"].get("file_count") == 0)
        check("段型 0 命中出 warning", bool(by["secw"]["warning"]))
        check("段型有命中无 warning", by["sec2"]["warning"] == "")
        check("zero_match 只看段型", s["zero_match_warning"] is True)
        check("无下划线内部键",
              all(not any(str(k).startswith("_") for k in i) for i in s["items"]),
              str([[k for k in i if str(k).startswith("_")] for i in s["items"]]))
        STATE.watchers["secw"]["matched_count"] = 1
        check("段型补命中后 zero_match=False",
              _watch_summary()["zero_match_warning"] is False)
    finally:
        STATE.watchers, STATE.watch_file_watcher = saved

    # 21) Inject endpoint event-name guard (event.* only).
    check("注入端点存在", "events/inject" in src)
    check("注入事件名契约（event.* 前缀）",
          "not etype.startswith(\"event.\")" in src)

    # 22) issue-key space: fully-qualified keys, read-only short-key compat,
    #     and the cross-repository counter-example.
    k1 = issue_key("Owner/Demo", 12)
    check("全限定键格式 owner/repo#issue", k1 == "owner/demo#12", k1)
    check("全限定键忽略大小写差异", issue_key("OWNER/demo", "12") == "owner/demo#12")
    check("缺 owner 不成键", issue_key("demo", 12) == "" and issue_key("", 1) == "")
    check("delivery 键带 evidence 后缀",
          delivery_key("o/demo", 12, "abc") == "o/demo#12#abc"
          and delivery_key("o/demo", 12) == "o/demo#12")
    check("旧短键候选含项目名与全名",
          legacy_issue_keys("o/Demo", 12) == ["o/demo#12", "demo#12"],
          str(legacy_issue_keys("o/Demo", 12)))

    # Two different owners, same repo name, same issue number.
    db = {
        "ownerA/demo#12": {"worker": {"name": "A", "session": "session_a"}},
        "ownerB/demo#12": {"worker": {"name": "B", "session": "session_b"}},
    }
    check("同名不同 owner 不串台（A）",
          (lookup_issue_record(db, "ownerA/demo", 12) or {}).get("worker", {}).get("name")
          == "A")
    check("同名不同 owner 不串台（B）",
          (lookup_issue_record(db, "ownerB/demo", 12) or {}).get("worker", {}).get("name")
          == "B")
    check("无全限定记录时不按裸仓名猜测",
          lookup_issue_record(db, "ownerC/demo", 12) is None)
    # A short key is only honoured when it cannot be ambiguous.
    short_db = {"demo#12": {"worker": {"name": "S", "session": "session_s"}}}
    check("旧短键只读兼容（唯一时可用）",
          (lookup_issue_record(short_db, "ownerA/demo", 12) or {}).get("worker", {})
          .get("name") == "S")
    mixed_db = dict(short_db)
    mixed_db["ownerB/demo#12"] = {"worker": {"name": "B", "session": "session_b"}}
    check("旧短键在有歧义时拒绝匹配",
          lookup_issue_record(mixed_db, "ownerC/demo", 12) is None)

    payload = qualify_event_payload({"project": "demo", "issue": 12, "md5": "x"},
                                    repo="OwnerA/Demo")
    check("事件 payload 保留 project 字段", payload.get("project") == "demo")
    check("事件 payload 另加 repo_full 字段", payload.get("repo_full") == "ownera/demo")
    check("事件 payload 带全限定 key", payload.get("key") == "ownera/demo#12")
    payload2 = qualify_event_payload({"project": "demo", "issue": 12}, repo="OwnerB/Demo")
    check("同号 issue 两仓 payload 键不同", payload["key"] != payload2["key"])

    # bind_worker must resolve through the same qualified lookup.
    bind_file = Path(tempfile.mkdtemp(prefix="wss_bind_")) / "issue_sessions.json"
    try:
        bind_file.write_text(json.dumps(db), encoding="utf-8")
        check("绑定查表命中全限定键（A）",
              bind_worker("ownerA/demo", 12, bind_file)[0] == "A")
        check("绑定查表命中全限定键（B）",
              bind_worker("ownerB/demo", 12, bind_file)[0] == "B")
        check("绑定查表不回退到歧义短键",
              bind_worker("ownerC/demo", 12, bind_file)[0] == "unknown")
    finally:
        shutil.rmtree(bind_file.parent, ignore_errors=True)

    # 23) Guardian read-side three states — behaviour, not string presence.
    gtmp = Path(tempfile.mkdtemp(prefix="wss_guard_"))
    _saved_paths = PATHS
    _saved_prod = _cfg().production_state_root
    _saved_gs = STATE.guardian_state
    try:
        globals()["PATHS"] = Paths(gtmp)
        globals()["production_state_root"] = lambda: gtmp
        PATHS.mkdir()
        (gtmp / "sessions.json").write_text("{}", encoding="utf-8")
        _hb_rel = GUARDIAN_THRESHOLDS["monitor"][0]
        _hb = gtmp / _hb_rel

        # ① file genuinely absent ⇒ missing
        check("① 心跳文件缺失判 missing",
              _guardian_summary()["monitor"]["state"] == "missing",
              str(_guardian_summary()["monitor"])[:120])

        # ② PermissionError ⇒ unknown, no alert, read_error surfaced
        _hb.parent.mkdir(parents=True, exist_ok=True)
        _hb.write_text(json.dumps({"ts": time.time()}), encoding="utf-8")
        check("② 新鲜心跳判 ok", _guardian_summary()["monitor"]["state"] == "ok")
        _orig_read = Path.read_text

        def _boom(self, *a, **kw):
            if str(self).endswith("monitor.json"):
                raise PermissionError(13, "concurrent replace")
            return _orig_read(self, *a, **kw)

        Path.read_text = _boom
        try:
            s2 = _guardian_summary()
            check("② PermissionError 判 unknown（不是 missing）",
                  s2["monitor"]["state"] == "unknown", str(s2["monitor"])[:120])
            check("② read_error 落到 /healthz 记录",
                  s2["monitor"].get("read_error") == "PermissionError",
                  str(s2["monitor"].get("read_error")))
            gs = {}
            due = _guardian_alert_due(s2, gs)
            check("② unknown 不投递",
                  all(d["name"] != "monitor" or d["state"] != "unknown" for d in due),
                  str([(d["name"], d["state"]) for d in due]))
            check("② unknown 不占去重名额", "monitor" not in gs, str(gs))
        finally:
            Path.read_text = _orig_read

        # ③ bad JSON ⇒ missing (not unknown)
        _hb.write_text("{ NOT JSON", encoding="utf-8")
        s3 = _guardian_summary()
        check("③ 坏 JSON 判 missing", s3["monitor"]["state"] == "missing",
              str(s3["monitor"])[:120])
        check("③ 坏 JSON 不算 read_error", "read_error" not in s3["monitor"])

        # ④ unknown during a real break: no slot consumed, alert still fires after
        _hb.unlink()
        gs = {"monitor": {"state": "stale", "ts": 0.0}}
        due = _guardian_alert_due({"monitor": {"state": "unknown", "age_s": None,
                                              "threshold_s": 300, "heartbeat": _hb_rel,
                                              "writer": ""}}, gs)
        check("④ unknown 期间不投递且清掉旧名额", due == [] and "monitor" not in gs)
        due2 = _guardian_alert_due({"monitor": {"state": "missing", "age_s": None,
                                               "threshold_s": 300, "heartbeat": _hb_rel,
                                               "writer": ""}}, gs)
        check("④ 恢复后同一轮可再报 missing", len(due2) == 1, str(due2)[:80])

        # ⑤ dedup window: same state inside the window is silent, recovery resets
        STATE.guardian_state = {}
        _fake = {"a": {"state": "stale", "age_s": 999, "threshold_s": 1,
                       "heartbeat": "x", "writer": ""},
                 "b": {"state": "ok", "age_s": 0, "threshold_s": 1,
                       "heartbeat": "y", "writer": ""}}
        _d1 = _guardian_alert_due(_fake, STATE.guardian_state)
        _d2 = _guardian_alert_due(_fake, STATE.guardian_state)
        STATE.guardian_state = {"a": {"state": "stale", "ts": time.time()}}
        _d3 = _guardian_alert_due({"a": {**_fake["a"], "state": "ok"}},
                                  STATE.guardian_state)
        check("guardian 去重＋恢复重置",
              len(_d1) == 1 and not _d2 and not _d3 and "a" not in STATE.guardian_state)

        # ⑥ guardian_extra.json four shapes
        extra = gtmp / _cfg().guardian_extra_file
        check("⑥ 无外部注册 ⇒ 只有内置",
              set(guardian_thresholds()) == set(GUARDIAN_THRESHOLDS))
        extra.write_text("{ NOT JSON", encoding="utf-8")
        check("⑥ 坏 JSON ⇒ 内置 + 不崩",
              set(guardian_thresholds()) == set(GUARDIAN_THRESHOLDS))
        extra.write_text(json.dumps({"io": {"heartbeat": "", "threshold_s": 10}}),
                         encoding="utf-8")
        check("⑥ 单条形状不合 ⇒ 跳过该条",
              "io" not in guardian_thresholds()
              and set(guardian_thresholds()) == set(GUARDIAN_THRESHOLDS))
        extra.write_text(json.dumps({"monitor": {"heartbeat": "hb/x.json",
                                                 "threshold_s": 1}}), encoding="utf-8")
        _th = guardian_thresholds()
        check("⑥ 同名冲突 ⇒ 内置优先",
              _th["monitor"] == GUARDIAN_THRESHOLDS["monitor"], str(_th["monitor"]))
        extra.write_text(json.dumps({"reporter": {"heartbeat": "hb/rep.json",
                                                  "threshold_s": 120}}), encoding="utf-8")
        _th2 = guardian_thresholds()
        check("⑥ 合法外部注册生效",
              _th2.get("reporter", ("", 0, ""))[:2] == ("hb/rep.json", 120.0),
              str(_th2.get("reporter")))
        check("⑥ 内置三项阈值未被改写",
              all(GUARDIAN_THRESHOLDS[n][1] == v for n, v in
                  (("delivery_watch", 900.0), ("decide_watch", 900.0),
                   ("monitor", 300.0))))
        extra.unlink()

        # ⑦ isolated state root never writes production
        prod = Path(tempfile.mkdtemp(prefix="wss_prod_"))
        iso = Path(tempfile.mkdtemp(prefix="wss_iso_"))
        try:
            prod.mkdir(parents=True, exist_ok=True)
            prod_file = prod / _cfg().guardian_state_file
            prod_file.write_text('{"probe": {"state": "stale", "ts": 1}}', encoding="utf-8")
            os.utime(prod_file, (1_700_000_000, 1_700_000_000))
            _before = prod_file.stat().st_mtime
            globals()["PATHS"] = Paths(prod)
            globals()["production_state_root"] = lambda: prod
            _orig_env = os.environ.get("WSS_BUS_STATE_DIR")
            os.environ["WSS_BUS_STATE_DIR"] = str(iso)
            STATE.guardian_state = {}
            _hr = guardian_check(do_notify=False)
            _after = prod_file.stat().st_mtime
            check("⑦ 隔离跑一轮不碰生产去重状态",
                  _before == _after and _hr["channel"] == "isolated",
                  f"prod_mtime {_before}->{_after} channel={_hr['channel']}")
            check("⑦ 隔离根下写入自己的去重状态",
                  (iso / _cfg().guardian_state_file).is_file())
            check("⑦ state_dir 等于实际隔离根",
                  guardian_context()["state_dir"] == str(iso.resolve()),
                  guardian_context()["state_dir"])
        finally:
            if _orig_env is None:
                os.environ.pop("WSS_BUS_STATE_DIR", None)
            else:
                os.environ["WSS_BUS_STATE_DIR"] = _orig_env
            shutil.rmtree(prod, ignore_errors=True)
            shutil.rmtree(iso, ignore_errors=True)
    finally:
        globals()["PATHS"] = _saved_paths
        globals()["production_state_root"] = lambda: _saved_prod
        STATE.guardian_state = _saved_gs
        shutil.rmtree(gtmp, ignore_errors=True)

    # 24) Injection gate: a missing boundary is an explicit error, never production.
    _mis = missing_injections(WssBusConfig())
    check("未注入时列出缺项", bool(_mis) and "state_root" in _mis, str(_mis))
    check("未注入时不静默连生产",
          [n for n in _mis if n in ("workspace_root", "state_root",
                                    "production_state_root")] != [],
          str(_mis))

    # 25) Supervisor key summary: three states + the /healthz field names.
    ktmp = Path(tempfile.mkdtemp(prefix="wss_key_"))
    _saved_paths = PATHS
    _saved_prod = _cfg().production_state_root
    try:
        globals()["PATHS"] = Paths(ktmp)
        globals()["production_state_root"] = lambda: ktmp
        kp = ktmp / _cfg().alarm_state_file
        kp.write_text(json.dumps({"ts": time.time(), "last_state": "ok", "sid": "s1"}),
                      encoding="utf-8")
        k1 = _supervisor_key_summary()
        check("supervisor 键 ok", k1["supervisor_key_state"] == "ok", str(k1)[:100])
        check("supervisor 键带 age", k1["last_supervisor_alarm_age_s"] is not None)
        kp.write_text(json.dumps({"ts": time.time() - 5000, "last_state": "dead",
                                  "sid": "s2"}), encoding="utf-8")
        k2 = _supervisor_key_summary()
        check("陈旧状态判 stale", k2["supervisor_key_state"] == "stale", str(k2)[:100])
        check("陈旧仍给真实 age", k2["last_supervisor_alarm_age_s"] is not None)
        check("陈旧保留 raw 状态", k2.get("supervisor_key_state_raw") == "dead")
        kp.unlink()
        STATE.events.clear()
        check("无状态无事件 ⇒ unknown",
              _supervisor_key_summary()["supervisor_key_state"] == "unknown")
        _saved_cfg = wb_cfg = DEFAULT_CONFIG
        globals()["DEFAULT_CONFIG"] = replace(wb_cfg,
                                              alarm_event="event.session.key_dead")
        STATE.events.append({"seq": 1, "ts": time.time(), "sid": "file",
                             "type": "event.session.key_dead",
                             "payload": {"old_sid": "s9"}, "boot_id": "b"})
        k4 = _supervisor_key_summary()
        check("事件环兜底判 dead", k4["supervisor_key_state"] == "dead", str(k4)[:100])
        check("兜底取到 sid", k4["supervisor_key_sid"] == "s9")
        STATE.events.clear()
        globals()["DEFAULT_CONFIG"] = _saved_cfg
    finally:
        globals()["PATHS"] = _saved_paths
        globals()["production_state_root"] = lambda: _saved_prod
        shutil.rmtree(ktmp, ignore_errors=True)

    check("healthz 有 supervisor 键三字段",
          "supervisor_key_state" in src and "last_supervisor_alarm_age_s" in src)
    check("healthz 有 guardians 段", '"guardians"' in src)
    check("healthz 有 state_dir 与 port", '"state_dir"' in src and '"port": PORT' in src)
    check("healthz 区分 ok 与 ready", '"ready": ready' in src and '"ok": True' in src)

    # 26) Startup announcement: normal vs degraded, reason stated.
    _orig_env_base = kimi_paths._env_base
    _orig_latest = kimi_paths.latest_instance
    try:
        kimi_paths._env_base = lambda: None
        kimi_paths.latest_instance = lambda root=None: {
            "server_id": "demo", "path": "x", "port": 58627,
            "host": "127.0.0.1", "mtime": 0.0}
        _line_ok = announce_upstream()
        check("启动打印含端口", "port=" in _line_ok and "58627" in _line_ok, _line_ok)
        check("正常情况下标 source=instance",
              "source=instance" in _line_ok and "degraded=False" in _line_ok, _line_ok)
        kimi_paths.latest_instance = lambda root=None: None
        _line_bad = announce_upstream()
        check("降级启动明确标示",
              "degraded=True" in _line_bad and "降级" in _line_bad, _line_bad)
        check("降级原因写进输出",
              "source=default" in _line_bad and "实例注册不可用" in _line_bad, _line_bad)
        kimi_paths._env_base = lambda: "http://127.0.0.1:59999"
        _line_env = announce_upstream()
        check("环境变量来源标 source=env",
              "source=env" in _line_env and "degraded=True" in _line_env, _line_env)
    finally:
        kimi_paths._env_base = _orig_env_base
        kimi_paths.latest_instance = _orig_latest

    # 27) Paths honours the environment override, explicit root wins.
    _penv = os.environ.get("WSS_BUS_STATE_DIR")
    try:
        with_env = Path(tempfile.mkdtemp(prefix="wss_env_"))
        os.environ["WSS_BUS_STATE_DIR"] = str(with_env)
        check("Paths() 认环境变量", Paths().root == with_env.resolve(), str(Paths().root))
        explicit = Path(tempfile.mkdtemp(prefix="wss_exp_"))
        check("显式 root 优先", Paths(explicit).root == explicit.resolve())
        os.environ.pop("WSS_BUS_STATE_DIR", None)
        check("环境变量清空后回落生产根",
              Paths().root == Path(production_state_root()).resolve())
    finally:
        if _penv is None:
            os.environ.pop("WSS_BUS_STATE_DIR", None)
        else:
            os.environ["WSS_BUS_STATE_DIR"] = _penv
        shutil.rmtree(with_env, ignore_errors=True)
        shutil.rmtree(explicit, ignore_errors=True)

    # 28) write_heartbeat writes only into the current root, never production.
    h3 = Path(tempfile.mkdtemp(prefix="wss_h3_"))
    prod3 = Path(tempfile.mkdtemp(prefix="wss_h3p_"))
    _saved_paths = PATHS
    _saved_prod = _cfg().production_state_root
    try:
        globals()["PATHS"] = Paths(h3)
        globals()["production_state_root"] = lambda: prod3
        ph = prod3 / GUARDIAN_THRESHOLDS["monitor"][0]
        ph.parent.mkdir(parents=True, exist_ok=True)
        ph.write_text('{"ts": 1}', encoding="utf-8")
        os.utime(ph, (1_700_000_000, 1_700_000_000))
        _before = ph.stat().st_mtime
        write_heartbeat("monitor", {"probe": 1})
        check("心跳写当前根", (h3 / GUARDIAN_THRESHOLDS["monitor"][0]).is_file())
        check("心跳不碰生产", ph.stat().st_mtime == _before,
              f"prod_mtime {_before}->{ph.stat().st_mtime}")
    finally:
        globals()["PATHS"] = _saved_paths
        globals()["production_state_root"] = lambda: _saved_prod
        shutil.rmtree(h3, ignore_errors=True)
        shutil.rmtree(prod3, ignore_errors=True)

    # 29) Key helpers and config validation edge cases.
    check("repo_full 归一化", repo_full_of("Owner/Demo") == "owner/demo")
    check("repo_full 无 owner 返回空", repo_full_of("demo") == "")
    check("project_of_repo 取仓名", project_of_repo("Owner/Demo") == "demo")
    check("issue 为空不成键", issue_key("o/demo", None) == "")
    check("role 不可识别为 unknown", _role_of("weird_name.md") == "unknown")
    check("空标题不命中", _heading_matches("", _cfg().decide_section) is False)
    check("标题归一化", _norm_heading("## 3.  待  拍板") == "待 拍板",
          _norm_heading("## 3.  待  拍板"))
    check("段目标归一化",
          _norm_section_want(_cfg().decide_section)
          == _norm_heading(_cfg().decide_section),
          _norm_section_want(_cfg().decide_section))

    _dup = ""
    try:
        WatchConfig.validate({"watchers": [
            {"id": "a", "kind": "dir_new_file", "path": "x", "event": "e"},
            {"id": "a", "kind": "dir_new_file", "path": "y", "event": "e"}]})
    except ValueError as exc:
        _dup = str(exc)
    check("重复 watcher id 被拒", "重复" in _dup, _dup)
    _poll = ""
    try:
        WatchConfig.validate({"watchers": [
            {"id": "a", "kind": "dir_new_file", "path": "x", "event": "e"}],
            "poll_interval_s": 999})
    except ValueError as exc:
        _poll = str(exc)
    check("poll 越界被拒", "越界" in _poll, _poll)
    _pat = ""
    try:
        WatchConfig.validate({"watchers": [
            {"id": "a", "kind": "file_section_md5", "path_glob": "x/*.md",
             "section": "s", "section_pattern": "([", "event": "e"}]})
    except ValueError as exc:
        _pat = str(exc)
    check("非法 section_pattern 被拒", "section_pattern" in _pat, _pat)

    # 30) Journal write failure flips ok=False (never silently accept).
    blocker = Path(tempfile.mkdtemp(prefix="wss_jblk_")) / "file"
    blocker.write_text("x", encoding="utf-8")
    try:
        jbad = Journal(blocker / "sub" / "j.jsonl")
        jbad.put({"request_id": "r", "verb": "send"})
        check("journal 写失败即判不可信", jbad.ok is False)
    finally:
        shutil.rmtree(blocker.parent, ignore_errors=True)

    # 31) Probe allowlist refuses registered sessions.
    _saved_reg = globals().get("REGISTRY_IDS")
    try:
        globals()["REGISTRY_IDS"] = {"session_reg"}
        STATE.probe_sids = set()
        check("探针拒绝在册会话", _probe_allowed("session_reg") is False)
        STATE.probe_sids = {"session_new"}
        check("探针接受自己建的靶子", _probe_allowed("session_new") is True)
    finally:
        globals()["REGISTRY_IDS"] = _saved_reg
        STATE.probe_sids = set()

    # 32) Injected binding lookup wins over the file.
    try:
        configure(bind_lookup=lambda repo, issue: ("injected", "session_i"))
        check("注入绑定查表生效", bind_worker("o/demo", 1)[0] == "injected")
    finally:
        configure(bind_lookup=None)

    # 33) Payload qualification is idempotent and never invents a key.
    _p = qualify_event_payload({"project": "demo", "issue": 12}, repo="OwnerA/Demo")
    _p2 = qualify_event_payload(_p, repo="OwnerA/Demo")
    check("payload 二次限定不漂移", _p2["key"] == _p["key"] == "ownera/demo#12")
    _p3 = qualify_event_payload({"project": "demo"}, repo="OwnerA/Demo")
    check("无 issue 号不造 key",
          "key" not in _p3 and _p3["repo_full"] == "ownera/demo")

    # 34) Port injection: configure() installs the port the caller supplied.
    _saved_port = PORT
    try:
        configure(port=59999)
        check("configure 注入端口", PORT == 59999, str(PORT))
        check("_port_of 用注入端口", _port_of(None) == 59999)
        configure(port=0)
    finally:
        configure(port=_saved_port)

    print(f"--self-test: {total - len(fails)}/{total} 通过")
    for f_ in fails:
        print(f"  FAIL  {f_}")
    return 0 if not fails else 1


# --------------------------------------------------------------------- probe
REGISTRY_IDS = None


def _registry_ids() -> set:
    global REGISTRY_IDS
    if REGISTRY_IDS is None:
        try:
            reg = json.loads(_sessions_path().read_text(encoding="utf-8"))
            REGISTRY_IDS = {v.get("id") for v in reg.values() if isinstance(v, dict)}
        except (OSError, ValueError):
            REGISTRY_IDS = set()
    return REGISTRY_IDS


def _probe_allowed(sid: str) -> bool:
    """Allowlist: created by this process, or a ``wk-probe-*`` not in the registry."""
    if sid in STATE.probe_sids:
        return True
    if sid in _registry_ids():
        return False
    info = K.session_info(sid)
    if "_err" in info:
        return False
    return str(info.get("title") or "").startswith(PROBE_TITLE_PREFIX)


def _probe_model() -> str:
    cfg = os.environ.get("WSS_PROBE_MODEL")
    if cfg:
        return cfg
    try:
        st, payload = K.get("/api/v1/config", timeout=8)
        d = K.data_of(payload) or {}
        return str(d.get("default_model") or "")
    except Exception:  # noqa: BLE001
        return ""


def probe(sid: str = "", keep: bool = False) -> int:
    """End-to-end probe: create a throw-away target, talk only to it, reclaim, verify.

    Allowlist discipline: only accepts ``STATE.probe_sids`` created by ``--probe``,
    or a ``--sid`` pointing at a ``wk-probe-*`` session **not** in the registry.
    Registered assistant sessions are always refused.
    """
    created = ""
    rc = 0
    target = sid
    try:
        if sid:
            if not _probe_allowed(sid):
                print(f"拒绝：{sid} 不在探针白名单（只允许 wk-probe-* 且非登记会话）")
                return 2
        else:
            title = f"{PROBE_TITLE_PREFIX}{time.strftime('%Y%m%d-%H%M%S')}"
            ok, d = K.create_session(title, cwd=str(_workspace_root()), model=_probe_model())
            if not ok:
                print(f"建靶失败：{d}")
                return 1
            target = d.get("id") or ""
            created = target
            STATE.probe_sids.add(target)
            print(f"[1/6] 靶子 {title} = {target}")

        # 2) Subscribe to that sid only (independent WS, no interference).
        frames, err = K.WSProbe(target, timeout=3, transcript=TRANSCRIPT_GRADE).collect(
            want=("agent", "usage"), budget=12)
        got = {f.get("type") for f in frames}
        print(f"[2/6] 订阅该 sid 收到帧类型 {sorted(t for t in got if t)} err={err or '-'}")
        if not {"agent", "usage"} & got:
            print("  FAIL 未收到 agent/usage 帧")
            rc = 1

        # 3) Send one message and verify the match.
        ok, detail = K.send(target, "只回两个字：收到")
        print(f"[3/6] send ok={ok} match={detail.get('match')} pid={detail.get('prompt_id')}")
        if not (ok and detail.get("match")):
            rc = 1
        time.sleep(6)

        # 4) Abort.
        aok, acode = K.abort_session(target)
        print(f"[4/6] abort ok={aok} code={acode}")
    finally:
        if rc == 2 and not created:
            print(f"[6/6] REFUSED target={sid}")
            return 2
        if created and not keep:
            try:
                K.abort_session(created)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1)
            aok, _ = K.archive_session(created)
            info = K.session_info(created)
            archived = bool(info.get("archived"))
            busy = info.get("busy")
            print(f"[5/6] 回收 archive={aok} archived={archived} busy={busy}")
            if not (archived and busy is False):
                print(f"  FAIL 回收未达 archived=true && busy=false；残留 sid={created}")
                rc = 1
        elif created:
            print(f"[5/6] --keep：保留靶子 {created}")
        print(f"[6/6] {'PASS' if rc == 0 else 'FAIL'} target={created or sid}")
    return rc


# --------------------------------------------------------------------- daemon
async def _run_daemon() -> int:
    from aiohttp import web

    PATHS.mkdir()
    announce_upstream()
    if not acquire_lock():
        return 3
    PATHS.pid.write_text(str(os.getpid()), encoding="utf-8")
    runner = web.AppRunner(make_app())
    await runner.setup()
    site = web.TCPSite(runner, HOST, PORT)
    await site.start()
    log(f"HTTP 面已起 http://{HOST}:{PORT}（pid={os.getpid()} state={PATHS.root}）")

    prod = asyncio.create_task(producer())
    flush = asyncio.create_task(_flush_loop())
    watch = asyncio.create_task(_watch_files())
    try:
        await asyncio.gather(prod, flush, watch)
    except asyncio.CancelledError:
        pass
    finally:
        for t in (prod, flush, watch):
            t.cancel()
        for t in (prod, flush, watch):
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        _write_cache()               # flush before releasing the lock
        STATE.seq.save()
        await runner.cleanup()
        PATHS.pid.unlink(missing_ok=True)
        release_lock()
    return 0


async def _flush_loop() -> None:
    while True:
        await asyncio.sleep(10)
        try:
            _write_cache()
        except Exception as exc:  # noqa: BLE001
            log(f"落盘失败：{type(exc).__name__}: {exc}")


def _status() -> int:
    h = bus_health(port=PORT)
    if not h:
        print(f"daemon: DOWN（localhost:{PORT} 不通；工具会降级直连）")
        return 1
    print("daemon: UP")
    print(json.dumps(h, ensure_ascii=False, indent=1))
    return 0


def main(argv=None) -> int:
    global PATHS, PORT

    ap = argparse.ArgumentParser()
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--install-autostart", action="store_true")
    ap.add_argument("--uninstall-autostart", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--self-test", dest="self_test", action="store_true")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--sid", default="", help="--probe 用：指定已有 wk-probe 靶子")
    ap.add_argument("--keep", action="store_true", help="--probe：不回收靶子（排障用）")
    ap.add_argument("--state-dir", default="", help="隔离运行目录")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--wait", type=float, default=8.0)
    ap.add_argument("--self-check", dest="self_check", action="store_true")
    args = ap.parse_args(argv)

    if args.state_dir:
        PATHS = Paths(args.state_dir)
        STATE.seq = SeqStore(PATHS.seq)
        STATE.journal = Journal(PATHS.journal)
    if args.port:
        PORT = args.port
    elif not PORT:
        PORT = int(os.environ.get("WSS_BUS_PORT", "0") or 0)

    if args.self_check:
        return self_check()
    if args.self_test:
        _cleanup = _isolate_for_self_test()
        try:
            return self_test()
        finally:
            if _cleanup is not None:
                shutil.rmtree(_cleanup, ignore_errors=True)
    if args.probe:
        return probe(sid=args.sid, keep=args.keep)
    if args.status:
        return _status()
    if args.install_autostart:
        return install_autostart()
    if args.uninstall_autostart:
        return uninstall_autostart()
    if args.once:
        return asyncio.run(_once(args.wait))
    if args.daemon:
        try:
            return asyncio.run(_run_daemon())
        except KeyboardInterrupt:
            return 0
    print(__doc__.strip())
    return 2


def _isolate_for_self_test() -> Path:
    """Point PATHS and the seq/journal at a throw-away root before self_test.

    The self-test asserts it never touches production, so both the state root and
    the "production" baseline it compares against are throw-away directories. A
    standalone ``python -m super_agent.wss_bus --self-test`` therefore needs no
    caller injection at all, and still cannot write a real state directory.

    Returns the root so the caller can remove it afterwards (an injected
    ``self_test_root`` is the caller's to clean, so it is never deleted here).
    """
    global PATHS
    cfg = _cfg()
    injected = cfg.self_test_root is not None
    root = Path(cfg.self_test_root or tempfile.mkdtemp(prefix="wss_selftest_run_"))
    root.mkdir(parents=True, exist_ok=True)
    fake_prod = root / "_fake_production"
    fake_prod.mkdir(parents=True, exist_ok=True)
    overrides: dict = {}
    if cfg.workspace_root is None:
        overrides["workspace_root"] = root
    if cfg.production_state_root is None:
        overrides["production_state_root"] = fake_prod
    if cfg.sessions_path is None:
        overrides["sessions_path"] = root / "sessions.json"
    if overrides:
        configure(**overrides)
    PATHS = Paths(root)
    PATHS.mkdir()
    STATE.seq = SeqStore(PATHS.seq)
    STATE.journal = Journal(PATHS.journal)
    return None if injected else root


async def _once(wait: float) -> int:
    import aiohttp

    token = kimi_paths.token()
    if not token:
        print("读不到 token")
        return 1
    n, types = 0, {}
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(K._ws_url(),
                                       headers={"Authorization": f"Bearer {token}"},
                                       heartbeat=20) as ws:
                await _handshake(ws)
                deadline = time.time() + wait
                while time.time() < deadline:
                    try:
                        msg = await ws.receive(timeout=max(0.5, deadline - time.time()))
                    except asyncio.TimeoutError:
                        break
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        break
                    try:
                        frame = json.loads(msg.data)
                    except ValueError:
                        continue
                    t = frame.get("type") or ""
                    if t == "ping":
                        await ws.send_json({"type": "pong",
                                            "payload": {"nonce": (frame.get("payload")
                                                                  or {}).get("nonce", "")}})
                        continue
                    if t in ("pong", "server_hello"):
                        continue
                    if t in ("ack", "error"):
                        _mark_ack(frame)
                        continue
                    types[t] = types.get(t, 0) + 1
                    n += 1
    except Exception as exc:  # noqa: BLE001
        print(f"--once 失败：{type(exc).__name__}: {exc}")
        return 1
    ok = sum(1 for r in STATE.subscribed.values() if r.get("ok"))
    print(f"--once 完成：请求订阅 {len(STATE.subscribed)}，ack 成功 {ok}，收到 {n} 帧")
    print("  类型分布：" + (json.dumps(types, ensure_ascii=False) if types else "（无事件）"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
