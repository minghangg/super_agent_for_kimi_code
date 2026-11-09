"""L1 tests for the M0 runtime guarantees (ISS-28).

Covers the five blocks shipped together:

* ``super_agent/allowlist.py`` - three-level allowlist, hot reload, bad file
  keeps the previous copy, ``creation_mode`` gate.
* ``super_agent/store.py``    - outbox state machine and rotation without loss.
* ``super_agent/pidlock.py``  - atomic single-instance lock and stale takeover.
* ``super_agent/health.py``   - ``health.json`` and the five doctor faults.
* ``super_agent/dispatch_adapter.py`` - dict-returning, JSON-only adapter.

Every test uses a temporary state directory; the real ``state/`` is never
touched. Run: ``python -m pytest tests/test_m0_runtime.py -q``
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from super_agent import cli, config, dispatch_adapter, health
from super_agent import pidlock as pidlock_mod
from super_agent import store as store_mod
from super_agent.allowlist import AllowlistError, ProjectAllowlist, validate
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
                 "issues": [28], "actions": ["collect", "reconcile"],
                 "max_active_writers": 1},
    },
}


class FakeBus:
    def __init__(self):
        self.injected = []

    def healthz(self):
        return {"ok": True, "ready": True, "connected": True, "boot_id": "t", "seq": 1}

    def inject(self, event_type, payload):
        self.injected.append((event_type, payload))
        return {"ok": True, "seq": 1}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.allow_path = self.root / "allowlist.json"
        self.allow_path.write_text(json.dumps(GOOD), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def engine(self, *, allowlist=True, source=None, bus=None) -> Engine:
        cfg = config.load(None, {"github": {"source": "none"}})
        projects = ProjectAllowlist(self.allow_path) if allowlist else None
        return Engine(cfg, Store(self.state), source or NullSource(), bus,
                      project_allowlist=projects)

    def allow_bus_inject(self) -> None:
        """Let the demo project dispatch ``bus_inject`` (the engine's only verb)."""
        data = json.loads(json.dumps(GOOD))
        data["projects"]["demo"]["actions"] = ["bus_inject", "collect", "reconcile"]
        self.allow_path.write_text(json.dumps(data), encoding="utf-8")


class AllowlistGate(Base):
    def test_unlisted_project_issue_and_action_are_refused(self):
        gate = ProjectAllowlist(self.allow_path)
        self.assertEqual(gate.check("demo", 28, "collect")[0], True)
        self.assertFalse(gate.check("other", 28, "collect")[0])
        self.assertFalse(gate.check("demo", 29, "collect")[0])
        self.assertFalse(gate.check("demo", 28, "comment")[0])

    def test_new_issue_does_not_inherit_write_permission(self):
        gate = ProjectAllowlist(self.allow_path)
        ok, reason = gate.check("demo", 99, "collect")
        self.assertFalse(ok)
        self.assertIn("never inherited", reason)

    def test_engine_preapproved_is_rejected_at_load(self):
        bad = json.loads(json.dumps(GOOD))
        bad["projects"]["demo"]["creation_mode"] = "engine_preapproved"
        with self.assertRaises(AllowlistError) as caught:
            validate(bad)
        self.assertIn("engine_preapproved", str(caught.exception))

    def test_bad_types_are_rejected(self):
        for mutate in (lambda d: d["projects"]["demo"].update(issues="28"),
                       lambda d: d["projects"]["demo"].update(actions=["delete"]),
                       lambda d: d["projects"]["demo"].update(max_active_writers=0),
                       lambda d: d["projects"]["demo"].update(repo="nope")):
            bad = json.loads(json.dumps(GOOD))
            mutate(bad)
            with self.subTest():
                with self.assertRaises(AllowlistError):
                    validate(bad)

    def test_bad_reload_keeps_the_previous_copy(self):
        gate = ProjectAllowlist(self.allow_path)
        self.assertTrue(gate.check("demo", 28, "collect")[0])
        good_generation = gate.generation

        self.allow_path.write_text(json.dumps(
            {**GOOD, "projects": {"demo": {**GOOD["projects"]["demo"],
                                           "issues": "28"}}}), encoding="utf-8")
        result = gate.poll()
        self.assertFalse(result["changed"])
        self.assertEqual(result["used"], "previous")
        self.assertIn("issues", result["error"])
        self.assertEqual(gate.generation, good_generation)
        # Behaviour unchanged: the previous copy is still authoritative.
        self.assertTrue(gate.check("demo", 28, "collect")[0])
        self.assertFalse(gate.check("demo", 29, "collect")[0])

    def test_valid_reload_applies_to_later_events(self):
        gate = ProjectAllowlist(self.allow_path)
        self.assertFalse(gate.check("demo", 29, "collect")[0])
        wider = json.loads(json.dumps(GOOD))
        wider["projects"]["demo"]["issues"] = [28, 29]
        self.allow_path.write_text(json.dumps(wider), encoding="utf-8")
        result = gate.poll()
        self.assertTrue(result["changed"])
        self.assertEqual(gate.generation, 2)
        self.assertTrue(gate.check("demo", 29, "collect")[0])

    def test_dispatch_refusal_moves_no_external_counter(self):
        engine = self.engine()
        engine.dispatch_enabled = True
        out = engine._dispatch({"kind": "bus_inject", "event": "event.agent.needs_person",
                                "issue": 999, "payload": {}})
        self.assertFalse(out["dispatched"])
        self.assertTrue(out["refused"])
        self.assertIn("not allowlisted", out["reason"])
        self.assertEqual(engine.store.load_counters()["external_actions"], 0)

    def test_creation_actions_are_refused_even_when_listed(self):
        listed = json.loads(json.dumps(GOOD))
        listed["projects"]["demo"]["actions"] = ["collect", "create_worker"]
        self.allow_path.write_text(json.dumps(listed), encoding="utf-8")
        gate = ProjectAllowlist(self.allow_path)
        ok, reason = gate.check("demo", 28, "create_worker")
        self.assertFalse(ok)
        self.assertIn("creation_mode_forbidden", reason)


class ConfigGate(Base):
    def test_engine_preapproved_fails_config_load(self):
        with self.assertRaises(config.ConfigError) as caught:
            config.load(None, {"engine": {"creation_mode": "engine_preapproved"}})
        self.assertIn("only 'pm_created'", str(caught.exception))

    def test_cli_reports_the_config_error_and_exits_2(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["--state-dir", str(self.state), "--mode", "observe",
                           "doctor", "--config", str(self.root / "missing.json")])
        self.assertEqual(rc, 2)
        self.assertIn("config error", err.getvalue())


class OutboxMachine(Base):
    def test_happy_path_three_states(self):
        store = Store(self.state)
        aid = store_mod.action_id("A1", "comment", "c-9")
        self.assertEqual(aid, "A1:comment:c-9")
        store.begin_action(aid, verb="comment", kind="comment",
                           assignment_id="A1", input_ref="c-9")
        self.assertEqual(store.outbox_action(aid)["status"], "prepared")
        store.mark_in_flight(aid)
        self.assertEqual(store.outbox_action(aid)["status"], "in_flight")
        store.mark_done(aid, comment_id=4242)
        self.assertIsNone(store.outbox_action(aid))
        lines = [json.loads(x) for x in
                 (self.state / "outbox.jsonl").read_text("utf-8").splitlines()]
        self.assertEqual([r["status"] for r in lines],
                         ["prepared", "in_flight", "done"])
        self.assertEqual(lines[-1]["comment_id"], 4242)

    def test_assignmentless_id_is_stable(self):
        first = store_mod.action_id(None, "send", "ref-1")
        second = store_mod.action_id(None, "send", "ref-1")
        self.assertEqual(first, second)
        self.assertNotEqual(first, store_mod.action_id(None, "send", "ref-2"))

    def test_timeout_goes_uncertain_then_reconciles(self):
        store = Store(self.state)
        aid = store_mod.action_id("A2", "send", "r")
        store.begin_action(aid, verb="send", kind="send")
        store.mark_in_flight(aid)
        store.mark_uncertain(aid, "timeout")
        statuses = [json.loads(x)["status"] for x in
                    (self.state / "outbox.jsonl").read_text("utf-8").splitlines()]
        self.assertEqual(statuses, ["prepared", "in_flight", "uncertain"])
        self.assertEqual(len(store.reconcile_queue()), 1)
        store.mark_reconciling(aid)
        store.mark_reconciled(aid, "done", "remote id matched")
        statuses = [json.loads(x)["status"] for x in
                    (self.state / "outbox.jsonl").read_text("utf-8").splitlines()]
        self.assertEqual(statuses, ["prepared", "in_flight", "uncertain",
                                    "reconcile", "done"])
        self.assertIsNone(store.outbox_action(aid))

    def test_waiting_person_stays_visible_and_is_not_a_stuck_alarm(self):
        store = Store(self.state)
        aid = store_mod.action_id("A3", "send", "r")
        store.begin_action(aid, verb="send")
        store.mark_in_flight(aid)
        store.mark_uncertain(aid)
        store.mark_reconciling(aid)
        store.mark_reconciled(aid, "waiting_person", "not found remotely")
        lines = [json.loads(x) for x in
                 (self.state / "outbox.jsonl").read_text("utf-8").splitlines()]
        self.assertEqual(lines[-1]["status"], "waiting_person")
        # waiting_person is unfinished: it must remain in outbox_state.json so
        # a person can still find it after the history rotates.
        record = store.outbox_action(aid)
        self.assertIsNotNone(record)
        self.assertEqual(record["status"], "waiting_person")
        self.assertEqual([r["action_id"] for r in store.waiting_person()], [aid])
        # It is not in the reconcile queue (nothing left to probe) and it is not
        # an outbox-stuck alarm (a person is already expected to act).
        store.outbox_stale_s = 0.0
        time.sleep(0.01)
        self.assertEqual(store.reconcile_queue(), [])
        self.assertEqual(store.stuck_outbox(), [])

    def test_rotation_keeps_unfinished_actions(self):
        store = Store(self.state, max_outbox_lines=3)
        aid = store_mod.action_id("A4", "comment", "c")
        store.begin_action(aid, verb="comment", assignment_id="A4", input_ref="c")
        store.mark_in_flight(aid)
        for i in range(12):                      # force several rotations
            store.log_outbox({"at": "t", "action_id": f"done-{i}", "status": "done"})
        current = (self.state / "outbox.jsonl").read_text("utf-8").splitlines()
        self.assertLessEqual(len(current), 3)
        self.assertTrue((self.state / "outbox.jsonl.1").is_file())
        self.assertEqual(store.outbox_action(aid)["status"], "in_flight",
                         "rotation must not drop an unfinished action")

    def test_restart_marks_in_flight_uncertain_and_replays_nothing(self):
        store = Store(self.state)
        iflight = store_mod.action_id("A5", "create", "x")
        prepared = store_mod.action_id("A6", "collect", "y")
        store.begin_action(iflight, verb="create")
        store.mark_in_flight(iflight)
        store.begin_action(prepared, verb="collect")

        recovered = store.recover_outbox()
        self.assertEqual(recovered["moved_to_uncertain"], [iflight])
        self.assertEqual([r["action_id"] for r in recovered["replayable"]],
                         [prepared])
        self.assertEqual(store.outbox_action(iflight)["status"], "uncertain")
        self.assertEqual(len(store.reconcile_queue()), 1)

    def test_engine_reconcile_marks_done_when_the_remote_confirms(self):
        engine = self.engine()
        aid = store_mod.action_id("A7", "comment", "c")
        engine.store.begin_action(aid, verb="comment")
        engine.store.mark_in_flight(aid)
        engine.recover()
        self.assertEqual(engine.store.outbox_action(aid)["status"], "uncertain")

        calls = []

        def probe(record):
            calls.append(record)
            return {"found": True, "comment_id": 777,
                    "detail": "comment exists remotely"}

        result = engine.reconcile_uncertain(probe=probe)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result[0]["outcome"], "done")
        self.assertIsNone(engine.store.outbox_action(aid))
        self.assertEqual(engine.store.load_counters()["external_actions"], 0,
                         "reconciliation must never resend")

    def test_engine_reconcile_without_confirmation_waits_for_a_person(self):
        engine = self.engine()
        aid = store_mod.action_id("A8", "send", "r")
        engine.store.begin_action(aid, verb="send")
        engine.store.mark_in_flight(aid)
        engine.recover()
        result = engine.reconcile_uncertain(probe=lambda rec: {"found": False,
                                                               "detail": "absent"})
        self.assertEqual(result[0]["outcome"], "waiting_person")
        record = engine.store.outbox_action(aid)
        self.assertIsNotNone(record, "waiting_person must stay in outbox_state.json")
        self.assertEqual(record["status"], "waiting_person")
        lines = [json.loads(x) for x in
                 (self.state / "outbox.jsonl").read_text("utf-8").splitlines()]
        self.assertEqual(lines[-1]["status"], "waiting_person")


class PidLockTests(Base):
    def test_second_acquire_is_refused_and_first_still_owns(self):
        first = PidLock(self.state / "engine.pid", argv=["super_agent", "run"])
        ok, _ = first.try_acquire()
        self.assertTrue(ok)
        second = PidLock(self.state / "engine.pid", argv=["super_agent", "run"])
        ok2, info = second.try_acquire()
        self.assertFalse(ok2)
        self.assertEqual(info["pid"], os.getpid())
        self.assertFalse(second.took_over)
        first.release()
        self.assertFalse((self.state / "engine.pid").exists())

    def test_stale_lock_is_taken_over(self):
        path = self.state / "engine.pid"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": 999999, "started_at": "t",
                                    "cmdline_hash": "deadbeef"}), encoding="utf-8")
        lock = PidLock(path)
        ok, info = lock.try_acquire()
        self.assertTrue(ok)
        self.assertTrue(lock.took_over)
        self.assertEqual(info["pid"], os.getpid())
        lock.release()

    def test_context_manager_releases_on_exception(self):
        path = self.state / "engine.pid"
        with self.assertRaises(RuntimeError):
            with PidLock(path):
                raise RuntimeError("boom")
        self.assertFalse(path.exists())

    def test_two_processes_race_and_only_one_wins(self):
        script = (
            "import sys, time\n"
            "sys.path.insert(0, r'%s')\n"
            "from super_agent.pidlock import PidLock\n"
            "lock = PidLock(r'%s')\n"
            "ok, info = lock.try_acquire()\n"
            "print('acquired' if ok else 'refused')\n"
            "sys.stdout.flush()\n"
            "time.sleep(2.0)\n"
            "lock.release()\n" % (ROOT, self.state / "engine.pid"))
        procs = [subprocess.Popen([sys.executable, "-c", script],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True) for _ in range(2)]
        outs = [p.communicate(timeout=30)[0].strip() for p in procs]
        self.assertEqual(sorted(outs), ["acquired", "refused"], outs)
        self.assertEqual([p.returncode for p in procs], [0, 0])


class HealthAndDoctor(Base):
    def test_health_fields_and_atomic_write(self):
        engine = self.engine()
        engine.run_round()
        record = health.read(self.state / "health.json")
        self.assertEqual(set(record), set(health.FIELDS))
        self.assertEqual(record["mode"], "observe")
        self.assertTrue(record["last_loop_at"])
        self.assertFalse(record["degraded"])
        self.assertFalse(list(self.state.glob("*.tmp")))

    def test_doctor_is_all_green_when_healthy(self):
        engine = self.engine(bus=FakeBus())
        lock = PidLock(self.state / "engine.pid")
        lock.try_acquire()
        try:
            engine.run_round()
            report = engine.doctor()
        finally:
            lock.release()
        bad = [c for c in report["checks"] if c["level"] == "fail"]
        self.assertEqual(bad, [], report["checks"])
        self.assertTrue(all(c["label"] == "ok" for c in report["checks"]),
                        report["checks"])

    def test_five_faults_have_distinct_labels(self):
        faults = {}

        # 1 process-dead: a stale pid record.
        engine = self.engine(bus=FakeBus())
        (self.state / "engine.pid").write_text(json.dumps(
            {"pid": 999999, "started_at": "t", "cmdline_hash": "x"}), encoding="utf-8")
        faults["process-dead"] = engine.doctor()

        # 2 bus-down
        cfg = config.load(None, {"github": {"source": "none"},
                                 "bus": {"port": 1, "timeout_s": 0.2}})
        bus_engine = Engine(cfg, Store(self.state), NullSource(),
                            __import__("super_agent.bus_client",
                                       fromlist=["BusClient"]).BusClient("127.0.0.1", 1, 0.2))
        faults["bus-down"] = bus_engine.doctor()

        # 3 github-down
        class Broken:
            def issues(self, limit=20):
                raise RuntimeError("offline")

            def comments(self, issue):
                raise RuntimeError("offline")

            def title(self, issue):
                raise RuntimeError("offline")

        gh_cfg = config.load(None, {"github": {"source": "live", "repo": "o/n"}})
        faults["github-down"] = Engine(gh_cfg, Store(self.state), Broken(),
                                       FakeBus()).doctor()

        # 4 config-bad: a broken file after a valid one.
        self.allow_path.write_text(json.dumps(
            {**GOOD, "projects": {"demo": {**GOOD["projects"]["demo"],
                                           "creation_mode": "engine_preapproved"}}}),
            encoding="utf-8")
        faults["config-bad"] = self.engine(bus=FakeBus()).doctor()

        # 5 outbox-stuck
        stuck_engine = self.engine(bus=FakeBus())
        stuck_engine.store.outbox_stale_s = 0.0
        aid = store_mod.action_id("A9", "comment", "c")
        stuck_engine.store.begin_action(aid, verb="comment")
        stuck_engine.store.mark_in_flight(aid)
        time.sleep(0.01)
        faults["outbox-stuck"] = stuck_engine.doctor()

        labels = {name: {c["label"] for c in report["checks"] if c["level"] != "ok"}
                  for name, report in faults.items()}
        for expected, seen in labels.items():
            with self.subTest(expected=expected):
                self.assertIn(expected, seen, seen)
        self.assertTrue(all(len(seen) >= 1 for seen in labels.values()))

    def test_crash_vs_clean_stop_are_distinguishable(self):
        engine = self.engine()
        engine.write_health(running=True)
        # A killed process leaves running=true behind and no pid lock.
        crashed = engine.doctor()
        check = next(c for c in crashed["checks"] if c["name"] == "process")
        self.assertEqual(check["level"], "fail")
        self.assertIn("crashed", check["detail"])

        engine.write_health(running=False)
        stopped = engine.doctor()
        check = next(c for c in stopped["checks"] if c["name"] == "process")
        self.assertEqual(check["level"], "ok", check)
        self.assertIn("stopped cleanly", check["detail"])

    def test_run_loop_marks_health_not_running_on_clean_exit(self):
        engine = self.engine()
        cli.run_loop(engine, engine.store, interval=1, max_rounds=1,
                     sleep=lambda s: None)
        self.assertFalse(health.read(engine.store.health_path)["running"])

    def test_doctor_cli_prints_labels_and_exits_non_zero(self):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "engine.pid").write_text(json.dumps(
            {"pid": 999999, "started_at": "t", "cmdline_hash": "x"}), encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = cli.main(["--state-dir", str(self.state), "doctor",
                           "--allowlist", str(self.allow_path)])
        text = out.getvalue()
        self.assertEqual(rc, 1, text)
        self.assertIn("process-dead", text)
        self.assertIn("result: fail", text)


class Adapter(Base):
    def adapter(self, payload=None, *, creation_mode="pm_created"):
        calls = []

        def runner(argv):
            calls.append(list(argv))
            return 0, json.dumps(payload if payload is not None else
                                 {"ok": True, "sid": "sess-1"}), ""

        return dispatch_adapter.DispatchAdapter(creation_mode=creation_mode,
                                                runner=runner), calls

    def test_every_method_returns_the_common_shape(self):
        adapter, calls = self.adapter()
        results = {
            "resolve_role_session": adapter.resolve_role_session("demo", "worker"),
            "bind_assignment": adapter.bind_assignment({"id": "A1"}),
            "send": adapter.send({"id": "A1"}, "ref"),
            "fetch": adapter.fetch({"id": "A1"}),
            "archive": adapter.archive({"id": "A1"}, "done"),
            "reconcile_close": adapter.reconcile_close({"id": "T1"}),
            "create_worker": adapter.create_worker({"id": "A1"}),
            "create_qa": adapter.create_qa({"id": "A1"}),
        }
        for name, result in results.items():
            with self.subTest(name=name):
                self.assertIsInstance(result, dict)
                self.assertTrue({"ok", "code", "session_id", "error",
                                 "source"} <= set(result), result)

    def test_creation_is_forbidden_under_pm_created(self):
        adapter, calls = self.adapter()
        for result in (adapter.create_worker({"id": "A1"}),
                       adapter.create_qa({"id": "A2"})):
            self.assertEqual(result["ok"], False)
            self.assertEqual(result["code"], "creation_mode_forbidden")
            self.assertEqual(result["draft"]["status"], "draft_only")
        self.assertEqual(calls, [], "a refused creation must not spawn the console")

    def test_human_text_is_never_parsed(self):
        def runner(argv):
            return 0, "created session abc123 for worker", ""

        adapter = dispatch_adapter.DispatchAdapter(runner=runner)
        result = adapter.resolve_role_session("demo", "worker")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "adapter_not_json")

    def test_non_zero_exit_carries_stderr(self):
        def runner(argv):
            return 4, "", "permission denied"

        adapter = dispatch_adapter.DispatchAdapter(runner=runner)
        result = adapter.fetch({"id": "A1"})
        self.assertEqual(result["code"], "adapter_rc_nonzero")
        self.assertIn("permission denied", result["error"])


class Integration(Base):
    def test_cli_run_refuses_a_second_instance_and_reports_the_lock(self):
        args = ["--state-dir", str(self.state), "--source", "none",
                "--allowlist", str(self.allow_path)]
        holder = PidLock(self.state / "engine.pid")
        self.assertTrue(holder.try_acquire()[0])
        try:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = cli.main(args + ["run", "--interval", "5", "--max-rounds", "1"])
            self.assertEqual(rc, 3)
            self.assertIn("refused to start", err.getvalue())
            self.assertIn("engine.pid", err.getvalue())
        finally:
            holder.release()
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(args + ["run", "--interval", "0.1", "--max-rounds", "1"])
        self.assertEqual(rc, 0)

    def test_bad_config_blocks_external_actions(self):
        engine = self.engine()
        engine.dispatch_enabled = True
        # Break the allowlist, then let a round run.
        self.allow_path.write_text('{"schema_version": 1, "projects": 5}',
                                   encoding="utf-8")
        engine.run_round()
        self.assertEqual(engine.store.load_counters()["external_actions"], 0)
        out = engine._dispatch({"kind": "bus_inject", "event": "event.agent.x",
                                "issue": 28, "payload": {}})
        self.assertFalse(out["dispatched"])
        self.assertEqual(engine.store.load_counters()["external_actions"], 0)


# --------------------------------------------------------------------- ISS-28 fix
class StartupRecovery(Base):
    """P1-1: recovery is a production entry again, and it never re-sends."""

    def crash_then_restart(self, *, probe_found=None, bus=None):
        """Run a child that dies while in_flight, then recover in this process.

        The child is the real ``cli.main(["once"])`` path against an isolated
        state dir, so the ``in_flight`` record is produced by production code,
        not by a hand-written file.
        """
        script = (
            "import os, sys\n"
            "sys.path.insert(0, r'%s')\n"
            "from super_agent import config as c\n"
            "from super_agent.bus_client import BusClient\n"
            "from super_agent.engine import Engine\n"
            "from super_agent.github_source import NullSource\n"
            "from super_agent.store import Store, action_id\n"
            "cfg = c.load(None, {'github': {'source': 'none'}, 'mode': 'assist'})\n"
            "store = Store(r'%s')\n"
            "engine = Engine(cfg, store, NullSource(), None)\n"
            "engine.dispatch_enabled = True\n"
            "aid = action_id('A1', 'bus_inject', '28')\n"
            "store.begin_action(aid, verb='bus_inject', assignment_id='A1',"
            " input_ref='28')\n"
            "store.bump_external_actions()\n"
            "store.mark_in_flight(aid)\n"
            "print('in_flight', aid)\n"
            "sys.stdout.flush()\n"
            "os._exit(9)\n" % (ROOT, self.state))
        done = subprocess.run([sys.executable, "-c", script],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 9, done.stderr)
        self.assertIn("in_flight", done.stdout)

        before = self.state_counter()
        store = Store(self.state)
        self.assertEqual([r["status"] for r in store.unfinished_outbox().values()],
                         ["in_flight"])

        engine = Engine(config.load(None, {"github": {"source": "none"},
                                           "mode": "assist"}),
                        Store(self.state), NullSource(), bus)
        engine.dispatch_enabled = True
        probe = None
        if probe_found is not None:
            probe = lambda rec: {"found": probe_found, "comment_id": 4242,
                                 "detail": "remote side matched"}
        out = engine.startup(probe=probe)
        return engine, out, before

    def state_counter(self) -> int:
        return int(Store(self.state).load_counters()["external_actions"])

    def test_restart_does_not_resend_and_marks_uncertain(self):
        engine, out, before = self.crash_then_restart()
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["replayed"], [], "in_flight must never be replayed")
        # P1-1 counter check: the crash-then-restart adds nothing.
        self.assertEqual(self.state_counter(), before)
        # No probe => waiting_person, and it is still on disk afterwards.
        self.assertEqual([r["outcome"] for r in out["reconciled"]],
                         ["waiting_person"])
        waiting = engine.store.waiting_person()
        self.assertEqual(len(waiting), 1)
        self.assertEqual(waiting[0]["status"], "waiting_person")
        self.assertEqual([r["status"] for r in engine.store.reconcile_queue()], [])

    def test_restart_with_a_probe_marks_the_action_done(self):
        engine, out, before = self.crash_then_restart(probe_found=True)
        self.assertEqual([r["outcome"] for r in out["reconciled"]], ["done"])
        self.assertEqual(self.state_counter(), before, "reconciliation resends nothing")
        self.assertEqual(engine.store.waiting_person(), [])
        self.assertEqual(engine.store.unfinished_outbox(), {})

    def test_recovery_failure_degrades_health_and_refuses_to_run(self):
        engine = self.engine()
        engine.dispatch_enabled = True
        self.state.mkdir(parents=True, exist_ok=True)
        engine.store.begin_action("A1:bus_inject:28", verb="bus_inject",
                                  assignment_id="A1", input_ref="28")
        engine.store.mark_in_flight("A1:bus_inject:28")

        def boom(*_a, **_kw):
            raise OSError("disk went away")

        engine.store.recover_outbox = boom
        out = engine.startup()
        self.assertFalse(out["ok"])
        self.assertIn("disk went away", out["error"])
        snapshot = health.read(self.state / "health.json")
        self.assertTrue(snapshot["degraded"])
        self.assertIn("disk went away", snapshot["last_error"])

        # The CLI must not start a round after a failed recovery.
        out_stream, err_stream = io.StringIO(), io.StringIO()
        cfg, store = cli._build(type("A", (), {
            "config": None, "state_dir": str(self.state), "mode": "observe",
            "role": None, "source": "none", "allowlist": str(self.allow_path)})())
        broken = Engine(cfg, store, NullSource(), None)
        broken.store.recover_outbox = boom
        with contextlib.redirect_stdout(out_stream), \
                contextlib.redirect_stderr(err_stream):
            rc = cli._recovery_refusal(cli._recover(broken))
        self.assertEqual(rc, 1)
        self.assertIn("recovery failed", err_stream.getvalue())

    def test_replay_prepared_reuses_the_recorded_attempt(self):
        self.allow_bus_inject()
        engine = self.engine()
        engine.dispatch_enabled = False          # prepared stays prepared
        self.state.mkdir(parents=True, exist_ok=True)
        store = engine.store
        store.begin_action("A1:bus_inject:28", verb="bus_inject",
                           assignment_id="A1", input_ref="28", issue=28)
        engine.dispatch_enabled = True
        out = engine.startup()
        self.assertEqual(len(out["replayed"]), 1)
        self.assertTrue(out["replayed"][0]["dispatched"])
        # Replay reused the same key, so no ``#2`` attempt was created; the
        # failed replay went uncertain, then startup's reconcile (no probe)
        # left it waiting for a person.
        record = store.outbox_action("A1:bus_inject:28")
        self.assertEqual(record["status"], "waiting_person")
        self.assertEqual(record["attempt"], 1)
        self.assertEqual([r["outcome"] for r in out["reconciled"]],
                         ["waiting_person"])


class WaitingPersonVisibility(Base):
    """P1-2: waiting_person is visible in state, in doctor, and after rotation."""

    def make_waiting(self, store=None) -> str:
        store = store or Store(self.state)
        aid = store_mod.action_id("W1", "bus_inject", "28")
        store.begin_action(aid, verb="bus_inject", assignment_id="W1",
                           input_ref="28")
        store.mark_in_flight(aid)
        store.mark_uncertain(aid, "timeout")
        store.mark_reconciling(aid)
        store.mark_reconciled(aid, "waiting_person", "no probe configured")
        return aid

    def test_state_file_and_doctor_both_show_it(self):
        aid = self.make_waiting()
        raw = json.loads((self.state / "outbox_state.json").read_text("utf-8"))
        self.assertIn(aid, raw["actions"])
        self.assertEqual(raw["actions"][aid]["status"], "waiting_person")

        engine = self.engine(bus=FakeBus())
        report = engine.doctor()
        check = next(c for c in report["checks"] if c["name"] == "waiting-person")
        self.assertEqual(check["level"], "fail")
        self.assertEqual(check["label"], "waiting-person")
        self.assertIn(aid, check["detail"])
        # It must not be mixed into outbox-stuck.
        outbox = next(c for c in report["checks"] if c["name"] == "outbox")
        self.assertEqual(outbox["level"], "ok")

    def test_rotation_does_not_drop_it(self):
        store = Store(self.state, max_outbox_lines=3)
        aid = self.make_waiting(store)
        for i in range(12):
            store.log_outbox({"at": "t", "action_id": f"done-{i}", "status": "done"})
        self.assertTrue((self.state / "outbox.jsonl.1").is_file())
        raw = json.loads((self.state / "outbox_state.json").read_text("utf-8"))
        self.assertIn(aid, raw["actions"], "rotation must not drop waiting_person")
        engine = self.engine()
        engine.store.max_outbox_lines = 3
        check = next(c for c in engine.doctor()["checks"]
                     if c["name"] == "waiting-person")
        self.assertIn(aid, check["detail"])

    def test_doctor_cli_prints_the_waiting_person_label(self):
        aid = self.make_waiting()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = cli.main(["--state-dir", str(self.state), "doctor",
                           "--allowlist", str(self.allow_path),
                           "--source", "none"])
        text = out.getvalue()
        self.assertEqual(rc, 1, text)
        self.assertIn("waiting-person", text)
        self.assertIn(aid, text)


class ActionIdNoOverwrite(Base):
    """P1-3: a second attempt gets a new id and never overwrites ``uncertain``."""

    def test_second_begin_creates_a_new_id_and_keeps_the_first(self):
        store = Store(self.state)
        base = store_mod.action_id(None, "send", "ref-1")
        first = store.begin_action(base, verb="send", input_ref="ref-1")
        store.mark_in_flight(first["action_id"])
        store.mark_uncertain(first["action_id"], "timeout")
        self.assertEqual(store.outbox_action(first["action_id"])["status"],
                         "uncertain")

        second = store.begin_action(base, verb="send", input_ref="ref-1")
        self.assertNotEqual(second["action_id"], first["action_id"])
        self.assertEqual(second["action_id"], base + "#2")
        statuses = {k: v["status"] for k, v in store.unfinished_outbox().items()}
        self.assertEqual(statuses[first["action_id"]], "uncertain")
        self.assertEqual(statuses[second["action_id"]], "prepared")

    def test_dispatch_twice_keeps_the_first_uncertain(self):
        self.allow_bus_inject()
        engine = self.engine(bus=None)             # no bus => first one is uncertain
        engine.dispatch_enabled = True
        action = {"kind": "bus_inject", "event": "event.agent.x", "issue": 28,
                  "input_ref": "28", "payload": {}}
        first = engine._dispatch(action)
        self.assertFalse(first["ok"])
        first_id = engine.store.outbox_action(
            store_mod.action_id(None, "bus_inject", "28"))["action_id"]
        second = engine._dispatch(action)
        self.assertFalse(second["ok"])
        records = engine.store.unfinished_outbox()
        self.assertEqual(len(records), 2)
        self.assertEqual(records[first_id]["status"], "uncertain")
        self.assertEqual(records[first_id + "#2"]["status"], "uncertain")
        self.assertEqual(engine.store.load_counters()["external_actions"], 2)

    def test_attempt_counter_survives_a_restart(self):
        store = Store(self.state)
        base = store_mod.action_id("A1", "comment", "c")
        store.begin_action(base, verb="comment")
        store.mark_done(base)
        # The base record is gone, but the counter must still move forward.
        third = Store(self.state).begin_action(base, verb="comment")
        self.assertEqual(third["action_id"], base + "#2")


class PidCmdlineFallback(Base):
    """Same-block item 1: the CIM fallback makes pid reuse detectable."""

    def test_cim_reports_this_process_command_line(self):
        text = pidlock_mod._pid_cmdline(os.getpid())
        self.assertIsInstance(text, str)
        self.assertTrue(text.strip())
        self.assertIn("python", text.lower())

    def test_cmdline_matches_is_false_for_a_foreign_identity(self):
        self.assertFalse(
            pidlock_mod._cmdline_matches(os.getpid(), "deadbeefdeadbeef"))

    def test_cmdline_matches_is_true_for_the_recorded_identity(self):
        text = pidlock_mod._pid_cmdline(os.getpid())
        recorded = pidlock_mod._cmdline_hash(text.split())
        self.assertTrue(pidlock_mod._cmdline_matches(os.getpid(), recorded))

    def test_reused_pid_is_recognised_as_stale(self):
        path = self.state / "engine.pid"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": os.getpid(), "started_at": "t",
                                    "cmdline_hash": "0000000000000000"}),
                        encoding="utf-8")
        lock = PidLock(path)
        self.assertTrue(lock.is_stale())
        ok, _info = lock.try_acquire()
        self.assertTrue(ok)
        self.assertTrue(lock.took_over)
        lock.release()

    def test_wmic_is_skipped_only_after_the_cim_probe(self):
        calls = []
        real_wmic = pidlock_mod._wmic_cmdline
        real_cim = pidlock_mod._cim_cmdline

        def fake_wmic(pid):
            calls.append("wmic")
            return None

        def fake_cim(pid):
            calls.append("cim")
            return "python -m super_agent.cli run"

        pidlock_mod._wmic_cmdline = fake_wmic
        pidlock_mod._cim_cmdline = fake_cim
        try:
            self.assertEqual(pidlock_mod._pid_cmdline(1234),
                             "python -m super_agent.cli run")
        finally:
            pidlock_mod._wmic_cmdline = real_wmic
            pidlock_mod._cim_cmdline = real_cim
        self.assertEqual(calls, ["wmic", "cim"])


class AttemptCounters(Base):
    """Per-attempt counters: readable before a send, prunable after a terminal."""

    def test_attempted_is_zero_until_an_attempt_exists(self):
        store = Store(self.state)
        base = store_mod.action_id("asg_1_worker_1", "bus_inject", "9001")
        self.assertEqual(store.action_attempted(base), 0)
        store.begin_action(base, verb="bus_inject", kind="bus_inject",
                           assignment_id="asg_1_worker_1", input_ref="9001")
        self.assertEqual(store.action_attempted(base), 1)
        store.begin_action(base, verb="bus_inject", kind="bus_inject",
                           assignment_id="asg_1_worker_1", input_ref="9001")
        self.assertEqual(store.action_attempted(base), 2)

    def test_counter_survives_done_and_is_read_only(self):
        store = Store(self.state)
        base = store_mod.action_id("asg_1_worker_2", "bus_inject", "9002")
        rec = store.begin_action(base, verb="bus_inject", input_ref="9002")
        store.mark_done(rec["action_id"])
        self.assertIsNone(store.outbox_action(rec["action_id"]))
        self.assertEqual(store.action_attempted(base), 1)

    def test_prune_attempts_only_touches_its_own_prefix(self):
        store = Store(self.state)
        keep = store_mod.action_id("asg_1_worker_3", "bus_inject", "9003")
        drop = store_mod.action_id("asg_1_worker_4", "bus_inject", "9004")
        for base in (keep, drop):
            store.begin_action(base, verb="bus_inject", input_ref="1")
        pruned = store.prune_attempts("asg_1_worker_4:")
        self.assertEqual(pruned, [drop])
        self.assertEqual(store.action_attempted(drop), 0)
        self.assertEqual(store.action_attempted(keep), 1)
        self.assertEqual(store.prune_attempts("asg_1_worker_4:"), [])


class AllowlistSummary(Base):
    """Same-block item 2: summary agrees with poll()."""

    def test_bad_reload_reports_using_previous_in_both(self):
        gate = ProjectAllowlist(self.allow_path)
        self.assertTrue(gate.check("demo", 28, "collect")[0])
        self.assertFalse(gate.summary()["using_previous"])

        self.allow_path.write_text(json.dumps(
            {**GOOD, "projects": {"demo": {**GOOD["projects"]["demo"],
                                           "issues": "28"}}}), encoding="utf-8")
        result = gate.poll()
        self.assertEqual(result["used"], "previous")
        self.assertIn("issues", result["error"])
        summary = gate.summary()
        self.assertTrue(summary["using_previous"])
        self.assertTrue(summary["valid"])

        fixed = json.loads(json.dumps(GOOD))
        fixed["projects"]["demo"]["issues"] = [28, 29]
        self.allow_path.write_text(json.dumps(fixed), encoding="utf-8")
        self.assertEqual(gate.poll()["used"], "new")
        self.assertFalse(gate.summary()["using_previous"])


class ShippedAllowlist(Base):
    """Same-block item 3: assist can really dispatch one action.

    The shipped file names a **test issue** rather than a fixed one (the probe
    target is ``#1``), and the real repository name lives in an untracked
    overlay. The assertions therefore read the shipped issue number instead of
    hard-coding it, and never assume the overlay exists.
    """

    def shipped(self) -> ProjectAllowlist:
        return ProjectAllowlist(ROOT / "config" / "allowlist.json")

    def shipped_issue(self) -> int:
        gate = self.shipped()
        return int(gate.data["projects"]["superagent"]["issues"][0])

    def test_shipped_allowlist_permits_bus_inject(self):
        shipped = json.loads((ROOT / "config" / "allowlist.json").read_text("utf-8"))
        actions = shipped["projects"]["superagent"]["actions"]
        self.assertIn("bus_inject", actions)
        gate = self.shipped()
        self.assertTrue(gate.check("superagent", self.shipped_issue(), "bus_inject")[0])

    def test_overlay_is_ignored_when_absent_and_wins_when_present(self):
        """The overlay may override ``repo`` only; the tracked policy stands."""
        allow = self.root / "tracked.json"
        allow.write_text(json.dumps(GOOD), encoding="utf-8")
        self.assertEqual(ProjectAllowlist(allow).data["projects"]["demo"]["repo"],
                         "owner/demo")

        overlay = self.root / "tracked.local.json"
        overlay.write_text(json.dumps({"projects": {"demo": {
            "repo": "someone/else", "creation_mode": "engine_preapproved"}}}),
            encoding="utf-8")
        gate = ProjectAllowlist(allow)
        entry = gate.data["projects"]["demo"]
        self.assertEqual(entry["repo"], "someone/else")
        # Not in OVERLAY_FIELDS: inherited, so the shipped gate is unchanged.
        self.assertEqual(entry["creation_mode"], "pm_created")
        self.assertEqual(entry["issues"], [28])
        self.assertEqual(gate.summary()["effective_from"]["demo"], "local")
        self.assertIn("local", gate.source_label())

    def test_broken_overlay_is_refused_and_tracked_copy_stays(self):
        allow = self.root / "tracked.json"
        allow.write_text(json.dumps(GOOD), encoding="utf-8")
        (self.root / "tracked.local.json").write_text(
            json.dumps({"projects": {"demo": {"issues": ["28"]}}}), encoding="utf-8")
        gate = ProjectAllowlist(allow)
        self.assertTrue(gate.data is not None)
        self.assertEqual(gate.data["projects"]["demo"]["issues"], [28])
        self.assertIn("issues", gate.summary()["error"])

    def test_assist_mode_dispatches_once_with_a_stub_bus(self):
        bus = FakeBus()
        shipped = self.shipped()
        issue = self.shipped_issue()
        engine = Engine(config.load(None, {"github": {"source": "none"},
                                           "mode": "assist"}),
                        Store(self.state), NullSource(), bus,
                        project_allowlist=shipped)
        self.assertTrue(engine.dispatch_enabled, "assist enables dispatch")
        out = engine._dispatch({"kind": "bus_inject",
                                "event": "event.agent.needs_person",
                                "issue": issue, "input_ref": str(issue),
                                "payload": {}})
        self.assertTrue(out["dispatched"], out)
        self.assertTrue(out["ok"], out)
        self.assertEqual(engine.store.load_counters()["external_actions"], 1)
        self.assertEqual(len(bus.injected), 1)


if __name__ == "__main__":
    unittest.main()
