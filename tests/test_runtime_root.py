"""End-to-end tests for runtime-root selection through the CLI.

Every case here runs ``cli.main`` and inspects the filesystem, because the
failure this file exists to catch was invisible to unit tests: ``choose_root``
was correct but nobody called it. A test that imports the function proves
nothing about the product path.

Run: ``python -m pytest tests/test_runtime_root.py -q``
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from super_agent import cli, config  # noqa: E402

ENGINE = ("index.json", "events.jsonl", "cursor.json", "run_heartbeat.json",
          "health.json")


class Base(unittest.TestCase):
    """Run the CLI in a scratch working directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.cwd = self.base / "work"
        self.cwd.mkdir()
        self._old_cwd = Path.cwd()
        os.chdir(self.cwd)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self.tmp.cleanup()

    def once(self, *extra: str) -> tuple[int, str]:
        argv = ["once", "--mode", "observe", "--source", "none",
                "--session-api", "none", *extra]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue() + err.getvalue()

    def seed(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        for name in ENGINE:
            (root / name).write_text("{}\n", encoding="utf-8")


class DefaultRoot(Base):
    def test_default_run_creates_data_not_state(self):
        code, text = self.once()
        self.assertEqual(code, 0, text)
        self.assertTrue((self.cwd / "data").is_dir())
        self.assertFalse((self.cwd / "state").exists())

    def test_default_root_in_config_is_data(self):
        self.assertEqual(config.DEFAULTS["state_dir"], "data")


class LegacyFallback(Base):
    """A **declared** legacy root is read; a guessed one never is (#140).

    The old default was the ``state`` sibling of the chosen root, i.e. a
    directory found by its name. When a deployment moved its root to ``data``
    that guess named a directory that no longer exists, so the look-up said
    "no legacy data" for ever - silence instead of an error. The legacy root is
    now declared or absent.
    """

    def once_with_legacy(self, legacy: Path, *extra: str) -> tuple[int, str]:
        cfg_path = self.base / "cfg.json"
        cfg_path.write_text('{"legacy_state_dir": "%s"}'
                            % legacy.as_posix(), encoding="utf-8")
        return self.once("--config", str(cfg_path), *extra)

    def test_declared_legacy_state_is_read_end_to_end(self):
        old = self.base / "old_runtime"
        self.seed(old)
        code, text = self.once_with_legacy(old)
        self.assertEqual(code, 0, text)
        self.assertIn("runtime state read from legacy path", text)
        self.assertFalse((self.cwd / "data").exists())
        self.assertTrue((old / "allowlist.last-good").is_file())

    def test_both_present_uses_data_and_warns(self):
        old = self.base / "old_runtime"
        self.seed(old)
        self.seed(self.cwd / "data")
        code, text = self.once_with_legacy(old)
        self.assertEqual(code, 0, text)
        self.assertIn("both", text)
        self.assertIn("delete", text)

    def test_fresh_install_has_no_warning(self):
        code, text = self.once()
        self.assertEqual(code, 0, text)
        self.assertNotIn("WARN", text)

    def test_undeclared_sibling_state_is_not_adopted(self):
        """A ``state/`` beside the root is only read when it is declared.

        This is the exact deployment that #140 fixed: root ``data`` with a
        stale ``state`` printed beside it. Reading it by name would resurrect
        files the migration deliberately left behind.
        """
        self.seed(self.cwd / "state")
        code, text = self.once()
        self.assertEqual(code, 0, text)
        self.assertNotIn("runtime state read from legacy path", text)
        self.assertTrue((self.cwd / "data" / "index.json").is_file())
        self.assertFalse((self.cwd / "state" / "allowlist.last-good").exists())


class ExplicitRoot(Base):
    """``--state-dir X`` never adopts a directory found by name beside ``X``."""

    def test_explicit_root_does_not_fall_back_to_its_sibling(self):
        """A ``state/`` beside ``X`` is not the legacy root unless declared.

        The sibling guess used to make an isolated run read a directory it was
        never pointed at. ``--state-dir X`` now reads ``X`` or nothing.
        """
        target = self.base / "a" / "x"
        target.mkdir(parents=True)
        self.seed(self.base / "a" / "state")
        code, text = self.once("--state-dir", str(target))
        self.assertEqual(code, 0, text)
        self.assertNotIn("runtime state read from legacy path", text)
        self.assertTrue((target / "index.json").is_file())

    def test_explicit_root_does_not_reach_into_the_checkout(self):
        # A ``state/`` in the current directory is not ``X``, so it must not be
        # adopted either.
        self.seed(self.cwd / "state")
        target = self.base / "b" / "x"
        target.mkdir(parents=True)
        code, text = self.once("--state-dir", str(target))
        self.assertEqual(code, 0, text)
        self.assertNotIn("runtime state read from legacy path", text)
        self.assertTrue((target / "index.json").is_file())

    def test_explicit_fresh_root_has_no_warning(self):
        target = self.base / "c" / "x"
        code, text = self.once("--state-dir", str(target))
        self.assertEqual(code, 0, text)
        self.assertNotIn("WARN", text)

    def test_explicit_root_still_reads_a_declared_legacy_root(self):
        target = self.base / "d" / "x"
        target.mkdir(parents=True)
        old = self.base / "old_runtime"
        self.seed(old)
        cfg_path = self.base / "cfg.json"
        cfg_path.write_text('{"legacy_state_dir": "%s"}'
                            % old.as_posix(), encoding="utf-8")
        code, text = self.once("--config", str(cfg_path), "--state-dir", str(target))
        self.assertEqual(code, 0, text)
        self.assertIn("runtime state read from legacy path", text)
        self.assertFalse((target / "index.json").exists())


if __name__ == "__main__":
    unittest.main()
