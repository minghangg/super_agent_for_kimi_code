"""L1 tests for the injectable decision/delivery watch port (ISS-25 item 7).

The original workbench script coupled its state machine to a private repo root,
a local event daemon, a session client and a set of fixed runtime files. The
package port must take all of those as explicit injections, must never silently
reach production, and must keep the self-test green (30 cases after ISS-83 added 7).

Target interpreters: **3.10 and 3.13**, both green. The import blockers below use
``find_spec`` (PEP 451, Python 3.4+) and never ``find_module``/``load_module``:
CPython 3.12 removed the legacy meta-path protocol, and a 3.13 run silently
*ignores* a legacy finder, so the import succeeds and the blocker proves nothing
(ISS-136 §一; measured ``1 failed`` on 3.13 against ``25 passed`` on 3.10).

Run: ``python -m pytest tests/test_decide_watch.py -q``
      ``./.venv/Scripts/python.exe -m pytest tests/test_decide_watch.py -q``  (3.13)
      ``D:/myenv/Scripts/python.exe -m pytest tests/test_decide_watch.py -q``  (3.10)
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from super_agent import decide_watch as dw

# ``tests/`` has no ``__init__.py``, so pytest puts it on ``sys.path``: the shared
# blocker is imported as a sibling module, not as ``tests.support_import_blocker``.
from support_import_blocker import blocker_source

ROOT = Path(__file__).resolve().parent.parent
PRIVATE = ("re" + "ed", "lai" + "ke", "yuan" + "hai",
           "ming" + "hangg", "Users/m" + "ingh", "Users\\m" + "ingh")
LEGACY = "Lai" + "ke_work_with_agent"


class Bus:
    PORT = 58800

    def __init__(self, events=None, seq=1, oldest=1):
        self.events = list(events or [])
        self.seq = seq
        self.oldest = oldest
        self.health_calls = []

    def guardian_thresholds(self):
        """ISS-55 ③：生产里这条注册由主管亲手写进 state/guardian_extra.json；
        夹具在同一个位置给出一条同名注册，测试才能走到轮询主体。"""
        return {
            "delivery_watch": ("heartbeat/delivery.json", 900.0, "收单哨"),
            "decide_watch": ("heartbeat/decide.json", 900.0, "主循环"),
            "supervisor_notice": ("heartbeat/supervisor_notice.json", 900.0, "投递哨"),
        }

    def bus_get(self, path, timeout=0, port=0):
        return {"events": list(self.events), "next_seq": self.seq,
                "oldest_seq": self.oldest, "boot_id": "isolated"}

    def bus_health(self, port=0):
        self.health_calls.append(port)
        return {"boot_id": "isolated", "seq": self.seq}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="iss25_7_")
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.ws = self.root / "ws"
        self.fixtures = self.root / "fixtures"
        for directory in (self.state, self.ws, self.fixtures):
            directory.mkdir(parents=True, exist_ok=True)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: dw.configure(dw.DecideWatchConfig()))
        (self.fixtures / "sessions.json").write_text(json.dumps({
            "pm-neutral_project": {"id": "session_pm_fixture"},
            "supervisor": {"id": "session_supervisor_fixture"},
            "steward": {"id": "session_steward_fixture"},
        }, ensure_ascii=False), encoding="utf-8")
        (self.fixtures / "issue_sessions.json").write_text("{}", encoding="utf-8")
        with (self.fixtures / "ledger.jsonl").open("w", encoding="utf-8") as fh:
            for i in range(3):
                fh.write(json.dumps({
                    "t": "2026-11-09T06:00:00", "role": "worker",
                    "act": "comment", "repo": "owner/neutral_project",
                    "issue": i + 1, "ok": True, "note": f"cid:fixture-{i}",
                }, ensure_ascii=False) + "\n")
        self.beats, self.injections, self.sends = [], [], []
        self.bus = Bus()
        self.config = dw.DecideWatchConfig(
            state_root=self.state, workspace_root=self.ws,
            session_script=self.ws / "session_client.py",
            event_source=self.bus,
            heartbeat=lambda name, extra: self.beats.append((name, extra)),
            inject_event=lambda path, body, port=0: self.injections.append((path, body)),
            ps_json=lambda script, timeout=60.0: (0, {}, ""),
            windowless_python=lambda: (sys.executable, ""),
            probe_session=lambda sid: (True, {"sid": sid}),
            send_transport=self._transport,
            self_test_root=self.root / "selftest",
            fixture_sessions_path=self.fixtures / "sessions.json",
            fixture_bind_path=self.fixtures / "issue_sessions.json",
            fixture_ledger_path=self.fixtures / "ledger.jsonl",
        )
        dw.configure(self.config)

    def _transport(self, sid, text, timeout=60.0):
        self.sends.append((sid, text))
        return subprocess.CompletedProcess([], 0, "", "")

    def reset_delivery_state(self, *, offset=None, established=True):
        state = dw._default_state()
        state["baseline_established"] = established
        state["offset"] = offset
        dw.save_delivery_state(state)
        return state

    def worker_row(self, issue=999, **kw):
        row = {"t": "2026-11-09T06:00:00", "role": "worker", "act": "comment",
               "repo": "owner/neutral_project", "issue": issue, "ok": True,
               "note": ""}
        row.update(kw)
        return row

    def append_ledger(self, row):
        with dw._delivery_paths()["ledger"].open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


class InjectionBoundary(Fixture):
    def test_source_has_no_private_literals_or_workbench_imports(self):
        text = Path(dw.__file__).read_text(encoding="utf-8")
        for token in PRIVATE:
            self.assertNotIn(token, text.lower())
        self.assertNotIn(LEGACY, text)
        for forbidden in ("import wss_bus", "import sess ", "sys.path.append",
                          "Path.home()", 'BASE / "state"', "auth/"):
            self.assertNotIn(forbidden, text)
        self.assertNotIn("D:/", text)
        self.assertEqual(Path(dw.__file__).resolve().parent.name, "super_agent")

    def test_import_chain_has_no_workbench_modules(self):
        proc = subprocess.run(
            [sys.executable, "-c",
             "import sys; import super_agent.decide_watch; "
             "print('lwa', 'wss_bus' in sys.modules, 'sess' in sys.modules, "
             "'ctx' in sys.modules)"],
            cwd=str(ROOT), capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "lwa False False False")

    def test_no_injection_is_parameter_error_and_no_production_access(self):
        dw.configure(dw.DecideWatchConfig())
        self.assertEqual(len(dw.missing_injections()), 10)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = dw.self_check()
        self.assertEqual(rc, 2)
        self.assertIn("未注入 state_root", err.getvalue())
        err2 = io.StringIO()
        with contextlib.redirect_stderr(err2):
            cli_rc = dw.main(["--once"])
        self.assertEqual(cli_rc, 9)
        self.assertIn("拒绝运行：未注入", err2.getvalue())
        self.assertEqual(self.beats, [])
        self.assertEqual(self.injections, [])
        self.assertEqual(self.sends, [])

    def test_injected_paths_are_isolated_and_defaults_are_empty(self):
        self.assertEqual(dw._delivery_paths()["state"].parent, self.state)
        self.assertEqual(dw.SESSIONS().parent, self.state)
        self.assertEqual(dw.CURSOR().parent, self.state)
        self.assertEqual(dw.LOG().parent, self.state)
        self.assertEqual(dw.LOCK().parent, self.state)
        proc = subprocess.run(
            [sys.executable, "-c",
             "from super_agent import decide_watch as d; "
             "print(d.DEFAULT_CONFIG.state_root, d.DEFAULT_CONFIG.event_source, "
             "d.DEFAULT_CONFIG.send_transport, len(d.missing_injections()))"],
            cwd=str(ROOT), capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "None None None 10")

    def test_missing_state_root_raises_instead_of_guessing(self):
        dw.configure(dw.DecideWatchConfig())
        with self.assertRaisesRegex(RuntimeError, "拒绝静默连生产"):
            dw._state_root()
        with self.assertRaisesRegex(RuntimeError, "拒绝静默连生产"):
            dw._delivery_paths()


class Behaviour(Fixture):
    def test_first_round_does_not_send_and_cursor_advances(self):
        self.bus.events = []
        self.bus.seq = 41
        with mock.patch.object(dw, "_loop", return_value=0) as loop:
            with mock.patch.object(dw, "acquire_lock", return_value=(True, "")):
                with mock.patch.object(dw, "release_lock"):
                    rc = dw.main(["--once", "--dry-run", "--supervisor",
                                  "session_supervisor_fixture"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.sends, [])
        cursor = json.loads(dw.CURSOR().read_text(encoding="utf-8"))
        self.assertEqual(cursor["seq"], 41)
        self.assertEqual(cursor["boot_id"], "isolated")
        self.assertIn("首轮", "".join(
            p.read_text(encoding="utf-8") for p in self.state.glob("*.log")))

    def test_cursor_replay_and_boot_change_rescan(self):
        dw.save_cursor(7, "old-boot")
        self.bus.seq = 12
        self.bus.events = [{"seq": 12, "ts": 9999999999.0,
                            "type": dw.HANDOFF_EVENT, "payload": {"name": "x"}}]
        with mock.patch.object(dw, "_loop", side_effect=lambda *a: 0):
            with mock.patch.object(dw, "acquire_lock", return_value=(True, "")):
                with mock.patch.object(dw, "release_lock"):
                    dw.main(["--once", "--dry-run"])
        self.assertIn("daemon 重启", dw.LOG().read_text(encoding="utf-8"))
        cursor = json.loads(dw.CURSOR().read_text(encoding="utf-8"))
        self.assertEqual(cursor["seq"], 7)

    def test_single_instance_lock_refuses_double_start(self):
        lock = dw.LOCK()
        lock.write_text(json.dumps({"pid": os.getpid(), "started_at": "x"}),
                        encoding="utf-8")
        with mock.patch.object(dw, "_pid_alive", return_value=True):
            with mock.patch.object(dw, "_cmdline_of",
                                   return_value="python decide_watch.py"):
                ok, why = dw.acquire_lock()
        self.assertFalse(ok)
        self.assertIn("拒绝启动", why)
        self.assertIn("避免同一事件双发", why)

    def test_main_returns_two_when_lock_held(self):
        dw.LOCK().write_text(json.dumps({"pid": os.getpid(),
                                         "started_at": "x"}),
                             encoding="utf-8")
        err = io.StringIO()
        with mock.patch.object(dw, "_pid_alive", return_value=True):
            with mock.patch.object(dw, "_cmdline_of",
                                   return_value="python decide_watch.py"):
                with mock.patch.object(dw, "_loop",
                                       side_effect=AssertionError("must not loop")):
                    with contextlib.redirect_stderr(err):
                        rc = dw.main(["--once", "--dry-run"])
        self.assertEqual(rc, 2)
        self.assertIn("拒绝启动", err.getvalue())
        self.assertIn("避免同一事件双发", err.getvalue())
        self.assertEqual(self.injections, [])
        self.assertEqual(self.sends, [])
        self.assertEqual(dw.EXIT_LOCKED, 2)
        self.assertNotEqual(dw.EXIT_LOCKED, dw.EXIT_SEND_MODE_UNKNOWN)

    def test_delivery_fixture_gate_default_off_and_sink_only(self):
        for name in ("DELIVERY_FIXTURE", "DELIVERY_SEND_SINK", "DELIVERY_ALLOW_SEND"):
            os.environ.pop(name, None)
        self.assertFalse(dw.gate_fixture())
        self.assertEqual(dw.gate_sink(), "")
        self.assertFalse(dw.gate_allow_send())
        os.environ["DELIVERY_FIXTURE"] = "1"
        self.assertIn("拒绝运行", dw.gate_check())
        sink = self.root / "sink.jsonl"
        os.environ["DELIVERY_SEND_SINK"] = str(sink)
        self.assertEqual(dw.gate_check(), "")
        dw.DELIVERY_SINK = None
        with mock.patch.object(dw, "_send", side_effect=AssertionError("must not send")):
            ok = dw.delivery_notify("session_fake", "text", False)
        self.assertTrue(ok)
        self.assertTrue(sink.is_file())
        self.assertIn('"why": "fixture"', sink.read_text(encoding="utf-8"))
        for name in ("DELIVERY_FIXTURE", "DELIVERY_SEND_SINK"):
            os.environ.pop(name, None)
        self.addCleanup(setattr, dw, "DELIVERY_SINK", None)

    def test_heartbeat_written_every_delivery_round(self):
        self.reset_delivery_state(offset=0, established=True)
        dw.DELIVERY_SINK = []
        self.addCleanup(setattr, dw, "DELIVERY_SINK", None)
        for i in range(3):
            dw.delivery_tick(dry=True, now=1000000.0 + i)
        self.assertEqual(len(self.beats), 3)
        self.assertTrue(all(name == "delivery_watch" for name, _ in self.beats))

    def test_isolated_state_root_never_writes_workspace_files(self):
        self.reset_delivery_state()
        dw.DELIVERY_SINK = []
        self.addCleanup(setattr, dw, "DELIVERY_SINK", None)
        self.append_ledger(self.worker_row(951))
        dw.delivery_tick(dry=False, now=1000000.0)
        self.assertTrue((self.state / "delivery_watch_state.json").is_file())
        self.assertEqual(list(self.ws.glob("**/*")), [])
        self.assertEqual(self.sends, [])

    def test_package_default_self_test_is_33_of_33(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = dw.self_test()
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("--self-test: A–T＋V＋SM 33/33 通过", out.getvalue())
        self.assertEqual(self.sends, [])
        self.assertEqual(self.injections, [])

    def test_heartbeat_failure_is_visible_not_swallowed_by_business_try(self):
        dw.configure(replace(self.config, heartbeat=lambda name, extra: (_ for _ in ()).throw(
            ModuleNotFoundError("name 'super_agent.session_health' is not available",
                                name="super_agent.session_health"))))
        err = io.StringIO()
        with contextlib.redirect_stdout(err):
            dw._write_hb("delivery_watch", {})
        self.assertIn("heartbeat failed", err.getvalue())


class DeliveryRegression(Fixture):
    def test_missing_ledger_creates_an_empty_file_with_a_visible_log(self):
        path = dw._delivery_paths()["ledger"]
        path.unlink(missing_ok=True)
        st = dw._default_state()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(dw.scan_deliveries(st, dry=False, now=100), 0)
        self.assertTrue(path.is_file())
        self.assertEqual(path.stat().st_size, 0)
        self.assertIn("ledger_created", out.getvalue())
        self.assertEqual(st["offset"], 0)
        self.assertTrue(st["baseline_established"])

    def test_bad_state_never_replays_the_undelivered_log(self):
        rec = {"repo": "owner/neutral_project", "issue": 1,
               "evidence": "cid:old", "detected_ts": 1}
        path = dw._delivery_paths()["undelivered"]
        path.write_text(json.dumps({"t": 1, "enqueued_ts": 1, "rec": rec}) + "\n",
                        encoding="utf-8")
        cases = ("{", "{}", json.dumps({
            "v": 1, "baseline_established": True, "offset": 0,
            "seen": {}, "stats": {}}))
        for raw in cases:
            with self.subTest(raw=raw):
                dw._delivery_paths()["state"].write_text(raw, encoding="utf-8")
                st = dw.load_delivery_state()
                with mock.patch.object(dw, "_send_delivery", return_value=1) as send:
                    self.assertEqual(dw.retry_undelivered(st, dry=False, now=100), 0)
                send.assert_not_called()
                self.assertEqual(st["undelivered_offset"], path.stat().st_size)

    def test_complete_baseline_still_retries_undelivered(self):
        rec = {"repo": "owner/neutral_project", "issue": 1,
               "evidence": "cid:old", "detected_ts": 1}
        path = dw._delivery_paths()["undelivered"]
        path.write_text(json.dumps({"t": 1, "enqueued_ts": 1, "rec": rec}) + "\n",
                        encoding="utf-8")
        dw._delivery_paths()["state"].write_text(json.dumps({
            "baseline_established": True, "undelivered_offset": 0}), encoding="utf-8")
        st = dw.load_delivery_state()
        with mock.patch.object(dw, "_send_delivery", return_value=1) as send:
            self.assertEqual(dw.retry_undelivered(st, dry=False, now=100), 1)
        send.assert_called_once()

    def test_delivery_goes_only_to_the_scoped_pm(self):
        dw.SESSIONS().write_text(json.dumps({
            "pm-neutral_project": {"id": "session_pm_fixture"},
            "supervisor": {"id": "session_supervisor_fixture"},
        }), encoding="utf-8")
        targets = dw.resolve_recipients("owner/neutral_project")
        self.assertEqual([name for name, _sid in targets], ["pm-neutral_project"])
        self.assertNotEqual(targets[0][1], "session_supervisor_fixture")
        self.assertEqual(dw.resolve_recipients("owner/other_project"), [])

    def test_supervisor_snapshot_is_current_and_wss_compatible(self):
        dw._last_state_write("ok", "session_supervisor_fixture")
        row = json.loads(dw._state_file().read_text(encoding="utf-8"))
        self.assertEqual(row["state"], "ok")
        self.assertEqual(row["last_state"], "ok")
        self.assertEqual(row["sid"], "session_supervisor_fixture")
        self.assertGreater(row["ts"], 0)

    def test_all_state_writes_use_pid_local_temp_files(self):
        with mock.patch.object(dw.os, "replace") as replace:
            dw._last_state_write("ok", "session_supervisor_fixture")
            dw._notice_last_write()
            dw._liveness_state_write({})
        self.assertEqual(replace.call_count, 3)
        for call in replace.call_args_list:
            self.assertIn(f".{os.getpid()}.tmp", str(call.args[0]))

    def test_health_check_without_a_previous_state_does_not_raise(self):
        self.assertFalse(dw._state_file().exists())
        result = dw.check_supervisor_health()
        self.assertIn(result["state"], ("ok", "missing", "dead", "unknown"))
        self.assertTrue(dw._state_file().exists())

    def test_write_failure_blocks_the_next_delivery_tick(self):
        dw._DELIVERY_WRITE_FAILED = True
        try:
            with mock.patch.object(dw, "load_delivery_state") as load:
                out = dw.delivery_tick(dry=False, now=100)
            self.assertEqual(out["blocked"], "state_write_failed")
            self.assertEqual(out["sent"], 0)
            load.assert_not_called()
        finally:
            dw._DELIVERY_WRITE_FAILED = False

    def test_os_lock_allows_only_one_subprocess(self):
        code = f"""
from pathlib import Path
import sys
from super_agent import decide_watch as d
class B:
    PORT = 58800
d.configure(d.DecideWatchConfig(
    state_root=Path({str(self.state)!r}), workspace_root=Path({str(self.ws)!r}),
    event_source=B()))
d._event_health = lambda port: {{}}
ok, _ = d.acquire_lock()
print(ok, flush=True)
if ok:
    sys.stdin.readline()
"""
        first = subprocess.Popen([sys.executable, "-c", code], cwd=str(ROOT),
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        try:
            line = first.stdout.readline().strip()
            if not line:
                self.fail(first.stderr.read())
            self.assertEqual(line, "True")
            second = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(second.stdout.strip().splitlines()[-1], "False", second.stderr)
        finally:
            if first.poll() is None:
                first.stdin.write("\n")
                first.stdin.flush()
            try:
                first.wait(timeout=10)
            except subprocess.TimeoutExpired:
                first.kill()
                first.wait(timeout=10)


class ShimFallback(Fixture):
    def test_inner_dependency_missing_must_raise_out_of_loader(self):
        code = (
            "import sys, importlib\n"
            + blocker_source(
                "fullname.startswith('super_agent') and fullname != 'super_agent'",
                message="inner missing")
            + "try:\n"
            "    importlib.import_module('super_agent.decide_watch')\n"
            "except ModuleNotFoundError as e:\n"
            "    print('RAISED', e.name)\n"
            "    raise SystemExit(0)\n"
            "raise SystemExit(3)\n"
        )
        proc = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("RAISED super_agent.", proc.stdout)

    def test_whole_package_missing_falls_back_to_legacy_exports(self):
        lwa_tools = Path("D:/github/" + ("lai" + "ke") + "_work_with_agent/tools")
        shim = lwa_tools / "decide_watch.py"
        legacy = lwa_tools / "decide_watch_legacy.py"
        if not shim.is_file() or not legacy.is_file():
            self.skipTest("phase B shim not present yet")
        code = (
            "import sys\n"
            + blocker_source(
                "fullname == 'super_agent' or fullname.startswith('super_agent.')",
                name="'super_agent'", message="package missing")
            + "sys.path.insert(0, %r)\n"
            "import decide_watch\n"
            "print('LEGACY', decide_watch.LEGACY_FALLBACK_PATH)\n"
            "print('HASH', decide_watch.LEGACY_FALLBACK_HASH[:12])\n"
            "print('SELFTEST', hasattr(decide_watch, 'self_test'))\n"
        ) % str(lwa_tools)
        proc = subprocess.run([sys.executable, "-c", code], cwd=str(lwa_tools.parent),
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("decide_watch_legacy.py", proc.stdout)
        self.assertIn("HASH ", proc.stdout)
        self.assertIn("SELFTEST True", proc.stdout)


if __name__ == "__main__":
    unittest.main()
