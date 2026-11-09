"""L1 tests for ISS-33: startup failure is not a crash, and a locked stale
pid lock is a refusal (exit 3), not a traceback.

Both cases have a wrong-looking-but-explainable failure mode:

* ``startup()`` used to write ``running=true`` and then exit, so ``doctor``
  reported "crashed" for a process that had refused to start at all.
* ``PidLock.try_acquire()`` used to let ``PermissionError [WinError 32]``
  escape when the stale lock was held open, bypassing the ``LockError`` path
  and the documented exit code 3.

Every test uses a temporary state directory; the real ``state/`` is never
touched. Run: ``python -m pytest tests/test_startup_health.py -q``
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

from super_agent import cli, config, health
from super_agent.engine import Engine
from super_agent.github_source import NullSource
from super_agent.pidlock import PidLock
from super_agent.store import Store

ROOT = Path(__file__).resolve().parent.parent

GOOD = {
    "schema_version": 1,
    "defaults": {"mode": "observe", "creation_mode": "pm_created"},
    "projects": {
        "demo": {"repo": "owner/demo", "cwd": ".",
                 "mode": "observe", "creation_mode": "pm_created",
                 "issues": [33], "actions": ["collect", "reconcile"],
                 "max_active_writers": 1},
    },
}

WINDOWS = unittest.skipUnless(os.name == "nt",
                             "an open handle only blocks unlink on Windows")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.allow_path = self.root / "allowlist.json"
        self.allow_path.write_text(json.dumps(GOOD), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def engine(self) -> Engine:
        cfg = config.load(None, {"github": {"source": "none"},
                                 "engine": {"archived_crosscheck": False}})
        return Engine(cfg, Store(self.state), NullSource())

    def cli_args(self) -> list[str]:
        return ["--state-dir", str(self.state), "--source", "none",
                "--allowlist", str(self.allow_path)]

    def process_check(self, engine: Engine) -> dict:
        return next(c for c in engine.doctor()["checks"] if c["name"] == "process")


class StartupFailure(Base):
    def test_failed_startup_is_labelled_startup_failed(self):
        engine = self.engine()

        def boom():
            raise RuntimeError("boom")

        engine.recover = boom
        out = engine.startup()
        self.assertFalse(out["ok"])
        self.assertIn("RuntimeError: boom", out["error"])

        snap = health.read(self.state / "health.json")
        self.assertEqual(snap["phase"], "startup-failed")
        self.assertFalse(snap["running"])
        self.assertTrue(snap["degraded"])
        self.assertIn("RuntimeError: boom", snap["last_error"])

        check = self.process_check(engine)
        self.assertEqual(check["level"], "fail")
        self.assertEqual(check["label"], "startup-failed", check)
        self.assertIn("boom", check["detail"])
        self.assertNotIn("crashed", check["detail"])

    def test_cli_prints_the_startup_failed_label_and_does_not_say_crashed(self):
        with unittest.mock.patch.object(Engine, "recover",
                                        side_effect=RuntimeError("boom")):
            once = io.StringIO()
            with contextlib.redirect_stdout(once), \
                    contextlib.redirect_stderr(once):
                rc = cli.main(self.cli_args() + ["once"])
            self.assertNotEqual(rc, 0, once.getvalue())

            text = io.StringIO()
            with contextlib.redirect_stdout(text), \
                    contextlib.redirect_stderr(text):
                doctor_rc = cli.main(self.cli_args() + ["doctor"])
        printed = text.getvalue()
        self.assertEqual(doctor_rc, 1, printed)
        self.assertIn("[FAIL] startup-failed", printed)
        self.assertIn("boom", printed)
        self.assertNotIn("crashed", printed)
        self.assertFalse((self.state / "engine.pid").exists())

    def test_killed_running_loop_is_still_reported_as_crashed(self):
        engine = self.engine()
        engine.write_health(running=True)
        snap = health.read(self.state / "health.json")
        self.assertEqual(snap["phase"], "running")
        self.assertFalse((self.state / "engine.pid").exists())

        check = self.process_check(engine)
        self.assertEqual(check["level"], "fail")
        self.assertEqual(check["label"], "process-dead")
        self.assertIn("crashed", check["detail"])

    def test_clean_stop_is_ok_and_says_stopped(self):
        engine = self.engine()
        engine.write_health(running=False)
        snap = health.read(self.state / "health.json")
        self.assertEqual(snap["phase"], "stopped")

        check = self.process_check(engine)
        self.assertEqual(check["level"], "ok", check)
        self.assertIn("stopped cleanly", check["detail"])

    def test_legacy_snapshot_without_phase_reads_as_running_or_stopped(self):
        path = self.state / "health.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"pid": 999999, "running": True, "last_error": "",
             "degraded": False, "last_loop_at": None}), encoding="utf-8")
        snap = health.read(path)
        self.assertEqual(snap["phase"], "running")
        self.assertEqual(set(snap), set(health.FIELDS))
        self.assertIn("crashed", self.process_check(self.engine())["detail"])

    def test_phase_field_survives_the_write_whitelist(self):
        path = self.state / "health.json"
        written = health.write(path, {"running": False,
                                      "phase": "startup-failed",
                                      "last_error": "boom",
                                      "ignored": "dropped"})
        self.assertEqual(written["phase"], "startup-failed")
        self.assertNotIn("ignored", written)
        raw = json.loads(path.read_text("utf-8"))
        self.assertEqual(raw["phase"], "startup-failed")
        self.assertEqual(health.read(path)["phase"], "startup-failed")
        # An unknown phase never leaks through: it degrades to running/stopped.
        self.assertEqual(health.write(path, {"running": True,
                                            "phase": "nonsense"})["phase"],
                         "running")


class HealthInvariant(Base):
    """``write()`` must never store a ``phase``/``running`` contradiction."""

    def write_capturing_stderr(self, record: dict) -> tuple[dict, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            written = health.write(self.state / "health.json", record)
        return written, err.getvalue()

    def test_running_true_with_startup_failed_is_corrected_and_reported(self):
        written, warn = self.write_capturing_stderr(
            {"pid": 1, "running": True, "phase": "startup-failed",
             "degraded": True, "last_error": "boom"})
        self.assertFalse(written["running"])
        self.assertEqual(written["phase"], "startup-failed")
        self.assertIn("health.write", warn)
        self.assertIn("running=False", warn)

        raw = json.loads((self.state / "health.json").read_text("utf-8"))
        self.assertEqual(raw["phase"], "startup-failed")
        self.assertFalse(raw["running"])

        check = self.process_check(self.engine())
        self.assertEqual(check["label"], "startup-failed", check)
        self.assertIn("boom", check["detail"])
        self.assertNotIn("crashed", check["detail"])

    def test_running_false_with_phase_running_is_corrected_to_stopped(self):
        written, warn = self.write_capturing_stderr({"running": False,
                                                     "phase": "running"})
        self.assertEqual(written["phase"], "stopped")
        self.assertFalse(written["running"])
        self.assertIn("已纠正为 stopped", warn)
        raw = json.loads((self.state / "health.json").read_text("utf-8"))
        self.assertEqual(raw["phase"], "stopped")
        self.assertFalse(raw["running"])
        self.assertEqual(health.read(self.state / "health.json")["phase"],
                         "stopped")

    def test_running_true_with_phase_stopped_is_corrected(self):
        written, warn = self.write_capturing_stderr({"running": True,
                                                     "phase": "stopped"})
        self.assertEqual(written["phase"], "stopped")
        self.assertFalse(written["running"])
        self.assertIn("running=False", warn)

    def test_consistent_pairs_are_stored_untouched_without_a_warning(self):
        for running, phase in ((True, "running"), (False, "startup-failed"),
                               (False, "stopped")):
            written, warn = self.write_capturing_stderr({"running": running,
                                                         "phase": phase})
            self.assertEqual((written["running"], written["phase"]),
                             (running, phase))
            self.assertEqual(warn, "")


class StaleLockOrdering(Base):
    """A leftover ``engine.pid`` must not bury a startup-failed snapshot."""

    PID = 999997

    def stale_lock(self) -> None:
        path = self.state / "engine.pid"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": self.PID, "started_at": "t",
                                    "cmdline_hash": "deadbeef"}), encoding="utf-8")

    def failed_snapshot(self, error: str = "outbox recovery failed: boom") -> None:
        health.write(self.state / "health.json",
                     {"pid": self.PID, "running": False,
                      "phase": "startup-failed", "degraded": True,
                      "last_error": error})

    def test_stale_lock_keeps_startup_failed_and_its_last_error(self):
        self.failed_snapshot()
        self.stale_lock()
        check = self.process_check(self.engine())
        self.assertEqual(check["level"], "fail")
        self.assertEqual(check["label"], "startup-failed", check)
        self.assertIn("last_error=outbox recovery failed: boom", check["detail"])
        self.assertIn(str(self.PID), check["detail"])
        self.assertNotIn("next start takes it over", check["detail"])

    def test_cli_doctor_prints_startup_failed_with_the_stale_lock(self):
        self.failed_snapshot()
        self.stale_lock()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(out):
            rc = cli.main(self.cli_args() + ["doctor"])
        printed = out.getvalue()
        self.assertEqual(rc, 1, printed)
        self.assertIn("[FAIL] startup-failed", printed)
        self.assertIn("outbox recovery failed: boom", printed)
        self.assertNotIn("next start takes it over", printed)

    def test_stale_lock_without_startup_failed_stays_process_dead(self):
        health.write(self.state / "health.json",
                     {"pid": self.PID, "running": False, "phase": "stopped",
                      "last_error": ""})
        self.stale_lock()
        check = self.process_check(self.engine())
        self.assertEqual(check["label"], "process-dead", check)
        self.assertIn("stale lock", check["detail"])
        self.assertNotIn("startup-failed", check["detail"])

    def test_startup_failed_without_a_lock_is_unchanged(self):
        self.failed_snapshot("boom")
        check = self.process_check(self.engine())
        self.assertEqual(check["label"], "startup-failed", check)
        self.assertIn("last_error=boom", check["detail"])
        self.assertIn("not a crash", check["detail"])


class LockedStaleLock(Base):
    def stale_lock(self) -> Path:
        path = self.state / "engine.pid"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": 999999, "started_at": "t",
                                    "cmdline_hash": "deadbeef"}), encoding="utf-8")
        return path

    @WINDOWS
    def test_open_handle_turns_the_takeover_into_a_refusal(self):
        path = self.stale_lock()
        handle = open(path, "rb")
        try:
            lock = PidLock(path)
            ok, info = lock.try_acquire()
            self.assertFalse(ok)
            self.assertFalse(lock.acquired)
            self.assertFalse(lock.took_over)
            self.assertEqual(info["pid"], 999999)
            self.assertIn("cannot be removed", lock.describe(info))
        finally:
            handle.close()

        retry = PidLock(path)
        self.assertTrue(retry.try_acquire()[0])
        self.assertTrue(retry.took_over)
        retry.release()

    @WINDOWS
    def test_cli_run_exits_3_without_a_traceback(self):
        path = self.stale_lock()
        handle = open(path, "rb")
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(out):
                rc = cli.main(self.cli_args() +
                              ["run", "--interval", "0.1", "--max-rounds", "1"])
        finally:
            handle.close()
        text = out.getvalue()
        self.assertEqual(rc, 3, text)
        self.assertIn("refused to start", text)
        self.assertIn("cannot be removed", text)
        self.assertNotIn("Traceback", text)

    def test_stale_lock_without_a_holder_is_taken_over(self):
        self.stale_lock()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = cli.main(self.cli_args() +
                          ["run", "--interval", "0.1", "--max-rounds", "2"])
        text = out.getvalue()
        self.assertEqual(rc, 0, text)
        self.assertIn("took over a stale lock", text)
        self.assertFalse((self.state / "engine.pid").exists())


if __name__ == "__main__":
    unittest.main()
