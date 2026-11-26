"""Component registration for the launcher: which legs, and when not to start one.

Split out of ``launcher.py`` (ISS-132, ``worker.md`` §3.1): the host is lock,
lifecycle and state, and the ticket added four legs to it. Keeping the four
`register_if_possible` functions there pushed the host past 300 lines, and their
rule is the same for all four - **a leg that cannot start is registered
``disabled`` with the reason in its health row**, never left out. A missing row
reads as "this launcher does not have that leg"; a disabled row says why.

``runner_mod`` is passed in rather than imported so this module has no import of
``runner`` at all: the constant (THREAD/SERVICE) and the type are the host's
business, and tests can pass a fake runner.
"""
from __future__ import annotations

import asyncio


async def _null_body() -> None:
    """Placeholder for a disabled thread component: never scheduled."""
    return None


async def _null_service(stop_event) -> None:
    """Placeholder for a disabled service component: waits for the stop signal.

    It must not return immediately: a service that returns is a service that
    crashed, and the health row would then read "started, then stopped" for a
    component that is off on purpose.
    """
    await stop_event.wait()


def register_c2(cfg, store, rn, *, runner_mod, bus, interval, budget, enable) -> None:
    """The decide_watch consumer leg, or a disabled row naming what is missing."""
    from . import components as parts

    if enable is False:
        row = rn.register("decide_watch", _null_body, kind=runner_mod.THREAD,
                          interval=interval, budget=budget, enabled=False)
        row.health.last_error = "disabled"
        return
    from .. import decide_watch as dw

    config, _waits, _port = parts.decide_watch_config(cfg, store, bus=bus)
    missing = dw.missing_injections(config)
    if missing:
        row = rn.register("decide_watch", _null_body, kind=runner_mod.THREAD,
                          interval=interval, budget=budget, enabled=False)
        row.health.last_error = f"未注入 {', '.join(missing)}"
        return
    rn.register("decide_watch", lambda: parts.decide_watch_tick(cfg, store, bus=bus),
                kind=runner_mod.THREAD, interval=interval, budget=budget)


def register_at(cfg, store, rn, *, runner_mod, budget, enable, transport) -> None:
    from . import components as parts

    if enable is False:
        row = rn.register("at", _null_service, kind=runner_mod.SERVICE,
                          budget=budget, enabled=False)
        row.health.last_error = "disabled"
        return
    service, reason = parts.build_at_service(cfg, store, transport=transport)
    if service is None:
        row = rn.register("at", _null_service, kind=runner_mod.SERVICE,
                          budget=budget, enabled=False)
        row.health.last_error = reason
        return
    rn.register("at", service.body, kind=runner_mod.SERVICE, budget=budget)


def register_reporter(cfg, store, rn, *, runner_mod, interval, budget, enable) -> None:
    from . import components as parts

    if enable is False:
        row = rn.register("reporter", _null_body, kind=runner_mod.THREAD,
                          interval=interval, budget=budget, enabled=False)
        row.health.last_error = "disabled"
        return
    rn.register("reporter", lambda: parts.reporter_refresh(cfg, store),
                kind=runner_mod.THREAD, interval=interval, budget=budget)


def register_readapi(cfg, store, rn, *, runner_mod, budget, enable,
                     get_health, keep) -> None:
    """The read-only API: a service that binds before it reports ready."""
    from . import readapi

    api_cfg = cfg.get("readapi") or {}
    enabled_cfg = str(api_cfg.get("enabled") or "on").lower() not in (
        "off", "none", "0", "false")
    if enable is False or not enabled_cfg:
        row = rn.register("readapi", _null_service, kind=runner_mod.SERVICE,
                          budget=budget, enabled=False)
        row.health.last_error = "disabled"
        return
    host = str(api_cfg.get("host") or "127.0.0.1")
    try:
        port = int(api_cfg.get("port") or 58732)
    except (TypeError, ValueError):
        port = 58732

    async def readapi_body(stop_event) -> None:
        from . import backends

        sessions = backends.KimiSessionBackend.for_root(store.root)
        server, _thread = await asyncio.to_thread(
            readapi.serve, host=host, port=port,
            snapshot=get_health, sessions=sessions.list)
        if keep is not None:
            keep["readapi"] = server
        try:
            await stop_event.wait()
        finally:
            await asyncio.to_thread(server.shutdown)
            await asyncio.to_thread(server.server_close)

    rn.register("readapi", readapi_body, kind=runner_mod.SERVICE, budget=budget)
