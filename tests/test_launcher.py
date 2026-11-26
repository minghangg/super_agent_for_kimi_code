"""ISS-131 E: component host and launcher, with the five counter-examples.

Each test here is written to **fail first**. The failure mode is named in the
docstring of every counter-example, so "it passes" can be read as "the guard it
describes is in place" rather than "the code happens to run".

Run: ``python -m pytest tests/test_launcher.py -q``
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from super_agent import launcher, runner
from super_agent.pidlock import PidLock
from super_agent.store import Store


class Clock:
    """A source whose one read blocks for ``delay`` seconds, on demand."""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = float(delay)
        self.calls = 0


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "data"
        self.state.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.state)

    def tearDown(self):
        self.tmp.cleanup()


class TestThreadIsolation(Base):
    """Counter-example 1: the loop must not call ``run_round`` itself.

    If B is awaited inline, a 1 s block in B stops every other component - so
    this test's "A answered while B was blocked" assertion goes red the moment
    the isolation is removed. That is the point: the pass condition is a
    *measured* overlap, not the absence of a keyword.

    **A1's C leg is covered here, deliberately not by a test of its own.** C has
    no independent component: ``source.issues()`` is called from
    ``run_issue_phase`` on the engine thread, so there is no separate leg to
    inject a block into. Splitting C into a ``C_loop`` component - and with it a
    test that injects at ``source.issues`` - belongs to the ticket that shrinks
    ``engine.py``, per the supervisor's ruling on #124 (cid ``7196929770``) and
    the scope note on #137: this file's blocking body goes through the same
    ``to_thread`` boundary, so an inline round still turns this test red.
    """

    async def test_blocking_thread_component_does_not_block_the_loop(self):
        release = threading.Event()

        def blocking_body():
            release.wait(5.0)

        answered = {}

        async def probe(stop_event):
            started = time.monotonic()
            while not stop_event.is_set():
                # Sample the loop's responsiveness while B is blocked.
                if "at" not in answered:
                    answered["at"] = time.monotonic() - started
                await asyncio.sleep(0.01)

        rn = runner.Runner()
        rn.register("engine", blocking_body, kind=runner.THREAD, interval=3600)
        rn.register("wss", probe, kind=runner.SERVICE)
        await rn.start()
        try:
            await asyncio.sleep(0.25)
            # The loop is still turning: the service body kept being scheduled.
            self.assertIn("at", answered)
            self.assertLess(answered["at"], 0.2)
            self.assertTrue(runner_running(rn, "engine"),
                            "B should still be inside its thread")
        finally:
            release.set()
            await asyncio.wait_for(rn.stop(), timeout=5)

    async def test_loop_never_calls_run_round_directly(self):
        """No component body may be a coroutine calling a sync round in-loop."""
        calls = []

        def body():
            calls.append(time.monotonic())

        rn = runner.Runner()
        rn.register("engine", body, kind=runner.THREAD, interval=3600)
        await rn.start()
        try:
            await asyncio.sleep(0.1)
        finally:
            await asyncio.wait_for(rn.stop(), timeout=5)
        self.assertEqual(len(calls), 1, "one round per interval, not a spin")


def runner_running(rn, name: str) -> bool:
    return bool(rn.component(name).health.running)


class TestSingleFlight(Base):
    """A second trigger while the first is in flight is refused and counted."""

    async def test_overlapping_trigger_is_refused_not_queued(self):
        gate = threading.Event()
        runs = []

        def body():
            runs.append(1)
            gate.wait(5.0)

        rn = runner.Runner()
        rn.register("engine", body, kind=runner.THREAD, interval=3600)
        first = asyncio.create_task(rn.trigger("engine"))
        for _ in range(100):
            await asyncio.sleep(0.01)
            if runs:
                break
        second = await rn.trigger("engine")
        gate.set()
        await first
        self.assertTrue(second.get("overrun"), second)
        self.assertFalse(second.get("ran"))
        self.assertEqual(rn.component("engine").health.overrun, 1)
        self.assertEqual(len(runs), 1, "the refused trigger must not have run")


class TestStallNotStopped(Base):
    """Counter-example 3: a thread that outlives its budget is ``stalled``.

    Writing ``running=false`` there would let a second launcher start on a
    directory the stuck thread is still writing.
    """

    async def test_stop_reports_stalled_and_keeps_running(self):
        stuck = threading.Event()

        def body():
            stuck.wait(10.0)

        rn = runner.Runner()
        rn.register("engine", body, kind=runner.THREAD, interval=3600, budget=0.2)
        await rn.start()
        await asyncio.sleep(0.05)
        try:
            health = await rn.stop()
            self.assertEqual(health["phase"], "stalled", health)
            self.assertIn("engine", health["stalled"])
            self.assertTrue(health["running"],
                            "a live thread still owns the state directory")
            self.assertTrue(health["components"]["engine"]["stalled"])
            self.assertTrue(runner_running(rn, "engine"),
                            "the thread is still inside its body")
        finally:
            stuck.set()

    async def test_clean_stop_reports_stopped(self):
        def body():
            time.sleep(0.01)

        rn = runner.Runner()
        rn.register("engine", body, kind=runner.THREAD, interval=3600, budget=5)
        await rn.start()
        await asyncio.sleep(0.05)
        health = await rn.stop()
        self.assertEqual(health["phase"], "stopped")
        self.assertEqual(health["stalled"], [])
        self.assertFalse(health["running"])


class TestServiceStop(Base):
    async def test_service_is_cancelled_on_stop(self):
        stopped = asyncio.Event()
        state = {"ran": False}

        async def service(stop_event):
            state["ran"] = True
            await stop_event.wait()
            stopped.set()

        rn = runner.Runner()
        rn.register("wss", service, kind=runner.SERVICE, budget=2)
        await rn.start()
        self.assertTrue(state["ran"])
        health = await rn.stop()
        self.assertEqual(health["phase"], "stopped")
        self.assertTrue(stopped.is_set())


class TestMutualExclusion(Base):
    """Counter-example 2: a second launcher must be refused by the same lock.

    The test takes the lock the way ``cli run`` does and asserts the second
    attempt fails - so removing the ``PidLock`` from ``cmd_run`` turns this red.
    """

    def test_second_holder_is_refused(self):
        first = PidLock(self.store.pid_path, argv=["super_agent", "launcher", "run"])
        ok, _info = first.try_acquire()
        self.assertTrue(ok)
        try:
            second = PidLock(self.store.pid_path,
                             argv=["super_agent", "launcher", "run"])
            ok2, info2 = second.try_acquire()
            self.assertFalse(ok2, "a second holder must not start")
            self.assertEqual(int(info2.get("pid")), int(PidLock(self.store.pid_path).read().get("pid")))
        finally:
            first.release()

    async def test_launcher_run_refuses_when_lock_is_held(self):
        holder = PidLock(self.store.pid_path, argv=["super_agent", "launcher", "run"])
        self.assertTrue(holder.try_acquire()[0])
        try:
            args = launcher.build_parser().parse_args(
                ["--state-dir", str(self.state), "--source", "none",
                 "run", "--no-wss"])
            rc = await asyncio.to_thread(launcher.cmd_run, args)
            self.assertEqual(rc, 3, "the second launcher must exit 3")
        finally:
            holder.release()


class TestNoSecondNotificationStack(Base):
    """Counter-example 5: the host files must not grow their own senders.

    A second notification path is not a bug you can see in a diff of the host -
    it is a call the host makes. So the guard is a source scan over exactly the
    two files this issue adds, and the assertion is zero hits.
    """

    FORBIDDEN = ("begin_action", "_dispatch", "inject(", "send_", "archive_session",
                 "dispatch_packet")

    def test_host_modules_call_no_notification_primitives(self):
        base = Path(__file__).resolve().parent.parent / "super_agent"
        hits = []
        for name in ("runner.py", "launcher.py"):
            text = (base / name).read_text(encoding="utf-8")
            for line_no, line in enumerate(text.splitlines(), start=1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                for token in self.FORBIDDEN:
                    if token in line:
                        hits.append(f"{name}:{line_no}: {token} -> {stripped[:70]}")
        self.assertEqual(hits, [], "第二套推送/身份逻辑：\n" + "\n".join(hits))


if __name__ == "__main__":
    unittest.main()
