"""Mutual exclusion for the two legacy writers (#124 A7, last two legs).

A7 names five writers that must not become a second writer beside the launcher:
the second launcher, old ``cli run``/``once``, old WSS, old ``decide_watch`` and
old ``at serve``. The suite covered the first three. These two were the gap:

* **old ``decide_watch``** holds an OS lock on ``decide_watch.lock``; a second
  holder must be refused by the operating system, not by a convention;
* **old ``at serve``** holds the AT port; the launcher's AT leg must **fail
  loudly** on that port, because a leg that silently pretends to be up is a
  second writer with no error anywhere.

The failure they guard is the same one in both: two writers on one state
directory, which is the split brain the single-instance lock exists to prevent.

Run: ``python -m pytest tests/test_iss137_legacy_writers.py -q``
"""
from __future__ import annotations

import asyncio
import socket
import tempfile
import unittest
from pathlib import Path

from super_agent import runner
from super_agent.store import Store


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class LegacyDecideWatchLock(unittest.TestCase):
    """The old daemon's OS lock must refuse a second holder."""

    def test_the_legacy_lock_refuses_a_second_holder(self):
        from super_agent import decide_watch as dw

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = Path(tmp.name)
        lock_path = state / "decide_watch.lock"
        state.mkdir(parents=True, exist_ok=True)

        first = lock_path.open("a+b")
        first.write(b"\0")
        first.flush()
        dw._os_lock(first)                       # the old daemon's lock
        self.addCleanup(lambda: (dw._os_lock(first, unlock=True), first.close()))

        second = lock_path.open("a+b")
        self.addCleanup(second.close)
        with self.assertRaises(OSError):
            dw._os_lock(second)


class LegacyAtPortInUse(unittest.TestCase):
    """Old ``at serve`` holds the port; the AT leg must fail, not pretend."""

    def test_bind_on_a_taken_port_fails_loudly(self):
        from super_agent import at_serve

        port = free_port()
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", port))
        blocker.listen(1)
        self.addCleanup(blocker.close)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with self.assertRaises(OSError):
            at_serve.serve(root=Path(tmp.name), port=port)

    def test_the_runner_reports_the_failed_service_instead_of_ready(self):
        """端口被占 ⇒ 组件行必须带 last_error，且 ready 为假。"""
        from super_agent.launcher_parts import at_service

        port = free_port()
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", port))
        blocker.listen(1)
        self.addCleanup(blocker.close)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        store = Store(root / "data")

        service = at_service.AtService(root=root / "data", port=port)

        async def run():
            rn = runner.Runner()
            rn.register("at", service.body, kind=runner.SERVICE, budget=2)
            await rn.start()
            await asyncio.sleep(0.3)
            health = rn.health()
            await asyncio.wait_for(rn.stop(), timeout=10)
            return health

        health = asyncio.run(run())
        row = health["components"]["at"]
        self.assertTrue(row.get("last_error"),
                        f"端口被占必须留下错误，实际 {row}")
        self.assertFalse(row.get("running"),
                         "绑定失败不得报 running")


if __name__ == "__main__":
    unittest.main()
