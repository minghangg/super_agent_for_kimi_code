"""L1 tests for the injectable context gate port (ISS-25 item 6).

The original workbench script coupled the decision matrix to a private repo
root, a sibling module and two state files. The package port must take all
four as explicit injections, must never silently connect to production, and
must keep the 22-branch decision matrix plus the CLI exit codes intact.

Run: ``python -m pytest tests/test_ctx_gate.py -q``
"""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from super_agent import ctx_gate

ROOT = Path(__file__).resolve().parent.parent
PRIVATE = ("re" + "ed", "lai" + "ke", "yuan" + "hai",
           "ming" + "hangg", "Users/m" + "ingh", "Users\\m" + "ingh")
LEGACY = "Lai" + "ke_work_with_agent"


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="iss25_6_")
        self.root = Path(self.tmp.name)
        self.sessions = self.root / "state" / "sessions.json"
        self.ledger = self.root / "state" / "ledger.jsonl"
        self.sessions.parent.mkdir(parents=True, exist_ok=True)
        self.sessions.write_text(json.dumps({
            "steward": {"id": "sid-long"},
            "wk-task": {"id": "sid-one"},
        }, ensure_ascii=False), encoding="utf-8")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(ctx_gate.configure)

    def provider(self, value):
        calls = []

        def status(sid):
            calls.append(sid)
            return value

        return status, calls

    def run_gate(self, sid="sid-long", value=None, **kwargs):
        status, calls = self.provider(value)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = ctx_gate.gate(
                sid, status_provider=status, sessions_path=self.sessions,
                ledger_path=self.ledger, **kwargs)
        return result, err.getvalue(), calls

    def lines(self):
        if not self.ledger.is_file():
            return []
        return [json.loads(line) for line in
                self.ledger.read_text(encoding="utf-8").splitlines() if line]


class DecisionMatrix(Fixture):
    def test_original_22_branch_self_test_is_green(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ctx_gate.self_test()
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("--self-test: 25/25 通过", out.getvalue())

    def test_thresholds_and_force(self):
        cases = [
            (0.59, "long-lived", False, "allow", 0),
            (0.60, "long-lived", False, "refuse-60", 4),
            (0.63, "long-lived", False, "refuse-60", 4),
            (0.75, "long-lived", False, "refuse-75", 5),
            (0.78, "long-lived", False, "refuse-75", 5),
            (0.90, "long-lived", False, "hard-cap", 6),
            (0.95, "one-shot", False, "hard-cap", 6),
            (0.34, "one-shot", False, "allow", 0),
            (0.35, "one-shot", False, "refuse-35", 8),
            (0.63, "one-shot", False, "refuse-35", 8),
            (0.35, "one-shot", True, "refuse-35", 8),
            (0.63, "long-lived", True, "refuse-60", 0),
            (0.78, "long-lived", True, "refuse-75", 0),
            (0.90, "long-lived", True, "hard-cap", 6),
            (0.95, "one-shot", True, "hard-cap", 6),
            (None, "long-lived", False, "allow-unknown", 0),
        ]
        for usage, kind, force, expect_v, expect_code in cases:
            with self.subTest(usage=usage, kind=kind, force=force):
                v, code, msg = ctx_gate.verdict(usage, kind, force)
                self.assertEqual((v, code), (expect_v, expect_code))
                self.assertTrue(msg)
        self.assertEqual(ctx_gate.THRESHOLD_ONESHOT_REFUSE, 0.35)
        self.assertEqual(ctx_gate.THRESHOLD_REFUSE, 0.60)
        self.assertEqual(ctx_gate.THRESHOLD_HANDOFF, 0.75)
        self.assertEqual(ctx_gate.THRESHOLD_FORCE_HARD, 0.90)

    def test_unknown_has_explicit_warning_and_forced_never_passes_hard_cap(self):
        (v, code, msg), err, _ = self.run_gate(value=None, quiet=False)
        self.assertEqual((v, code), ("allow-unknown", 0))
        self.assertIn("ctx 未知", msg)
        self.assertIn("[ctx-gate] PASS", err)
        self.assertEqual(self.lines()[-1]["note"].count("unknown=true"), 1)
        (v2, code2, _), err2, _ = self.run_gate(value={"context_usage": 0.93},
                                                force=True)
        self.assertEqual((v2, code2), ("hard-cap", 6))
        self.assertIn("BLOCK", err2)
        self.assertNotIn("--force 已越过门禁", err2)

    def test_session_kind_by_key_name_long_vs_one_shot(self):
        self.assertEqual(ctx_gate.session_kind("sid-long", self.sessions),
                         ("long-lived", "steward"))
        self.assertEqual(ctx_gate.session_kind("sid-one", self.sessions),
                         ("one-shot", "wk-task"))
        self.assertEqual(ctx_gate.session_kind("unregistered", self.sessions),
                         ("one-shot", ""))
        self.assertEqual(ctx_gate._is_fixed_key("steward"), True)
        self.assertEqual(ctx_gate._is_fixed_key("pm-any"), True)
        self.assertEqual(ctx_gate._is_fixed_key("qa-lwa-any"), True)
        self.assertEqual(ctx_gate._is_fixed_key("wk-any"), False)

    def test_key_name_drives_kind_decision_end_to_end(self):
        (v_long, code_long, _), _, _ = self.run_gate(
            sid="sid-long", value={"context_usage": 0.63})
        (v_one, code_one, _), _, _ = self.run_gate(
            sid="sid-one", value={"context_usage": 0.34})
        (v_one_spent, code_one_spent, _), _, _ = self.run_gate(
            sid="sid-one", value={"context_usage": 0.63})
        self.assertEqual((v_long, code_long), ("refuse-60", 4))
        self.assertEqual((v_one, code_one), ("allow", 0))
        self.assertEqual((v_one_spent, code_one_spent), ("refuse-35", 8))


class Injection(Fixture):
    def test_provider_three_states_allow_refuse_unknown(self):
        for value, expect in (({"context_usage": 0.10}, ("allow", 0)),
                              ({"context_tokens": 780, "max_context_tokens": 1000},
                               ("refuse-75", 5)),
                              (None, ("allow-unknown", 0))):
            with self.subTest(value=value):
                (v, code, _), _, calls = self.run_gate(value=value)
                self.assertEqual((v, code), expect)
                self.assertEqual(calls, ["sid-long"])

    def test_provider_exception_is_unknown_and_visible(self):
        def broken(sid):
            raise RuntimeError("provider down")

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            v, code, msg = ctx_gate.gate(
                "sid-long", status_provider=broken, sessions_path=self.sessions,
                ledger_path=self.ledger)
        self.assertEqual((v, code), ("allow-unknown", 0))
        self.assertIn("provider down", err.getvalue())
        self.assertIn("ctx 未知", msg)

    def test_no_injection_returns_param_error_and_does_not_connect(self):
        ctx_gate.configure()
        self.assertEqual(ctx_gate.missing_injections(),
                         ["status_provider", "sessions_path", "ledger_path"])
        with mock.patch.object(ctx_gate, "DEFAULT_STATUS_PROVIDER",
                               side_effect=AssertionError("must not call")):
            # A configured-but-None default must still be rejected before any call.
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                result = ctx_gate.gate("sid-long", quiet=True)
        self.assertEqual(result[0], "missing-injection")
        self.assertEqual(result[1], 7)
        self.assertIn("拒绝静默连生产", err.getvalue())
        self.assertEqual(self.lines(), [])
        ctx_gate.configure()
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            code = ctx_gate.main(["--id", "sid-long"])
        self.assertEqual(code, 7)
        self.assertIn("未注入", out.getvalue())
        self.assertEqual(self.lines(), [])

    def test_configure_sets_all_three_defaults_and_gate_accepts_them(self):
        status, calls = self.provider({"context_usage": 0.10})
        cfg = ctx_gate.configure(status_provider=status,
                                 sessions_path=self.sessions,
                                 ledger_path=self.ledger)
        self.assertEqual(ctx_gate.missing_injections(cfg), [])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ctx_gate.gate("sid-long", quiet=True)[1], 0)
        self.assertEqual(calls, ["sid-long"])
        self.assertEqual(len(self.lines()), 1)

    def test_ledger_write_failure_does_not_block(self):
        bad_ledger = self.root / "not-a-dir" / "ledger.jsonl"
        (self.root / "not-a-dir").write_text("occupied", encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            v, code, _ = ctx_gate.gate(
                "sid-long", status_provider=lambda sid: {"context_usage": 0.10},
                sessions_path=self.sessions, ledger_path=bad_ledger)
        self.assertEqual((v, code), ("allow", 0))
        self.assertIn("ledger 写入失败（不阻断）", err.getvalue())

    def test_dry_ledger_skips_write_but_keeps_decision(self):
        (v, code, _), _, _ = self.run_gate(value={"context_usage": 0.63},
                                          dry_ledger=True)
        self.assertEqual((v, code), ("refuse-60", 4))
        self.assertEqual(self.lines(), [])


class SessionsTable(Fixture):
    def write_raw(self, text):
        self.sessions.write_text(text, encoding="utf-8")

    def test_broken_json_is_one_shot(self):
        self.write_raw("{not-json")
        self.assertEqual(ctx_gate.session_kind("sid-long", self.sessions),
                         ("one-shot", ""))
        (v, code, _), _, _ = self.run_gate(value={"context_usage": 0.63})
        self.assertEqual((v, code), ("refuse-35", 8))

    def test_non_object_top_level_is_one_shot(self):
        self.write_raw('["sid-long"]')
        self.assertEqual(ctx_gate.session_kind("sid-long", self.sessions),
                         ("one-shot", ""))

    def test_missing_file_is_one_shot(self):
        self.sessions.unlink()
        self.assertEqual(ctx_gate.session_kind("sid-long", self.sessions),
                         ("one-shot", ""))

    def test_resolve_name_uses_injected_table(self):
        self.assertEqual(ctx_gate._resolve_name("steward", self.sessions), "sid-long")
        self.assertEqual(ctx_gate._resolve_name("absent", self.sessions), "")


class CliMapping(Fixture):
    def cli_with_defaults(self, *args):
        script = textwrap.dedent("""
            import json, sys
            from super_agent import ctx_gate
            ctx_gate.configure(
                status_provider=lambda sid: (
                    {"context_usage": float(sys.argv[1])} if sys.argv[1] != "none" else None),
                sessions_path=sys.argv[2], ledger_path=sys.argv[3])
            raise SystemExit(ctx_gate.main(%r))
        """) % (list(args),)
        proc = subprocess.run(
            [sys.executable, "-c", script, "none", str(self.sessions),
             str(self.ledger)], cwd=str(ROOT), capture_output=True, text=True)
        return proc

    def test_exit_codes_0_4_7(self):
        for usage, args, expect in (
                ("0.10", ["--id", "sid-long"], 0),
                ("0.63", ["--id", "sid-long"], 4),
                ("0.78", ["--id", "sid-long"], 5),
                ("0.90", ["--id", "sid-long"], 6),
                ("0.90", ["--id", "sid-long", "--force"], 6),
                ("none", ["--id", "sid-long"], 0)):
            with self.subTest(usage=usage, args=args):
                script = textwrap.dedent("""
                    import sys
                    from super_agent import ctx_gate
                    ctx_gate.configure(
                        status_provider=lambda sid: (
                            {"context_usage": float(sys.argv[1])}
                            if sys.argv[1] != "none" else None),
                        sessions_path=sys.argv[2], ledger_path=sys.argv[3])
                    raise SystemExit(ctx_gate.main(%r))
                """) % (args,)
                proc = subprocess.run(
                    [sys.executable, "-c", script, usage, str(self.sessions),
                     str(self.ledger)], cwd=str(ROOT), capture_output=True,
                    text=True)
                self.assertEqual(proc.returncode, expect,
                                 proc.stdout + proc.stderr)
                self.assertIn("[ctx-gate]", proc.stderr)

    def test_param_error_on_unknown_name(self):
        proc = self.cli_with_defaults("--name", "absent")
        self.assertEqual(proc.returncode, 7)
        self.assertIn("未记账", proc.stderr)

    def test_json_output_is_one_line_and_no_stderr_noise(self):
        proc = self.cli_with_defaults("--id", "sid-long", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = json.loads(proc.stdout.strip())
        self.assertEqual(doc["code"], 0)
        self.assertEqual(doc["verdict"], "allow-unknown")
        self.assertEqual(proc.stderr.strip(), "")

    def test_self_test_cli_is_22_of_22(self):
        proc = subprocess.run(
            [sys.executable, "-m", "super_agent.ctx_gate", "--self-test"],
            cwd=str(ROOT), capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--self-test: 25/25 通过", proc.stdout)


class Neutrality(unittest.TestCase):
    def test_source_has_no_private_literals_or_private_imports(self):
        text = Path(ctx_gate.__file__).read_text(encoding="utf-8")
        for token in PRIVATE:
            self.assertNotIn(token, text.lower())
        self.assertNotIn(LEGACY, text)
        for forbidden in ("import ctx", "sys.path.append",
                          'BASE / "state"', "auth/"):
            self.assertNotIn(forbidden, text)
        self.assertEqual(Path(ctx_gate.__file__).resolve().name, "ctx_gate.py")
        self.assertEqual(Path(ctx_gate.__file__).resolve().parent.name,
                         "super_agent")

    def test_import_chain_has_no_workbench_modules(self):
        proc = subprocess.run(
            [sys.executable, "-c",
             "import sys; import super_agent.ctx_gate; "
             "print('lwa' , 'ctx' in sys.modules, "
             "'kimi_bus' in sys.modules, 'kimi_direct' in sys.modules)"],
            cwd=str(ROOT), capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "lwa False False False")

    def test_package_defaults_are_empty(self):
        proc = subprocess.run(
            [sys.executable, "-c",
             "from super_agent import ctx_gate as g; "
             "print(g.DEFAULT_STATUS_PROVIDER, g.DEFAULT_SESSIONS_PATH, "
             "g.DEFAULT_LEDGER_PATH, g.missing_injections())"],
            cwd=str(ROOT), capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("None None None", proc.stdout)
        self.assertIn("status_provider", proc.stdout)


if __name__ == "__main__":
    unittest.main()
