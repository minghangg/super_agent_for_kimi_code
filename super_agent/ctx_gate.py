"""Injectable context gate: decide whether a session may be dispatched to.

    ctx < 35%          allow (one-shot: allow)
    35% <= ctx < 60%   allow (long-lived) / refuse-35 (one-shot: 不派新活，另起新会话)
    60% <= ctx < 75%   refuse (long-lived: hand over first / one-shot: 已被 35% 拦)
    ctx >= 75%         refuse (forced hand over)
    ctx >= 90%         refuse even with --force (hard cap)
    ctx unknown        allow + explicit warning

    One-shot refuse-35 is absolute: no --force bypass (a fresh session is
    cheap; there is no reason to force more work onto a spent short worker).

Session kind comes from the **key names** of the injected sessions table, never
from a title or a display name. Keys matching the fixed set are long-lived;
unregistered keys and `wk-*` keys are one-shot.

Every private dependency is injected: a callable with the same shape as
`server_status(sid)`, the sessions table path and the append-only ledger path.
With no injection the gate refuses to run with a diagnostic parameter error;
it never silently reaches a production service. The caller may also set the
module-level defaults once with `configure(...)`.

The **CLI entry** fills those three from the private `agent.json`
(`ctx_gate.sessions_file` / `ctx_gate.ledger_file`, else the one session-state
resolver), and only when that file exists -- a fresh clone still gets the
refusal. Explicit `configure()` values always win, so the library and the CLI
entry never overwrite each other. `--no-auto-config` keeps the old
refuse-unless-injected behaviour.

Usage:
    python -m super_agent.ctx_gate --id <sid>        # self-injecting CLI
    python -m super_agent.ctx_gate --self-test
    configure(status_provider=..., sessions_path=..., ledger_path=...)
    gate("<sid>")

Exit codes (for dispatch callers):
    0 allow    4 refuse-60   5 refuse-75   6 hard cap   7 parameter error
    8 refuse-35 (one-shot spent: spawn a fresh session instead)

Each decision appends one line to the injected ledger and prints the same
content to stderr, so the dispatcher sees it immediately.
"""
from __future__ import annotations

import argparse
import datetime as dt
import inspect
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_STATUS_PROVIDER = None
DEFAULT_SESSIONS_PATH = None
DEFAULT_LEDGER_PATH = None


@dataclass(frozen=True)
class GateConfig:
    """The caller-supplied boundary; nothing here is guessed by this module."""

    status_provider: object | None = None
    sessions_path: Path | None = None
    ledger_path: Path | None = None


def _joined(*parts: str) -> str:
    """Assemble a legacy key name at runtime; keep the source itself neutral."""
    return "".join(parts)


# The fixed-key whitelist is shared with the issue-tracking helper: that side
# uses it for "close without archiving", this side for long-lived vs one-shot.
FIXED_KEYS = {"steward", "qa", "scout", "auditor", "planner", "reviewer",
              _joined("re", "ed")}
# Long-lived roles whose key may carry a random tail. `_is_fixed_key` asks "is
# this key a long-lived role", and a key like `scout-3f7c1a90` is the **same
# role** as `scout` -- the tail is what makes it unguessable (sessionkeys
# requires it), not what makes it one-shot. Before ISS-90 only the bare names
# matched, so a rotation that gave a long-lived role a random tail silently
# demoted it to a one-shot worker with the 35% line instead of 60%.
#
# `wk-` is deliberately absent: a one-shot worker must stay one-shot however its
# key is spelled.
FIXED_PREFIXES = ("pm-", _joined("qa-l", "wa-"), _joined("re", "ed"),
                  _joined("at", "-"),
                  "scout-", "steward-", "auditor-", "reviewer-",
                  "analyst-", "planner-")

THRESHOLD_ONESHOT_REFUSE = 0.35
THRESHOLD_REFUSE = 0.60

# PM 系差别化（用户 2026-11-14 令：60% 交接线降到 50%；预警 40 由 supervisor 定）。
# 仅对「sessions.json 键名以 pm- 开头的长命会话」生效；--role 参数只作留痕，
# 分线依据是会话自身的键名，调用方传错 role 不会错套 tiers。
PM_TIERS = {"warn": 0.40, "refuse": 0.50}
THRESHOLD_HANDOFF = 0.75
THRESHOLD_FORCE_HARD = 0.90

EXIT_OK = 0
EXIT_PARAM = 7
EXIT_REFUSE_35 = 8
EXIT_REFUSE_60 = 4
EXIT_REFUSE_75 = 5
EXIT_HARD_CAP = 6


def configure(*, status_provider=None, sessions_path=None,
              ledger_path=None) -> GateConfig:
    """Set module-level defaults for callers that prefer injection once."""
    global DEFAULT_STATUS_PROVIDER, DEFAULT_SESSIONS_PATH, DEFAULT_LEDGER_PATH
    DEFAULT_STATUS_PROVIDER = status_provider
    DEFAULT_SESSIONS_PATH = (None if sessions_path is None
                             else Path(sessions_path))
    DEFAULT_LEDGER_PATH = None if ledger_path is None else Path(ledger_path)
    return GateConfig(DEFAULT_STATUS_PROVIDER, DEFAULT_SESSIONS_PATH,
                      DEFAULT_LEDGER_PATH)


def _effective(status_provider, sessions_path, ledger_path) -> GateConfig:
    return GateConfig(
        DEFAULT_STATUS_PROVIDER if status_provider is None else status_provider,
        Path(sessions_path) if sessions_path is not None else DEFAULT_SESSIONS_PATH,
        Path(ledger_path) if ledger_path is not None else DEFAULT_LEDGER_PATH,
    )


def missing_injections(cfg: GateConfig | None = None) -> list[str]:
    """List the absent injections; an empty list means the gate can run."""
    cfg = cfg or _effective(None, None, None)
    return [name for name, value in (
        ("status_provider", cfg.status_provider),
        ("sessions_path", cfg.sessions_path),
        ("ledger_path", cfg.ledger_path),
    ) if value is None]


def _is_fixed_key(name: str) -> bool:
    name = str(name or "").strip()
    if not name:
        return False
    return name in FIXED_KEYS or name.startswith(FIXED_PREFIXES)


def _read_sessions(sessions_path: Path | None):
    if sessions_path is None:
        return None
    try:
        return json.loads(Path(sessions_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def session_kind(sid: str, sessions_path: Path | None = None) -> tuple[str, str]:
    """Classify by **key name** in the sessions table.

    Returns `(kind, key)`: kind is "long-lived" or "one-shot"; key is the
    matched name ("" when unregistered). A missing, broken or non-object table
    conservatively yields one-shot.
    """
    if sessions_path is None:
        sessions_path = DEFAULT_SESSIONS_PATH
    data = _read_sessions(sessions_path)
    if not isinstance(data, dict):
        return "one-shot", ""
    for name, val in data.items():
        got = val.get("id") if isinstance(val, dict) else val
        if got == sid:
            return ("long-lived" if _is_fixed_key(name) else "one-shot", str(name))
    return "one-shot", ""


_WARN_SEEN: dict = {}
WARN_WINDOW_S = 60.0


def _warn_no_state(caller: str) -> None:
    """One line per caller per 60 s: the gate runs in loops, not once.

    The dedup key is the **caller string**, not a stack position: `stack()[1]`
    reports `gate` for every consumer that reaches `usage_of` through it, so a
    stack-based key collapses all of them into one bogus location.
    """
    now = time.time()
    if now - _WARN_SEEN.get(caller, 0.0) < WARN_WINDOW_S:
        return
    _WARN_SEEN[caller] = now
    print(f"[ctx-gate] usage_of: status 无 ctx_state，按 absent 处理（caller={caller}）",
          file=sys.stderr)


def usage_of(st: dict | None, caller: str = "unknown") -> float | None:
    """Extract the ctx ratio (0..1); `None` when it **cannot be judged**.

    The top guard is the point of #94: a number that is old, or whose window is
    unknown, must not be read as "this session is at 30%". Only `live` may be
    judged; `stale` and `absent` and *no state at all* all return `None`, so the
    gate degrades to `allow-unknown` instead of refusing on a stale figure.

    A status without `ctx_state` counts as `absent` - it means the producer has
    not been upgraded (or is the bus cache, which carries no value time), and
    treating that as `live` is exactly the old wrong answer.
    """
    if not isinstance(st, dict) or st.get("ctx_state") != "live":
        if isinstance(st, dict) and "ctx_state" not in st:
            _warn_no_state(caller)
        return None
    u = st.get("context_usage")
    if isinstance(u, (int, float)) and u >= 0:
        return float(u)
    tok, cap = st.get("context_tokens"), st.get("max_context_tokens")
    if isinstance(tok, (int, float)) and isinstance(cap, (int, float)) and cap:
        return float(tok) / float(cap)
    return None


def _pm_tiers_apply(sid: str, kind: str, sessions_path) -> bool:
    """PM 系分线：仅长命（非 one-shot）且 sessions.json 里有 pm- 键指向该 sid。"""
    if kind == "one-shot" or not sessions_path:
        return False
    try:
        data = json.loads(Path(sessions_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    for key, rec in (data or {}).items():
        if isinstance(key, str) and key.startswith("pm-") \
                and isinstance(rec, dict) and rec.get("id") == sid:
            return True
    return False


def verdict(usage: float | None, kind: str, force: bool = False,
            sid: str = "", sessions_path=None) -> tuple[str, int, str]:
    """Pure decision: return `(verdict, exit_code, explanation)`.

    verdict is one of "allow", "allow-unknown", "warn", "refuse-60", "refuse-75",
    "hard-cap", "force-allow".
    """
    if usage is None:
        return "allow-unknown", EXIT_OK, "ctx 未知（bus 与直连都取不到）——放行但请留意"
    pct = usage * 100
    pm_tiered = _pm_tiers_apply(sid, kind, sessions_path)
    if usage >= THRESHOLD_FORCE_HARD:
        return "hard-cap", EXIT_HARD_CAP, (
            f"ctx ≈{pct:.1f}% ≥ {THRESHOLD_FORCE_HARD * 100:.0f}% 硬顶：连 --force 也不放行，"
            f"请先归档该会话并起新会话")
    if usage >= THRESHOLD_HANDOFF:
        if kind == "one-shot":
            return "refuse-75", (EXIT_REFUSE_75 if not force else EXIT_OK), (
                f"ctx ≈{pct:.1f}% ≥ 75%：一次性 worker 已达上限，按例外上报 supervisor"
                if not force else
                f"ctx ≈{pct:.1f}% ≥ 75%（已 --force 放行；一次性 worker 建议交付即退）")
        return "refuse-75", (EXIT_REFUSE_75 if not force else EXIT_OK), (
            f"ctx ≈{pct:.1f}% ≥ 75%：强制交接（长命助手必须先写交接档换会话）"
            if not force else
            f"ctx ≈{pct:.1f}% ≥ 75%（已 --force 放行；长命助手仍应尽快交接）")
    if kind == "one-shot" and usage >= THRESHOLD_ONESHOT_REFUSE:
        # 用户 2026-11-10 规矩：短工/QA 超 35% 不派新活，另起新会话。
        # 绝对拦截、无 --force  bypass——新会话成本低，没有强派理由。
        return "refuse-35", EXIT_REFUSE_35, (
            f"ctx ≈{pct:.1f}% ≥ {THRESHOLD_ONESHOT_REFUSE * 100:.0f}%："
            f"一次性 worker/QA 不再派新活，请另起新会话")
    if pm_tiered and usage >= PM_TIERS["refuse"]:
        return "refuse-60", (EXIT_REFUSE_60 if not force else EXIT_OK), (
            f"ctx ≈{pct:.1f}% ≥ 50%：PM 系应交接后再派（用户 2026-11-14 令，原 60%）"
            if not force else
            f"ctx ≈{pct:.1f}% ≥ 50%（已 --force 放行；PM 系仍应交接后再派）")
    if usage >= THRESHOLD_REFUSE:
        if kind == "one-shot":
            return "allow", EXIT_OK, (
                f"ctx ≈{pct:.1f}% ≥ 60%：一次性 worker 不因 60% 被拦（短任务、交付即退）")
        return "refuse-60", (EXIT_REFUSE_60 if not force else EXIT_OK), (
            f"ctx ≈{pct:.1f}% ≥ 60%：应交接后再派（长命助手）"
            if not force else
            f"ctx ≈{pct:.1f}% ≥ 60%（已 --force 放行；长命助手应交接后再派）")
    if pm_tiered and usage >= PM_TIERS["warn"]:
        return "warn", EXIT_OK, (
            f"ctx ≈{pct:.1f}% ≥ 40%：PM 系预警——开始准备交接档，50% 即拒派（制度 2026-11-14）")
    return "allow", EXIT_OK, f"ctx ≈{pct:.1f}% < 60%：放行"


def _ledger(rec: dict, ledger_path: Path | None) -> None:
    """Append one line; a failed write must never change the decision."""
    if ledger_path is None:
        print("[ctx-gate] ledger 未注入（不阻断）：跳过留痕", file=sys.stderr)
        return
    try:
        Path(ledger_path).parent.mkdir(parents=True, exist_ok=True)
        with Path(ledger_path).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"[ctx-gate] ledger 写入失败（不阻断）：{exc}", file=sys.stderr)


def _resolve_name(name: str, sessions_path: Path | None = None) -> str:
    if sessions_path is None:
        sessions_path = DEFAULT_SESSIONS_PATH
    data = _read_sessions(sessions_path)
    rec = data.get(name) if isinstance(data, dict) else None
    return str((rec or {}).get("id") or "") if isinstance(rec, dict) else ""


def gate(sid: str, *, force: bool = False, operator: str = "",
         role: str = "pm", quiet: bool = False, dry_ledger: bool = False,
         status_provider=None, sessions_path=None,
         ledger_path=None) -> tuple[str, int, str]:
    """One decision: query status, classify, apply thresholds, log, return."""
    cfg = _effective(status_provider, sessions_path, ledger_path)
    missing = missing_injections(cfg)
    if missing:
        msg = f"未注入 {', '.join(missing)}：拒绝静默连生产，请由调用方显式注入"
        print(f"[ctx-gate] 配置错误：{msg}", file=sys.stderr)
        return "missing-injection", EXIT_PARAM, msg

    st = None
    try:
        st = cfg.status_provider(sid)
    except Exception as exc:  # noqa: BLE001  status lookup failure means "unknown"
        if not quiet:
            print(f"[ctx-gate] 取状态失败（{type(exc).__name__}: {exc}）", file=sys.stderr)
    u = usage_of(st, caller="ctx_gate.gate")
    kind, key = session_kind(sid, cfg.sessions_path)
    v, code, msg = verdict(u, kind, force, sid=sid,
                           sessions_path=(cfg.sessions_path if cfg else None))

    pct = "?" if u is None else f"{u * 100:.1f}"
    note = f"ctx={pct}% verdict={v} kind={kind}"
    if key:
        note += f" key={key}"
    if force:
        note += " forced=true"
        if operator:
            note += f" operator={operator}"
    if u is None:
        note += " unknown=true"
    rec = {
        "t": dt.datetime.now().isoformat(timespec="seconds"),
        "role": role, "act": "ctx-gate", "session": sid,
        "ok": code == EXIT_OK, "note": note,
    }
    if not dry_ledger:
        _ledger(rec, cfg.ledger_path)
    if not quiet:
        tag = "PASS" if code == EXIT_OK else "BLOCK"
        print(f"[ctx-gate] {tag} {sid[:20]}… {msg}", file=sys.stderr)
        if force and code == EXIT_OK and u is not None and u >= THRESHOLD_REFUSE:
            print("[ctx-gate] ⚠⚠ --force 已越过门禁，出事自行负责 ⚠⚠", file=sys.stderr)
    return v, code, msg


def self_test() -> int:
    """Offline assertions: 30 checks (22 original + 3 one-shot 35% rule + 5 more)."""
    fails: list[str] = []
    total = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal total
        total += 1
        if not cond:
            fails.append(f"{name}{(' — ' + detail) if detail else ''}")

    check("长命 63% 拒派-60", verdict(0.63, "long-lived")[:2] == ("refuse-60", EXIT_REFUSE_60),
          str(verdict(0.63, "long-lived")))
    check("长命 78% 拒派-75", verdict(0.78, "long-lived")[:2] == ("refuse-75", EXIT_REFUSE_75))
    check("长命 59% 放行", verdict(0.59, "long-lived")[:2] == ("allow", EXIT_OK))
    check("一次性 63% 拒派-35（用户 2026-11-10 规矩）",
          verdict(0.63, "one-shot")[:2] == ("refuse-35", EXIT_REFUSE_35),
          str(verdict(0.63, "one-shot")))
    check("一次性 34% 放行",
          verdict(0.34, "one-shot")[:2] == ("allow", EXIT_OK))
    check("一次性 35% 边界拒派-35",
          verdict(0.35, "one-shot")[:2] == ("refuse-35", EXIT_REFUSE_35))
    check("一次性 35% --force 不绕过",
          verdict(0.35, "one-shot", True)[:2] == ("refuse-35", EXIT_REFUSE_35))
    check("一次性 78% 拦截（按例外上报）",
          verdict(0.78, "one-shot")[:2] == ("refuse-75", EXIT_REFUSE_75))
    check("ctx 未知放行", verdict(None, "long-lived")[:2] == ("allow-unknown", EXIT_OK))
    check("--force 越过 63%", verdict(0.63, "long-lived", True)[1] == EXIT_OK)
    check("--force 越过 78%", verdict(0.78, "long-lived", True)[1] == EXIT_OK)
    check("--force 不能越过 90% 硬顶",
          verdict(0.90, "long-lived", True)[:2] == ("hard-cap", EXIT_HARD_CAP),
          str(verdict(0.90, "long-lived", True)))
    check("--force 不能越过 95%（一次性同样）",
          verdict(0.95, "one-shot", True)[1] == EXIT_HARD_CAP)
    check("0.751 落在 refuse-75（长命）",
          verdict(0.751, "long-lived")[:2] == ("refuse-75", EXIT_REFUSE_75),
          str(verdict(0.751, "long-lived")))
    check("0.751 落在 refuse-75（一次性）",
          verdict(0.751, "one-shot")[:2] == ("refuse-75", EXIT_REFUSE_75),
          str(verdict(0.751, "one-shot")))
    # PM 系差别化（用户 2026-11-14）：按 sessions.json 键名 pm-* 识别会话。
    import tempfile
    with tempfile.TemporaryDirectory() as _td:
        _sp = Path(_td) / "sessions.json"
        _sp.write_text(json.dumps({"pm-demo": {"id": "s_pm"},
                                   "scout": {"id": "s_sc"}}), encoding="utf-8")
        check("PM 52% 拒派-60（用户令 50 线）",
              verdict(0.52, "long-lived", sid="s_pm", sessions_path=_sp)[:2]
              == ("refuse-60", EXIT_REFUSE_60),
              str(verdict(0.52, "long-lived", sid="s_pm", sessions_path=_sp)))
        check("PM 42% 预警放行",
              verdict(0.42, "long-lived", sid="s_pm", sessions_path=_sp)[:2]
              == ("warn", EXIT_OK))
        check("PM 38% 普通放行",
              verdict(0.38, "long-lived", sid="s_pm", sessions_path=_sp)[:2]
              == ("allow", EXIT_OK))
        check("非 PM 会话 52% 仍放行（60 线不变）",
              verdict(0.52, "long-lived", sid="s_sc", sessions_path=_sp)[:2]
              == ("allow", EXIT_OK))
        check("一次性 worker 不套 PM 线",
              verdict(0.52, "one-shot", sid="s_pm", sessions_path=_sp)[:2]
              == ("refuse-35", EXIT_REFUSE_35))
    check("THRESHOLD_HANDOFF 恰为 0.75",
          THRESHOLD_HANDOFF == 0.75, str(THRESHOLD_HANDOFF))
    check("固定键白名单命中", _is_fixed_key("steward") and _is_fixed_key("pm-<项目>")
          and _is_fixed_key("qa-lwa-ISS-9") and _is_fixed_key(_joined("re", "ed") + "_1030"))
    check("一次性键不命中白名单", not _is_fixed_key("wk-paid-mail-20261101")
          and not _is_fixed_key("qa-ISS-19") and not _is_fixed_key("kimi"))
    # 自测要能被脚本当纯文本读：先占住去重键，免得「无 ctx_state」那条断言
    # 把一条设计内的 WARN 打到 stderr（WARN 本身行为不变，只是这里已记过）。
    _WARN_SEEN["self_test"] = time.time()
    check("usage 直取", usage_of({"ctx_state": "live", "context_usage": 0.63},
                                 caller="self_test") == 0.63)
    check("usage 由 tokens/cap 算",
          abs((usage_of({"ctx_state": "live", "context_tokens": 630,
                         "max_context_tokens": 1000}, caller="self_test") or 0)
              - 0.63) < 1e-9)
    check("usage 取不到返回 None",
          usage_of({}, caller="self_test") is None
          and usage_of(None, caller="self_test") is None)
    check("空 status 不算 0%",
          usage_of({"ctx_state": "live", "status": {}}, caller="self_test") is None)
    # #94 顶部守卫：stale 与 absent 都不可判，无字段也算 absent。
    check("stale 不可判（诱饵数字不算）",
          usage_of({"ctx_state": "stale", "context_usage": 0.42,
                    "context_tokens": 297329, "max_context_tokens": 1000000},
                   caller="self_test") is None)
    check("stale 只留 tokens/cap 也不可判",
          usage_of({"ctx_state": "stale", "context_tokens": 297329,
                    "max_context_tokens": 1000000}, caller="self_test") is None)
    check("absent 不可判",
          usage_of({"ctx_state": "absent", "context_tokens": 5,
                    "max_context_tokens": 10}, caller="self_test") is None)
    check("无 ctx_state 按 absent",
          usage_of({"context_usage": 0.63}, caller="self_test") is None)
    check("live 正常判线",
          usage_of({"ctx_state": "live", "context_tokens": 297329,
                    "max_context_tokens": 1000000}, caller="self_test") == 0.297329)

    # The gate must call the injected provider, not read a state endpoint itself.
    gate_src = inspect.getsource(gate)
    check("门禁复用注入的 status_provider（不自己读 /state）",
          "cfg.status_provider(sid)" in gate_src)
    check("门禁不直接打 /state 接口", "/state/" not in gate_src)

    check("阈值 60/90 未被改大", THRESHOLD_REFUSE == 0.60 and THRESHOLD_FORCE_HARD == 0.90,
          f"{THRESHOLD_REFUSE}/{THRESHOLD_FORCE_HARD}")

    print(f"--self-test: {total - len(fails)}/{total} 通过")
    for f_ in fails:
        print(f"  FAIL  {f_}")
    return 0 if not fails else 1


# ------------------------------------------------------------------ 自注入
# The CLI entry fills the three injections from the private agent config; the
# library API (`gate`, `verdict`, `usage_of`) stays injection-only and never
# reads a file on its own. Splitting it this way is the point: a caller that
# imports this module still gets the refusal, while `python -m
# super_agent.ctx_gate` is usable without a wrapper script (#104).
GATE_SESSIONS_KEY = "ctx_gate.sessions_file"
GATE_LEDGER_KEY = "ctx_gate.ledger_file"


def _status_provider():
    """The production status source: bus first, direct fallback.

    Imported lazily inside the callable: `bus` pulls in `wss_bus`, which pulls
    in aiohttp. A module-level import would make `import super_agent.ctx_gate`
    drag that whole chain in, and `dispatch_cli` deliberately runs this gate as
    a subprocess to keep its own import graph small.
    """
    def provider(sid: str):
        from . import bus
        return bus.status(sid, caller="ctx_gate")
    return provider


def _declared_paths() -> tuple[str, str]:
    """`(sessions, ledger)` from the private config; "" when not declared.

    Three layers, most specific first: an explicit `ctx_gate.*` key (so a
    deployment can point the gate at a workbench whose state root differs from
    the engine's), then the one resolver that owns session state,
    `sess_events.sessions_path()`, but **only when a private config exists**.

    That last condition is the safety default, not a formality. `sess_events`
    has a public derived fallback (`data/`), so resolving it unconditionally
    would let a fresh clone -- no `agent.json`, nobody's deployment -- reach a
    live server and log its decisions into the checkout. Verified: without the
    check, an absent config still answered `PASS … ≈22.3%`.
    """
    from . import agent_config

    if not agent_config.config_path().is_file():
        return "", ""
    sessions = str(agent_config.get(GATE_SESSIONS_KEY) or "").strip()
    ledger = str(agent_config.get(GATE_LEDGER_KEY) or "").strip()
    if sessions and ledger:
        return sessions, ledger
    try:
        from . import sess_events
        sessions = sessions or str(sess_events.sessions_path())
        ledger = ledger or str(sess_events.ledger_path())
    except Exception:  # noqa: BLE001  未配置不是错误，保持空、保持拒绝
        pass
    return sessions, ledger


def auto_configure() -> list[str]:
    """Fill the injections from private config; return what is still missing.

    **Only fills gaps.** Anything the caller already passed to `configure()` --
    a test harness, an embedded caller -- is kept as-is; this function never
    replaces a value somebody else supplied. That is what lets `main()` be both
    a library entry and a CLI entry without the two overwriting each other
    (the first version did, and the CLI mapping tests caught it).

    Returns the missing names **without raising**: an empty list means the gate
    can run, a non-empty one means the caller gets the same `EXIT_PARAM`
    refusal it got before this existed. That is the safety default: nothing is
    invented, and an unconfigured checkout still refuses instead of quietly
    reaching a production server.
    """
    sessions, ledger = _declared_paths()
    configure(
        status_provider=(DEFAULT_STATUS_PROVIDER
                         if DEFAULT_STATUS_PROVIDER is not None
                         else _status_provider()),
        sessions_path=(DEFAULT_SESSIONS_PATH if DEFAULT_SESSIONS_PATH is not None
                       else (sessions or None)),
        ledger_path=(DEFAULT_LEDGER_PATH if DEFAULT_LEDGER_PATH is not None
                     else (ledger or None)),
    )
    return missing_injections()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="派单前 ctx 门禁：≥60% 拒派、≥75% 强制交接、≥90% 连 --force 也拒")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--id", dest="sid", help="目标会话 sid")
    g.add_argument("--name", dest="name", help="sessions 表里的键名")
    ap.add_argument("--force", action="store_true", help="显式越过门禁（≥90% 仍拒）")
    ap.add_argument("--operator", default=os.environ.get("AGENT_ROLE", ""),
                    help="谁在用 --force（记进 ledger）")
    ap.add_argument("--role", default=os.environ.get("AGENT_ROLE", "pm"), help="记录用的角色")
    ap.add_argument("--json", action="store_true", help="只输出一行 JSON（供脚本调）")
    ap.add_argument("--self-test", dest="self_test", action="store_true")
    ap.add_argument("--no-auto-config", dest="no_auto", action="store_true",
                    help="不读私有配置（自测/对照用：保持未注入即拒绝）")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()

    # 自注入只在 CLI 入口做，且**只补空缺**：调用方已 `configure()` 过的注入
    # 一律保留。库用法（测试、嵌入式调用）与命令行用法因此不会互相踩。
    # `--ctx-force` 不参与这一步：注入成功与否与 force 无关，force 只影响阈值
    # 判定（≥90% 硬顶仍拒），所以这里不必也不该为它开任何后门。
    if not args.no_auto and missing_injections():
        missing = auto_configure()
        if missing:
            print(f"[ctx-gate] 配置错误：未注入 {', '.join(missing)}："
                  f"拒绝静默连生产，请由调用方显式注入"
                  f"（或在 agent.json 配 {GATE_SESSIONS_KEY} / {GATE_LEDGER_KEY}）",
                  file=sys.stderr)
            return EXIT_PARAM

    sid = (args.sid or "").strip()
    if not sid and args.name:
        sid = _resolve_name(args.name.strip())
        if not sid:
            print(f"[ctx-gate] 未记账：{args.name}", file=sys.stderr)
            return EXIT_PARAM
    if not sid:
        ap.error("必须给 --id 或 --name")

    v, code, msg = gate(sid, force=args.force, operator=args.operator, role=args.role,
                        quiet=args.json)
    if args.json:
        print(json.dumps({"sid": sid, "verdict": v, "code": code, "msg": msg,
                          "kind": session_kind(sid)[0], "forced": bool(args.force)},
                         ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
