"""Tell someone the engine stopped because it could not go on.

The fail-loud rule makes the engine stop; this module makes the stop **heard**.
Without it a `fatal` exit is a process that disappears and a `health.json` that
says `running=false` - correct, and invisible to whoever was relying on it.

Delivery rides the leg that already exists: one ``event.agent.supervisor``
injection carrying ``payload.sid`` and the full text. The consumer
(``decide_watch``) delivers the text to that session and keeps its own
at-most-once ledger, so this is one shot, not a retry loop - a stop notice that
retries is a second thing to debug while the first is down.

Never raises, and says what it could not do: a notice that fails silently would
turn "the engine stopped and told you" back into "it stopped".
"""
from __future__ import annotations

from . import fault_report


def text(reason: str, error: str) -> str:
    """The one line a supervisor reads: why it stopped, and what to do."""
    detail = (error or "").strip() or "（无详情，见 health.last_error 与 run.stopped）"
    what = ("致命故障，引擎已停" if reason == "fatal"
            else "连续失败超限，引擎已停")
    return (f"【引擎停机】{what}（reason={reason}）：{detail}\n"
            "health.last_error 与 run.stopped 已记录；处理后重启 launcher。")


def notice(engine, store, *, reason: str, error: str, now) -> dict:
    """Inject one stop notice; return what happened, never raise."""
    out = {"sent": False, "sid": "", "reason": reason, "error": ""}
    try:
        sid = engine._supervisor_sid(store.load_index()) or ""
    except Exception as exc:  # noqa: BLE001  a broken index must not hide the stop
        out["error"] = f"supervisor sid unresolved: {fault_report.one_line(exc)}"
        store.log_event({"type": "run.stop_notice", "at": now(), "ok": False,
                         "reason": reason, "detail": out["error"]})
        return out
    out["sid"] = sid
    if not sid:
        out["error"] = "no supervisor sid"
        store.log_event({"type": "run.stop_notice", "at": now(), "ok": False,
                         "reason": reason, "detail": out["error"]})
        return out
    bus = getattr(engine, "bus", None)
    inject = getattr(bus, "inject", None)
    if not callable(inject):
        out["error"] = "no bus client"
        store.log_event({"type": "run.stop_notice", "at": now(), "ok": False,
                         "reason": reason, "sid": sid, "detail": out["error"]})
        return out
    body = text(reason, error)
    try:
        reply = inject("event.agent.supervisor",
                       {"sid": sid, "text": body, "kind": "engine_stop",
                        "assignment": "", "at": now()})
        ok = not (isinstance(reply, dict) and reply.get("ok") is False)
        out["sent"] = bool(ok)
        if not ok:
            out["error"] = str((reply or {}).get("error") or "bus refused the event")
    except Exception as exc:  # noqa: BLE001  the notice is never the failure
        out["error"] = fault_report.one_line(exc)
    store.log_event({"type": "run.stop_notice", "at": now(), "ok": out["sent"],
                     "reason": reason, "sid": sid, "detail": out["error"]})
    return out
