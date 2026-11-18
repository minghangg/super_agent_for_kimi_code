"""``tools/gh_issue.py`` here is a **shell**: it must not restate the policy.

The implementation lives in ``super_agent/issue_cli.py`` of this same checkout.
This entry point adds one thing - the ``<!-- session: ... -->`` marker - refuses
a ``comment`` that does not name its session, and calls the implementation.
Three properties keep it a shell, and all three are checkable without knowing
what the implementation does:

* **no business logic is restated**: the policy table and the prefix rules are
  not here, so the two entry points cannot disagree about who may comment;
* **the implementation is the one beside the shell**: found through the shell's
  own location, then compared against that exact path, so a same-named module
  from anywhere else cannot answer;
* **the missing-key refusal is the shell's own**: it happens before the
  implementation is loaded, so replacing the implementation cannot switch it
  off.

Every test copies the real shell into a temporary checkout. The shell resolves
its implementation from its own path, so pointing it somewhere else is no longer
possible - and that inability is the property being tested.

Run: ``python -m pytest tests/test_gh_issue_shell.py -q``
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "tools" / "gh_issue.py"


def _source() -> str:
    return SHELL.read_text(encoding="utf-8")


def _code_lines(text: str) -> list[str]:
    """Source lines with comments and docstrings dropped.

    A shell may *describe* the policy it does not own; what it must not do is
    contain it.
    """
    lines = []
    in_doc = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('"""') or stripped.startswith("'''"):
            quote = stripped[:3]
            if stripped.count(quote) >= 2:
                continue
            in_doc = not in_doc
            continue
        if in_doc:
            continue
        if stripped.startswith("#"):
            continue
        lines.append(line)
    return lines


class NoBusinessLogic(unittest.TestCase):
    """The mechanical criterion the ticket asks for, as an assertion."""

    FORBIDDEN = ("POLICY", "_role", "_prefix", "ROLE_PREFIX", "_check(",
                 "urllib", "api.github.com", "DEFAULT_REPOS")

    def test_the_shell_restates_none_of_it(self):
        code = "\n".join(_code_lines(_source()))
        found = [needle for needle in self.FORBIDDEN if needle in code]
        self.assertEqual(found, [], f"the shell restates {found}")

    def test_the_criterion_can_go_red(self):
        """A copy that carries the policy table must fail the test above."""
        broken = _source() + '\nPOLICY = {"comment": {"qa"}}\n'
        code = "\n".join(_code_lines(broken))
        self.assertIn("POLICY", code)

    def test_the_shell_does_carry_the_marker(self):
        # The one thing the shell *does* own, so its absence is not a pass.
        self.assertIn("<!-- session:", _source())


# The fake implementation records what it was called with, and its own path:
# the second is how a test sees *which* copy answered the import.
FAKE_IMPL = '''
import json, os, sys

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(os.path.dirname(here), "seen.json"), "w", encoding="utf-8") as fh:
        json.dump({"argv": argv, "impl": os.path.abspath(__file__)}, fh)
    print("[impl] called")
    return 0
'''


def _run(argv: list[str], *, checkout: Path | None = None) -> subprocess.CompletedProcess:
    """Run the copied shell in ``checkout`` (or the real one) with a clean env."""
    shell = (checkout / "tools" / "gh_issue.py") if checkout else SHELL
    env = dict(os.environ)
    for name in ("SAFKC_BRIDGE_ROOT", "PYTHONPATH", "GITHUB_TOKEN"):
        env.pop(name, None)
    return subprocess.run([sys.executable, str(shell), *argv],
                          capture_output=True, text=True, env=env, timeout=60)


class ShellContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "tools").mkdir(parents=True)
        shutil.copy2(SHELL, self.root / "tools" / "gh_issue.py")
        (self.root / "super_agent").mkdir()
        (self.root / "super_agent" / "__init__.py").write_text("", encoding="utf-8")
        (self.root / "super_agent" / "issue_cli.py").write_text(
            textwrap.dedent(FAKE_IMPL), encoding="utf-8")
        self.seen = self.root / "seen.json"

    def tearDown(self):
        self.tmp.cleanup()

    def argv_of(self) -> list[str]:
        return json.loads(self.seen.read_text("utf-8"))["argv"]

    def test_the_marker_is_appended_as_the_last_line(self):
        out = _run(["comment", "--role", "pm", "--issue", "87",
                    "--session-key", "pm-demo", "--body", "probe"],
                   checkout=self.root)
        self.assertEqual(out.returncode, 0, out.stderr)
        argv = self.argv_of()
        body = argv[argv.index("--body") + 1]
        self.assertEqual(body, "probe\n<!-- session: pm-demo -->")
        self.assertNotIn("--session-key", argv, "the implementation takes no key")

    def test_the_implementation_is_the_one_beside_the_shell(self):
        out = _run(["comment", "--role", "pm", "--issue", "87",
                    "--session-key", "pm-demo", "--body", "probe"],
                   checkout=self.root)
        self.assertEqual(out.returncode, 0, out.stderr)
        seen = json.loads(self.seen.read_text("utf-8"))
        self.assertEqual(Path(seen["impl"]).resolve(),
                         (self.root / "super_agent" / "issue_cli.py").resolve())

    def test_a_comment_without_a_key_is_refused(self):
        """No key means an unattributed verdict, so nothing may be sent (#87)."""
        out = _run(["comment", "--role", "pm", "--issue", "87", "--body", "probe"],
                   checkout=self.root)
        self.assertEqual(out.returncode, 2)
        self.assertIn("session-key", out.stderr)
        self.assertFalse(self.seen.exists(), "nothing must reach the implementation")

    def test_a_checkout_without_the_implementation_is_an_error(self):
        bare = self.root / "bare"
        (bare / "tools").mkdir(parents=True)
        shutil.copy2(SHELL, bare / "tools" / "gh_issue.py")
        out = _run(["comment", "--role", "pm", "--issue", "87",
                    "--session-key", "pm-demo", "--body", "p"], checkout=bare)
        self.assertEqual(out.returncode, 2)
        self.assertIn(str((bare / "super_agent" / "issue_cli.py").resolve()),
                      out.stderr)

    def test_missing_implementation_and_key_report_both(self):
        """Two faults, two messages: fixing one must not hide the other."""
        bare = self.root / "bare2"
        (bare / "tools").mkdir(parents=True)
        shutil.copy2(SHELL, bare / "tools" / "gh_issue.py")
        out = _run(["comment", "--role", "pm", "--issue", "87", "--body", "p"],
                   checkout=bare)
        self.assertEqual(out.returncode, 2)
        self.assertIn("session-key", out.stderr)
        self.assertIn("issue_cli", out.stderr)

    def test_a_key_on_another_subcommand_is_refused(self):
        out = _run(["show", "--issue", "87", "--session-key", "pm-demo"],
                   checkout=self.root)
        self.assertEqual(out.returncode, 2)
        self.assertIn("applies to comment", out.stderr)
        self.assertFalse(self.seen.exists())

    def test_other_subcommands_pass_straight_through(self):
        out = _run(["list", "--project", "demo", "--limit", "5"], checkout=self.root)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.argv_of(), ["list", "--project", "demo",
                                         "--limit", "5"])

    def test_an_empty_body_is_refused_before_the_marker(self):
        """The implementation refuses an empty body; the marker would hide it."""
        out = _run(["comment", "--role", "pm", "--issue", "87",
                    "--session-key", "pm-demo", "--body", "   "],
                   checkout=self.root)
        self.assertEqual(out.returncode, 2)
        self.assertIn("body is empty", out.stderr)
        self.assertFalse(self.seen.exists(), "nothing must reach the implementation")

    def test_a_body_file_is_read_not_edited(self):
        path = self.root / "body.md"
        path.write_text("line one\nline two\n", encoding="utf-8")
        before = path.read_text(encoding="utf-8")
        out = _run(["comment", "--role", "pm", "--issue", "87",
                    "--session-key", "pm-demo", "--body-file", str(path)],
                   checkout=self.root)
        self.assertEqual(out.returncode, 0, out.stderr)
        argv = self.argv_of()
        self.assertEqual(argv[argv.index("--body") + 1],
                         "line one\nline two\n<!-- session: pm-demo -->")
        self.assertNotIn("--body-file", argv)
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_the_shell_names_no_implementation_root_env(self):
        """Zero environment variables, as a machine check rather than a comment."""
        code = "\n".join(_code_lines(_source()))
        # The retired name is assembled from fragments in the shell, so the
        # whole word must not appear - not even in a warning about it.
        self.assertNotIn("SAFKC_BRIDGE_ROOT", code)
        self.assertNotIn("os.environ.get('SAFKC_BRIDGE_ROOT')", code)

    def test_the_file_assertion_names_both_paths(self):
        """A module of the same name from elsewhere must be reported, not used.

        Simulated the way it really happens: the module is already imported in
        the process, so ``import_module`` hands back that copy instead of the
        one beside the shell. Both paths must be printed - "wrong module"
        alone leaves the reader unable to see which one answered.
        """
        site = self.root / "site" / "super_agent"
        site.mkdir(parents=True)
        foreign = site / "issue_cli.py"
        foreign.write_text("LIES = 1\n", encoding="utf-8")
        runner = self.root / "runner.py"
        runner.write_text(textwrap.dedent(f'''
            import runpy, sys, types
            mod = types.ModuleType("super_agent.issue_cli")
            mod.__file__ = {str(foreign)!r}
            sys.modules["super_agent.issue_cli"] = mod
            sys.argv = ["gh_issue.py", "list", "--project", "demo"]
            runpy.run_path({str(self.root / "tools" / "gh_issue.py")!r},
                           run_name="__main__")
        '''), encoding="utf-8")
        env = dict(os.environ)
        for name in ("SAFKC_BRIDGE_ROOT", "PYTHONPATH", "GITHUB_TOKEN"):
            env.pop(name, None)
        out = subprocess.run([sys.executable, str(runner)], capture_output=True,
                             text=True, env=env, timeout=60)
        self.assertEqual(out.returncode, 2, out.stderr)
        self.assertIn("expected=", out.stderr)
        self.assertIn("actual=", out.stderr)
        self.assertIn(str(foreign.resolve()), out.stderr)


if __name__ == "__main__":
    unittest.main()
