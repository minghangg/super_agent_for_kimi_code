"""L1 tests for the M0 skeleton.

Run: ``python -m unittest discover -s tests -v``

Covers acceptance A1-A8 and the negative controls R1-R3 (each R proves that the
corresponding acceptance check can actually turn red).
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from super_agent import cli, config, store as store_mod
from super_agent.bus_client import BusClient
from super_agent.engine import Engine, build_engine
from super_agent.github_source import FixtureSource
from super_agent.store import Store

FIXTURE = {
    "issues": {
        "42": {
            "title": "sample issue",
            "comments": [
                {"id": 9001, "body": "hello", "author": "teammate"},
                {"id": 9002, "body": "still hello", "author": "teammate"},
                {"id": 9003, "body": "plain remark", "author": "teammate"},
                {"id": 9004, "body": "echo", "author": "supervisor"},
            ],
        }
    }
}


class FakeBus:
    def __init__(self):
        self.injected = []

    def healthz(self):
        return {"ok": True, "ready": True, "connected": True, "boot_id": "test",
                "seq": 1}

    def inject(self, event_type, payload):
        self.injected.append((event_type, payload))
        return {"ok": True, "seq": 1}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fixture_path = self.root / "fixture.json"
        self.fixture_path.write_text(json.dumps(FIXTURE), encoding="utf-8")
        self.state = self.root / "state"
        cfg = config.load(None, {
            "github": {"source": "fixture", "fixture": str(self.fixture_path),
                       "poll_interval_s": 60},
            "store": {"max_event_lines": 10_000, "max_outbox_lines": 10_000},
        })
        self.cfg = cfg
        self.store = Store(self.state)
        self.engine = Engine(cfg, self.store, FixtureSource(self.fixture_path),
                             FakeBus(), clock=lambda: 1_000_000.0)

    def tearDown(self):
        self.tmp.cleanup()


class Acceptance(Base):
    def test_a2_once_produces_index_and_events(self):
        summary = self.engine.run_round()
        self.assertEqual(summary["mode"], "observe")
        self.assertTrue((self.state / "index.json").is_file())
        self.assertTrue((self.state / "events.jsonl").is_file())
        index = json.loads((self.state / "index.json").read_text(encoding="utf-8"))
        self.assertIn("42", index["issues"])
        self.assertEqual(index["meta"]["run_count"], 1)
        self.assertFalse(list(self.state.glob("*.tmp")))

    def test_a3_status_empty_and_populated(self):
        self.engine.run_round()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["--state-dir", str(self.state), "status"])
        self.assertIn("无", out.getvalue())
        self.assertIn("external_actions=0", out.getvalue())

        # Now make one item: append a marker comment and reload the source.
        fixture = json.loads(json.dumps(FIXTURE))
        fixture["issues"]["42"]["comments"].append(
            {"id": 9005, "body": "[blocked] waiting", "author": "teammate"})
        self.fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
        self.engine.source = FixtureSource(self.fixture_path)
        self.engine.run_round()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["--state-dir", str(self.state), "status"])
        text = out.getvalue()
        self.assertEqual(text.count("无"), 0)
        self.assertIn("#42", text)

    def test_a4_observe_never_dispatches(self):
        self.engine.run_round()
        counters = self.store.load_counters()
        self.assertEqual(counters["external_actions"], 0)
        self.assertFalse(self.engine.dispatch_enabled)

    def test_a5_replay_is_read_only(self):
        out = self.engine.replay(42)
        self.assertEqual(out["comments"], 4)
        self.assertEqual(out["external_actions"], 0)
        index = json.loads((self.state / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["issues"]["42"]["comments_seen"], 4)

    def test_a6_cursor_prevents_reprocessing(self):
        first = self.engine.run_round()
        second = self.engine.run_round()
        self.assertEqual(first["processed"], 4)
        self.assertEqual(second["processed"], 0)
        self.assertEqual(second["comments_new"], 0)
        cursor = json.loads((self.state / "cursor.json").read_text(encoding="utf-8"))
        self.assertEqual(cursor["issues"]["42"], 9004)

    def test_a7_atomic_replace_keeps_old_index(self):
        self.store.save_index({"version": 1, "meta": {"good": True},
                               "issues": {}, "assignments": {}})
        before = (self.state / "index.json").read_bytes()

        def boom(_tmp):
            raise OSError("simulated crash before rename")

        store_mod.FAULT_HOOK = boom
        try:
            with self.assertRaises(OSError):
                self.store.save_index({"version": 1, "meta": {"good": False},
                                       "issues": {}, "assignments": {}})
        finally:
            store_mod.FAULT_HOOK = None
        self.assertEqual((self.state / "index.json").read_bytes(), before)
        self.assertFalse(list(self.state.glob("*.tmp")),
                         "a failed write must not leave the temporary file behind")

    def test_a8_event_log_rotates(self):
        bounded = Store(self.state, max_event_lines=10)
        for i in range(35):
            bounded.log_event({"type": "x", "i": i})
        current = (self.state / "events.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertLessEqual(len(current), 10)
        self.assertTrue((self.state / "events.jsonl.1").is_file())
        self.assertTrue((self.state / "events.jsonl.2").is_file())

    def test_a1_doctor_survives_dead_bus(self):
        cfg = config.load(None, {
            "bus": {"port": 1, "timeout_s": 0.5},
            "github": {"source": "none"},
        })
        engine = build_engine(cfg, Store(self.root / "doctor_state"),
                              BusClient("127.0.0.1", 1, 0.5))
        report = engine.doctor()
        bus_check = next(c for c in report["checks"] if c["name"] == "bus")
        self.assertFalse(bus_check["ok"])
        self.assertIn("unreachable", bus_check["detail"])
        # Point the CLI at a port nothing listens on: doctor must report, not raise.
        dead_cfg = self.root / "dead_bus.json"
        dead_cfg.write_text(json.dumps({
            "bus": {"port": 1, "timeout_s": 0.5},
            "github": {"source": "none"},
            "store": {"max_event_lines": 100, "max_outbox_lines": 100},
        }), encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["--config", str(dead_cfg),
                           "--state-dir", str(self.root / "doctor_state2"), "doctor"])
        self.assertEqual(rc, 1)
        self.assertIn("[FAIL]", out.getvalue())
        self.assertNotIn("Traceback", err.getvalue())


class NegativeControls(Base):
    """Each test proves the matching acceptance check can fail."""

    def test_r1_enabled_dispatch_moves_the_counter(self):
        self.engine.run_round()
        self.assertEqual(self.store.load_counters()["external_actions"], 0)
        self.engine.dispatch_enabled = True          # stub, as the task requires
        self.engine.run_round()                      # cooldown blocks repeat
        self.engine._mark_cooldown(
            self.store.load_index()["issues"]["42"], "needs_person", 0.0)
        self.engine._dispatch({"kind": "bus_inject", "event": "event.agent.needs_person",
                               "issue": 42, "payload": {}})
        self.assertEqual(self.store.load_counters()["external_actions"], 1)
        self.assertTrue(self.engine.bus.injected)

    def test_r2_no_cursor_write_reprocesses(self):
        # Cursor persistence disabled from the very first round: the engine
        # then has no way to know these comments were already handled.
        saved = self.store.save_cursor
        self.store.save_cursor = lambda data: None
        try:
            first = self.engine.run_round()
            second = self.engine.run_round()
        finally:
            self.store.save_cursor = saved
        self.assertGreater(first["processed"], 0)
        self.assertGreater(second["processed"], 0)

    def test_r3_naive_write_destroys_old_index(self):
        self.state.mkdir(parents=True, exist_ok=True)
        target = self.state / "index.json"
        target.write_text('{"version": 1, "meta": {"good": true}}', encoding="utf-8")

        def naive_write(path, text):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text[:5])
                raise OSError("crash mid write")

        with self.assertRaises(OSError):
            naive_write(target, '{"version": 1, "meta": {"good": false}}')
        # The destructive write left a truncated, unparseable file: the old
        # index is gone, so a concurrent reader would lose all state.
        with self.assertRaises(json.JSONDecodeError):
            json.loads(target.read_text(encoding="utf-8"))


class Gate(Base):
    def test_a9_role_name_comes_from_config(self):
        cfg = config.load(None, {"role": "quartermaster"})
        engine = Engine(cfg, Store(self.root / "role_state"), FixtureSource(
            self.fixture_path), FakeBus())
        self.assertEqual(engine.role, "quartermaster")
        engine.run_round()
        self.assertEqual(engine.status()["external_actions"], 0)


if __name__ == "__main__":
    unittest.main()
