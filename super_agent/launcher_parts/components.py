"""The consumer legs of the launcher as runner components (ISS-132 C2).

Why they are components and not loops of their own: two of them use the network
and the session console synchronously to the bottom, so they must run on a thread
(``runner.Component`` does that), and all three must stop within the launcher's
budget. A second loop would mean a second stop protocol, and the ticket's first
counter-example is exactly "one leg blocks and everything else stops answering".

What each leg is:

**``decide_watch``** - one consumption round: ``/events`` long poll, the PM/SR
notice leg, the ledger retry and the liveness patrol. It is *one* component on
purpose: the three legs share one cursor and one notice ledger, so running them
as two components would advance the cursor from two threads. The AT and Reporter
components stay independent of it, which is what makes "C2 blocks 5 s, AT still
answers" measurable rather than asserted.

**``at``** - the questionnaire service. Its storage and send path already exist
(``at_serve``); this wraps the bind/serve/close lifecycle so a port is released
on stop and the component row carries the real error when the bind fails.

**``reporter``** - the resident page: collect + render + write, on its own
interval. The page itself is untouched (the ticket says 不重做页面); what is new
is that it refreshes on a thread instead of waiting for someone to run the CLI.
Its heartbeat is written on every refresh, which is how ``/healthz`` sees it.

Nothing here dispatches, notifies or writes a verdict. The legs deliver their own
messages through the modules that already own those rules.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from . import seams

#: Loopback only. Same value as ``at_serve.HOST``; kept here so the launcher does
#: not import the service module just to learn an address.
HOST = "127.0.0.1"

#: The Reporter page file name under the state root. A file, not a table: the
#: page is a document, and a document that lives in JSON is one nobody can open.
REPORTER_PAGE_NAME = "reporter.html"

#: Default long-poll wait, matching ``decide_watch.main``'s ``--wait``.
DEFAULT_WAIT_S = 20.0


# ------------------------------------------------------------------ decide_watch
def decide_watch_config(cfg: dict, store, *, bus=None, port: int = 0,
                        dry: bool = False, wait: float | None = None):
    """The complete ``DecideWatchConfig`` for this launcher. No guesswork.

    Every injectable is supplied, because ``decide_watch.missing_injections()``
    refuses to start on a partial set and a launcher that half-wires the leg would
    register a component that can only fail - a permanent red row in every health
    summary. Returning the config (rather than configuring as a side effect) keeps
    this function testable without touching module state.
    """
    from .. import decide_watch as dw

    if bus is None:
        from .. import wss_bus as bus

    state = Path(store.root)
    workspace = seams.workspace_root(cfg)
    engine_cfg = cfg.get("engine") or {}
    resolved_port = int(port or getattr(bus, "PORT", 58800))
    waits = float(wait if wait is not None
                  else (engine_cfg.get("decide_watch_wait_s") or DEFAULT_WAIT_S))

    def inject(path: str, body: dict, port_=0):
        return bus.bus_post(path, body, port=port_ or resolved_port)

    return dw.DecideWatchConfig(
        state_root=state,
        workspace_root=workspace,
        event_source=bus,
        heartbeat=seams.heartbeat(state),
        inject_event=inject,
        ps_json=seams.ps_json,
        windowless_python=seams.windowless_python,
        probe_session=seams.probe_session,
        send_transport=seams.send_transport,
        session_script=workspace / "tools" / "session_client.py",
        liveness_state_path=state / "worker_liveness_state.json",
        lock_path=state / "decide_watch.lock",
    ), waits, resolved_port


def decide_watch_tick(cfg: dict, store, *, bus=None, port: int = 0,
                      dry: bool = False, wait: float | None = None) -> dict:
    """One consumption round as a **synchronous** callable (a thread body).

    The order is ``decide_watch._loop``'s order, and it matters: the delivery leg
    runs first (a stuck send must not stop the notice leg from being reached this
    round), the notice leg and its ledger retry share one ``attempted`` set (so a
    round counts as one round, not two), and the cursor is written only after
    every leg has run - writing it earlier would drop the events a failing leg
    never saw.

    ``dry=True`` still consumes, exactly as ``--dry-run`` does.
    """
    from .. import decide_watch as dw

    config, waits, resolved_port = decide_watch_config(
        cfg, store, bus=bus, port=port, dry=dry, wait=wait)
    dw.configure(config)
    refused = dw.gate_check() or dw.notice_registration_problem()
    if refused:
        raise RuntimeError(f"decide_watch refused to run: {refused}")

    dw.delivery_tick(dry=dry)
    cursor = dw.load_cursor()
    since = int(cursor.get("seq") or 0)
    boot = ""
    health = dw.health(resolved_port)
    if health:
        boot = str(health.get("boot_id") or "")
        if since <= 0:
            # First round never replays the ring: push the cursor to its end.
            since = int(health.get("seq") or 0)
            dw.save_cursor(since, boot)
        elif cursor.get("boot_id") and boot and cursor["boot_id"] != boot:
            since = 0

    data = dw.fetch_events(since, waits, resolved_port)
    if data is None:
        raise RuntimeError("decide_watch: daemon unreachable")
    if dw.notice_cursor_rollback(since, int(data.get("seq") or 0)) is not None:
        since, data = 0, {"events": []}

    events = data.get("events") or []
    dw.handle(events, dw.supervisor_sid(""), dry, dw.MAX_AGE_S)
    attempted: set = set()
    dw.handle_supervisor_notices(events, dry=dry, port=resolved_port,
                                 attempted=attempted)
    dw._retry_pending(dry=dry, port=resolved_port, attempted=attempted)
    dw.worker_liveness_tick(dry=dry, port=resolved_port)
    if events:
        since = max(int(e.get("seq") or since) for e in events)
    dw.save_cursor(since, str(data.get("boot_id") or boot))
    return {"events": len(events), "since": since}

# The AT service and the Reporter refresh live in their own modules; the names
# are re-exported here because ``host`` and the tests address ``components.at``.
from .at_service import (AtService, build_at_service,  # noqa: E402,F401
                         _deployment_resolver)
from .reporter_leg import (REPORTER_PAGE_NAME, render_reporter_page,  # noqa: E402,F401
                           reporter_refresh)
