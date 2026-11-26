"""One registered component and its health row (ISS-131).

Why separate from ``runner``: this half is data plus two run shapes and touches
no loop, no event and no task. ``runner`` owns the scheduling - start,
single-flight, budgeted stop - and that is the half that needs the loop.

The two run shapes are the whole point of the split:

* :meth:`Component.run_once` puts a **synchronous** body on ``asyncio.to_thread``.
  Never call a body directly from the loop: B and C are synchronous to the
  bottom (HTTP, archive, read-back), so an inline call freezes every other
  component for as long as the network takes.
* :meth:`Component.serve` awaits a **service** body and keeps its health row
  truthful while it runs. A service holds the loop for its whole lifetime, so
  ``run_once`` never sees it - which is how its row used to read
  ``started=false, runs=0`` while the WSS was up and serving (ISS-131 QA P1-3).
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

SERVICE = "service"
THREAD = "thread"


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class ComponentHealth:
    """One component's row in the aggregate. Never the component's own health.

    A component that writes business health (the engine's ``health.json``, the
    WSS's ``/healthz``) keeps doing so; this row only answers "did the host
    start it, when did it last succeed, is it stuck".
    """

    name: str
    kind: str
    budget: float
    enabled: bool = True
    running: bool = False
    started: bool = False
    last_started_at: str = ""
    last_success_at: str = ""
    last_error: str = ""
    overrun: int = 0
    stalled: bool = False
    runs: int = 0

    def as_dict(self) -> dict:
        return {"enabled": bool(self.enabled), "running": bool(self.running),
                "kind": self.kind, "budget": float(self.budget),
                "started": bool(self.started),
                "last_started_at": self.last_started_at,
                "last_success_at": self.last_success_at,
                "last_error": self.last_error, "overrun": int(self.overrun),
                "stalled": bool(self.stalled), "runs": int(self.runs)}


class Component:
    """One registered unit of work.

    ``kind="service"``: ``body(stop_event)`` is awaited once and cancelled on
    stop - the WSS shape. ``kind="thread"``: ``body()`` is synchronous and is
    re-run every ``interval`` seconds inside ``to_thread`` - the engine shape.
    """

    def __init__(self, name: str, body, *, kind: str = THREAD,
                 interval: float = 60.0, budget: float = 30.0,
                 enabled: bool = True) -> None:
        if kind not in (SERVICE, THREAD):
            raise ValueError(f"component kind must be {SERVICE!r} or {THREAD!r}")
        self.name = str(name)
        self.body = body
        self.interval = max(0.0, float(interval))
        self.health = ComponentHealth(name=self.name, kind=kind, budget=float(budget),
                                      enabled=bool(enabled))
        self._lock = asyncio.Lock()

    @property
    def kind(self) -> str:
        return self.health.kind

    @property
    def budget(self) -> float:
        return float(self.health.budget)

    @property
    def enabled(self) -> bool:
        return self.health.enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self.health.enabled = bool(value)

    async def run_once(self) -> dict:
        """One run in a worker thread. Never called from the loop directly."""
        h = self.health
        self._begin_run()
        try:
            await asyncio.to_thread(self.body)
        except Exception as exc:  # noqa: BLE001  one bad run is a health row
            self._fail_run(exc)
            return {"component": self.name, "ran": True, "ok": False,
                    "error": h.last_error}
        finally:
            h.running = False
        h.last_success_at = now()
        h.last_error = ""
        return {"component": self.name, "ran": True, "ok": True}

    async def serve(self, stop_event: asyncio.Event) -> None:
        """Run a SERVICE body and keep its health row truthful while it runs.

        A service owns the loop for its whole lifetime, so ``run_once`` never
        sees it - which is how its row used to read ``started=false, runs=0``
        while the WSS was up and serving. The row is written **around** the
        body: ``running=true`` from entry, ``last_success_at`` when it returns
        normally, ``last_error`` when it raises. A crash therefore shows up in
        the health line at the moment it happens, not only after ``stop()``.
        """
        h = self.health
        self._begin_run()
        try:
            await self.body(stop_event)
        except asyncio.CancelledError:
            h.running = False
            raise
        except Exception as exc:  # noqa: BLE001  a crashed service is a health row
            self._fail_run(exc)
            raise
        else:
            h.running = False
            h.last_success_at = now()
            h.last_error = ""

    def _begin_run(self) -> None:
        self.health.running = True
        self.health.started = True
        self.health.last_started_at = now()
        self.health.runs += 1

    def _fail_run(self, exc: BaseException) -> None:
        self.health.running = False
        self.health.last_error = f"{type(exc).__name__}: {exc}"
