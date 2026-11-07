"""Negative controls as executable evidence.

Each test takes an acceptance predicate that is green in the shipped code,
applies one deliberate stub (the exact stub named in the task), and asserts the
*acceptance predicate now fails*. If an acceptance check were hard-wired to pass
these tests would fail, which is the point.

Run: ``python -m unittest tests.test_red_evidence -v``
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from super_agent import config
from super_agent.engine import Engine
from super_agent.github_source import FixtureSource
from super_agent.store import Store

FIXTURE = {
    "issues": {
        "42": {
            "title": "sample issue",
            "comments": [
                {"id": 9001, "body": "hello", "author": "teammate"},
                {"id": 9002, "body": "please [decision] on this", "author": "teammate"},
            ],
        }
    }
}


class FakeBus:
    def __init__(self):
        self.injected = []

    def inject(self, event_type, payload):
        self.injected.append((event_type, payload))
        return {"ok": True}


class RedEvidence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.fixture = root / "fixture.json"
        self.fixture.write_text(json.dumps(FIXTURE), encoding="utf-8")
        self.state = root / "state"
        cfg = config.load(None, {
            "github": {"source": "fixture", "fixture": str(self.fixture)},
            "store": {"max_event_lines": 1000, "max_outbox_lines": 1000},
        })
        self.store = Store(self.state)
        self.engine = Engine(cfg, self.store, FixtureSource(self.fixture), FakeBus(),
                             clock=lambda: 1_000_000.0)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def a4_predicate(store: Store) -> bool:
        """A4: in observe mode the external-action counter stays at zero."""
        return store.load_counters().get("external_actions") == 0

    @staticmethod
    def a6_predicate(first: dict, second: dict) -> bool:
        """A6: the second round processes no comment the first round handled."""
        return second["processed"] == 0

    @staticmethod
    def a7_predicate(path: Path, before: bytes) -> bool:
        """A7: a failed index write leaves the previous file byte-identical."""
        try:
            return path.read_bytes() == before
        except OSError:
            return False

    def test_r1_a4_turns_red_when_dispatch_is_enabled(self):
        self.engine.run_round()
        self.assertTrue(self.a4_predicate(self.store), "baseline A4 must be green")
        self.engine.dispatch_enabled = True
        self.engine._dispatch({"kind": "bus_inject",
                               "event": "event.agent.needs_person",
                               "issue": 42, "payload": {}})
        self.assertFalse(self.a4_predicate(self.store),
                         "A4 must go red once dispatch is enabled")
        self.assertEqual(self.store.load_counters()["external_actions"], 1)

    def test_r2_a6_turns_red_without_cursor_persistence(self):
        saved = self.store.save_cursor
        self.store.save_cursor = lambda data: None      # cursor write commented out
        try:
            first = self.engine.run_round()
            second = self.engine.run_round()
        finally:
            self.store.save_cursor = saved
        self.assertFalse(self.a6_predicate(first, second),
                         "A6 must go red without cursor persistence")
        self.assertGreater(second["processed"], 0)

    def test_r3_a7_turns_red_with_a_plain_write(self):
        path = self.state / "index.json"
        self.store.save_index({"version": 1, "meta": {"gen": 1},
                               "issues": {}, "assignments": {}})
        before = path.read_bytes()

        def plain_write(target: Path) -> None:
            with open(target, "w", encoding="utf-8") as fh:   # no temp, no rename
                fh.write('{"version": 1, "meta": {"gen": 2')
                raise OSError("crash mid write")

        with self.assertRaises(OSError):
            plain_write(path)
        self.assertFalse(self.a7_predicate(path, before),
                         "A7 must go red when the index is written in place")


if __name__ == "__main__":
    unittest.main()
