"""L1 tests for the injectable decision/delivery watch port (ISS-25 item 7).

The original workbench script coupled its state machine to a private repo root,
a local event daemon, a session client and a set of fixed runtime files. The
package port must take all of those as explicit injections, must never silently
reach production, and must keep the original 22-case self-test green.

Run: ``python -m pytest tests/test_decide_watch.py -q``
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
        args = SimpleNamespace(dry_run=True, wait=0, once=True, max_age=1800,
                               supervisor="session_supervisor_fixture", reed=None)
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

    def test_package_default_self_test_is_22_of_22(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = dw.self_test()
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("--self-test: A–T＋SM 22/22 通过", out.getvalue())
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


class ShimFallback(Fixture):
    def test_inner_dependency_missing_must_raise_out_of_loader(self):
        code = (
            "import sys, types, importlib\n"
            "class Blocker:\n"
            "    def find_module(self, fullname, path=None):\n"
            "        if fullname.startswith('super_agent') and fullname != 'super_agent':\n"
            "            return self\n"
            "    def load_module(self, fullname):\n"
            "        raise ModuleNotFoundError(\"inner missing\", name=fullname)\n"
            "sys.meta_path.insert(0, Blocker())\n"
            "try:\n"
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
        lwa_tools = Path(r"D:/github/laike_work_with_agent/tools")
        shim = lwa_tools / "decide_watch.py"
        legacy = lwa_tools / "decide_watch_legacy.py"
        if not shim.is_file() or not legacy.is_file():
            self.skipTest("phase B shim not present yet")
        code = (
            "import sys\n"
            "class Blocker:\n"
            "    def find_module(self, fullname, path=None):\n"
            "        if fullname == 'super_agent' or fullname.startswith('super_agent.'):\n"
            "            return self\n"
            "    def load_module(self, fullname):\n"
            "        raise ModuleNotFoundError(\"package missing\", name='super_agent')\n"
            "sys.meta_path.insert(0, Blocker())\n"
            "sys.path.insert(0, %r)\n"
            "import decide_watch\n"
            "print('LEGACY', decide_watch.__file__)\n"
            "print('SELFTEST', hasattr(decide_watch, 'self_test'))\n"
        ) % str(lwa_tools)
        proc = subprocess.run([sys.executable, "-c", code], cwd=str(lwa_tools.parent),
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("decide_watch_legacy", proc.stdout)
        self.assertIn("SELFTEST True", proc.stdout)


if __name__ == "__main__":
    unittest.main()
