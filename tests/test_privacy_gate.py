"""Tests for ``tools/privacy_gate.py``.

The gate is only trustworthy if each surface can independently turn red, so the
history and remote-URL checks are exercised against throwaway repositories in
both directions: a neutral identity/URL stays green, a private one goes red.

Private tokens are assembled from fragments, exactly as the gate does, so this
file passes the very scan it performs.

Run: ``python -m pytest tests/test_privacy_gate.py -q``
"""
from __future__ import annotations

import contextlib
import io
import re
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

    def commit_as(self, email: str, *, branch: str | None = None) -> str:
        who = f"{gate._joined('saf', 'kc')}-bot"
        if branch:
            subprocess.run(["git", "checkout", "-q", "-b", branch],
                           cwd=self.root, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", f"user.name={who}", "-c", f"user.email={email}",
             "commit", "-q", "--allow-empty", "-m", "probe"],
            cwd=self.root, check=True, capture_output=True)
        return git(self.root, "rev-parse", "HEAD").stdout.strip()

    def test_neutral_identity_is_clean(self):
        self.commit_as("test@example.com")
        self.assertEqual(gate.scan_history(self.root), [])

    def test_private_identity_turns_red(self):
        self.commit_as("x@" + PRIVATE_DOMAIN)
        hits = gate.scan_history(self.root)
        self.assertTrue(any("commit " in h and "命中" in h for h in hits), hits)

    def test_default_refs_scans_the_branch_only(self):
        self.commit_as("test@example.com")
        sha = self.commit_as("x@" + PRIVATE_DOMAIN, branch="residue")
        git(self.root, "checkout", "-q", "main")
        git(self.root, "update-ref", "refs/original/refs/heads/residue", sha)
        git(self.root, "update-ref", "-d", "refs/heads/residue")

        self.assertEqual(gate.scan_history(self.root), [])
        hits = gate.scan_history(self.root, "all")
        self.assertTrue(hits, "refs=all must reach the residue")
        self.assertTrue(any("refs/original/refs/heads/residue" in h
                            for h in hits), hits)

    def test_main_defaults_to_branch(self):
        self.assertEqual(gate._parser().parse_args([]).refs, "branch")
        self.assertEqual(gate._parser().parse_args(["--refs", "all"]).refs, "all")


class RuntimePaths(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        git(self.root, "init", "-q", "-b", "main")

    def tearDown(self):
        self.tmp.cleanup()

    def test_tracked_runtime_directory_turns_red(self):
        (self.root / "state").mkdir()
        (self.root / "state" / "index.json").write_text("{}", encoding="utf-8")
        git(self.root, "add", "state/index.json")
        self.assertTrue(gate.scan_runtime_paths(self.root))

    def test_untracked_runtime_directory_is_fine(self):
        (self.root / "state").mkdir()
        (self.root / "state" / "index.json").write_text("{}", encoding="utf-8")
        self.assertEqual(gate.scan_runtime_paths(self.root), [])

    def test_repository_has_no_tracked_runtime_paths(self):
        self.assertEqual(gate.scan_runtime_paths(ROOT), [])


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


class Tokens(unittest.TestCase):
    """A token-shaped value in a tracked file must turn the gate red."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        git(self.root, "init", "-q", "-b", "main")

    def tearDown(self):
        self.tmp.cleanup()

    def track(self, name: str, text: str) -> None:
        (self.root / name).write_text(text, encoding="utf-8")
        git(self.root, "add", name)

    def assert_token_hit(self, name: str, text: str, label: str) -> None:
        self.track(name, text)
        hits = gate.scan_files(self.root)
        self.assertTrue(any(label in h for h in hits), hits)
        self.assertTrue(all("token" in h for h in hits), hits)

    def test_classic_github_pat_turns_red(self):
        self.assert_token_hit("leak.txt", gate._joined("gh", "p_") + "A" * 36,
                              "token: github pat")

    def test_other_github_pat_prefixes_turn_red(self):
        for prefix in ("gho_", "ghu_", "ghs_", "ghr_"):
            with self.subTest(prefix=prefix):
                self.assert_token_hit(f"{prefix}.txt", prefix + "B" * 36,
                                      "token: github pat")

    def test_fine_grained_github_pat_turns_red(self):
        self.assert_token_hit("fine.txt", gate._joined("gith", "ub_p", "at_")
                              + "C" * 40, "token: github fine-grained pat")

    def test_sk_key_turns_red(self):
        self.assert_token_hit("sk.txt", gate._joined("s", "k-") + "d" * 40,
                              "token: sk api key")

    def test_aws_access_key_id_turns_red(self):
        self.assert_token_hit("aws.txt", gate._joined("AK", "IA") + "D" * 16,
                              "token: aws access key id")

    def test_ordinary_hyphenated_words_stay_green(self):
        self.track("prose.txt",
                   "sk-learn and task-name are words, not credentials\n")
        self.assertEqual(gate.scan_files(self.root), [])

    def test_bare_prefixes_stay_green(self):
        self.track("prefixes.txt",
                   gate._joined("AK", "IA") + "\n"
                   + gate._joined("gh", "p_") + "\n")
        self.assertEqual(gate.scan_files(self.root), [])

    def test_short_sk_suffix_stays_green(self):
        self.track("short.txt", gate._joined("s", "k-") + "shorty\n")
        self.assertEqual(gate.scan_files(self.root), [])

    def test_patterns_are_fragment_assembled(self):
        source = (ROOT / "tools" / "privacy_gate.py").read_text(encoding="utf-8")
        for whole in ("ghp_", "github_pat_", "AKIA"):
            with self.subTest(whole=whole):
                self.assertNotIn(whole, source)
        # ``sk-`` also occurs inside ordinary hyphenated words such as
        # ``task-name``, so only a token-initial spelling counts as "whole".
        self.assertIsNone(re.search(r"(?<![\w-])" + re.escape(gate._joined("s", "k-")),
                                    source))


class OwnerWarning(unittest.TestCase):
    """The owner segment of origin warns; it never fails and never goes quiet."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        git(self.root, "init", "-q", "-b", "main")

    def tearDown(self):
        self.tmp.cleanup()

    def set_origin(self, url: str) -> None:
        git(self.root, "remote", "add", "origin", url)

    def test_private_owner_is_reported(self):
        self.set_origin("https://example.invalid/" + PRIVATE_PROJECT + "/repo.git")
        warnings = gate.scan_remote_warnings(self.root)
        self.assertTrue(warnings, "owner hit must not be silent")
        self.assertTrue(any("private account" in w or "project name" in w
                            for w in warnings), warnings)

    def test_neutral_owner_is_silent(self):
        self.set_origin("https://example.invalid/safkc-bot/repo.git")
        self.assertEqual(gate.scan_remote_warnings(self.root), [])

    def test_main_warns_but_stays_green(self):
        self.set_origin("https://example.invalid/" + PRIVATE_PROJECT + "/repo.git")
        (self.root / "README.md").write_text("hello\n", encoding="utf-8")
        git(self.root, "add", "README.md")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = gate.main([], root=self.root)
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("WARN", out)
        self.assertIn(PRIVATE_PROJECT, out)

    def test_fail_path_survives_the_warning(self):
        self.set_origin("https://example.invalid/acct/" + PRIVATE_PROJECT + ".git")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = gate.main([], root=self.root)
        self.assertEqual(rc, 1)
        self.assertIn("hit(s)", buf.getvalue())

    def test_repository_origin_warns_about_its_owner(self):
        warnings = gate.scan_remote_warnings(ROOT)
        self.assertTrue(warnings, "this repo's origin owner is the private account")


if __name__ == "__main__":
    unittest.main()
