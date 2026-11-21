from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from super_agent import config, requirement_cli, requirements as req, role_config


class CommentPaging(unittest.TestCase):
    def test_live_reader_uses_real_github_pagination_and_normalises_rows(self):
        pages = [
            [{"id": 100, "body": "one", "user": {"login": "at"},
              "created_at": "then"}] * 100,
            [{"id": 200, "body": "two", "user": {"login": "at"},
              "created_at": "later"}],
        ]
        ctx = SimpleNamespace(fetch={})
        with mock.patch.object(requirement_cli.issue_cli, "_call",
                               side_effect=pages) as call:
            rows = requirement_cli._comments(ctx, "owner/demo", 7)
        self.assertEqual(len(rows), 101)
        self.assertEqual(rows[-1], {"id": 200, "body": "two", "author": "at",
                                    "created_at": "later"})
        paths = [args[0][1] for args in call.call_args_list]
        self.assertEqual(paths[0], "/repos/owner/demo/issues/7/comments?per_page=100&page=1")
        self.assertEqual(paths[1], "/repos/owner/demo/issues/7/comments?per_page=100&page=2")


class PublishQuestionLink(unittest.TestCase):
    def test_publish_returns_a_real_question_url_and_files_one_question(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = {
                "schema_version": 1, "requirement_id": "owner/demo#7", "revision": 1,
                "status": "clarifying", "objective": "choose a boundary",
                "context_refs": [], "scope": ["core"], "out_of_scope": [],
                "constraints": [], "acceptance": ["answer is returned"],
                "negative_tests": ["duplicate publish reuses the link"],
                "risk_tier": "T1",
                "review_policy": {"plan_qa": False, "implementation_qa": True,
                                  "worker_mode": "plan_and_implement", "test_level": "L1"},
                "open_questions": ["which boundary?"],
                "interaction": {"choice_mode": "single", "allow_custom": True,
                                "options": [{"id": "core", "label": "core"}]},
                "approval_ref": "", "updated_at": "",
            }
            package["requirement_hash"] = req.requirement_hash(package)
            source = root / "package.json"
            source.write_text(json.dumps(package), encoding="utf-8")
            ctx = SimpleNamespace(
                roles=role_config.load(root / "missing-roles.json"),
                projects={"demo": "owner/demo"}, call={}, fetch={},
                cfg=config.load(None, {"engine": {"at_question_port": 58731}}))
            args = SimpleNamespace(
                role="analyst", project="demo", issue=7, file=str(source),
                session_key="at-demo-12345678", worker_name="", summary="", question="",
                dry_run=False, state_dir=str(root))
            with mock.patch.object(requirement_cli, "_ctx", return_value=ctx), \
                    mock.patch.object(requirement_cli, "_comments", return_value=[]), \
                    mock.patch.object(requirement_cli, "_sync", return_value=({}, "owner/demo")), \
                    mock.patch.object(requirement_cli.issue_cli, "_call", return_value={"id": 100}), \
                    mock.patch.object(requirement_cli.issue_cli, "_ledger"), \
                    mock.patch.object(requirement_cli.session_ledger, "load_issue_sessions",
                                      return_value={"demo#7": {"at": {"session": "session_at"}}}):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(requirement_cli.cmd_publish(args), 0)
            self.assertIn("question_url=http://127.0.0.1:58731/q/", output.getvalue())
            files = list((root / "atserve" / "questions").glob("*.json"))
            self.assertEqual(len(files), 1)
            record = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(record["comment_id"], 100)
            self.assertEqual(record["sid"], "session_at")
            self.assertEqual(record["interaction"]["options"][0]["label"], "core")


class ApprovalSource(unittest.TestCase):
    def _fixture(self):
        package = {
            "schema_version": 1, "requirement_id": "owner/demo#7", "revision": 1,
            "status": "ready", "objective": "approved only by supervisor",
            "context_refs": [], "scope": [], "out_of_scope": [], "constraints": [],
            "acceptance": ["valid approval carries source"],
            "negative_tests": ["wrong supervisor key is refused"], "risk_tier": "T1",
            "review_policy": {"plan_qa": False, "implementation_qa": True,
                              "worker_mode": "plan_and_implement", "test_level": "L1"},
            "open_questions": [], "approval_ref": "", "updated_at": "",
        }
        package["requirement_hash"] = req.requirement_hash(package)
        projection = req.project_comments(
            [{"id": 100, "body": req.fence(req.PACKAGE_FENCE, package)}],
            package_verifier=lambda comment: True)
        ctx = SimpleNamespace(
            roles=role_config.load("missing-roles.json"), projects={"demo": "owner/demo"},
            call={}, fetch={},
            cfg=config.load(None, {"engine": {"supervisor_keys": ["supervisor"]}}))
        args = SimpleNamespace(role="supervisor", project="demo", issue=7,
                               revision=1, hash="", session_key="supervisor",
                               dry_run=False, state_dir="")
        return projection, ctx, args

    def test_approval_comment_carries_the_verified_supervisor_key(self):
        projection, ctx, args = self._fixture()
        with mock.patch.object(requirement_cli, "_ctx", return_value=ctx), \
                mock.patch.object(requirement_cli, "_project", return_value=projection), \
                mock.patch.object(requirement_cli, "_sync"), \
                mock.patch.object(requirement_cli.issue_cli, "_ledger"), \
                mock.patch.object(requirement_cli.issue_cli, "_call", return_value={"id": 101}) as call:
            self.assertEqual(requirement_cli.cmd_approve(args), 0)
        body = call.call_args.args[2]["body"]
        self.assertIn("<!-- session: supervisor -->", body)
        self.assertIn(req.APPROVAL_FENCE, body)

    def test_wrong_supervisor_key_is_refused_before_post(self):
        projection, ctx, args = self._fixture()
        args.session_key = "worker"
        with mock.patch.object(requirement_cli, "_ctx", return_value=ctx), \
                mock.patch.object(requirement_cli, "_project", return_value=projection), \
                mock.patch.object(requirement_cli.issue_cli, "_call") as call:
            with self.assertRaises(requirement_cli.issue_cli.Denied):
                requirement_cli.cmd_approve(args)
        call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
