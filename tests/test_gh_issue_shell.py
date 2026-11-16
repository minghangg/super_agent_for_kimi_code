"""``tools/gh_issue.py`` here is a **shell**: it must not restate the policy.

The workbench's ``tools/gh_issue.py`` holds the policy table, the role
prefixes, the project registry and the HTTP calls. This repository's entry
point adds one thing - the ``<!-- session: ... -->`` marker - and calls the
implementation. Two properties keep it a shell, and both are checkable without
knowing what the implementation does:

* **no business logic is restated**: the policy table and the prefix rules are
  not here, so the two entry points cannot disagree about who may comment;
* **the implementation is found through one environment variable**, never a
  private path written into the open repository and never whatever module
  happens to be importable.

Run: ``python -m pytest tests/test_gh_issue_shell.py -q``
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "tools" / "gh_issue.py"

# Read as a file, not imported: the assertion is about what the source says.
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


FAKE_IMPL = '''
import json, os, sys

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    with open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "seen.json"), "w", encoding="utf-8") as fh:
        json.dump({"argv": argv, "workbench": os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))}, fh)
    print("[impl] called")
    return 0
'''


def _run(argv: list[str], *, root: str | None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("SAFKC_BRIDGE_ROOT", None)
    if root is not None:
        env["SAFKC_BRIDGE_ROOT"] = root
    return subprocess.run([sys.executable, str(SHELL), *argv],
                          capture_output=True, text=True, env=env, timeout=60)


class ShellContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "tools").mkdir(parents=True)
        (self.root / "tools" / "gh_issue.py").write_text(
            textwrap.dedent(FAKE_IMPL), encoding="utf-8")
        self.seen = self.root / "seen.json"

    def tearDown(self):
        self.tmp.cleanup()

    def argv_of(self) -> list[str]:
        return json.loads(self.seen.read_text("utf-8"))["argv"]

    def test_the_marker_is_appended_as_the_last_line(self):
        out = _run(["comment", "--role", "pm", "--issue", "87",
                    "--session-key", "pm-demo", "--body", "probe"],
                   root=str(self.root))
        self.assertEqual(out.returncode, 0, out.stderr)
        argv = self.argv_of()
        body = argv[argv.index("--body") + 1]
        self.assertEqual(body, "probe\n<!-- session: pm-demo -->")
        self.assertNotIn("--session-key", argv, "the implementation takes no key")

    def test_the_implementation_keeps_its_own_workbench(self):
        out = _run(["comment", "--role", "pm", "--issue", "87",
                    "--session-key", "pm-demo", "--body", "probe"],
                   root=str(self.root))
        self.assertEqual(out.returncode, 0, out.stderr)
        seen = json.loads(self.seen.read_text("utf-8"))
        self.assertEqual(Path(seen["workbench"]), self.root)

    def test_no_key_leaves_the_body_alone(self):
        out = _run(["comment", "--role", "pm", "--issue", "87", "--body", "probe"],
                   root=str(self.root))
        self.assertEqual(out.returncode, 0, out.stderr)
        argv = self.argv_of()
        self.assertEqual(argv[argv.index("--body") + 1], "probe")
        self.assertIn("unattributed", out.stdout)

    def test_the_missing_root_is_an_error_not_a_guess(self):
        out = _run(["comment", "--role", "pm", "--issue", "87",
                    "--session-key", "pm-demo", "--body", "probe"], root=None)
        self.assertEqual(out.returncode, 2)
        self.assertIn("SAFKC_BRIDGE_ROOT", out.stderr)
        self.assertFalse(self.seen.exists())

    def test_a_root_without_the_implementation_is_an_error(self):
        empty = self.root / "empty"
        empty.mkdir()
        out = _run(["comment", "--role", "pm", "--issue", "87", "--body", "p"],
                   root=str(empty))
        self.assertEqual(out.returncode, 2)
        self.assertIn("has no tools/gh_issue.py", out.stderr)

    def test_a_key_on_another_subcommand_is_refused(self):
        out = _run(["show", "--issue", "87", "--session-key", "pm-demo"],
                   root=str(self.root))
        self.assertEqual(out.returncode, 2)
        self.assertIn("applies to comment", out.stderr)
        self.assertFalse(self.seen.exists())

    def test_other_subcommands_pass_straight_through(self):
        out = _run(["list", "--project", "demo", "--limit", "5"],
                   root=str(self.root))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.argv_of(), ["list", "--project", "demo",
                                         "--limit", "5"])

    def test_an_empty_body_is_refused_before_the_marker(self):
        """The implementation refuses an empty body; the marker would hide it."""
        out = _run(["comment", "--role", "pm", "--issue", "87",
                    "--session-key", "pm-demo", "--body", "   "],
                   root=str(self.root))
        self.assertEqual(out.returncode, 2)
        self.assertIn("body is empty", out.stderr)
        self.assertFalse(self.seen.exists(), "nothing must reach the implementation")

    def test_a_body_file_is_read_not_edited(self):
        path = Path(self.tmp.name) / "body.md"
        path.write_text("line one\nline two\n", encoding="utf-8")
        before = path.read_text(encoding="utf-8")
        out = _run(["comment", "--role", "pm", "--issue", "87",
                    "--session-key", "pm-demo", "--body-file", str(path)],
                   root=str(self.root))
        self.assertEqual(out.returncode, 0, out.stderr)
        argv = self.argv_of()
        self.assertEqual(argv[argv.index("--body") + 1],
                         "line one\nline two\n<!-- session: pm-demo -->")
        self.assertNotIn("--body-file", argv)
        self.assertEqual(path.read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
