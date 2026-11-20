from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from super_agent import at_cli


class ProcessLifecycle(unittest.TestCase):
    def test_windows_liveness_never_calls_os_kill(self):
        with mock.patch.object(at_cli.os, "name", "nt"), \
                mock.patch.object(at_cli, "_windows_alive", return_value=True), \
                mock.patch.object(at_cli.os, "kill") as kill:
            self.assertTrue(at_cli._alive(1234))
        kill.assert_not_called()

    def test_start_delegates_lifetime_to_the_real_foreground_service(self):
        args = SimpleNamespace(action="start", config=None, state_dir="",
                               port=58731, allow_send=True)
        with mock.patch.object(at_cli, "_config", return_value={}), \
                mock.patch.object(at_cli, "_store_root", return_value="root"), \
                mock.patch.object(at_cli, "_pid", return_value=0), \
                mock.patch.object(at_cli, "_port_open", return_value=False), \
                mock.patch.object(at_cli.at_serve, "run", return_value=0) as run:
            self.assertEqual(at_cli.cmd_serve(args), 0)
        self.assertEqual(run.call_args.kwargs["transport"], at_cli.at_serve._real_transport)

    def test_new_at_uses_activate_only_then_a_separate_task_turn(self):
        completed = SimpleNamespace(returncode=0, stderr="",
                                    stdout='{"session_id":"session_new"}\n')
        with mock.patch.object(at_cli.subprocess, "run", return_value=completed) as run:
            result = at_cli._dispatch("cwd", "real task", name="at-demo-12345678")
        self.assertEqual(result["session_id"], "session_new")
        first, second = [call.args[0] for call in run.call_args_list]
        self.assertIn("--activate-only", first)
        self.assertNotIn("--session", first)
        self.assertIn("--session", second)
        self.assertIn("--no-activate", second)
        self.assertNotIn("--activate-only", second)


if __name__ == "__main__":
    unittest.main()
