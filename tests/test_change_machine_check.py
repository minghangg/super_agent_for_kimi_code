"""The md5 double check must be able to go red, and to say "cannot judge".

Two failures this file exists to prevent, both measured (#100 P0-2):

* **the criterion that is always true** -- the watched path used to be a
  literal host path. On any machine without that file ``md5_state`` returned
  ``(0, "")`` for both reads, so ``before == after`` held and the assertion
  passed while proving nothing. An absent file now returns ``None`` and the
  gate reports SKIP.
* **the red that is not red** -- an appended row without ``seq`` leaves
  ``last_seq`` unchanged, so a ``last_seq`` criterion stays green while the
  file grew. The md5 pair sees it.

The third answer matters as much as the first two: a file that exists and did
not change is the only state in which the gate may report a pass.

Run: ``python -m pytest tests/test_change_machine_check.py -q``
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.change_machine_check import md5_state


class AbsentFileTest(unittest.TestCase):
    """Absent is "cannot judge", never "unchanged"."""

    def test_an_absent_file_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(md5_state(Path(tmp) / "events.jsonl"))

    def test_none_is_not_the_empty_state(self):
        """``(0, "")`` and ``None`` were the same answer before the fix, and
        that is exactly why the assertion could never fail."""
        self.assertNotEqual(md5_state(Path("nope") / "x.jsonl"), (0, ""))


class AppendedRowTest(unittest.TestCase):
    """A row without ``seq`` changes the pair and leaves ``last_seq`` alone."""

    def test_an_appended_row_changes_the_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text(json.dumps({"seq": 1, "type": "run.started"}) + "\n",
                            encoding="utf-8")
            before = md5_state(path)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"type": "run.started"}) + "\n")
            after = md5_state(path)
            self.assertNotEqual(before, after)
            self.assertGreater(after[0], before[0])

    def test_an_unchanged_file_keeps_the_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text(json.dumps({"seq": 1}) + "\n", encoding="utf-8")
            self.assertEqual(md5_state(path), md5_state(path))


if __name__ == "__main__":
    unittest.main()
