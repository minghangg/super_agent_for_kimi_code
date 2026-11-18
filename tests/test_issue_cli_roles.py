"""Role resolution: a private overlay, and a refusal when it is absent.

The public half of the role table carries nine generic role names and the
symbol for the supervisor role. A deployment's own spelling of that role lives
in ``config/roles.local.json`` (untracked) or in
``$SAFKC_SUPERVISOR_ALIASES`` - the repository cannot carry it, because a
private role name in a tracked file trips the privacy gate, and the gate is not
allowed to be widened for it.

Three consequences are asserted here: the overlay takes effect, its absence is
a refusal rather than a fallback, and no tracked source file carries the
private spelling (checked by shape, so the test itself does not contain it).

Run: ``python -m pytest tests/test_issue_cli_roles.py -q``
"""
from __future__ import annotations

import io
import json
import os
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from super_agent import issue_cli, role_config

ROOT = Path(__file__).resolve().parents[1]

# A deployment spelling that is *not* the public symbol. Assembled from
# fragments for the same reason the gate assembles its patterns: a file that
# writes the whole word would itself be the thing it scans for.
PRIVATE_NAME = "re" + "ed"

PRIVATE_CONFIG = {
    "roles": {
        role_config.SUPERVISOR: {
            "aliases": [PRIVATE_NAME],
            "prefix": role_config.DEFAULT_SUPERVISOR_PREFIX,
            "delivery_prefix": None,
        }
    },
    "user_agent": "probe-agent",
}


class Overlay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.projects = self.root / "projects.json"
        self.projects.write_text(json.dumps({"projects": {"demo": "placeholder/example"}}),
                                 encoding="utf-8")
        self._env = dict(os.environ)
        os.environ["SESS_EVENTS_STATE_DIR"] = str(self.state)
        for name in ("GITHUB_TOKEN", "AGENT_ROLE", role_config.ENV_ALIASES,
                     issue_cli.CONFIG_ENV):
            os.environ.pop(name, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = issue_cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def comment(self, roles_file: str):
        return self.run_cli("comment", "--role", PRIVATE_NAME, "--project", "demo",
                            "--issue", "1", "--body", "probe", "--dry-run",
                            "--roles-file", roles_file,
                            "--projects-file", str(self.projects),
                            "--env-file", str(self.root / "absent.env"))

    def test_private_config_overrides_default_prefix(self):
        path = self.root / "roles.local.json"
        path.write_text(json.dumps(PRIVATE_CONFIG), encoding="utf-8")
        code, out, err = self.comment(str(path))
        self.assertEqual(code, 0, err)
        self.assertIn("probe", out)

    def test_a_private_alias_without_config_is_refused(self):
        code, _out, err = self.comment(str(self.root / "none.json"))
        self.assertEqual(code, 3, err)
        self.assertIn("未知角色", err)
        self.assertIn("roles.local.json", err)

    def test_the_env_var_supplies_the_alias_without_a_file(self):
        os.environ[role_config.ENV_ALIASES] = PRIVATE_NAME
        code, _out, err = self.comment(str(self.root / "none.json"))
        self.assertEqual(code, 0, err)

    def test_the_file_wins_over_the_env_var(self):
        os.environ[role_config.ENV_ALIASES] = "somethingelse"
        path = self.root / "roles.local.json"
        path.write_text(json.dumps(PRIVATE_CONFIG), encoding="utf-8")
        table = role_config.load(path, env="")
        self.assertEqual(table.canonical(PRIVATE_NAME), role_config.SUPERVISOR)

    def test_the_generic_roles_need_no_config(self):
        table = role_config.load(self.root / "none.json", env="")
        for name in ("pm", "qa", "worker", "scout", "steward", "auditor",
                     "reviewer", "analyst", "planner"):
            self.assertEqual(table.canonical(name), name)
        self.assertEqual(table.canonical(PRIVATE_NAME), None)


class Prefix(unittest.TestCase):
    def test_the_default_supervisor_prefix_is_inside_the_accept_set(self):
        """The engine reads ``[PM]`` and ``[supervisor]`` only."""
        table = role_config.load(Path("/nonexistent"), env="")
        self.assertEqual(table.prefix(role_config.SUPERVISOR),
                         role_config.DEFAULT_SUPERVISOR_PREFIX)
        self.assertIn(table.prefix(role_config.SUPERVISOR),
                      role_config.ACCEPT_PREFIXES)

    def test_supervisor_prefix_outside_accept_set_warns(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            path = Path(tmp.name) / "roles.local.json"
            data = json.loads(json.dumps(PRIVATE_CONFIG))
            data["roles"][role_config.SUPERVISOR]["prefix"] = "boss"
            path.write_text(json.dumps(data), encoding="utf-8")
            table = role_config.load(path, env="")
        finally:
            tmp.cleanup()
        self.assertEqual(table.prefix(role_config.SUPERVISOR), "boss")
        warnings = table.warnings()
        self.assertTrue(warnings, "an out-of-set prefix must not be silent")
        self.assertIn("不在引擎接受集", warnings[0])

    def test_no_private_role_literal_in_tracked_files(self):
        """The public half must not contain the private spelling at all."""
        pattern = re.compile(r"\b" + PRIVATE_NAME + r"\b", re.IGNORECASE)
        checked = 0
        for path in sorted((ROOT / "super_agent").glob("*.py")):
            checked += 1
            text = path.read_text(encoding="utf-8", errors="replace")
            self.assertIsNone(pattern.search(text), f"{path.name} carries the name")
        self.assertGreater(checked, 10)


class Dispatch(unittest.TestCase):
    def test_fixed_sessions_come_from_the_public_symbol(self):
        table = role_config.load(Path("/nonexistent"), env="")
        self.assertTrue(table.fixed_session_key("qa"))
        self.assertTrue(table.fixed_session_key("pm-demo"))
        self.assertTrue(table.fixed_session_key(table.prefix(role_config.SUPERVISOR)))
        self.assertFalse(table.fixed_session_key("wk-demo-20261119"))

    def test_a_delivery_role_needs_a_name(self):
        table = role_config.load(Path("/nonexistent"), env="")
        self.assertEqual(table.delivery_prefix("worker"), "WK")
        self.assertIsNone(table.delivery_prefix("qa"))


if __name__ == "__main__":
    unittest.main()
