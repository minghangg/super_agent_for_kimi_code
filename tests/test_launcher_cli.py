from __future__ import annotations

import unittest

from super_agent import launcher_cli


class LauncherArgumentOrder(unittest.TestCase):
    def test_global_options_before_run_are_accepted(self):
        args = launcher_cli.build_parser().parse_args([
            "--mode", "assist", "--source", "live", "--state-dir", "D:/state",
            "--config", "D:/engine.json", "--allowlist", "D:/allowlist.json", "run"])
        self.assertEqual(args.command, "run")
        self.assertEqual(args.mode, "assist")
        self.assertEqual(args.state_dir, "D:/state")

    def test_global_options_after_run_are_rejected(self):
        with self.assertRaises(SystemExit):
            launcher_cli.build_parser().parse_args([
                "run", "--mode", "assist", "--source", "live"])

    def test_global_options_before_status_are_accepted(self):
        args = launcher_cli.build_parser().parse_args([
            "--state-dir", "D:/state", "--config", "D:/engine.json", "status"])
        self.assertEqual(args.command, "status")


if __name__ == "__main__":
    unittest.main()
