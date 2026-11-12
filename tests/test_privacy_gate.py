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


class RuntimePrefixes(unittest.TestCase):
    """``data/`` is the post-migration runtime root and must be gated too."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        git(self.root, "init", "-q", "-b", "main")

    def tearDown(self):
        self.tmp.cleanup()

    def track(self, name: str) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")
        git(self.root, "add", "-f", name)

    def test_data_directory_turns_red(self):
        self.track("data/events.jsonl")
        hits = gate.scan_runtime_paths(self.root)
        self.assertTrue(any("prefix data/" in h for h in hits), hits)

    def test_state_directory_still_turns_red(self):
        self.track("state/events.jsonl")
        hits = gate.scan_runtime_paths(self.root)
        self.assertTrue(any("prefix state/" in h for h in hits), hits)

    def test_ordinary_directory_stays_green(self):
        self.track("docs/auth.md")
        self.assertEqual(gate.scan_runtime_paths(self.root), [])

    def test_prefixes_cover_both_roots(self):
        self.assertIn("state/", gate.RUNTIME_PREFIXES)
        self.assertIn("data/", gate.RUNTIME_PREFIXES)


class CredentialValue(unittest.TestCase):
    """The ``credential value`` shape: key name, assignment, long literal.

    Every value below is assembled from fragments, exactly as the gate builds
    its own patterns, so this file passes the scan it performs.
    """

    def red_shapes(self):
        secret = "Super" + "Secret1234567890"
        hex40 = "9f8e7d6c5b4a" + "39281706f5e4d3c2b1a0"
        letters = "abcdefghijklmnop" + "qrstuvwx"
        jwt = "eyJhbGciOiJIUzI1NiIs" + "InR5cCI6IkpXVCJ9"
        return (
            "DB_PASSWORD" + "=" + secret,
            "api_key" + ": " + hex40,
            "token" + " = \"" + letters + "\"",
            "Authorization" + ": Bear" + "er " + jwt,
        )

    def green_shapes(self):
        return (
            "token" + " = kimi_paths.token()",
            "token" + " = load_token_from_env()",
            "API_KEY" + "=",
            "GITHUB_TOKEN" + "=<your-token-here>",
            "TOKEN" + "=abc123",
            "TOKEN" + ": '" + "XXXXXXXXXXXXXXXXXXXX" + "'",
            "token 只在本机使用",
        )

    def test_red_shapes_match(self):
        pattern = gate.PATTERNS["credential value"]
        for line in self.red_shapes():
            with self.subTest(line=line):
                self.assertTrue(pattern.search(line), line)

    def test_green_shapes_do_not_match(self):
        pattern = gate.PATTERNS["credential value"]
        for line in self.green_shapes():
            with self.subTest(line=line):
                self.assertFalse(pattern.search(line), line)


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

    def test_real_shaped_sk_key_turns_red(self):
        self.assert_token_hit(
            "sk-mixed.txt",
            gate._joined("s", "k-") + "Ab3dEf9hIj2kLm4nOp6qRs8tUv0wXy2z",
            "token: sk api key")

    def test_project_scoped_sk_key_turns_red(self):
        self.assert_token_hit(
            "sk-proj.txt",
            gate._joined("s", "k-") + gate._joined("pro", "j-")
            + "Ab3dEf9hIj2kLm4nOp6qRs8tUv0wXy2z",
            "token: sk api key")

    def test_aws_access_key_id_turns_red(self):
        self.assert_token_hit("aws.txt", gate._joined("AK", "IA") + "D" * 16,
                              "token: aws access key id")

    def test_ordinary_hyphenated_words_stay_green(self):
        self.track("prose.txt",
                   "sk-learn and task-name are words, not credentials\n")
        self.assertEqual(gate.scan_files(self.root), [])

    def test_sk_shaped_english_words_stay_green(self):
        for word in ("sk-learn", "sk-image-classification",
                     "sk-learn-pipeline-tools", "sk-build-documentation"):
            with self.subTest(word=word):
                self.track(f"{word}.txt", word + "\n")
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

    def test_fake_origin_owner_warns_only_for_private_names(self):
        """Owner WARN is a behaviour of the URL, not of wherever this repo lives.

        Both sides run against a throwaway repository with a synthetic origin,
        so the test stays green no matter which account the real remote points
        at. Changing the real remote to prove that is never required.
        """
        (self.root / "README.md").write_text("hello\n", encoding="utf-8")
        git(self.root, "add", "README.md")

        self.set_origin("https://example.invalid/neutralacct/repo.git")
        self.assertEqual(gate.scan_remote_warnings(self.root), [])
        self.assertEqual(gate.scan_remote(self.root), [])
        self.assertEqual(gate.main([], root=self.root), 0)

        git(self.root, "remote", "set-url", "origin",
            "https://example.invalid/" + PRIVATE_PROJECT + "/repo.git")
        warnings = gate.scan_remote_warnings(self.root)
        self.assertTrue(warnings, "owner hit must not be silent")
        self.assertTrue(any("project name" in w for w in warnings), warnings)
        self.assertEqual(gate.scan_remote(self.root), [],
                         "the owner segment is warn-only, never a fail hit")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = gate.main([], root=self.root)
        self.assertEqual(rc, 0)
        self.assertIn("WARN", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
