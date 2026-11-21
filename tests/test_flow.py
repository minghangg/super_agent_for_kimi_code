from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from super_agent import at_cli, config, flow, issue_cli, requirement_cli, role_config
from super_agent.github_source import FixtureSource, LiveSource


class Labels(unittest.TestCase):
    def test_one_label_selects_each_flow(self):
        self.assertEqual(flow.from_labels([{"name": "flow:at"}]), "at")
        self.assertEqual(flow.from_labels(["flow:standard"]), "standard")
        self.assertEqual(flow.from_labels([]), "")

    def test_conflicting_flow_labels_are_refused(self):
        with self.assertRaises(flow.FlowError):
            flow.from_labels(["flow:at", "flow:standard"])

    def test_absent_labels_use_the_legacy_project_default(self):
        self.assertEqual(flow.resolve([], default_at=True), "at")
        self.assertEqual(flow.resolve([], default_at=False), "standard")


class Sources(unittest.TestCase):
    def test_fixture_reads_flow_from_the_issue_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fixture.json"
            path.write_text(json.dumps({"issues": {
                "1": {"labels": ["flow:at"]},
                "2": {"labels": [{"name": "flow:standard"}]},
            }}), encoding="utf-8")
            source = FixtureSource(path)
            self.assertEqual(source.flow(1), "at")
            self.assertEqual(source.flow(2), "standard")

    def test_live_source_reuses_labels_from_the_existing_list_request(self):
        source = LiveSource("owner/demo")
        with mock.patch.object(source, "_get", return_value=[
            {"number": 1, "labels": [{"name": "flow:at"}]},
            {"number": 2, "labels": [{"name": "flow:standard"}]},
        ]) as get:
            self.assertEqual(source.issues(), [1, 2])
        self.assertEqual(source.flow(1), "at")
        self.assertEqual(source.flow(2), "standard")
        self.assertEqual(get.call_count, 1)

    def test_live_source_fetches_labels_when_startup_has_no_list_cache(self):
        source = LiveSource("owner/demo")
        with mock.patch.object(source, "_get", return_value={
                "number": 1, "labels": [{"name": "flow:standard"}]}) as get:
            self.assertEqual(source.flow(1), "standard")
            self.assertEqual(source.flow(1), "standard")
        self.assertEqual(get.call_count, 1)


class Commands(unittest.TestCase):
    def test_flow_command_adds_one_and_removes_the_opposite_label(self):
        ctx = SimpleNamespace(roles=role_config.load("missing-roles.json"))
        args = SimpleNamespace(role="supervisor", flow="at", issue=7)
        with mock.patch.object(issue_cli, "cmd_label", return_value=0) as label:
            self.assertEqual(issue_cli.cmd_flow(args, ctx), 0)
        self.assertEqual(args.add, "flow:at")
        self.assertEqual(args.remove, "flow:standard")

    def test_flow_command_tolerates_an_absent_opposite_label(self):
        ctx = SimpleNamespace(
            roles=role_config.load("missing-roles.json"),
            projects={"demo": "owner/demo"}, call={}, cfg=config.load())
        args = SimpleNamespace(role="supervisor", project="demo", issue=7,
                               flow="at", dry_run=False)
        with mock.patch.object(issue_cli, "_label_exists", return_value=True), \
                mock.patch.object(issue_cli, "_call", return_value={}), \
                mock.patch.object(issue_cli, "_call_raw", return_value={
                    "_status": 404, "_error": "Label does not exist"}), \
                mock.patch.object(issue_cli, "_ledger"):
            self.assertEqual(issue_cli.cmd_flow(args, ctx), 0)

    def test_at_start_refuses_a_standard_issue_before_dispatch(self):
        ctx = SimpleNamespace(
            roles=role_config.load("missing-roles.json"),
            projects={"demo": "owner/demo"}, call={}, cfg=config.load())
        args = SimpleNamespace(role="supervisor", project="demo", issue=1, cwd=".",
                               new=False, dry_run=False, task="", provider="",
                               model="", effort="")
        with mock.patch.object(at_cli, "_config", return_value=config.load()), \
                mock.patch.object(at_cli.issue_cli, "_Ctx", return_value=ctx), \
                mock.patch.object(at_cli.session_ledger, "load_sessions", return_value={}), \
                mock.patch.object(at_cli.issue_cli, "_call", return_value={
                    "labels": [{"name": "flow:standard"}]}), \
                mock.patch.object(at_cli, "_dispatch") as dispatch:
            with self.assertRaises(issue_cli.Denied):
                at_cli.cmd_start(args)
        dispatch.assert_not_called()

    def test_requirement_commands_refuse_only_an_explicit_standard_label(self):
        args = SimpleNamespace(issue=7)
        ctx = SimpleNamespace(call={})
        with mock.patch.object(requirement_cli.issue_cli, "_call", return_value={
                "labels": [{"name": "flow:standard"}]}):
            with self.assertRaises(issue_cli.Denied):
                requirement_cli._refuse_standard(args, ctx, "owner/demo")
        with mock.patch.object(requirement_cli.issue_cli, "_call", return_value={
                "labels": []}):
            self.assertIsNone(requirement_cli._refuse_standard(args, ctx, "owner/demo"))


if __name__ == "__main__":
    unittest.main()
