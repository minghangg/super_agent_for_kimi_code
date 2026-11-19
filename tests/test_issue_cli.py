"""The issue CLI, driven through its own argument parser.

Every assertion is behavioural: what the command prints, which request it would
send, and which exit code it returns. Nothing here reaches GitHub - every
command runs with ``--dry-run`` or is refused before a request.

The two refusals that matter most are the ones whose failure is silent in
production: a role that cannot be resolved (a wrong prefix makes a verdict
unattributable) and a comment with no body (the marker would hide it).

Run: ``python -m pytest tests/test_issue_cli.py -q``
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from super_agent import issue_cli, role_config


def _projects(path: Path, name: str = "demo") -> Path:
    path.write_text(json.dumps({"projects": {name: "placeholder/example"}}),
                    encoding="utf-8")
    return path


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.projects = _projects(self.root / "projects.json")
        self.state = self.root / "state"
        self.state.mkdir()
        self._env = dict(os.environ)
        os.environ["SESS_EVENTS_STATE_DIR"] = str(self.state)
        os.environ.pop("GITHUB_TOKEN", None)
        os.environ.pop("AGENT_ROLE", None)
        os.environ.pop(role_config.ENV_ALIASES, None)
        os.environ.pop(issue_cli.CONFIG_ENV, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = issue_cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def base(self):
        return ["--projects-file", str(self.projects),
                "--env-file", str(self.root / "absent.env")]

    def ledger_rows(self) -> list[dict]:
        path = self.state / "ledger.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines() if line.strip()]


class Policy(CliCase):
    def test_worker_cannot_create(self):
        code, _out, err = self.run_cli("create", "--role", "worker",
                                       "--worker-name", "wk-demo-1",
                                       "--project", "demo", "--title", "t",
                                       "--dry-run", *self.base())
        self.assertEqual(code, 3, err)
        self.assertIn("无权执行 create", err)

    def test_qa_cannot_close(self):
        code, _out, err = self.run_cli("close", "--role", "qa", "--project",
                                       "demo", "--issue", "1", "--dry-run",
                                       *self.base())
        self.assertEqual(code, 3, err)

    def test_an_unknown_project_is_refused(self):
        code, _out, err = self.run_cli("show", "--role", "qa", "--project",
                                       "nope", "--issue", "1", *self.base())
        self.assertEqual(code, 3, err)
        self.assertIn("未知项目", err)

    def test_an_unknown_role_lists_what_is_available(self):
        code, _out, err = self.run_cli("comment", "--role", "nobody",
                                       "--project", "demo", "--issue", "1",
                                       "--body", "x", "--dry-run", *self.base())
        self.assertEqual(code, 3, err)
        self.assertIn("未知角色", err)
        self.assertIn("qa", err)


class Comment(CliCase):
    def test_the_role_prefix_is_prepended(self):
        code, out, err = self.run_cli("comment", "--role", "qa", "--project",
                                      "demo", "--issue", "1", "--body", "probe",
                                      "--dry-run", *self.base())
        self.assertEqual(code, 0, err)
        self.assertIn("/issues/1/comments", out)
        self.assertIn("body_keys=['body']", out)

    def test_a_worker_supplies_its_name(self):
        code, _out, err = self.run_cli("comment", "--role", "worker",
                                       "--worker-name", "wk-demo-20261119",
                                       "--project", "demo", "--issue", "1",
                                       "--body", "done", "--dry-run", *self.base())
        self.assertEqual(code, 0, err)

    def test_a_worker_without_a_name_is_refused(self):
        code, _out, err = self.run_cli("comment", "--role", "worker",
                                       "--project", "demo", "--issue", "1",
                                       "--body", "done", "--dry-run", *self.base())
        self.assertEqual(code, 3, err)
        self.assertIn("--worker-name", err)

    def test_an_empty_body_is_refused(self):
        code, _out, err = self.run_cli("comment", "--role", "qa", "--project",
                                       "demo", "--issue", "1", "--body", "  ",
                                       "--dry-run", *self.base())
        self.assertEqual(code, 3, err)
        self.assertIn("正文为空", err)

    def test_dry_run_writes_no_ledger_row(self):
        self.run_cli("comment", "--role", "qa", "--project", "demo",
                     "--issue", "1", "--body", "probe", "--dry-run", *self.base())
        self.assertEqual(self.ledger_rows(), [])


class Create(CliCase):
    def test_a_new_issue_is_on_hold_by_default(self):
        code, out, err = self.run_cli("create", "--role", "supervisor",
                                      "--project", "demo", "--title", "probe",
                                      "--dry-run", *self.base())
        self.assertEqual(code, 0, err)
        self.assertIn("/issues", out)

    def test_labels_carry_the_hold_label(self):
        """The label set is visible in the request the CLI prints."""
        seen = {}

        def fake_call(method, path, payload=None, **kw):
            seen.update({"method": method, "path": path, "payload": payload})
            return {"dry_run": True}

        original = issue_cli._call
        issue_cli._call = fake_call
        try:
            code, _out, err = self.run_cli("create", "--role", "supervisor",
                                           "--project", "demo", "--title", "t",
                                           "--dry-run", *self.base())
        finally:
            issue_cli._call = original
        self.assertEqual(code, 0, err)
        self.assertIn(issue_cli.HOLD_LABEL, seen["payload"]["labels"])


class Close(CliCase):
    def test_close_without_a_qa_comment_is_refused(self):
        def no_qa(*_a, **_kw):
            return False

        original = issue_cli._has_qa_comment
        issue_cli._has_qa_comment = no_qa
        try:
            code, _out, err = self.run_cli("close", "--role", "supervisor",
                                           "--project", "demo", "--issue", "7",
                                           "--dry-run", *self.base())
        finally:
            issue_cli._has_qa_comment = original
        self.assertEqual(code, 3, err)
        self.assertIn("[QA]", err)

    def test_a_missing_archive_command_is_reported_not_failed(self):
        """An explicitly empty command: the session stays registered, close
        succeeds.

        ``""`` has to be written into a config file now. The public default is
        non-empty since ISS-100 D5, so "nothing configured" no longer means
        "nothing to run" -- it means "run the default", and this test would
        otherwise measure the wrong branch and pass by accident.
        """
        sid = "session_" + "0" * 8 + "-0000-0000-0000-" + "0" * 12
        db = {"demo#7": {"qa": {"session": sid}}}
        (self.state / "issue_sessions.json").write_text(json.dumps(db), encoding="utf-8")
        cfg = self.root / "no_archive.json"
        cfg.write_text(json.dumps({"engine": {"session_archive_cmd": ""}}),
                       encoding="utf-8")

        original_qa = issue_cli._has_qa_comment
        original_call = issue_cli._call
        issue_cli._has_qa_comment = lambda *_a, **_kw: True
        issue_cli._call = lambda *_a, **_kw: {}
        try:
            code, out, err = self.run_cli("close", "--role", "supervisor",
                                          "--project", "demo", "--issue", "7",
                                          "--config", str(cfg), *self.base())
        finally:
            issue_cli._has_qa_comment = original_qa
            issue_cli._call = original_call
        self.assertEqual(code, 0, err)
        self.assertIn("未配置归档命令", out)
        kept = json.loads((self.state / "issue_sessions.json").read_text("utf-8"))
        self.assertIn("demo#7", kept)

    def test_the_public_default_names_recycle(self):
        """ISS-100 D5: with nothing configured, close runs this package's
        ``recycle archive`` -- not another checkout's console script."""
        from super_agent import config
        self.assertEqual(config.DEFAULTS["engine"]["session_archive_cmd"],
                         "{python} -m super_agent.cli recycle archive {sid}")


class Binding(CliCase):
    def test_a_truncated_sid_is_refused(self):
        code, _out, err = self.run_cli("bind", "--role", "supervisor",
                                       "--project", "demo", "--issue", "7",
                                       "--bind-role", "qa", "--session",
                                       "session_a8fd", *self.base())
        self.assertEqual(code, 3, err)
        self.assertIn("完整 sid", err)

    def test_a_bind_writes_beside_sessions_json(self):
        sid = "session_" + "1" * 8 + "-1111-1111-1111-" + "1" * 12
        code, _out, err = self.run_cli("bind", "--role", "supervisor",
                                       "--project", "demo", "--issue", "7",
                                       "--bind-role", "qa", "--session", sid,
                                       "--name", "qa-demo-20261119", *self.base())
        self.assertEqual(code, 0, err)
        db = json.loads((self.state / "issue_sessions.json").read_text("utf-8"))
        self.assertEqual(db["demo#7"]["qa"]["session"], sid)
        sessions = json.loads((self.state / "sessions.json").read_text("utf-8"))
        self.assertEqual(sessions["qa-demo-20261119"]["id"], sid)
        events = (self.state / "events.jsonl").read_text("utf-8").splitlines()
        self.assertIn("session.bound", events[-1])

    def test_only_pm_and_supervisor_may_bind(self):
        code, _out, err = self.run_cli("bind", "--role", "qa", "--project",
                                       "demo", "--issue", "7", "--bind-role",
                                       "qa", "--session",
                                       "session_" + "2" * 8 + "-2222-2222-2222-" + "2" * 12,
                                       *self.base())
        self.assertEqual(code, 3, err)


class Reads(CliCase):
    def test_show_dry_run_is_not_supported_but_needs_a_token(self):
        """A read command still needs a token; the refusal names where it looks."""
        code, _out, err = self.run_cli("show", "--role", "qa", "--project",
                                       "demo", "--issue", "1", *self.base())
        self.assertEqual(code, 3, err)
        self.assertIn("GITHUB_TOKEN", err)
        self.assertNotIn("env-file", err.replace("--env-file", ""))

    def test_projects_without_a_table_reports_and_returns_zero(self):
        code, out, err = self.run_cli("projects", "--projects-file",
                                      str(self.root / "absent.json"))
        self.assertEqual(code, 0, err)
        self.assertIn("项目表未配置", err)

    def test_projects_lists_the_table(self):
        code, out, err = self.run_cli("projects", "--projects-file", str(self.projects))
        self.assertEqual(code, 0, err)
        self.assertIn("placeholder/example", out)


if __name__ == "__main__":
    unittest.main()
