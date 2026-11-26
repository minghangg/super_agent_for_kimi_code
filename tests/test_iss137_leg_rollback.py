"""Rollback when a leg fails to start, and concurrent registration (#124 A12).

A12 requires that a failed start rolls back without disturbing the components
already running, and that concurrent registration is safe. Port release was
covered (``AtService.shutdown`` and the read API's ``finally``); the "one leg
fails, the others keep running" end-to-end assertion was not, so a change that
let one exception take the whole host down stayed green.

The two properties:

* **isolation** - a service whose bind fails leaves the other legs running and
  the port free, because a leaked listener makes every later start fail too;
* **concurrency** - eight threads racing to register one name leave exactly one
  component and seven refusals. Any other outcome is two bodies on one slot.

Run: ``python -m pytest tests/test_iss137_leg_rollback.py -q``
"""
from __future__ import annotations

import asyncio
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from super_agent import runner


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _blocker(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen(1)
    return sock


class FailedServiceDoesNotStopTheOthers(unittest.IsolatedAsyncioTestCase):
    """One service fails to bind; the other legs keep running."""

    async def test_a_failing_service_leaves_the_others_running(self):
        from super_agent.launcher_parts import at_service

        port = free_port()
        blocker = _blocker(port)
        self.addCleanup(blocker.close)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)

        beats = []

        async def healthy_service(stop_event):
            while not stop_event.is_set():
                beats.append(1)
                await asyncio.sleep(0.02)

        service = at_service.AtService(root=root, port=port)

        rn = runner.Runner()
        rn.register("wss", healthy_service, kind=runner.SERVICE, budget=2)
        rn.register("at", service.body, kind=runner.SERVICE, budget=2)
        await rn.start()
        await asyncio.sleep(0.3)

        self.assertTrue(beats, "健康组件必须仍在运行")
        self.assertTrue(rn.component("wss").health.running,
                        "一个腿绑定失败不得让另一个腿停摆")
        self.assertTrue(rn.component("at").health.last_error,
                        "失败腿必须留下错误")
        await asyncio.wait_for(rn.stop(), timeout=10)

    async def test_the_port_is_released_after_a_failed_bind(self):
        """绑定失败后端口不得被本进程占住（否则下一次启动永远失败）。"""
        from super_agent.launcher_parts import at_service

        port = free_port()
        blocker = _blocker(port)
        self.addCleanup(blocker.close)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)

        service = at_service.AtService(root=root, port=port)
        rn = runner.Runner()
        rn.register("at", service.body, kind=runner.SERVICE, budget=2)
        await rn.start()
        await asyncio.sleep(0.3)
        await asyncio.wait_for(rn.stop(), timeout=10)

        blocker.close()
        # 端口现在应当可以重新绑定：本进程没有泄漏监听套接字。
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        finally:
            probe.close()


class ConcurrentRegistration(unittest.IsolatedAsyncioTestCase):
    """Eight threads, one name: one component, seven refusals."""

    async def test_two_registrations_of_one_name_leave_one_component(self):
        rn = runner.Runner()
        rn.register("readapi", lambda: None, kind=runner.THREAD)
        errors = []

        def contender():
            try:
                rn.register("readapi", lambda: None, kind=runner.THREAD)
            except ValueError as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=contender) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(len(errors), 8, "并发注册同名必须全部被拒")
        self.assertEqual(rn.names(), ["readapi"], "已注册的组件必须原样保留")


if __name__ == "__main__":
    unittest.main()
