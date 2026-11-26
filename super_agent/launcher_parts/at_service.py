"""The AT questionnaire service as a launcher component (ISS-132 D).

Why this is its own module: the lifecycle is the whole content - bind, serve on a
daemon thread, then shut the port down and remove the pid file - and it shares
nothing with the consumer legs. It is also the leg whose failure is a **port**,
which is the one thing a reader needs to find quickly when a launcher will not
start.

Two rules travel with it:

**Bind before reporting ready.** ``serve`` binds on the caller's thread, so a
port already held raises where the component row can carry the reason instead of
disappearing inside a daemon thread.

**No transport by default.** The real console client is wired only when
``engine.at_send`` says so, the same rule as ``at_serve --allow-send``: an answer
leaving the machine is a decision, not a default.
"""
from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass
from pathlib import Path



# --------------------------------------------------------------------------- AT
@dataclass
class AtService:
    """The questionnaire server as a ``body(stop_event)`` service.

    Lifecycle: bind on a worker thread (so a port conflict is raised where the
    component can report it), serve on a daemon thread, then ``shutdown()`` +
    ``server_close()`` on stop. The pid file ``at_serve.serve`` writes is removed
    on the way out, because a stale one makes the next start look like a second
    instance - which is the one failure a link service must not produce.

    ``transport`` is injected and **None means no send is wired**: the real
    console client is opt-in through ``engine.at_send``, the same rule as
    ``at_serve --allow-send``. An answer leaving the machine is a decision, not a
    default.
    """

    root: Path
    port: int
    transport: object | None = None
    requirement_resolver: object | None = None
    binding_resolver: object | None = None
    _server: object | None = None
    _thread: threading.Thread | None = None

    def bind(self) -> None:
        from .. import at_serve

        self._server = at_serve.serve(
            root=self.root, port=self.port, transport=self.transport,
            requirement_resolver=self.requirement_resolver,
            binding_resolver=self.binding_resolver)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="at-serve", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5.0)
        try:
            (Path(self.root) / "atserve" / "atserve.pid").unlink()
        except OSError:
            pass

    async def body(self, stop_event) -> None:
        await asyncio.to_thread(self.bind)
        try:
            await stop_event.wait()
        finally:
            await asyncio.to_thread(self.shutdown)


def build_at_service(cfg: dict, store, *, transport=None):
    """The AT service, or ``(None, reason)`` when the config turns it off."""
    from .. import at_serve

    engine_cfg = cfg.get("engine") or {}
    try:
        port = int(engine_cfg.get("at_question_port") or at_serve.DEFAULT_PORT)
    except (TypeError, ValueError):
        port = at_serve.DEFAULT_PORT
    if str(engine_cfg.get("at_service") or "on").lower() in ("off", "none", "0", "false"):
        return None, "at_service=off"
    if transport is None and str(engine_cfg.get("at_send") or "").lower() in (
            "1", "true", "yes", "on"):
        transport = at_serve._real_transport
    service = AtService(root=Path(store.root), port=port, transport=transport,
                        requirement_resolver=_deployment_resolver(store, "requirement"),
                        binding_resolver=_deployment_resolver(store, "binding"))
    return service, ""


def _deployment_resolver(store, which: str):
    """The same live lookups ``at_serve.run`` wires, bound to **this** state root.

    ``at_serve``'s own :func:`_deployment_resolvers` resolves both maps through
    configuration, which is right for a standalone service and wrong for a
    launcher started with ``--state-dir``: an isolated run would look up the AT
    binding in the production map and answer "no current AT session" for a
    question it just filed. Both lookups are therefore read from ``store.root``.
    """
    root = Path(store.root)
    if which == "requirement":
        def requirement(requirement_id):
            from .. import requirements

            return requirements.current(requirements.load_projection(root), requirement_id)
        return requirement

    def binding(record):
        from .. import session_map_store

        path = root / "issue_sessions.json"
        try:
            database = json.loads(session_map_store.read_text_shared(path))
        except (OSError, ValueError):
            database = {}
        if not isinstance(database, dict):
            database = {}
        for key in record.get("binding_keys") or [record.get("thread")]:
            entry = database.get(str(key)) or {}
            at = entry.get("at") if isinstance(entry, dict) else {}
            if isinstance(at, dict) and at.get("session"):
                return at
        return {}
    return binding


