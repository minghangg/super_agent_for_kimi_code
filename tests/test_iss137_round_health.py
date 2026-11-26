"""#137 §一: the four round fields reach ``/healthz``, and nothing else follows.

``#124`` A2 requires health to list **collection incomplete, consumption left
over, and the failed set** when ``round_complete`` is false. All four values were
computed every round and written to the event stream, but never published: the
read API projected a five-name whitelist (``pid``/``running``/``phase``/
``started_at``/``stalled``) and ``Runner.health()`` had no round data at all.

Each test below fails if a field is merely *computed* rather than **reached
through the production path**: the source is a real round result, the carrier is
``Runner.health()``, and the endpoint is a real HTTP response. A test that called
``round_fields()`` on its own would pass while the endpoint stayed silent - which
is exactly the gap this issue is about.

Run: ``python -m pytest tests/test_iss137_round_health.py -q``
"""
from __future__ import annotations

import json
import socket
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from super_agent import runner
from super_agent.batch import CollectedBatch, ScanPolicy
from super_agent.engine_parts import round as round_mod
from super_agent.launcher_parts import readapi


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _Round:
    """The engine's ``_round_state`` handle, as ``health()`` sees it."""

    def __init__(self, round_result) -> None:
        self.round_result = round_result


def _engine(round_result) -> object:
    """A stand-in for the engine object: only ``_round_state`` is read."""
    return type("_Engine", (), {"_round_state": _Round(round_result)})()


def _complete_round():
    """One thread, every row consumed: the confirming reading."""
    collected = round_mod.CollectedRound(1, policy=ScanPolicy())
    batch = CollectedBatch("owner/demo#1", [{"id": 10}], cursor_before=0,
                           complete=True, contiguous=True)
    batch.consume([10])
    collected.add("owner/demo#1", batch)
    return collected


def _incomplete_round():
    """The A2 case: one thread failed, one carries rows it did not consume."""
    collected = round_mod.CollectedRound(2, policy=ScanPolicy())
    carried = CollectedBatch("owner/demo#2", [{"id": 20}, {"id": 21}],
                             cursor_before=0, complete=True, contiguous=True)
    carried.consume([20])
    collected.add("owner/demo#2", carried)
    collected.fail("owner/demo#1")
    return collected


class RoundFieldsCarrier(unittest.TestCase):
    """``Runner.health()`` is the only carrier between the round and the endpoint."""

    def test_an_incomplete_round_publishes_all_four(self):
        rn = runner.Runner(round_source=_engine(_incomplete_round()))
        health = rn.health()
        self.assertFalse(health["collection_complete"])
        self.assertFalse(health["consumption_complete"])
        self.assertEqual(health["carry"], {"owner/demo#2": 1},
                         "余量必须按线程报出条数")
        self.assertEqual(health["failed"], ["owner/demo#1"])

    def test_a_complete_round_still_publishes_all_four(self):
        """The confirmation reading is a value, not an absence."""
        rn = runner.Runner(round_source=_engine(_complete_round()))
        health = rn.health()
        self.assertTrue(health["collection_complete"])
        self.assertTrue(health["consumption_complete"])
        self.assertEqual(health["carry"], {})
        self.assertEqual(health["failed"], [])

    def test_before_the_first_round_nothing_is_claimed(self):
        """No round yet is not "a round that ran and came back incomplete"."""
        health = runner.Runner().health()
        for name in round_mod.ROUND_FIELDS:
            self.assertNotIn(name, health, name)

    def test_the_round_judgement_is_not_recomputed_here(self):
        """A carried row is what makes it incomplete; the runner must not differ."""
        collected = _incomplete_round()
        health = runner.Runner(round_source=_engine(collected)).health()
        self.assertEqual(health["collection_complete"],
                         collected.collection_complete)
        self.assertEqual(health["consumption_complete"],
                         collected.consumption_complete)


class HealthzPublishesThem(unittest.TestCase):
    """A real HTTP response, because a dict assertion cannot see a whitelist."""

    def payload(self, health: dict) -> dict:
        port = free_port()
        server, thread = readapi.serve(host="127.0.0.1", port=port,
                                       snapshot=lambda: health)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz",
                                        timeout=5) as response:
                return json.loads(response.read())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_an_incomplete_round_is_readable_over_http(self):
        rn = runner.Runner(round_source=_engine(_incomplete_round()))
        payload = self.payload(rn.health())
        self.assertFalse(payload["collection_complete"])
        self.assertFalse(payload["consumption_complete"])
        self.assertEqual(payload["carry"], {"owner/demo#2": 1})
        self.assertEqual(payload["failed"], ["owner/demo#1"])

    def test_a_complete_round_is_still_readable_over_http(self):
        rn = runner.Runner(round_source=_engine(_complete_round()))
        payload = self.payload(rn.health())
        self.assertTrue(payload["collection_complete"])
        self.assertTrue(payload["consumption_complete"])
        self.assertEqual(payload["carry"], {})
        self.assertEqual(payload["failed"], [])

    def test_the_original_whitelist_is_unchanged(self):
        payload = self.payload({"pid": 1, "running": True, "phase": "running",
                                "started_at": "t", "stalled": [],
                                "components": {}})
        self.assertEqual(set(readapi.TOP_FIELDS) - {"carry", "failed"},
                         {"pid", "running", "phase", "started_at", "stalled"})
        for name in ("pid", "running", "phase", "started_at", "stalled",
                     "ok", "ready", "components", "failures"):
            self.assertIn(name, payload, name)

    def test_a_field_outside_the_whitelist_still_does_not_leak(self):
        """A12 must not regress: adding four names must not open the surface."""
        rn = runner.Runner(round_source=_engine(_incomplete_round()))
        health = dict(rn.health(), internal_secret_table={"token": "ghp_leak"})
        payload = self.payload(health)
        self.assertNotIn("internal_secret_table", payload)
        self.assertNotIn("ghp_leak", json.dumps(payload))

    def test_no_round_means_no_round_fields_on_the_endpoint(self):
        payload = self.payload(runner.Runner().health())
        for name in round_mod.ROUND_FIELDS:
            self.assertNotIn(name, payload, name)


class ProductionPath(unittest.TestCase):
    """``build_runner`` is where the carrier is wired; a test double is not."""

    def test_build_runner_hands_the_engine_to_the_runner(self):
        import tempfile
        from pathlib import Path

        from super_agent import launcher
        from super_agent.store import Store

        class Engine:
            _round_state = _Round(_incomplete_round())

            def run_once(self):
                return {"ok": True}

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name) / "data")
        cfg = {"engine": {"launcher_stop_budget_s": 5},
               "github": {"poll_interval_s": 3600},
               "readapi": {"port": 0}}
        rn = launcher.build_runner(cfg, store, engine=Engine(),
                                   enable_wss=False, enable_decide_watch=False,
                                   enable_at=False, enable_reporter=False,
                                   enable_readapi=False, keep={})
        health = rn.health()
        self.assertEqual(health["failed"], ["owner/demo#1"],
                         "build_runner 没有把 engine 交给 Runner")


if __name__ == "__main__":
    unittest.main()
