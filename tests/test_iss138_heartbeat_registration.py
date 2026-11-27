"""#138: a missing supervisor-notice heartbeat must not kill the collection leg.

The live failure this pins (launcher pid 22620, 2026-11-26 → 27): the
``decide_watch`` leg was registered, enabled and scheduled - ``runs`` climbed
134 → 135 - but every round raised in ``C2Leg.prepare()`` because the heartbeat
name ``supervisor_notice`` was absent from ``guardian_thresholds()``. The raise
happens **before** ``delivery_tick()``, so the whole collection leg never ran
once: no scan, no heartbeat, no cursor advance, and worker deliveries stopped
reaching the PM.

The ruling (``7199016601``) chose: warn and continue, never refuse. Two halves
must hold together, and each has a test here:

* **the leg keeps working** - ``last_error`` empty, ``running`` true, so the
  feature is not traded away for a monitor detail;
* **the gap stays visible** - the reason rides on the row as
  ``heartbeat_problem`` and the name is registered with the patrol, so
  ``/healthz`` shows it instead of the leg silently disappearing.

Run: ``python -m pytest tests/test_iss138_heartbeat_registration.py -q``
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from super_agent import wss_bus
from super_agent.launcher_parts import c2
from super_agent.launcher_parts import readapi


class _Bus:
    """The bus seam ``C2Leg`` talks to: thresholds, registration, port."""

    PORT = 58800

    def __init__(self, registered: bool = False) -> None:
        self.registered = registered
        self.registers: list[tuple] = []

    def guardian_thresholds(self) -> dict:
        names = {"delivery_watch": ("heartbeat/delivery.json", 900.0, "x"),
                 "decide_watch": ("heartbeat/decide.json", 900.0, "y")}
        if self.registered:
            names["supervisor_notice"] = (
                "heartbeat/supervisor_notice.json", 900.0, "z")
        return names

    def register_guardian(self, name, heartbeat, threshold_s, writer=""):
        self.registers.append((name, heartbeat, threshold_s, writer))
        return (heartbeat, threshold_s, writer)


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def config(self, bus, *, registered: bool = False):
        """A ``DecideWatchConfig`` whose event source is the fake bus."""
        from super_agent import decide_watch as dw

        return dw.DecideWatchConfig(
            state_root=self.root, workspace_root=self.root,
            event_source=bus, heartbeat=lambda name, extra=None: None,
            inject_event=lambda *a, **k: None,
            ps_json=lambda *a, **k: "[]",
            windowless_python=lambda *a, **k: None,
            probe_session=lambda sid: None,
            send_transport=lambda *a, **k: None,
            session_script=self.root / "session_client.py",
            liveness_state_path=self.root / "worker_liveness_state.json",
            lock_path=self.root / "decide_watch.lock")


class TheLegKeepsRunning(_Base):
    """Ruling §2.2 判据 1: missing registration must not stop the leg."""

    def test_an_unregistered_heartbeat_does_not_raise(self):
        bus = _Bus(registered=False)
        leg = c2.C2Leg({}, None, bus=bus, decide_watch_config=self.config(bus))
        waits, port = leg.prepare()          # must not raise
        self.assertGreaterEqual(waits, 0)
        self.assertEqual(port, bus.PORT)

    def test_the_reason_is_recorded_but_last_error_stays_empty(self):
        """``last_error`` empty is the judge's first criterion - the leg is healthy."""
        from super_agent.runner_parts.components import ComponentHealth

        bus = _Bus(registered=False)
        leg = c2.C2Leg({}, None, bus=bus, decide_watch_config=self.config(bus))
        row = ComponentHealth(name="decide_watch", kind="thread", budget=30.0)
        leg.health_row = row
        leg.prepare()
        self.assertEqual(row.last_error, "",
                         "缺心跳名不得写 last_error：这条腿是健康的")
        self.assertIn("supervisor_notice", row.heartbeat_problem,
                      "缺失必须在行上可见，不许静默")

    def test_a_registered_name_records_no_problem(self):
        bus = _Bus(registered=True)
        leg = c2.C2Leg({}, None, bus=bus, decide_watch_config=self.config(bus))
        leg.prepare()
        self.assertEqual(leg.heartbeat_problem, "")
        self.assertEqual(bus.registers, [], "已注册时不应再补注册")


class TheGapStaysVisible(_Base):
    """Ruling §2.2 判据 3: ``/healthz`` must still show the missing heartbeat."""

    def test_the_missing_name_is_registered_with_the_patrol(self):
        bus = _Bus(registered=False)
        leg = c2.C2Leg({}, None, bus=bus, decide_watch_config=self.config(bus))
        leg.prepare()
        self.assertEqual(len(bus.registers), 1, "缺名必须补注册一次")
        name, rel, threshold, _writer = bus.registers[0]
        self.assertEqual(name, "supervisor_notice")
        self.assertEqual(rel, "heartbeat/supervisor_notice.json")
        self.assertEqual(threshold, 900.0)

    def test_the_warn_is_written_once_not_every_round(self):
        bus = _Bus(registered=False)
        leg = c2.C2Leg({}, None, bus=bus, decide_watch_config=self.config(bus))
        for _ in range(5):
            leg.prepare()
        self.assertEqual(len(bus.registers), 1,
                         "每轮都补注册＝每轮刷一条 WARN，不是一次告警")

    def test_the_problem_reaches_healthz_when_the_row_carries_it(self):
        """The row field is whitelisted, so the operator can read it over HTTP."""
        row = {"enabled": True, "running": True, "kind": "thread",
               "started": True, "last_error": "", "stalled": False, "runs": 3,
               "heartbeat_problem": "心跳名 'supervisor_notice' 未注册"}
        payload = readapi.health_payload({"pid": 1, "running": True,
                                          "phase": "running", "stalled": [],
                                          "components": {"decide_watch": row}})
        self.assertIn("supervisor_notice",
                      payload["components"]["decide_watch"]["heartbeat_problem"])

    def test_the_field_is_absent_on_a_row_without_one(self):
        """Only the leg that owns a supervisor-notice heartbeat carries the field."""
        row = {"enabled": True, "running": True, "kind": "thread",
               "started": True, "last_error": "", "stalled": False, "runs": 3}
        payload = readapi.health_payload({"pid": 1, "running": True,
                                          "phase": "running", "stalled": [],
                                          "components": {"engine": row}})
        self.assertNotIn("heartbeat_problem", payload["components"]["engine"])


class ProductionAssembly(_Base):
    """``bus=None`` is what production builds: ``launcher.py`` passes no ``bus``.

    The twelve tests above all inject a ``_Bus`` that already has
    ``register_guardian``, which is exactly why they were green while the live
    leg stayed invisible (QA P0-1, measured on ``8c448fd``). These go through the
    same ``getattr`` the production object does.
    """

    def setUp(self) -> None:
        super().setUp()
        saved = wss_bus.DEFAULT_CONFIG
        self.addCleanup(lambda: wss_bus.configure(saved))
        self.addCleanup(wss_bus._RUNTIME_GUARDIANS.clear)
        wss_bus._RUNTIME_GUARDIANS.clear()
        # ``self.root`` is the throw-away state root of a real ``wss_bus``.
        wss_bus.configure(wss_bus.WssBusConfig(
            state_root=self.root, production_state_root=self.root / "prod",
            port=58800))
        (self.root / "prod").mkdir(parents=True, exist_ok=True)

    def test_a_leg_built_without_a_bus_still_registers_the_name(self):
        """The repair must reach the module registry, not be skipped on ``None``."""
        self.assertNotIn("supervisor_notice", wss_bus.guardian_thresholds())
        leg = c2.C2Leg({}, None, decide_watch_config=self.config(wss_bus))
        leg.prepare()                        # must not raise, must not skip
        self.assertIn("supervisor_notice", wss_bus.guardian_thresholds(),
                      "bus=None 时补注册被静默跳过 ⇒ /healthz 看不到缺失")
        self.assertIn("supervisor_notice", leg.heartbeat_problem)

    def test_healthz_reports_missing_not_absent_without_a_bus(self):
        """Criterion 3 end to end: the patrol lists it and says ``missing``."""
        leg = c2.C2Leg({}, None, decide_watch_config=self.config(wss_bus))
        leg.prepare()
        summary = wss_bus._guardian_summary()
        self.assertIn("supervisor_notice", summary,
                      "名字不在注册表 ⇒ 监护表里连 missing 都不报")
        self.assertEqual(summary["supervisor_notice"]["state"], "missing")

    def test_the_problem_is_cleared_once_the_name_is_registered(self):
        """P2-2: a healed registration must not leave a permanent WARN behind."""
        from super_agent.runner_parts.components import ComponentHealth

        leg = c2.C2Leg({}, None, decide_watch_config=self.config(wss_bus))
        row = ComponentHealth(name="decide_watch", kind="thread", budget=30.0)
        leg.health_row = row
        leg.prepare()
        self.assertIn("supervisor_notice", row.heartbeat_problem)
        wss_bus.register_guardian("supervisor_notice",
                                  "heartbeat/supervisor_notice.json", 900.0)
        leg.prepare()
        self.assertEqual(leg.heartbeat_problem, "", "已注册却不回清＝永久假告警")
        self.assertEqual(row.heartbeat_problem, "")
        self.assertEqual(row.last_error, "", "清理不得改写 last_error")


class RuntimeRegistration(unittest.TestCase):
    """``wss_bus.register_guardian`` is the seam the leg uses; pin its contract."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        wss_bus.configure(wss_bus.WssBusConfig(
            state_root=self.root, production_state_root=self.root, port=58800))
        wss_bus._RUNTIME_GUARDIANS.clear()
        self.addCleanup(wss_bus._RUNTIME_GUARDIANS.clear)

    def test_a_registered_name_appears_in_the_patrol_table(self):
        self.assertNotIn("supervisor_notice", wss_bus.guardian_thresholds())
        wss_bus.register_guardian("supervisor_notice",
                                  "heartbeat/supervisor_notice.json", 900.0)
        self.assertIn("supervisor_notice", wss_bus.guardian_thresholds())

    def test_it_is_never_persisted(self):
        """A disk write would hide the missing file that caused the gap."""
        wss_bus.register_guardian("supervisor_notice",
                                  "heartbeat/supervisor_notice.json", 900.0)
        self.assertEqual(list(self.root.iterdir()), [],
                         "运行时补注册不得写盘")
        wss_bus._RUNTIME_GUARDIANS.clear()
        self.assertNotIn("supervisor_notice", wss_bus.guardian_thresholds(),
                         "清空后必须回到真实答案")

    def test_a_builtin_of_the_same_name_wins(self):
        wss_bus.register_guardian("decide_watch", "heartbeat/other.json", 5.0)
        self.assertEqual(wss_bus.guardian_thresholds()["decide_watch"][0],
                         "heartbeat/decide.json", "内置优先，运行时注册不得削弱它")

    def test_a_malformed_argument_is_refused(self):
        for args in (("", "x.json", 1.0), ("n", "", 1.0), ("n", "x.json", 0)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                wss_bus.register_guardian(*args)

    def test_the_registered_name_reports_missing_not_absent(self):
        """The point of registering: the patrol lists it and says ``missing``."""
        wss_bus.register_guardian("supervisor_notice",
                                  "heartbeat/supervisor_notice.json", 900.0)
        summary = wss_bus._guardian_summary()
        self.assertIn("supervisor_notice", summary)
        self.assertEqual(summary["supervisor_notice"]["state"], "missing")


class TheModuleEntryPointRuns(unittest.TestCase):
    """``python -m super_agent.wss_bus --self-test`` must not crash (P0-2).

    Under ``-m`` the file runs as ``__main__`` while ``wss_bus_parts.self_test``
    imports the *package* copy, so the split created two module objects with two
    separate ``PATHS``. Isolation configured only one, the assertions read the
    other, and the entry point died on its first ``PATHS`` access - one assertion
    short of running. No ``-m … --self-test`` test existed for this module, which
    is why the whole suite stayed green. The assertion set itself is unchanged
    (two pre-existing autostart failures), so the check is "it ran to the summary".
    """

    ROOT = Path(__file__).resolve().parents[1]

    def test_the_module_entry_point_reaches_the_summary(self):
        proc = subprocess.run(
            [sys.executable, "-m", "super_agent.wss_bus", "--self-test"],
            cwd=str(self.ROOT), capture_output=True, text=True, timeout=300)
        out = proc.stdout + proc.stderr
        self.assertNotIn("未注入 state_root", out,
                         "-m 入口在自检隔离前就读了 PATHS")
        self.assertNotIn("Traceback", out, out[-2000:])
        self.assertIn("--self-test:", out, out[-2000:])

    def test_isolation_configures_both_module_identities(self):
        """The isolation assertion itself must pass, which needs *both* copies.

        ``PATHS 非生产根（自检隔离）`` reads ``_wb.PATHS`` through the package
        copy. Under ``-m`` that copy is a different object from ``__main__``, so
        the assertion passing is the observable proof that both were configured -
        a single-object isolation leaves it reading ``_UnconfiguredPaths`` and
        raises instead of checking.
        """
        proc = subprocess.run(
            [sys.executable, "-m", "super_agent.wss_bus", "--self-test"],
            cwd=str(self.ROOT), capture_output=True, text=True, timeout=300)
        out = proc.stdout + proc.stderr
        failures = [ln for ln in out.splitlines() if ln.startswith("  FAIL")]
        self.assertFalse([f for f in failures if "自检隔离" in f], out[-2000:])
        self.assertIn("--self-test:", out, out[-2000:])


if __name__ == "__main__":
    unittest.main()
