"""L1 tests for the ISS-101 project-environment pointer in ``dispatch_cli``.

A new capability must have a production caller. The pointer is that caller:
on a successful dispatch whose ``--cwd`` basename matches a registered
environment document, one line names that document on stderr. The test pins
three things that are easy to get wrong:

* the key is the **directory basename, lower-cased** -- there is no project
  table in safkc, and the one in the workbench is a cross-repo dependency;
* a missing key or document is silent (a dispatch outside a registered project
  is normal, and this optional pointer must not block it);
* the pointer goes to **stderr only** -- ``--json``'s stdout is a machine
  contract and must stay parseable.

No test creates a remote session: the success path is driven with a fake sid
and patched HTTP helpers, and the pure helper is tested directly.
Run: ``python -m pytest tests/test_dispatch_project_env.py -q``
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from super_agent import agent_config, dispatch_cli as D


class PointerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in (agent_config.ENV_FILE,)}
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.env_dir = self.root / "project_env"
        self.env_dir.mkdir()
        agent_config._reset_cache()

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        agent_config._reset_cache()
        self._tmp.cleanup()

    def configure(self) -> None:
        path = self.root / "agent.json"
        path.write_text(json.dumps(
            {"paths": {"project_env_dir": str(self.env_dir)}}), encoding="utf-8")
        os.environ[agent_config.ENV_FILE] = str(path)
        agent_config._reset_cache()

    def test_basename_lowercased_matches_the_document(self) -> None:
        self.configure()
        (self.env_dir / "demo_project.md").write_text("# doc\n", encoding="utf-8")
        cwd = self.root / "Demo_Project"   # mixed case: the key is lower-cased
        line = D.project_env_pointer(str(cwd))
        self.assertEqual(line, f"项目环境：demo_project → "
                               f"{self.env_dir / 'demo_project.md'}")

    def test_no_document_is_silent(self) -> None:
        self.configure()
        self.assertEqual(D.project_env_pointer(str(self.root / "random_project")), "")

    def test_repository_name_with_no_document_is_silent(self) -> None:
        """``super_agent_for_kimi_code`` has no matching md (the key is
        ``superagent``). That mismatch is known and is not "fixed" here: a
        missing document stays silent, never a made-up pointer.
        """
        self.configure()
        (self.env_dir / "superagent.md").write_text("# doc\n", encoding="utf-8")
        self.assertEqual(
            D.project_env_pointer(str(self.root / "super_agent_for_kimi_code")), "")

    def test_two_candidates_are_not_a_pointer(self) -> None:
        self.configure()
        (self.env_dir / "demo.md").write_text("# a\n", encoding="utf-8")
        # A second stem equal to the key (different extension spelling on a
        # case-insensitive FS) must not become "the first hit".
        with mock.patch.object(Path, "glob",
                               lambda self, pat: iter([self / "demo.md",
                                                       self / "demo.md"])):
            self.assertEqual(D.project_env_pointer(str(self.root / "demo")), "")

    def test_unconfigured_directory_is_silent(self) -> None:
        os.environ[agent_config.ENV_FILE] = str(self.root / "absent.json")
        agent_config._reset_cache()
        self.assertEqual(D.project_env_pointer(str(self.root / "demo")), "")

    def test_missing_directory_is_silent(self) -> None:
        path = self.root / "agent.json"
        path.write_text(json.dumps(
            {"paths": {"project_env_dir": str(self.root / "no-such")}}), encoding="utf-8")
        os.environ[agent_config.ENV_FILE] = str(path)
        agent_config._reset_cache()
        self.assertEqual(D.project_env_pointer(str(self.root / "demo")), "")

    def test_caller_is_on_the_dispatch_success_path(self) -> None:
        """The pointer line sits between ``task_text`` and ``submit_prompt``."""
        source = Path(D.__file__).read_text(encoding="utf-8")
        self.assertIn("project_env_pointer(cwd)", source)
        self.assertLess(source.index("task_text = args.task"),
                        source.index("project_env_pointer(cwd)"))
        self.assertLess(source.index("project_env_pointer(cwd)"),
                        source.index("submit_prompt(base, token, sid, task_text"))
        # stderr only: a stdout print would corrupt --json.
        self.assertIn("file=sys.stderr", source[source.index("pointer = project_env_pointer"):
                                                source.index("pidemp = prompt_ref")])


class DispatchSuccessPathTest(unittest.TestCase):
    """Drive the detach success path with a fake sid; no remote session."

    A ``--detach`` dispatch submits a prompt and returns before polling, so it
    reaches the pointer line without any HTTP response beyond the submit. That
    is enough to prove the call happens in production and that stderr stays
    separate from the ``--json`` stdout block.
    """

    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in (agent_config.ENV_FILE,)}
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.env_dir = self.root / "project_env"
        self.env_dir.mkdir()
        self.cwd = self.root / "demo"
        self.cwd.mkdir()
        (self.env_dir / "demo.md").write_text("# demo\n", encoding="utf-8")
        path = self.root / "agent.json"
        path.write_text(json.dumps(
            {"paths": {"project_env_dir": str(self.env_dir)}}), encoding="utf-8")
        os.environ[agent_config.ENV_FILE] = str(path)
        agent_config._reset_cache()

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        agent_config._reset_cache()
        self._tmp.cleanup()

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        patches = [
            mock.patch.object(D, "quota_gate", lambda: None),
            mock.patch.object(D, "load_catalog", lambda: object()),
            mock.patch.object(D, "resolve_choice", lambda a, p, c: ("prov", "model", "high")),
            mock.patch.object(D, "read_token", lambda: "fake-token"),
            mock.patch.object(D, "discover_base", lambda t, p: "http://127.0.0.1:1"),
            mock.patch.object(D, "create_session", lambda *a, **k: "session_fake"),
            # ISS-95: session creation now refuses without a baseline charter.
            # This test's subject is the project-environment pointer, not the
            # charter, so it supplies both halves directly.
            mock.patch.object(D, "charter_files",
                              lambda n: ("/fake/worker.md", "")),
            mock.patch.object(D, "record_created", lambda *a, **k: None),
            mock.patch.object(D, "apply_profile", lambda *a, **k: {}),
            mock.patch.object(D, "activate_session", lambda *a, **k: None),
            mock.patch.object(D, "model_self_name", lambda *a, **k: "fake-name"),
            mock.patch.object(D, "submit_prompt", lambda *a, **k: None),
            mock.patch.object(D, "guard_token", lambda *a, **k: None),
            mock.patch.object(D, "_sess_events", lambda: mock.Mock(
                apply_state_dir=lambda *a, **k: None)),
        ]
        started = [p.start() for p in patches]
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = D.main(argv)
        finally:
            for p in reversed(patches):
                p.stop()
        return rc, out.getvalue(), err.getvalue()

    def test_pointer_on_stderr_json_stays_parseable(self) -> None:
        rc, out, err = self._run([
            "--task", "do work", "--cwd", str(self.cwd), "--detach",
            "--no-activate", "--skip-quota-gate", "--json"])
        self.assertEqual(rc, 0)
        self.assertIn(f"项目环境：demo → {self.env_dir / 'demo.md'}", err)
        # The pointer never enters stdout; --json stays one JSON object.
        self.assertNotIn("项目环境", out)
        payload = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(payload["session_id"], "session_fake")

    def test_no_document_is_silent_and_dispatch_still_succeeds(self) -> None:
        other = self.root / "unregistered"
        other.mkdir()
        rc, out, err = self._run([
            "--task", "do work", "--cwd", str(other), "--detach",
            "--no-activate", "--skip-quota-gate", "--json"])
        self.assertEqual(rc, 0)
        self.assertNotIn("项目环境", err)
        self.assertEqual(json.loads(out.strip().splitlines()[-1])["session_id"],
                         "session_fake")


if __name__ == "__main__":
    unittest.main()
