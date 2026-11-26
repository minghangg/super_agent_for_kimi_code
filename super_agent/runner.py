"""Component host for the single-process launcher (ISS-131 E).

Why this module exists: three kinds of work share one process, and only one of
them may run inside the event loop.

* A service (the WSS) is native ``await``. Its whole job is to stay responsive,
  so waiting on it *in* the loop is correct and costs nothing.
* B (the engine round) and C (the Issue read) are **synchronous** all the way
  down: HTTP, session archive, read-back. Awaiting one in the loop blocks every
  other component for as long as the network takes - 5 s on a slow day, and the
  WSS stops answering meanwhile.

So the two rules this module enforces are:

1. **A thread component is only ever run through** :func:`asyncio.to_thread`.
   The loop is never allowed to execute it.
2. **One flight per component.** A component that is asked to run again while
   its previous run is still in flight is *not* started twice; the attempt is
   counted as ``overrun``. Two overlapping rounds would both write the cursor,
   and the second write would be built on a read the first one is still using.

Stop semantics are the third thing that must not be fudged: ``to_thread`` cannot
be cancelled, so a thread that does not finish inside its budget is **stalled**,
not stopped. Reporting ``stopped`` there would hand the next launcher a state
directory that a live thread is still writing - exactly the split brain the
single-instance lock exists to prevent.
"""
from __future__ import annotations

import asyncio

from .runner_parts.components import SERVICE, THREAD, Component, now


class Runner:
    """Registry plus the one place a component is started, stopped or probed."""

    def __init__(self) -> None:
        self._components: dict[str, Component] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._stop = asyncio.Event()
        self._running = False
        self._phase = "stopped"
        self._stalled: list[str] = []
        self._started_at = ""

    # ---------------------------------------------------------------- registry
    def register(self, name: str, body, *, kind: str = THREAD,
                 interval: float = 60.0, budget: float = 30.0,
                 enabled: bool = True) -> Component:
        """Add a component. A duplicate name is refused, not merged.

        Merging would make ``stop`` wait on whichever body happened to be
        registered last, and the first one would keep writing.
        """
        key = str(name)
        if key in self._components:
            raise ValueError(f"component {key!r} already registered")
        comp = Component(key, body, kind=kind, interval=interval,
                         budget=budget, enabled=enabled)
        self._components[key] = comp
        return comp

    def component(self, name: str) -> Component:
        return self._components[str(name)]

    def names(self) -> list[str]:
        return list(self._components)

    # ------------------------------------------------------------------ running
    async def start(self) -> dict:
        if self._running:
            raise RuntimeError("runner already started")
        self._stop = asyncio.Event()
        self._running = True
        self._phase = "running"
        self._started_at = now()
        for name, comp in self._components.items():
            if not comp.enabled:
                continue
            if comp.kind == SERVICE:
                self._tasks[name] = asyncio.create_task(
                    comp.serve(self._stop), name=name)
            else:
                self._tasks[name] = asyncio.create_task(
                    self._thread_loop(comp), name=name)
        # One turn of the loop so a service gets to bind before start() returns.
        await asyncio.sleep(0)
        return self.health()

    async def _thread_loop(self, comp: Component) -> None:
        while not self._stop.is_set():
            await self.trigger(comp.name)
            if self._stop.is_set():
                break
            await self._sleep_until_stop(comp.interval)

    async def _sleep_until_stop(self, seconds: float, slice_s: float = 0.2) -> None:
        remaining = max(0.0, float(seconds))
        while remaining > 0 and not self._stop.is_set():
            await asyncio.sleep(min(remaining, slice_s))
            remaining -= slice_s

    async def trigger(self, name: str) -> dict:
        """Run one component once, single-flight.

        The lock is what makes ``overrun`` countable: an attempt that finds the
        previous run in flight is recorded and refused. It must never queue -
        a queued second round would run on a cursor the first one is moving.
        """
        comp = self._components[str(name)]
        if comp.kind == SERVICE:
            return {"component": comp.name, "ran": False,
                    "reason": "service components are awaited, not triggered"}
        if comp._lock.locked():
            comp.health.overrun += 1
            return {"component": comp.name, "ran": False, "overrun": True}
        async with comp._lock:
            return await comp.run_once()

    # ------------------------------------------------------------------- stop
    async def stop(self) -> dict:
        """Stop every component within its own budget; never claim a false stop.

        A service is cancelled (it is pure coroutine, cancellation is exact). A
        thread is **not** cancelled - ``to_thread`` has no cancellation - so the
        host waits for the in-flight run to return, and a run that outlives its
        budget marks the component ``stalled``. ``running`` then stays true on
        purpose: the thread may still write, so the state directory is still
        owned.
        """
        if not self._running:
            return self.health()
        self._phase = "stopping"
        self._stop.set()
        stalled: list[str] = []
        for name, comp in self._components.items():
            task = self._tasks.get(name)
            if task is None:
                continue
            if comp.kind == THREAD:
                # A thread component runs ``to_thread``; cancelling the wrapper
                # would mark the task done while the OS thread keeps writing.
                # That is the false "stopped" this issue forbids, so the wrapper
                # is never cancelled - a run that outlives the budget is a stall.
                done, pending = await asyncio.wait({task}, timeout=comp.budget)
                if pending:
                    stalled.append(name)
                    comp.health.stalled = True
                    continue
            else:
                # Grace first, cancel second: a service watches the stop event
                # and runs its own cleanup (flush, release ports), so cancelling
                # on the first turn would skip exactly that.
                done, pending = await asyncio.wait({task}, timeout=comp.budget)
                if pending:
                    task.cancel()
                    done2, pending = await asyncio.wait({task}, timeout=comp.budget)
                    if pending:
                        stalled.append(name)
                        comp.health.stalled = True
                        continue
            exc = None if task.cancelled() else task.exception()
            if exc is not None:
                comp.health.last_error = f"{type(exc).__name__}: {exc}"
        self._stalled = stalled
        if stalled:
            # Deliberately not "stopped": a live thread still owns the state
            # directory, so the caller must keep the lock and report a stall.
            self._phase = "stalled"
        else:
            self._phase = "stopped"
            self._running = False
        return self.health()

    # ----------------------------------------------------------------- health
    def health(self) -> dict:
        return {
            "pid": __import__("os").getpid(),
            "running": bool(self._running),
            "phase": self._phase,
            "started_at": self._started_at,
            "stalled": list(self._stalled),
            "components": {name: comp.health.as_dict()
                           for name, comp in self._components.items()},
        }
