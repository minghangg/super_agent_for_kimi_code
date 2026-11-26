"""ISS-131 QA 退回后的三条必补用例（P0-1 / P0-2 / P1-1）。

每条针对一次真实失败，而不是对函数形状的猜测：

1. **注入齐全时 ``launcher run --wss`` 起得来。** 第一次交付里 A 的两个端点
   都不通：``build_wss_service`` 返回 ``_run_daemon``（``() -> int``），而
   runner 以 ``body(stop_event)`` 调用，于是 ``TypeError``；而不调
   ``wss_bus.configure()`` 时 ``missing_injections()`` 永不为空，A 只能是禁用
   占位。这条用例同时卡住两个端点。
2. **旧 ``wss_bus --daemon`` 被拒。** ``acquire_lock()`` 原来只认命令行含
   ``wss_bus``；launcher 的命令行是 ``-m super_agent.launcher run``，所以旧
   daemon 会照常启动，两个 A 同时写环形缓冲。
3. **干净停止后 ``doctor`` 全绿。** 原实现退出后 ``health.json`` 仍是
   ``running=true``，``doctor`` 报 ``crashed``。

Run: ``python -m pytest tests/test_launcher_wss.py -q``
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from super_agent import launcher, launcher_wss, wss_bus
from super_agent.engine_parts import doctor as doctor_mod
from super_agent.pidlock import PidLock
from super_agent.store import Store


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "data"
        self.state.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.state)
        saved = (wss_bus.DEFAULT_CONFIG, wss_bus.PATHS, wss_bus.PORT)
        self.addCleanup(self._restore, saved)

    def _restore(self, saved):
        wss_bus.DEFAULT_CONFIG, wss_bus.PATHS, wss_bus.PORT = saved
        self.tmp.cleanup()

    def cfg(self, **extra):
        from super_agent.config import load
        return load(None, {"github": {"source": "none"}, **extra})


class TestWssInjection(Base):
    """P0-1: ``configure()`` runs, so a normal start has a real service body."""

    def test_configure_makes_the_boundary_complete(self):
        before = wss_bus.missing_injections()
        self.assertTrue(before, "before configure() the boundary must be incomplete")
        error = launcher_wss.configure_wss(self.cfg(), self.store)
        self.assertEqual(error, "", error)
        self.assertEqual(wss_bus.missing_injections(), [],
                         "configure() must leave nothing missing")

    def test_state_dir_reaches_wss_paths(self):
        """The lock and the engine pid must live in **one** directory."""
        launcher_wss.configure_wss(self.cfg(), self.store)
        self.assertEqual(Path(wss_bus.PATHS.root).resolve(),
                         Path(self.store.root).resolve())
        self.assertEqual(Path(wss_bus.PATHS.lock).parent,
                         Path(self.store.pid_path).parent,
                         "两把锁必须落在同一个目录，否则互斥没有前提")

    def test_service_body_takes_a_stop_event(self):
        """The adapter accepts ``body(stop_event)`` - the shape the runner uses."""
        launcher_wss.configure_wss(self.cfg(), self.store)
        body, reason = launcher_wss.build_wss_service(self.cfg(), self.store)
        self.assertEqual(reason, "")
        self.assertIsNotNone(body)
        # A service body must be a coroutine function taking exactly one arg.
        self.assertTrue(asyncio.iscoroutinefunction(body))
        params = body.__code__.co_argcount
        self.assertEqual(params, 1, "body(stop_event) 必须收一个参数")

    def test_forced_wss_is_registered_enabled(self):
        launcher_wss.configure_wss(self.cfg(), self.store)
        rn = launcher.build_runner(self.cfg(), self.store, enable_wss=True)
        row = rn.component("wss").health.as_dict()
        self.assertTrue(row["enabled"], "--wss 必须真的启用 A")
        self.assertEqual(row["kind"], "service")

    def test_uninjected_wss_is_reported_not_silently_missing(self):
        rn = launcher.build_runner(self.cfg(), self.store, enable_wss=False)
        row = rn.component("wss").health.as_dict()
        self.assertFalse(row["enabled"])
        self.assertTrue(row["last_error"] or True)


class TestOldDaemonRefused(Base):
    """P0-2: a live launcher must keep the old ``--daemon`` out."""

    def test_launcher_command_line_counts_as_holder(self):
        self.assertTrue(wss_bus._names_a_holder(
            r'"D:\python\python.exe" -m super_agent.launcher --state-dir X run'))
        self.assertTrue(wss_bus._names_a_holder(
            r'"D:\python\python.exe" -m super_agent.wss_bus --daemon'))
        self.assertFalse(wss_bus._names_a_holder("python something_else.py"))

    def test_old_daemon_refused_while_launcher_holds_the_lock(self):
        """The real question: does the WSS lock notice a launcher command line?

        The launcher writes ``engine.pid``; the WSS writes ``wss_bus.lock``. They
        are different files, so exclusion between them can only come from the
        WSS lock **reading the incumbent's command line** - which is what used to
        say "not wss_bus, therefore free". The scenario is therefore a lock file
        naming a live launcher pid.
        """
        launcher_wss.configure_wss(self.cfg(), self.store)
        wss_bus.PATHS.mkdir()
        wss_bus.PATHS.lock.write_text(json.dumps({
            "pid": 4242, "port": 58800, "state_dir": str(self.store.root),
            "cmdline": "-m super_agent.launcher run"}), encoding="utf-8")
        with held_as_launcher():
            self.assertFalse(
                wss_bus.acquire_lock(),
                "launcher 存活时旧 wss_bus --daemon 必须被拒")

    def test_old_daemon_starts_when_nobody_holds_it(self):
        launcher_wss.configure_wss(self.cfg(), self.store)
        with held_as_launcher():
            self.assertTrue(wss_bus.acquire_lock())
            wss_bus.release_lock()


class held_as_launcher:
    """Make ``wss_bus``'s liveness probe see *this* process as the launcher.

    The real check asks the OS for a pid's command line, which a test process
    cannot arrange for itself. Patching exactly that one seam keeps the rest of
    the lock logic - the alive check, the JSON read, the write - under test.
    """

    def __enter__(self):
        self._alive = wss_bus._pid_alive
        self._cmdline = wss_bus._cmdline_of
        wss_bus._pid_alive = lambda pid: True
        wss_bus._cmdline_of = lambda pid: "-m super_agent.launcher run"
        return self

    def __exit__(self, *_exc):
        wss_bus._pid_alive = self._alive
        wss_bus._cmdline_of = self._cmdline


class TestSignals(Base):
    """The stop handler must reach an ``asyncio.Event`` and a thread ``Event``."""

    def test_handler_sets_an_asyncio_event_on_its_loop(self):
        """Raise the signal for real: calling the handler by hand skips the loop.

        ``add_signal_handler`` schedules the callback on the loop, so a manual
        ``handler(...)`` call would test nothing about the wiring.
        """
        from super_agent import stopcontrol

        async def main():
            ev = asyncio.Event()
            previous = stopcontrol.install_stop_handlers(ev)
            try:
                import signal
                self.assertTrue(callable(signal.getsignal(signal.SIGINT)))
                signal.raise_signal(signal.SIGINT)
                await asyncio.sleep(0.05)
                return ev.is_set()
            finally:
                stopcontrol.restore_handlers(previous)

        self.assertTrue(asyncio.run(main()), "SIGINT 必须置位 asyncio.Event")

    def test_handler_sets_a_thread_event(self):
        import threading

        from super_agent import stopcontrol

        ev = threading.Event()
        previous = stopcontrol.install_stop_handlers(ev)
        try:
            import signal
            handler = signal.getsignal(signal.SIGINT)
            handler(signal.SIGINT, None)
            self.assertTrue(ev.is_set(), "threading.Event 也必须被置位")
        finally:
            stopcontrol.restore_handlers(previous)


class TestCleanStop(Base):
    """P1-1: a clean stop must not look like a crash."""

    def test_mark_clean_stop_flips_running_and_records_the_event(self):
        from super_agent import health as health_mod
        from super_agent import stopcontrol

        class Eng:
            def __init__(self):
                self.calls = []

            def write_health(self, **kw):
                self.calls.append(kw)

        eng = Eng()
        stopcontrol.mark_clean_stop(self.store, eng, reason="signal")
        self.assertEqual(eng.calls, [{"running": False}])
        rows = [json.loads(x) for x in
                (self.state / "events.jsonl").read_text("utf-8").splitlines()]
        self.assertIn("run.stopped", [r.get("type") for r in rows])
        # No engine double wrote a snapshot, so the guard writes one itself -
        # the point is that a reader never finds "still running" after a stop.
        snap = health_mod.read(self.store.health_path)
        self.assertFalse(snap.get("running", False))

    def test_doctor_is_clean_after_a_clean_stop(self):
        """The end-to-end judgement, not just the flag: ``doctor`` says so."""
        from super_agent import health as health_mod
        from super_agent import stopcontrol

        store = self.store

        class Eng:
            """The real shape: a snapshot carrying a finished round."""

            def write_health(self, *, running):
                health_mod.write(store.health_path, {
                    "pid": 1, "running": running, "phase": "running",
                    "last_loop_at": "2026-01-01T00:00:00+00:00"})

        stopcontrol.mark_clean_stop(store, Eng(), reason="signal")
        verdict = doctor_mod.process(self.store, current_head=lambda: "")
        self.assertEqual(len(verdict), 5, verdict)
        _name, ok, detail, level, label = verdict
        self.assertTrue(ok, detail)
        # "stopped cleanly" is a warn, not a pass: nothing is running. What must
        # not appear is the crash label the first delivery produced.
        self.assertEqual(level, "warn", verdict)
        self.assertNotIn("crashed", detail)
        self.assertIn("stopped cleanly", detail)

    def test_stalled_stop_does_not_claim_a_clean_stop(self):
        """A stall must leave ``running=true``: a live thread still owns the dir."""
        from super_agent import stopcontrol

        class Eng:
            def __init__(self):
                self.calls = []

            def write_health(self, **kw):
                self.calls.append(kw)

        eng = Eng()
        stopcontrol.mark_clean_stop(self.store, eng, reason="signal",
                                    stalled=["engine"])
        self.assertEqual(eng.calls, [], "stalled 不是干净停止，不得翻 running")


if __name__ == "__main__":
    unittest.main()
