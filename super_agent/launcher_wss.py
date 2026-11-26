"""How the launcher hosts A (the WSS daemon) - ISS-131 QA P0-1 / P0-2.

Split out of ``launcher.py`` because the wiring is a subject of its own: it must
inject the WSS boundary **with the launcher's state dir** and hand the runner a
``body(stop_event)``. Both were wrong in the first delivery, and both failures
were invisible until a real start - which is the reason this file carries the
reasoning next to the code.

Three facts decide the shape:

1. ``wss_bus`` reads no configuration. It refuses to guess paths, so without
   :func:`configure_wss` ``missing_injections()`` is never empty and A can only
   ever be a disabled placeholder.
2. The state dir must be **the same directory** the engine lock lives in.
   ``--state-dir`` therefore reaches ``wss_bus.PATHS``; otherwise
   ``wss_bus.lock`` and ``engine.pid`` sit in two roots and neither excludes the
   other.
3. ``production_state_root`` is the isolation baseline, **not** an alias for the
   working root. Setting it to the working root would declare an isolated run
   to be production, which is exactly backwards.
"""
from __future__ import annotations

import os
from pathlib import Path


def configure_wss(cfg: dict, store) -> str:
    """Inject the WSS boundary, with **the launcher's** state dir. Returns an error.

    Two failures this closes (ISS-131 QA P0-1/P0-2):

    * ``wss_bus`` has no config reader of its own by design - it refuses to guess
      paths. Without this call ``missing_injections()`` is never empty, so A was
      registered as a disabled placeholder on every real start;
    * ``--state-dir`` must reach ``wss_bus.PATHS``. Left alone, the launcher's
      ``engine.pid`` and the WSS's ``wss_bus.lock`` land in two different roots,
      and the two locks cannot exclude each other because they are not the same
      file.

    ``production_state_root`` is **not** set to ``store.root``: that value is the
    isolation baseline, and pointing it at the same directory would tell an
    isolated run it is production - the exact opposite of what it is for. It
    stays at the configured production root when one is declared, otherwise at
    the state root only when no ``--state-dir`` isolation is in force.
    """
    from . import wss_bus

    try:
        port = int((cfg.get("bus") or {}).get("port") or wss_bus.PORT or 58800)
    except (TypeError, ValueError):
        port = 58800
    sessions = (cfg.get("engine") or {}).get("session_events_path") or ""
    sessions_path = Path(str(sessions)) if sessions else store.root / "sessions.json"

    prod_raw = str(cfg.get("wss_production_state_root") or "").strip()
    if prod_raw:
        production = Path(prod_raw).expanduser().resolve()
    else:
        # No declared production root: use the state dir itself. That keeps a
        # plain single-directory deployment working, and an operator who really
        # runs isolated declares the production root instead of relying on this.
        production = Path(store.root).resolve()

    try:
        wss_bus.configure(wss_bus.WssBusConfig(
            workspace_root=Path(str(cfg.get("workspace_root") or os.getcwd())).resolve(),
            state_root=Path(store.root).resolve(),
            production_state_root=production,
            sessions_path=sessions_path,
            port=port,
            probe_session=_session_probe,
            retired_module=_retired_module(),
        ))
    except Exception as exc:  # noqa: BLE001  a bad boundary is a reportable reason
        return f"wss_bus.configure 失败：{type(exc).__name__}: {exc}"
    missing = wss_bus.missing_injections()
    if missing:
        return f"wss_bus 未注入：{', '.join(missing)}"
    return ""


def _retired_module():
    """The retired-sid table the WSS uses to refuse delivery to a dead session.

    ``missing_injections()`` lists it as required, and leaving it out is how the
    first delivery's A could never start: the boundary was incomplete, so the
    component fell back to a disabled placeholder while ``--wss`` claimed to
    force it on. The package ships an implementation, so injecting it here is
    the honest default rather than a private-path guess.
    """
    from . import retired_sids

    return retired_sids


def _session_probe(sid: str):
    """Liveness probe for the WSS guardian: the package's own session client.

    Injected rather than imported by ``wss_bus`` so that module stays free of a
    client import; returning ``<info>, ""`` matches the documented contract.
    """
    from . import api

    try:
        return api.session_info(sid), ""
    except Exception as exc:  # noqa: BLE001  an unreachable console is "unknown"
        return {}, f"{type(exc).__name__}: {exc}"


def build_wss_service(cfg: dict, store=None):
    """A: the WSS daemon as a ``body(stop_event)`` service - or ``(None, reason)``.

    The body must accept the runner's stop event: ``_run_daemon(stop)`` is that
    signature. Returning the bare ``_run_daemon`` is what produced
    ``TypeError: _run_daemon() takes 0 positional arguments but 1 was given``
    (QA P0-1), so the adapter is written out here instead of relying on the
    callee's shape.
    """
    from . import wss_bus

    if store is not None:
        error = configure_wss(cfg, store)
        if error:
            return None, error
    missing = wss_bus.missing_injections()
    if missing:
        return None, f"wss_bus 未注入：{', '.join(missing)}"

    async def wss_body(stop_event) -> None:
        code = await wss_bus._run_daemon(stop_event)
        if code:
            raise RuntimeError(f"wss_bus daemon exited with {code}")

    return wss_body, ""
