from __future__ import annotations

import unittest

from super_agent import switchback


class SwitchAudit(unittest.TestCase):
    def snapshot(self, roles, enabled, lock_pid):
        commands = {
            "launcher": "pythonw -m super_agent.launcher run",
            "engine": "pythonw -m super_agent.cli run",
            "at": "pythonw -m super_agent.at serve start",
            "wss": "pythonw wss_bus_safkc.py --daemon",
            "decide_watch": "pythonw decide_watch.py",
        }
        return {
            "processes": [{"pid": 100 + n, "command": commands[role]}
                          for n, role in enumerate(roles)],
            "tasks": [{"name": name, "enabled": name in enabled}
                      for name in ("KimiSafkcLauncher", "KimiWssBus",
                                   "KimiDecideWatch")],
            "engine_lock": {"pid": lock_pid},
            "ports": {"58800": True, "58731": True},
        }

    def test_old_writer_and_second_launcher_are_refused(self):
        data = self.snapshot(["launcher", "engine"], ["KimiSafkcLauncher"], 100)
        errors = switchback.audit(data, "launcher")
        self.assertIn("old writer still running: engine", errors)
        data["processes"].append({"pid": 200, "command": data["processes"][0]["command"]})
        errors = switchback.audit(data, "launcher")
        self.assertTrue(any("duplicate daemon: launcher" in error for error in errors))

    def test_two_enabled_entries_are_refused(self):
        data = self.snapshot(["launcher"], ["KimiSafkcLauncher", "KimiWssBus"], 100)
        self.assertTrue(any("task set" in error for error in
                            switchback.audit(data, "launcher")))

    def test_valid_launcher_snapshot_passes(self):
        data = self.snapshot(["launcher"], ["KimiSafkcLauncher"], 100)
        self.assertEqual(switchback.audit(data, "launcher"), [])

    def test_rollback_requires_every_legacy_writer_and_task(self):
        roles = ["engine", "at", "wss", "decide_watch"]
        tasks = ["KimiWssBus", "KimiDecideWatch"]
        data = self.snapshot(roles, tasks, 100)
        self.assertEqual(switchback.audit(data, "legacy"), [])
        data["processes"] = data["processes"][:-1]
        self.assertIn("rollback missing decide_watch", switchback.audit(data, "legacy"))

    def test_wrong_lock_owner_is_refused(self):
        data = self.snapshot(["launcher"], ["KimiSafkcLauncher"], 999)
        self.assertTrue(any("lock" in error for error in
                            switchback.audit(data, "launcher")))


if __name__ == "__main__":
    unittest.main()
