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


def register_c2(cfg, store, rn, *, runner_mod, bus, interval, budget, enable,
                supervisor=None) -> None:
    """The decide_watch consumer leg, or a disabled row naming what is missing.

    A **single** :class:`launcher_parts.c2.C2Leg` is registered, not a fresh
    closure: the backoff and the liveness clock live on the instance and only mean
    anything across rounds. Building a new one per call - which the previous
    ``lambda`` did - reset both every round, which is how the liveness interval
    gate and the backoff stopped existing without anything going red.
    """
    from . import components as parts
    from .c2 import C2Leg

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
    leg = C2Leg(cfg, store, bus=bus, supervisor=supervisor)
    rn.register("decide_watch", leg, kind=runner_mod.THREAD,
                interval=interval, budget=budget)


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
        issues = _issue_reader(cfg, store)
        server, _thread = await asyncio.to_thread(
            readapi.serve, host=host, port=port,
            snapshot=get_health, sessions=sessions.list, issues=issues)
        if keep is not None:
            keep["readapi"] = server
        try:
            await stop_event.wait()
        finally:
            await asyncio.to_thread(server.shutdown)
            await asyncio.to_thread(server.server_close)

    rn.register("readapi", readapi_body, kind=runner_mod.SERVICE, budget=budget)


def _issue_reader(cfg, store):
    """``(refs, reason)`` for ``GET /issues`` - the ``IssueBackend`` production caller.

    The keys come from ``index.json``, which is the only place this repository
    already stores an issue identity, and each one is turned into an
    :class:`~super_agent.launcher_parts.backends.IssueRef` by the backend. That is
    the whole point of routing through the backend instead of echoing the key: the
    provider is attached **beside** the canonical ``owner/repo#N`` here, so a
    second provider could never change a key that every cursor is keyed on.

    A missing or unreadable index returns a **reason**, not ``[]``: an empty list
    reads as "no issues", which is the same silent-zero this repository refuses
    everywhere else.
    """
    from . import backends

    try:
        index = store.load_index()
    except Exception as exc:                      # noqa: BLE001  a read failure is a field
        return [], f"index.json 读取失败：{type(exc).__name__}: {exc}"
    repo = str((cfg.get("github") or {}).get("repo") or "")
    backend = backends.issue_backend("github")
    refs = []
    for key in (index.get("issues") or {}):
        name, number = str(key).rsplit("#", 1) if "#" in str(key) else ("", str(key))
        refs.append(backend.ref(name or repo, number))
    if not refs:
        return [], "index.json 里没有 issue"
    # Sorted **after** normalisation, not before: the raw keys sort by case
    # (``Owner/...`` before ``other/...``), so sorting the source dict would put a
    # canonical ``owner/...`` row in the wrong place. The page order is stability
    # for a human, and canonical order is the only one that does not move when a
    # repo name changes case in one file.
    return sorted(refs, key=lambda ref: ref.key), ""
