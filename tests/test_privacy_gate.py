"""Tests for ``tools/privacy_gate.py``.

The gate is only trustworthy if each surface can independently turn red, so the
history and remote-URL checks are exercised against throwaway repositories in
both directions: a neutral identity/URL stays green, a private one goes red.

Private tokens are assembled from fragments, exactly as the gate does, so this
file passes the very scan it performs.

Run: ``python -m pytest tests/test_privacy_gate.py -q``
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import privacy_gate as gate  # noqa: E402

PRIVATE_DOMAIN = gate._joined("ming", "hangg", ".com")
PRIVATE_PROJECT = gate._joined("lai", "ke")


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


class GateSource(unittest.TestCase):
    def test_readme_is_not_exempt(self):
        self.assertFalse(hasattr(gate, "EXEMPT_MACHINE_CODES"))
        self.assertEqual(gate.scan_files(ROOT), [])
        self.assertEqual(gate.main(), 0)

    def test_repository_history_is_clean(self):
        self.assertEqual(gate.scan_history(ROOT), [])

    def test_repository_origin_is_clean(self):
        self.assertEqual(gate.scan_remote(ROOT), [])


class History(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        git(self.root, "init", "-q", "-b", "main")

    def tearDown(self):
        self.tmp.cleanup()

    def commit_as(self, email: str) -> None:
        who = f"{gate._joined('saf', 'kc')}-bot"
        subprocess.run(
            ["git", "-c", f"user.name={who}", "-c", f"user.email={email}",
             "commit", "-q", "--allow-empty", "-m", "probe"],
            cwd=self.root, check=True, capture_output=True)

    def test_neutral_identity_is_clean(self):
        self.commit_as("test@example.com")
        self.assertEqual(gate.scan_history(self.root), [])

    def test_private_identity_turns_red(self):
        self.commit_as("x@" + PRIVATE_DOMAIN)
        hits = gate.scan_history(self.root)
        self.assertTrue(any("commit " in h and "命中" in h for h in hits), hits)


class Remote(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        git(self.root, "init", "-q", "-b", "main")

    def tearDown(self):
        self.tmp.cleanup()

    def set_origin(self, url: str) -> None:
        git(self.root, "remote", "add", "origin", url)

    def test_neutral_url_is_clean(self):
        self.set_origin("https://example.invalid/safkc-bot/super_agent.git")
        self.assertEqual(gate.scan_remote(self.root), [])

    def test_owner_segment_is_not_scanned(self):
        self.set_origin("https://example.invalid/" + PRIVATE_PROJECT + "/repo.git")
        self.assertEqual(gate.scan_remote(self.root), [])

    def test_private_repository_path_turns_red(self):
        self.set_origin("https://example.invalid/acct/" + PRIVATE_PROJECT + ".git")
        self.assertTrue(gate.scan_remote(self.root))


class Ignore(unittest.TestCase):
    def test_pytest_cache_is_ignored_by_the_tracked_gitignore(self):
        if shutil.which("git") is None:
            self.skipTest("git not installed")
        out = git(ROOT, "check-ignore", "-v", ".pytest_cache/")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(".gitignore:", out.stdout)
        self.assertNotIn(".pytest_cache/.gitignore:", out.stdout)


if __name__ == "__main__":
    unittest.main()
