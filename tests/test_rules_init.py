"""Tests for ``super_agent/rules_init.py``.

Six behaviours, one class each, because each one is a promise the command makes
on a re-run: skip identical content, never overwrite a differing active copy
without ``--force``, never delete an unselected file, report a stale candidate
instead of adopting it, leave everything alone on ``--dry-run``, and write
nothing outside the active directory.

Run: ``python -m pytest tests/test_rules_init.py -q``
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from super_agent import rules_init  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for level in rules_init.LEVELS:
            (self.root / rules_init.CANDIDATES / level).mkdir(parents=True)
        (self.root / rules_init.ACTIVE).mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def candidate(self, name: str, text: str, level: str = "rules") -> Path:
        path = self.root / rules_init.CANDIDATES / level / (name + ".md")
        path.write_text(text, encoding="utf-8")
        return path

    def active(self, name: str) -> Path:
        return self.root / rules_init.ACTIVE / (name + ".md")

    def run_cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = rules_init.main(["--root", str(self.root), *argv])
        return code, out.getvalue() + err.getvalue()


class IdempotentRun(Base):
    def test_second_run_copies_nothing(self):
        self.candidate("alpha", "alpha body\n")
        code, text = self.run_cli("--rules", "alpha")
        self.assertEqual(code, 0, text)
        self.assertIn("same=0 copied=1", text)
        code, text = self.run_cli("--rules", "alpha")
        self.assertEqual(code, 0, text)
        self.assertIn("same=1 copied=0 conflict=0 stale=0 orphan=0", text)
        self.assertEqual(self.active("alpha").read_text(encoding="utf-8"),
                         "alpha body\n")


class Conflict(Base):
    def test_differing_active_copy_is_not_overwritten(self):
        candidate = self.candidate("alpha", "candidate body\n")
        self.active("alpha").write_text("local edit\n", encoding="utf-8")
        # An active copy newer than its candidate was edited locally.
        os.utime(candidate, (0, 0))
        code, text = self.run_cli("--rules", "alpha")
        self.assertEqual(code, 1, text)
        self.assertIn("copied=0", text)
        self.assertIn("conflict=1", text)
        self.assertEqual(self.active("alpha").read_text(encoding="utf-8"),
                         "local edit\n")

    def test_force_overwrites_the_conflict(self):
        candidate = self.candidate("alpha", "candidate body\n")
        self.active("alpha").write_text("local edit\n", encoding="utf-8")
        os.utime(candidate, (0, 0))
        code, text = self.run_cli("--rules", "alpha", "--force")
        self.assertEqual(code, 0, text)
        self.assertIn("copied=1", text)
        self.assertEqual(self.active("alpha").read_text(encoding="utf-8"),
                         "candidate body\n")


class Orphan(Base):
    def test_unselected_active_file_is_kept_and_reported(self):
        self.candidate("alpha", "a\n")
        self.candidate("beta", "b\n")
        self.run_cli("--rules", "alpha,beta")
        code, text = self.run_cli("--rules", "alpha")
        self.assertEqual(code, 0, text)
        self.assertIn("orphan=1", text)
        self.assertTrue(self.active("beta").is_file())

    def test_unticking_never_deletes_a_file(self):
        self.candidate("alpha", "a\n")
        self.candidate("beta", "b\n")
        self.run_cli("--all")
        before = sorted(p.name for p in (self.root / rules_init.ACTIVE).glob("*.md"))
        self.run_cli("--rules", "alpha")
        after = sorted(p.name for p in (self.root / rules_init.ACTIVE).glob("*.md"))
        self.assertEqual(before, after)


class Stale(Base):
    def test_newer_candidate_is_reported_not_adopted(self):
        self.candidate("alpha", "new candidate\n")
        self.active("alpha").write_text("old active\n", encoding="utf-8")
        os.utime(self.active("alpha"), (0, 0))
        code, text = self.run_cli("--rules", "alpha")
        self.assertEqual(code, 1, text)
        self.assertIn("stale=1", text)
        self.assertEqual(self.active("alpha").read_text(encoding="utf-8"),
                         "old active\n")


class DryRun(Base):
    def snapshot(self) -> list[tuple[str, int]]:
        return sorted((p.as_posix(), p.stat().st_size)
                      for p in self.root.rglob("*") if p.is_file())

    def test_dry_run_writes_nothing(self):
        self.candidate("alpha", "a\n")
        before = self.snapshot()
        code, text = self.run_cli("--rules", "alpha", "--dry-run")
        self.assertEqual(code, 0, text)
        self.assertIn("copied=1", text)
        self.assertIn("dry_run=1", text)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.active("alpha").exists())


class Listing(Base):
    def test_list_writes_nothing_and_names_both_levels(self):
        self.candidate("alpha", "a\n", level="rules")
        self.candidate("beta", "b\n", level="skills")
        before = sorted(p.name for p in self.root.rglob("*"))
        code, text = self.run_cli("--list")
        self.assertEqual(code, 0, text)
        self.assertIn("alpha", text)
        self.assertIn("beta", text)
        self.assertEqual(sorted(p.name for p in self.root.rglob("*")), before)

    def test_unknown_name_stops_before_writing(self):
        self.candidate("alpha", "a\n")
        code, text = self.run_cli("--rules", "nope")
        self.assertEqual(code, 2)
        self.assertIn("未知候选", text)
        self.assertFalse(self.active("alpha").exists())

    def test_duplicate_stem_across_levels_is_an_error(self):
        self.candidate("alpha", "a\n", level="rules")
        self.candidate("alpha", "b\n", level="skills")
        code, text = self.run_cli("--list")
        self.assertEqual(code, 2)
        self.assertIn("重名", text)


if __name__ == "__main__":
    unittest.main()
