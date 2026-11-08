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

    def test_waiting_person_is_a_terminal_reconcile_outcome(self):
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
        self.assertIsNone(store.outbox_action(aid))

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
        self.assertIsNone(engine.store.outbox_action(aid))
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


if __name__ == "__main__":
    unittest.main()
