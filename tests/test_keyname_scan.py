"""Private Issues may name sessions; public files keep the key-name gate."""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import keyname_scan as scan  # noqa: E402

PRIVATE_KEY = "pm-" + "private-abc12345"


class PrivateIssue(unittest.TestCase):
    @mock.patch.object(scan, "issue_text", side_effect=AssertionError("network called"))
    def test_private_issue_is_exempt_without_reading_github(self, issue_text):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = scan.main(["--issue", "105", "--project", "private",
                            "--cid", "7099683763"])
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("私有 Issue", out.getvalue())
        issue_text.assert_not_called()

    @mock.patch.object(scan, "issue_text", return_value=PRIVATE_KEY)
    def test_public_issue_keeps_the_old_scan(self, issue_text):
        with contextlib.redirect_stdout(io.StringIO()):
            rc = scan.main(["--issue", "86", "--project", "public",
                            "--public-issue"])
        self.assertEqual(rc, 1)
        issue_text.assert_called_once()


class RepositoryFiles(unittest.TestCase):
    def test_key_in_a_file_still_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "document.md"
            path.write_text(f"owner={PRIVATE_KEY}\n", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                rc = scan.main(["--file", str(path)])
        self.assertEqual(rc, 1)

    def test_private_issue_does_not_hide_a_file_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "document.md"
            path.write_text(PRIVATE_KEY, encoding="utf-8")
            with mock.patch.object(scan, "issue_text") as issue_text:
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = scan.main(["--issue", "105", "--project", "private",
                                    "--file", str(path)])
        self.assertEqual(rc, 1)
        issue_text.assert_not_called()


if __name__ == "__main__":
    unittest.main()
