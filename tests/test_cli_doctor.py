"""``doctor --project``: one project's environment document (#101).

The contract is narrow on purpose, and each rule below is one a "read the file"
helper would plausibly break:

* the file name **is** the project key -- the same token ``gh_projects.json``
  and dispatch use. A repository name is not a key, and passing one is
  "unregistered", not "the closest match";
* the document is printed **verbatim**: no parsing, no re-wrapping, no
  front-matter. ``diff`` against the file is empty;
* "not registered" (rc=1, missing entry) and "misconfigured" (rc=2, no
  directory) are different codes, because they are different actions;
* two candidates for one key is an error, never "take the first". ``glob``
  order is not a contract, so taking the first makes the answer depend on the
  file system.

The second rule cannot be built on NTFS -- it is case-insensitive, so
``superagent.md`` and ``Superagent.md`` are one file, and ``fsutil
SetCaseSensitiveInfo`` is refused without the privilege. That branch is proved
here instead: ``glob`` is patched to return two candidates, which is the only
way to show the refusal really fires.

Run: ``python -m pytest tests/test_cli_doctor.py -q``
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

from super_agent import agent_config, cli, config


class _Base(unittest.TestCase):
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

    def configure(self, paths: dict | None = None) -> None:
        doc = {"paths": {"project_env_dir": str(self.env_dir)}}
        doc["paths"].update(paths or {})
        path = self.root / "agent.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        os.environ[agent_config.ENV_FILE] = str(path)
        agent_config._reset_cache()

    def doctor(self, project: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["doctor", "--project", project])
        return rc, out.getvalue(), err.getvalue()


class VerbatimPrintTest(_Base):
    def test_prints_the_file_byte_for_byte(self) -> None:
        self.configure()
        body = "# demo 环境档案\n\n仓库：owner/demo\r\n\n表格 | 一行\n"
        (self.env_dir / "demo.md").write_bytes(body.encode("utf-8"))
        rc, out, err = self.doctor("demo")
        self.assertEqual(rc, 0)
        self.assertEqual(out, body)
        self.assertIn("来源：", err)

    def test_file_name_is_the_key_not_the_repository_name(self) -> None:
        # ``superagent.md`` is the key; ``super_agent_for_kimi_code.md`` is a
        # different name and must not be picked up by a repo-name lookup.
        self.configure()
        (self.env_dir / "superagent.md").write_bytes(b"KEY\n")
        rc, out, _ = self.doctor("superagent")
        self.assertEqual((rc, out), (0, "KEY\n"))
        rc, out, err = self.doctor("super_agent_for_kimi_code")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("未登记", err)


class UnregisteredTest(_Base):
    def test_missing_entry_is_rc1_and_silent_on_stdout(self) -> None:
        self.configure()
        rc, out, err = self.doctor("webapp")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("未登记：webapp", err)

    def test_near_miss_is_reported_but_never_adopted(self) -> None:
        # ``super_agent`` normalises to ``superagent`` -- the same key. It is
        # reported as the closest name and still rc=1: "looks like" is not "is".
        self.configure()
        (self.env_dir / "superagent.md").write_text("CHOSEN\n", encoding="utf-8")
        rc, out, err = self.doctor("super_agent")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("最接近", err)
        self.assertIn("superagent", err)

    def test_unrelated_name_gets_no_hint(self) -> None:
        self.configure()
        (self.env_dir / "superagent.md").write_text("CHOSEN\n", encoding="utf-8")
        _rc, _out, err = self.doctor("webapp")
        self.assertNotIn("最接近", err)


class AmbiguousTest(_Base):
    def test_two_candidates_for_one_key_refuse(self) -> None:
        """The a-branch: two files whose stem is exactly the key.

        Built by patching ``glob``, not on disk: NTFS cannot hold two names that
        differ only by case, and a trailing space changes the stem, so neither
        lands in this branch for real.
        """
        self.configure()
        first = self.env_dir / "demo.md"
        second = self.env_dir / "demo.MD"
        real_glob = Path.glob

        def fake_glob(self, pattern, **kwargs):  # noqa: ANN001
            if pattern == "*.md":
                return iter([first, second])
            return real_glob(self, pattern, **kwargs)

        with mock.patch.object(Path, "glob", fake_glob):
            rc, out, err = self.doctor("demo")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("歧义", err)
        self.assertIn("2 个候选", err)

    def test_one_candidate_still_prints(self) -> None:
        self.configure()
        (self.env_dir / "demo.md").write_bytes(b"ONLY\n")
        rc, out, _ = self.doctor("demo")
        self.assertEqual((rc, out), (0, "ONLY\n"))


class ConfiguredDirectoryTest(_Base):
    def test_unconfigured_dir_is_rc2(self) -> None:
        path = self.root / "agent.json"
        path.write_text("{}", encoding="utf-8")
        os.environ[agent_config.ENV_FILE] = str(path)
        agent_config._reset_cache()
        rc, out, err = self.doctor("demo")
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("project_env_dir 未配置", err)

    def test_missing_dir_is_rc2_and_names_the_path(self) -> None:
        self.configure({"project_env_dir": str(self.root / "nope")})
        rc, out, err = self.doctor("demo")
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("目录不存在", err)
        self.assertIn("nope", err)

    def test_missing_dir_differs_from_missing_entry(self) -> None:
        """A configuration fault must not read as a missing registration."""
        self.configure({"project_env_dir": str(self.root / "nope")})
        rc, _out, err = self.doctor("webapp")
        self.assertEqual(rc, 2)
        self.assertNotIn("未登记", err)


class ConfigKeyTest(_Base):
    def test_relative_value_anchors_to_the_checkout(self) -> None:
        path = self.root / "agent.json"
        path.write_text(json.dumps(
            {"paths": {"project_env_dir": "new_workbench/config/project_env"}}),
            encoding="utf-8")
        os.environ[agent_config.ENV_FILE] = str(path)
        agent_config._reset_cache()
        expected = (agent_config.CHECKOUT
                    / "new_workbench" / "config" / "project_env").resolve()
        self.assertEqual(Path(agent_config.project_env_dir()), expected)

    def test_unset_is_the_empty_string(self) -> None:
        os.environ[agent_config.ENV_FILE] = str(self.root / "absent.json")
        agent_config._reset_cache()
        self.assertEqual(agent_config.project_env_dir(), "")

    def test_non_string_is_refused_by_validate(self) -> None:
        with self.assertRaises(config.ConfigError):
            config.load(overrides={"paths": {"project_env_dir": 3}})

    def test_declared_in_defaults_so_validate_owns_it(self) -> None:
        self.assertIn("project_env_dir", config.DEFAULTS["paths"])


if __name__ == "__main__":
    unittest.main()
