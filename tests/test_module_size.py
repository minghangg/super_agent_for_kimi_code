"""Tests for ``tools/module_size.py`` (user ruling 2026-11-25).

The tool exists because a size rule with no measurement is a wish: the
repository's ``engine.py`` grew from 599 to 5787 lines over 76 commits, none of
which added 5000 lines. Each one added a few dozen.

Two properties carry the design, and each is a counter-example rather than a
happy path:

* **growth fails, history reports.** A check that fails on the 13 files that are
  already over 750 lines can never pass, and a check that can never pass gets
  switched off -- which is how #109's six permanently-red selfchecks happened.
  So an unchanged large file must be reported and still exit 0, while a file
  that grew past its baseline must exit 1.
* **a scan that did not run must not look clean.** Unreadable or unparsable
  input exits 2, never 0.

Run: ``python -m pytest tests/test_module_size.py -q``
"""
from __future__ import annotations

import importlib.util
import io
import contextlib
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("module_size", ROOT / "tools" / "module_size.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ms = _load()


class Growth(unittest.TestCase):
    """Historical debt never fails; new debt and large growth do.

    The rule (user ruling 2026-11-25): a file that was already over the line is
    never a failure for being big, because blocking it stops work on exactly the
    code that needs work. ``worker.md`` §3.1.1 puts it the other way round:
    refusing to edit a large file is a P0, since that makes the debt permanent.
    """

    def setUp(self) -> None:
        self._saved = dict(ms.BASELINE)

    def tearDown(self) -> None:
        ms.BASELINE.clear()
        ms.BASELINE.update(self._saved)

    def row(self, name: str, lines: int) -> dict:
        return {"path": name, "lines": lines, "docstring": True,
                "functions": 1, "long_functions": []}

    def test_an_unchanged_large_file_is_not_a_failure(self):
        ms.BASELINE["big.py"] = 900
        self.assertEqual(ms.over_budget([self.row("big.py", 900)]), [])

    def test_a_large_file_may_be_edited_without_going_red(self):
        """The whole point: ordinary edits to historical debt must pass."""
        ms.BASELINE["big.py"] = 900
        for lines in (905, 920, 900 + ms.GROWTH_ALLOWANCE):
            with self.subTest(lines=lines):
                self.assertEqual(ms.over_budget([self.row("big.py", lines)]), [])

    def test_a_large_file_growing_past_the_allowance_fails(self):
        ms.BASELINE["big.py"] = 900
        over = ms.over_budget([self.row("big.py", 900 + ms.GROWTH_ALLOWANCE + 1)])
        self.assertEqual(len(over), 1)
        self.assertIn("grew", over[0]["reason"])

    def test_a_new_large_file_fails(self):
        ms.BASELINE.pop("new.py", None)
        over = ms.over_budget([self.row("new.py", ms.FILE_ALARM + 1)])
        self.assertEqual(len(over), 1)
        self.assertIn("new file", over[0]["reason"])

    def test_a_small_new_file_passes(self):
        ms.BASELINE.pop("small.py", None)
        self.assertEqual(ms.over_budget([self.row("small.py", 100)]), [])

    def test_a_file_that_was_under_the_line_and_crossed_it_fails(self):
        """The one case that is a genuine new problem."""
        ms.BASELINE.pop("tiny.py", None)
        self.assertEqual(ms.over_budget([self.row("tiny.py", ms.FILE_ALARM)]), [])
        self.assertEqual(len(ms.over_budget([self.row("tiny.py", ms.FILE_ALARM + 1)])), 1)

    def test_a_shrunk_file_is_never_a_failure_and_is_marked_cleaned(self):
        ms.BASELINE["big.py"] = 900
        self.assertEqual(ms.over_budget([self.row("big.py", 700)]), [])
        status = ms.cleanup_status([self.row("big.py", 700)])
        self.assertTrue(status[0]["cleaned"])
        self.assertEqual(status[0]["delta"], -200)


class Scanning(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_a_missing_docstring_is_reported(self):
        self.write("nodoc.py", "x = 1\n")
        rows = ms.scan(self.root)
        self.assertEqual(ms.missing_docstrings(rows), ["nodoc.py"])

    def test_a_present_docstring_is_not_reported(self):
        self.write("ok.py", '"""What this module is for."""\nx = 1\n')
        self.assertEqual(ms.missing_docstrings(ms.scan(self.root)), [])

    def test_a_long_function_is_counted(self):
        body = "\n".join(f"    x{i} = {i}" for i in range(60))
        self.write("long.py", f'"""doc."""\n\ndef f():\n{body}\n')
        rows = ms.scan(self.root)
        self.assertEqual(len(rows[0]["long_functions"]), 1)
        self.assertGreater(rows[0]["long_functions"][0]["lines"], ms.FUNC_ALARM)

    def test_an_unparsable_file_is_an_error_not_a_clean_pass(self):
        self.write("broken.py", "def f(:\n")
        rows = ms.scan(self.root)
        self.assertIn("error", rows[0])
        self.assertEqual(ms.main([], root=self.root), 2)

    def test_an_empty_directory_exits_two(self):
        """A scan that found nothing must not report success."""
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = ms.main([], root=self.root)
        self.assertEqual(rc, 2)
        self.assertIn("扫不到", buf.getvalue())


class RealRepository(unittest.TestCase):
    """The shipped tree must not be over its own recorded baselines."""

    def test_no_file_has_grown_past_its_baseline(self):
        rows = ms.scan()
        self.assertTrue(rows, "scan found no modules")
        self.assertEqual(ms.over_budget(rows), [])

    def test_every_recorded_baseline_names_a_real_file(self):
        names = {r["path"] for r in ms.scan()}
        for name in ms.BASELINE:
            self.assertIn(name, names, f"baseline names a file that is gone: {name}")

    def test_at_most_one_module_lacks_a_docstring(self):
        """Pinned as a count so a new undocumented module fails the test."""
        missing = ms.missing_docstrings(ms.scan())
        self.assertLessEqual(len(missing), 1, missing)


if __name__ == "__main__":
    unittest.main()
