"""L1 tests for the injected monitor port (ISS-25 item 5).

The original workbench script kept its workspace root, daemon heartbeat,
PowerShell bridge, session probe and alarm recipients as implicit imports.
The package port must instead take every one of those as an explicit
injection. These tests pin the hard behaviours that must survive the move:
no-change means no log line, a None bus result falls back to direct, the
heartbeat runs every tick, a missing alarm dependency is loud, role names are
configurable, the source is neutral, and the import chain stays in-package.

Run: ``python -m pytest tests/test_monitor.py -q``
"""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from super_agent import monitor

ROOT = Path(__file__).resolve().parent.parent
PRIVATE = ("re" + "ed", "lai" + "ke", "yuan" + "hai",
           "ming" + "hangg", "Users/m" + "ingh", "Users\\m" + "ingh")
LEGACY = "Lai" + "ke_work_with_agent"


def make_config(root: Path, **overrides):
    values = {
        "state_root": root,
        "workspace_root": root,
        "heartbeat": lambda name, payload: None,
        "ps_json": lambda script, timeout: (0, None, ""),
        "probe_session": lambda sid: (True, {}),
        "wire_root": lambda: root / "sessions",
        "live_root": root,
        "session_script": root / "sess.py",
        "alarm_check": False,
    }
    values.update(overrides)
    return monitor.MonitorConfig(**values)


class Injection(unittest.TestCase):
    def test_default_role_is_supervisor_and_can_be_overridden(self):
        self.assertEqual(monitor.MonitorConfig().supervisor_role, "supervisor")
        config = monitor.MonitorConfig(supervisor_role="duty-owner")
        self.assertEqual(monitor.Monitor(config).config.supervisor_role, "duty-owner")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = monitor.Monitor(make_config(Path("C:/tmp/iss25_5/full"))).self_check()
        self.assertEqual(rc, 0)
        self.assertIn("role=supervisor", out.getvalue())

    def test_missing_injections_are_visible_and_self_check_can_go_red(self):
        mon = monitor.Monitor()
        missing = mon.missing_injections()
        for name in ("state_root", "workspace_root", "heartbeat", "ps_json",
                     "probe_session", "wire_root", "live_root", "session_script"):
            self.assertIn(name, missing)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(mon.self_check(), 2)
        self.assertIn("未注入", err.getvalue())

    def test_missing_alarm_internal_dependency_raises(self):
        broken = object()
        with self.assertRaises(ModuleNotFoundError):
            monitor.Monitor(make_config(Path("C:/tmp/iss25_5/alarm"),
                                        alarm_module=broken))._load_alarm_module()
        proc = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.modules['super_agent.session_health'] = None; "
             "from super_agent import monitor; "
             "m = monitor.Monitor(monitor.MonitorConfig(alarm_module=monitor.alarm_mod)); "
             "exec('try:\\n m._load_alarm_module()\\n"
             "except Exception as e:\\n print(type(e).__name__)\\n"
             "else:\\n print(\\\"swallowed\\\")')"],
            cwd=str(ROOT), capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
        self.assertIn("ModuleNotFoundError", proc.stdout)


class Snapshot(unittest.TestCase):
    def setUp(self):
        parent = Path("C:/tmp/iss25_5")
        parent.mkdir(parents=True, exist_ok=True)  # 靶目录可能被当轮即删清理，setUp 自建保独立
        self.tmp = tempfile.TemporaryDirectory(prefix="iss25_5_", dir=str(parent))
        self.root = Path(self.tmp.name)
        self.config = make_config(self.root)
        self.mon = monitor.Monitor(self.config)
        for rel in monitor.WATCH_DIRS:
            (self.root / rel).mkdir(parents=True, exist_ok=True)
        (self.root / "sessions.json").write_text(
            json.dumps({"qa": {"id": "qa-sid"}, "supervisor": {"id": "sv-sid"}}),
            encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def items(self, busy=False):
        return [{"id": "qa-sid", "title": "qa", "busy": busy, "_src": "bus"},
                {"id": "sv-sid", "title": "supervisor", "busy": False, "_src": "bus"}]

    def test_no_change_writes_zero_new_lines(self):
        with mock.patch.object(monitor.BUSD, "list_envelopes",
                               return_value=self.items()):
            first, src = self.mon.tick(None)
            self.assertEqual(src, "bus")
            self.assertEqual(self.mon.log_path().read_text(encoding="utf-8").count("\n"), 1)
            second, _ = self.mon.tick(first)
            self.assertEqual(second, first)
            self.assertEqual(self.mon.log_path().read_text(encoding="utf-8").count("\n"), 1)

    def test_bus_none_falls_back_to_direct(self):
        payload = {"data": {"items": self.items()}}
        with mock.patch.object(monitor.BUSD, "list_envelopes", return_value=None), \
             mock.patch.object(monitor.K, "get", return_value=(200, payload)) as get, \
             mock.patch.object(monitor.K, "data_of", return_value=payload["data"]):
            items, err, src = self.mon.fetch_sessions()
        self.assertIsNone(err)
        self.assertEqual(src, "direct")
        self.assertEqual(len(items), 2)
        get.assert_called_once_with("/api/v1/sessions?page_size=30", timeout=8)

    def test_heartbeat_callable_runs_every_tick(self):
        calls = []
        config = make_config(self.root, heartbeat=lambda name, payload: calls.append(name))
        mon = monitor.Monitor(config)
        with mock.patch.object(monitor.BUSD, "list_envelopes",
                               return_value=self.items()):
            line, _ = mon.tick(None)
            mon.tick(line)
        self.assertEqual(calls, ["monitor", "monitor"])

    def test_snapshot_distinguishes_idle_busy_gone(self):
        with mock.patch.object(monitor.BUSD, "list_envelopes",
                               return_value=[{"id": "qa-sid", "busy": False, "_src": "bus"}]):
            line, flags, _ = self.mon.snapshot()
        self.assertFalse(flags["qa"])
        self.assertTrue(flags["supervisor"] is None)
        self.assertIn("qa=idle", line)
        self.assertIn("supervisor=gone", line)

    def test_dry_run_startup_does_not_write_or_delete(self):
        with mock.patch.dict("os.environ", {"APPDATA": str(self.root / "appdata")}):
            self.assertEqual(self.mon.startup_install(dry_run=True), monitor.EXIT_OK)
            self.assertEqual(self.mon.startup_uninstall(dry_run=True), monitor.EXIT_OK)
        target = self.root / "appdata/Microsoft/Windows/Start Menu/Programs/Startup/KimiMonitor.vbs"
        self.assertFalse(target.exists())


class Source(unittest.TestCase):
    def test_source_has_no_private_literals(self):
        for path in (ROOT / "super_agent" / "monitor.py", Path(__file__)):
            text = path.read_text(encoding="utf-8").lower()
            for token in PRIVATE:
                self.assertNotIn(token.lower(), text, f"{path.name}: {token}")

    def test_relative_imports_and_no_absolute_legacy_chain(self):
        text = (ROOT / "super_agent" / "monitor.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(sum(1 for ln in text.splitlines()
                                    if ln.startswith("from . import ")), 5)
        self.assertIsNone(__import__("re").search(
            r"^(?:import|from)\s+(?:kimi_|super_agent)", text, __import__("re").M))
        self.assertNotIn(LEGACY, text)

    def test_import_chain_has_no_workbench_path_or_top_level_modules(self):
        proc = subprocess.run(
            [sys.executable, "-c",
             "import sys, super_agent.monitor as m; "
             "assert 'super_agent.bus' in sys.modules; "
             "assert 'super_agent.direct' in sys.modules; "
             "assert 'super_agent.paths' in sys.modules; "
             "assert 'super_agent.autostart' in sys.modules; "
             "assert 'super_agent.session_alarm' in sys.modules; "
             "assert not [x for x in sys.modules if x.startswith('kimi_')]; "
             "print([x for x in sys.modules if x.startswith('kimi_')])"],
            cwd=str(ROOT), capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
        self.assertEqual(proc.stdout.strip(), "[]")
        self.assertNotIn(LEGACY, str(Path(monitor.__file__).resolve()))


if __name__ == "__main__":
    unittest.main()
