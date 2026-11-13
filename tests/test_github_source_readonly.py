"""S-5 as a real check: `github_source` must stay read-only (issue #60).

The v1 criterion was ``grep "POST|PATCH" super_agent/github_source.py`` ⇒ rc=0.
That is green for the wrong reason: the only hit is the docstring at
``github_source.py:17`` ("there is deliberately no POST/PATCH/PUT/DELETE path"),
so the grep can never go red and proves nothing.

This test parses the module instead and asserts that no HTTP method call is
ever made. Comments and docstrings are invisible to the AST, so the assertion
tracks behaviour, not prose.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "super_agent" / "github_source.py"
MUTATING = {"post", "patch", "put", "delete"}


def _mutating_calls(tree: ast.AST) -> list[str]:
    """Names of ``<something>.post(...)``-style calls anywhere in the module."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr.lower() in MUTATING:
            found.append(func.attr)
    return found


class GithubSourceIsReadOnly(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tree = ast.parse(SOURCE.read_text(encoding="utf-8"))

    def test_no_mutating_http_call(self) -> None:
        self.assertEqual(_mutating_calls(self.tree), [])

    def test_ast_sees_the_docstring_it_ignores(self) -> None:
        """Guards the counter-example: the word appears, the call does not."""
        text = SOURCE.read_text(encoding="utf-8")
        self.assertIn("POST", text)
        self.assertEqual(_mutating_calls(self.tree), [])


if __name__ == "__main__":
    unittest.main()
