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


class ReviewParsing(unittest.TestCase):
    """``[QA]`` verdicts, read by position rather than by table order.

    The first seven cases are the **shape** of real review first lines: the
    project's own name is replaced by ``X`` and the evidence dropped, so only
    the structure that the parser has to survive is committed. Every one of
    them was a false pass under the old "check ``通过`` before ``不通过``"
    table, because ``"通过" in "不通过"`` is true.
    """

    def assertVerdict(self, body, want):
        out = markers.parse_review(body)
        self.assertIsNotNone(out, body)
        self.assertEqual(out["verdict"], want, body)

    def test_double_qa_prefix_reject_with_counts(self):
        self.assertVerdict(
            "[QA] [QA] X-32 独立复核：**不通过**（P0=0，P1=1，P2=4）。…", "reject")

    def test_double_qa_prefix_reject_with_slashes(self):
        self.assertVerdict(
            "[QA] [QA] X-29 独立复核：**不通过**（P0=0 / P1=2 / P2=5）。…", "reject")

    def test_second_round_reject(self):
        self.assertVerdict(
            "[QA] [QA] #27 第二轮复核（顺修后）：**不通过**（P0=0 / P1=1 / P2=5）",
            "reject")

    def test_reject_with_a_bracketed_reason(self):
        self.assertVerdict("[QA] **不通过（退回改）** — 方案主干可落地，但…",
                           "reject")

    def test_bare_return_word_is_a_reject(self):
        self.assertVerdict("[QA] **退回改** —— 门禁全绿、P1-1…四条修得对，但…",
                           "reject")

    def test_conclusion_clause_is_judged_not_the_rest_of_the_line(self):
        self.assertVerdict(
            "[QA] 结论：不通过——步 2 检查点不算过（P0：2 / P1：2 / P2：2）。…",
            "reject")

    def test_reject_after_a_parenthesised_scope(self):
        self.assertVerdict(
            "[QA] 方案复核（X-21 出方案轮 · 零代码改动 · 只读）：**不通过（退回补写）**",
            "reject")

    def test_pass_with_counts(self):
        self.assertVerdict("[QA] #24 独立复核：**通过**（P0=0 / P1=3 / P2=2）",
                           "pass")

    def test_quoted_rule_after_the_conclusion_does_not_flip_it(self):
        # The first line concludes "通过" and then quotes the stop-loss rule; a
        # word-table reader would see ``退回`` in the quote and reject.
        self.assertVerdict(
            "[QA] **按停损规则，仅 P0 可退 —— 本轮结论：通过**（…）", "pass")

    def test_a_word_followed_by_a_tail_is_not_a_verdict(self):
        self.assertVerdict("[QA] 通过项先说：…", "")

    def test_full_width_prefix_unpassed_variant_and_non_qa_lines(self):
        self.assertVerdict("［QA］［QA］ #8 复核：未通过", "reject")
        self.assertIsNone(markers.parse_review("[WK:wk-1] done"))
        self.assertIsNone(markers.parse_review("plain remark"))


class _NullStore:
    root = None


class _NullSource:
    def issues(self, limit=20):
        return []


if __name__ == "__main__":
    unittest.main()
