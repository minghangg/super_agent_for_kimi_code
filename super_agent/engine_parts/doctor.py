"""The ``doctor`` self-checks: one readable label per distinguishable fault.

Why this module exists (ISS-130 §3.1.1 cleanup): four checks answered "is this
install healthy?" inside ``engine.py``, and each of them is a pure decision over
a snapshot plus a few injected readers. They belong together, away from the round
code, because a check that mutates what it measures is not a check - keeping them
here makes that easy to see.

The labels are the contract: ``process-dead``, ``process-stalled``,
``process-version``, ``startup-failed``, ``config-bad``, ``charter-refs-broken``,
``supervisor-silent``. A fault is reported once, with the detail that makes it
actionable.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .. import health


def process(store, *, now=None, process_alive=None, current_head=None,
            parse_ts=None) -> tuple:
    """Fault 1: a dead pid lock, or a health record with no live engine."""
    from ..pidlock import process_alive as _alive
    alive = process_alive or _alive
    # Read the snapshot first: the real sequence for a failed startup is
    # "startup failed -> process exits (rc=1) -> the lock was not cleaned up",
    # so the stale pid lock and the startup-failed snapshot arrive together.
    # Judging the lock first would answer "the next start takes it over" and
    # never mention why the last start gave up.
    snap = health.read(store.health_path)
    startup_failed = (snap.get("phase") == "startup-failed"
                      and not snap.get("running"))
    try:
        info = json.loads(store.pid_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        info = {}
    if info.get("pid"):
        pid = info.get("pid")
        if alive(pid):
            last_loop = (parse_ts or _parse)(snap.get("last_loop_at"))
            age_s = (int(((now or datetime.now(timezone.utc)) - last_loop)
                         .total_seconds()) if last_loop else None)
            if snap.get("running") and (age_s is None or age_s > 300):
                return ("process", False,
                        f"stalled: pid={pid} alive but last_loop_at="
                        f"{snap.get('last_loop_at')} age_s={age_s} threshold_s=300",
                        "fail", "process-stalled")
            head = (current_head or _no_head)()
            running_commit = str(snap.get("code_commit") or "")
            if head and running_commit and running_commit != head:
                return ("process", False,
                        f"stale code: running={running_commit} HEAD={head} pid={pid}; "
                        "restart the engine to activate the checkout",
                        "fail", "process-version")
            return ("process", True,
                    f"engine.pid pid={pid} alive since={info.get('started_at')} "
                    f"last_loop_age_s={age_s} code_commit={running_commit or 'unknown'} "
                    f"HEAD={head or 'unknown'}",
                    "fail", "process-dead")
        if startup_failed:
            return ("process", False,
                    f"startup-failed with a stale engine.pid pid={pid}: "
                    f"last_error={snap.get('last_error') or 'unknown'}; "
                    f"the last start failed during startup and left the "
                    f"lock behind", "fail", "startup-failed")
        return ("process", False,
                f"engine.pid pid={pid} is not alive (stale lock; the next "
                f"start takes it over)", "fail", "process-dead")
    if startup_failed:
        # The process wrote its snapshot, failed during startup and exited
        # (rc=1) before any round ran. It did not crash mid-loop, so the label
        # says so and the detail carries the reason it gave up.
        return ("process", False,
                f"startup-failed: the engine exited while starting up "
                f"(last_error={snap.get('last_error') or 'unknown'}, "
                f"pid={snap.get('pid')}); no round ran, so this is not a "
                f"crash of a running loop", "fail", "startup-failed")
    if snap.get("running"):
        return ("process", False,
                f"crashed: no pid lock but health.json is still running=true "
                f"(last_loop_at={snap.get('last_loop_at')}, pid={snap.get('pid')}); "
                f"the process died without a clean exit", "fail", "process-dead")
    if snap.get("last_loop_at"):
        return ("process", True,
                f"engine stopped cleanly (last_loop_at={snap.get('last_loop_at')}, "
                f"pid={snap.get('pid')})", "warn", "process-dead")
    return ("process", True,
            "engine not running (no pid lock, no health record)", "warn",
            "process-dead")


def config(projects) -> tuple:
    """Fault 4: allowlist rejected; the previous copy is still in force."""
    if projects is None:
        return ("config", True, "no project allowlist configured", "fail",
                "config-bad")
    snap = projects.summary()
    if snap["valid"] and not snap["error"]:
        return ("config", True,
                f"allowlist ok projects={snap['projects']} "
                f"allowlist={projects.source_label()} "
                f"generation={snap['generation']}", "fail", "config-bad")
    if snap["valid"]:
        return ("config", False,
                f"config-bad: {snap['error']} - still using the previous copy "
                f"(last-good cache, generation={snap['generation']})",
                "fail", "config-bad")
    return ("config", False,
            f"config-bad: {snap['error'] or 'no valid copy'}; nothing is allowed",
            "fail", "config-bad")


def charters() -> tuple:
    """Fault 11: a charter that points at a rule file this package lacks.

    ``charter_files`` refuses an **absent** charter, so that shape was already
    visible. A charter that exists but says "see
    ``rules/shared/rules/newtrack_common.md``" while that file was never shipped
    reads as a working install: every check passed, the session was created, and
    the rule it points at could not be opened (ISS-107).

    Not a refusal, a report: the session still runs, and the reader is told which
    charter is missing or unreadable, or which pointer is dead.
    """
    from .. import agent_config as agent_cfg
    from .. import charters as charters_mod

    names = [prefix for prefix, _leaf in agent_cfg.charter_map()]
    broken = charters_mod.broken_charters(names)
    if not broken:
        return ("charters", True,
                f"{len(names)} session prefixes resolve every rule reference",
                "warn", "charter-refs-broken")
    detail = "; ".join(f"{name}: {', '.join(refs)}"
                       for name, refs in sorted(broken.items()))
    return ("charters", False,
            f"charter-refs-broken: {detail}", "warn", "charter-refs-broken")


def supervisor_visibility(cfg: dict, events_path, malformed_row: str,
                          tail) -> tuple:
    """Fault 10: a supervisor sid that no frame can reach.

    The engine resolves ``engine.supervisor_sid`` from config first, so a
    truncated id is *returned* and every frame built from it is refused - each
    refusal is a route event about one frame, so the shape problem itself stayed
    invisible (#74). This check turns that row into a doctor line.

    Three states, and two of them are ``ok``:

    * a malformed row in the tail ⇒ ``ok=false`` with the count, label
      ``supervisor-silent``: every frame is being refused right now;
    * no sid at all ⇒ ``ok=true``: a deployment shape, not a fault. An engine
      with no supervisor never resolves one, so ``warn`` here keeps every such
      deployment off-green for good and buries the real fault in routine noise;
    * a resolvable sid **and** no malformed row ⇒ ``ok=true``.

    Only the config field is read on purpose: resolving the sid the way the route
    path does would let a health check write an event, and a check that mutates
    the thing it measures is not a check.
    """
    sid = str(((cfg.get("engine") or {}).get("supervisor_sid")) or "").strip()
    resolved = bool(sid)
    rows = [r for r in tail(events_path, limit=500) if r.get("type") == malformed_row]
    if rows:
        return ("supervisor-visibility", False,
                f"ok=false {malformed_row}={len(rows)} "
                f"sid_len={rows[-1].get('sid_len')} "
                f"resolved_sid={'yes' if resolved else 'no'} - every frame "
                f"to the supervisor is refused",
                "fail", "supervisor-silent")
    if not resolved:
        return ("supervisor-visibility", True,
                "ok=true no supervisor sid (engine.supervisor_sid empty and "
                "engine.supervisor_keys resolve nothing) - deployment shape, "
                "not a fault", "ok", "supervisor-silent")
    return ("supervisor-visibility", True,
            f"ok=true resolved_sid=yes {malformed_row}=0",
            "ok", "supervisor-silent")


def _parse(value: str):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _no_head() -> str:
    return ""


def bus_check(bus) -> tuple:
    """The bus line: health plus a refusal that names what is missing."""
    if bus is None:
        return ("bus", False, "no bus client configured", "warn", "bus-down")
    health = bus.healthz() or {}
    return ("bus", bool(health.get("ok")),
            f"ok={health.get('ok')} ready={bool(health.get('ready'))} "
            f"connected={bool(health.get('connected'))} "
            f"boot_id={health.get('boot_id') or '-'} "
            f"seq={health.get('seq')} "
            f"{health.get('error') or ''}".strip(), "fail", "bus-down")


def outbox_check(store) -> tuple:
    """``outbox-stuck``: an action past its stale budget that nobody looked at."""
    stuck = store.stuck_outbox()
    if stuck:
        detail = "; ".join(f"{r.get('action_id')} {r.get('status')} "
                           f"age={r.get('age_s')}s" for r in stuck[:5])
        return ("outbox", False,
                f"{len(stuck)} action(s) past {store.outbox_stale_s:g}s: {detail}",
                "fail", "outbox-stuck")
    return ("outbox", True, "no prepared/in_flight/uncertain action is overdue",
            "fail", "outbox-stuck")


def waiting_person_check(store) -> tuple:
    """The second outbox line on its own (kept for callers that split them)."""
    waiting = store.waiting_person()
    return ("waiting-person", not waiting,
            "none" if not waiting else
            f"{len(waiting)} action(s) need a person: " + "; ".join(
                f"{r.get('action_id')} ({r.get('reason') or 'no remote confirmation'})"
                for r in waiting[:5]), "fail", "waiting-person")


def cursor_check(cursor: dict, problem: str, issue_number) -> tuple:
    """Two questions, reported separately: bad values, and a lost cursor.

    A per-issue ``0`` is a fresh issue, not a fault (see
    ``Store.cursor_problem``), so the value check and the emptiness check are
    different questions with different answers.
    """
    bad = [k for k, v in (cursor.get("issues") or {}).items()
           if not isinstance(v, int) or v < 0 or issue_number(k) is None]
    return ("cursor", not (bad or problem),
            problem if problem else ("cursor ok" if not bad
                                     else f"invalid cursors: {bad}"),
            "fail", "cursor")


def task_book_check(rows) -> tuple:
    """The ``[PM:TASK]`` guard writes an event and nothing read it (#ISS-85).

    Bounded on purpose - the tail, not the file. The live log passed 9000 rows
    and grows every round; reading it whole in a health check would turn the
    check itself into the load.
    """
    if not rows:
        return ("pm-task-book", True,
                "no conclusion under [PM:TASK] in the last 500 events",
                "warn", "pm-task-book")
    last = rows[-1]
    return ("pm-task-book", False,
            f"{len(rows)} conclusion(s) written under [PM:TASK] in the last 500 "
            f"events; latest comment_id={last.get('comment_id')} "
            f"at={last.get('at')} - use [PM] + verdict marker instead",
            "warn", "pm-task-book")


def assignments_check(residual) -> tuple:
    """Non-terminal assignments are a warning, not a fault: work in flight."""
    return ("assignments", not residual,
            "none" if not residual else f"non-terminal: {sorted(residual)}",
            "warn", "assignments")
