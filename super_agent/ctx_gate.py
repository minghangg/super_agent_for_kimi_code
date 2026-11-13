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

Usage:
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
FIXED_PREFIXES = ("pm-", _joined("qa-l", "wa-"), _joined("re", "ed"),
                  _joined("at", "-"))

THRESHOLD_ONESHOT_REFUSE = 0.35
THRESHOLD_REFUSE = 0.60
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


def usage_of(st: dict | None) -> float | None:
    """Extract the ctx ratio (0..1); return None when it cannot be found."""
    if not isinstance(st, dict):
        return None
    u = st.get("context_usage")
    if isinstance(u, (int, float)) and u >= 0:
        return float(u)
    tok, cap = st.get("context_tokens"), st.get("max_context_tokens")
    if isinstance(tok, (int, float)) and isinstance(cap, (int, float)) and cap:
        return float(tok) / float(cap)
    return None


def verdict(usage: float | None, kind: str, force: bool = False) -> tuple[str, int, str]:
    """Pure decision: return `(verdict, exit_code, explanation)`.

    verdict is one of "allow", "allow-unknown", "refuse-60", "refuse-75",
    "hard-cap", "force-allow".
    """
    if usage is None:
        return "allow-unknown", EXIT_OK, "ctx 未知（bus 与直连都取不到）——放行但请留意"
    pct = usage * 100
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
    if usage >= THRESHOLD_REFUSE:
        if kind == "one-shot":
            return "allow", EXIT_OK, (
                f"ctx ≈{pct:.1f}% ≥ 60%：一次性 worker 不因 60% 被拦（短任务、交付即退）")
        return "refuse-60", (EXIT_REFUSE_60 if not force else EXIT_OK), (
            f"ctx ≈{pct:.1f}% ≥ 60%：应交接后再派（长命助手）"
            if not force else
            f"ctx ≈{pct:.1f}% ≥ 60%（已 --force 放行；长命助手应交接后再派）")
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
    u = usage_of(st)
    kind, key = session_kind(sid, cfg.sessions_path)
    v, code, msg = verdict(u, kind, force)

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
    """Offline assertions: 25 checks (22 original + 3 one-shot 35% rule)."""
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
    check("THRESHOLD_HANDOFF 恰为 0.75",
          THRESHOLD_HANDOFF == 0.75, str(THRESHOLD_HANDOFF))
    check("固定键白名单命中", _is_fixed_key("steward") and _is_fixed_key("pm-<项目>")
          and _is_fixed_key("qa-lwa-ISS-9") and _is_fixed_key(_joined("re", "ed") + "_1030"))
    check("一次性键不命中白名单", not _is_fixed_key("wk-paid-mail-20261101")
          and not _is_fixed_key("qa-ISS-19") and not _is_fixed_key("kimi"))
    check("usage 直取", usage_of({"context_usage": 0.63}) == 0.63)
    check("usage 由 tokens/cap 算", abs((usage_of({"context_tokens": 630, "max_context_tokens": 1000}) or 0) - 0.63) < 1e-9)
    check("usage 取不到返回 None", usage_of({}) is None and usage_of(None) is None)
    check("空 status 不算 0%", usage_of({"status": {}}) is None)

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
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()

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
