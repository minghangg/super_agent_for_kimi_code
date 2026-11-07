"""Tests for the resident ``run`` loop (``cli run``).

The loop must be startable without a network, must survive a bad round, must
give up after N consecutive failures, and must leave the state directory in a
readable state on exit. Observe mode keeps ``external_actions`` at zero because
every action still goes through ``Engine._dispatch``.

Run: ``python -m pytest tests/test_run_loop.py -q``
"""
from __future__ import annotations

import contextlib
import io
import json
import signal
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from super_agent import cli, config
from super_agent.engine import Engine
from super_agent.github_source import FixtureSource
from super_agent.store import Store

FIXTURE = {
    "issues": {
        "42": {
            "title": "sample issue",
            "comments": [
                {"id": 9001, "body": "hello", "author": "teammate"},
                {"id": 9002, "body": "[blocked] need a call", "author": "teammate"},
            ],
        }
    }
}


class FakeBus:
    def __init__(self):
        self.injected = []

    def healthz(self):
        return {"ok": True, "ready": True, "connected": True, "boot_id": "t", "seq": 1}

    def inject(self, event_type, payload):
        self.injected.append((event_type, payload))
        return {"ok": True, "seq": 1}


class RunLoopBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fixture = self.root / "fixture.json"
        self.fixture.write_text(json.dumps(FIXTURE), encoding="utf-8")
        self.state = self.root / "state"
        cfg = config.load(None, {
            "github": {"source": "fixture", "fixture": str(self.fixture),
                       "poll_interval_s": 60},
            "store": {"max_event_lines": 10_000, "max_outbox_lines": 1_000},
        })
        self.cfg = cfg
        self.store = Store(self.state)
        self.engine = Engine(cfg, self.store, FixtureSource(self.fixture), FakeBus(),
                             clock=lambda: 1_000_000.0)
        self.sleeps = []

    def tearDown(self):
        self.tmp.cleanup()

    def fake_sleep(self, seconds):
        self.sleeps.append(seconds)

    def heartbeats(self):
        seen = []
        save = self.store.save_heartbeat

        def spy(record):
            seen.append(dict(record))
            save(record)

        self.store.save_heartbeat = spy
        return seen


class Heartbeat(RunLoopBase):
    def test_r1_r4_each_round_writes_a_heartbeat(self):
        seen = self.heartbeats()
        out = cli.run_loop(self.engine, self.store, interval=2, max_rounds=5,
                           sleep=self.fake_sleep)
        self.assertEqual(out["rounds"], 5)
        self.assertEqual(out["exit_code"], 0)
        self.assertEqual(len(seen), 5, "one heartbeat per round")
        self.assertEqual([h["round"] for h in seen], [1, 2, 3, 4, 5])
        for beat in seen:
            self.assertTrue(beat["ts"])
            self.assertTrue(beat["ok"])
            self.assertEqual(beat["external_actions"], 0)

        on_disk = json.loads((self.state / "run_heartbeat.json").read_text("utf-8"))
        self.assertEqual(set(on_disk), {"ts", "round", "ok", "external_actions"})
        self.assertEqual(on_disk["round"], 5)

        self.assertEqual(self.store.load_counters()["external_actions"], 0)
        self.assertEqual(self.engine.bus.injected, [],
                         "observe must not reach the bus")

    def test_r1_interval_is_honoured(self):
        cli.run_loop(self.engine, self.store, interval=2, max_rounds=4,
                     sleep=self.fake_sleep)
        self.assertAlmostEqual(sum(self.sleeps), 6.0, places=6,
                               msg="sleeps between rounds only")
        self.assertLessEqual(max(self.sleeps), 0.2)

    def test_r4_heartbeat_tracks_the_round_count(self):
        cli.run_loop(self.engine, self.store, interval=1, max_rounds=3,
                     sleep=self.fake_sleep)
        index = json.loads((self.state / "index.json").read_text("utf-8"))
        self.assertEqual(index["meta"]["run_count"], 3)
        beat = json.loads((self.state / "run_heartbeat.json").read_text("utf-8"))
        self.assertEqual(beat["round"], 3)


class Stop(RunLoopBase):
    def test_r2_stop_event_ends_the_loop_and_keeps_state(self):
        event = threading.Event()

        def sleep_and_stop(seconds):
            self.sleeps.append(seconds)
            if sum(self.sleeps) >= 4:        # two full 2s waits, i.e. two rounds
                event.set()

        out = cli.run_loop(self.engine, self.store, interval=2, max_rounds=0,
                           stop_event=event, sleep=sleep_and_stop)
        self.assertEqual(out["reason"], "signal")
        self.assertEqual(out["exit_code"], 0)
        self.assertEqual(out["rounds"], 2)
        cursor = json.loads((self.state / "cursor.json").read_text("utf-8"))
        index = json.loads((self.state / "index.json").read_text("utf-8"))
        self.assertEqual(cursor["issues"]["42"], 9002)
        self.assertIn("42", index["issues"])

    def test_r2_sigint_handler_sets_the_stop_event(self):
        event = threading.Event()
        previous = cli._install_stop_handlers(event)
        try:
            self.assertTrue(previous, "expected at least one handler installed")
            signal.raise_signal(signal.SIGINT)
            self.assertTrue(event.is_set())
        finally:
            cli._restore_handlers(previous)
        out = cli.run_loop(self.engine, self.store, interval=1, max_rounds=0,
                           stop_event=event, sleep=self.fake_sleep)
        self.assertEqual(out["rounds"], 0)
        self.assertEqual(out["reason"], "signal")
        self.assertEqual(out["exit_code"], 0)

    def test_r3_consecutive_failures_end_the_loop(self):
        class Boom:
            def __init__(self):
                self.calls = 0

            def run_once(self):
                self.calls += 1
                raise RuntimeError(f"boom {self.calls}")

        engine = Boom()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = cli.run_loop(engine, self.store, interval=1, max_failures=3,
                               sleep=self.fake_sleep)
        self.assertEqual(engine.calls, 3)
        self.assertEqual(out["exit_code"], 1)
        self.assertEqual(out["reason"], "max_failures")
        self.assertIn("boom 3", err.getvalue())
        events = (self.state / "events.jsonl").read_text("utf-8").splitlines()
        failed = [json.loads(line) for line in events
                  if json.loads(line)["type"] == "run.failed"]
        self.assertEqual(len(failed), 3)
        self.assertIn("RuntimeError", failed[-1]["error"])

    def test_r3_a_good_round_resets_the_failure_counter(self):
        class Flaky:
            def __init__(self):
                self.calls = 0

            def run_once(self):
                self.calls += 1
                if self.calls in (1, 2, 4, 5):
                    raise RuntimeError("flaky")
                return {"mode": "observe", "issues": 0, "comments_new": 0,
                        "processed": 0, "needs_person": 0, "errors": []}

        with contextlib.redirect_stderr(io.StringIO()):
            out = cli.run_loop(Flaky(), self.store, interval=1, max_failures=3,
                               max_rounds=5, sleep=self.fake_sleep)
        self.assertEqual(out["rounds"], 5)
        self.assertEqual(out["exit_code"], 0)


class Command(RunLoopBase):
    def test_r3_cli_run_exits_non_zero_after_max_failures(self):
        class Boom:
            def run_once(self):
                raise RuntimeError("always broken")

        class Args:
            interval = 0.01
            max_failures = 2
            max_rounds = 0
            config = None
            mode = "observe"
            role = "supervisor"
            state_dir = str(self.state)
            source = "none"

        out, err = io.StringIO(), io.StringIO()
        store = Store(self.state)
        with mock.patch.object(cli, "build_engine", lambda *a, **k: Boom()), \
                mock.patch.object(cli, "Store", lambda *a, **k: store), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.cmd_run(Args())
        self.assertEqual(rc, 1)
        self.assertIn("max_failures", out.getvalue())
        self.assertIn("always broken", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        beat = store.load_heartbeat()
        self.assertEqual(beat["round"], 2)
        self.assertFalse(beat["ok"])

    def test_run_is_registered_with_the_other_five_commands(self):
        parser = cli.build_parser()
        for command in ("once", "run", "status", "doctor", "replay"):
            with self.subTest(command=command):
                args = parser.parse_args([command] + (["--issue", "1"]
                                                      if command == "replay" else []))
                self.assertTrue(callable(args.func))
        args = parser.parse_args(["run", "--interval", "2", "--max-failures", "3"])
        self.assertEqual(args.interval, 2.0)
        self.assertEqual(args.max_failures, 3)
        self.assertIs(args.func, cli.cmd_run)

    def test_run_interval_defaults_to_the_poll_interval(self):
        parser = cli.build_parser()
        args = parser.parse_args(["run"])
        self.assertIsNone(args.interval)
        cfg = config.load(None, {"github": {"source": "none"}})
        self.assertEqual((cfg.get("github") or {})["poll_interval_s"], 60)


if __name__ == "__main__":
    unittest.main()
