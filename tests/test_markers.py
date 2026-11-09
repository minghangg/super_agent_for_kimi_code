"""Delivery marker parsing and the triage order it depends on.

``gh_issue.py`` prefixes a worker comment with ``[WK:<worker-name>]``. Three
things must hold:

* only the **first non-empty line** is a marker (a quoted marker in the body is
  discussion, not a second delivery);
* the delivery judgement comes **before** role-author noise, otherwise a
  worker whose role is configured has its own delivery ignored;
* a comment without a marker keeps the historical behaviour byte for byte.

Run: ``python -m pytest tests/test_markers.py -q``
"""
from __future__ import annotations

import unittest

from super_agent import markers
from super_agent.engine import Engine


class MarkerParsing(unittest.TestCase):
    def test_first_line_marker(self):
        out = markers.parse_delivery("[WK:wk-probe-1] done: tests green")
        self.assertEqual(out["name"], "wk-probe-1")
        self.assertEqual(out["marker"], "WK")

    def test_leading_blank_lines_are_skipped(self):
        out = markers.parse_delivery("\n\n   \n[WK:wk-probe-2] body")
        self.assertEqual(out["name"], "wk-probe-2")

    def test_case_insensitive(self):
        self.assertEqual(markers.parse_delivery("[wk:x] y")["name"], "x")

    def test_quoted_marker_in_the_body_is_not_a_delivery(self):
        self.assertIsNone(markers.parse_delivery(
            "see [WK:someone] above, that one is theirs"))

    def test_marker_not_on_the_first_line_is_ignored(self):
        self.assertIsNone(markers.parse_delivery("hello\n[WK:x] body"))

    def test_empty_name_is_not_a_delivery(self):
        self.assertIsNone(markers.parse_delivery("[WK:] body"))
        self.assertIsNone(markers.parse_delivery("[WK:   ] body"))

    def test_plain_comment_has_no_marker(self):
        self.assertFalse(markers.has_marker("plain text\n[decision] later"))


class TriageOrder(unittest.TestCase):
    """The four-way order, checked through the engine's own ``_triage``."""

    def engine(self, *, role="supervisor"):
        class Cfg(dict):
            pass

        cfg = {"mode": "observe", "role": role,
               "engine": {"escalation_markers": ["[decision]", "[blocked]"]}}
        return Engine(cfg, _NullStore(), _NullSource(), None)

    def test_delivery_beats_role_noise(self):
        engine = self.engine(role="worker")
        state, reason = engine._triage({"author": "worker",
                                        "body": "[WK:worker] delivered"})
        self.assertEqual(state, "delivery", reason)

    def test_role_noise_still_ignored_without_a_marker(self):
        engine = self.engine(role="worker")
        self.assertEqual(engine._triage({"author": "worker", "body": "hi"})[0],
                         "ignored")

    def test_escalation_marker_still_asks_a_person(self):
        engine = self.engine()
        state, reason = engine._triage({"author": "someone",
                                        "body": "[decision] pick one"})
        self.assertEqual(state, "needs_person")
        self.assertIn("decision", reason)

    def test_plain_comment_is_logged(self):
        self.assertEqual(self.engine()._triage({"author": "x", "body": "ok"})[0],
                         "logged")

    def test_quoted_marker_in_body_still_follows_legacy_rules(self):
        engine = self.engine(role="worker")
        state, _ = engine._triage({"author": "worker",
                                   "body": "reply\n[WK:other] quoted"})
        self.assertEqual(state, "ignored")


class _NullStore:
    root = None


class _NullSource:
    def issues(self, limit=20):
        return []


if __name__ == "__main__":
    unittest.main()
