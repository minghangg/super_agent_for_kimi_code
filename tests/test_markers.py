"""Delivery marker parsing, the triage order it depends on, and the fix marker.

``gh_issue.py`` prefixes a worker comment with ``[WK:<worker-name>]``. Three
things must hold:

* only the **first non-empty line** is a marker (a quoted marker in the body is
  discussion, not a second delivery);
* the delivery judgement comes **before** role-author noise, otherwise a
  worker whose role is configured has its own delivery ignored;
* a comment without a marker keeps the historical behaviour byte for byte.

The ``fix_needed`` bit is **not** read from prose: it is the structured marker
``<!-- fix_needed: true|false -->`` on the first line, and its absence means
"work is left" - the safe side. Four earlier revisions tried to infer it from
the wording and each inference layer produced false "ready to close" lines;
those tables and functions are gone, so the tests for them are gone too.

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


class FixTokenParsing(unittest.TestCase):
    """``fix_needed`` comes from the marker, never from the wording.

    The table is ``(first line after the [QA] prefix, fix_needed,
    fix_structured)``. ``fix_structured`` is the difference between "the QA
    said so" and "nobody said anything, so we assume the safe side" - it is
    what a caller would use to ask a person to add the marker.
    """

    SHAPES = [
        ("通过  <!-- fix_needed: false -->", False, True),
        ("通过  <!-- fix_needed: true -->", True, True),
        ("有条件通过（1 条 P1 必须修）<!-- fix_needed: true -->", True, True),
        ("通过 <!--fix_needed:false-->", False, True),
        ("通过 <!-- FIX_NEEDED: FALSE -->", False, True),
        ("通过 <!-- fix_needed: True -->", True, True),
        # No marker at all: the safe side, and not structured.
        ("通过（P0=0，可关单）", True, False),
        ("有条件通过（1 条 P1 必须修）", True, False),
        ("通过；无需修改", True, False),
        ("通过；已全部修完", True, False),
        # A marker that does not parse is an absent marker, never a guess.
        ("通过 <!-- fix_need: false -->", True, False),
        ("通过 <!-- fix_needed= false -->", True, False),
        ("通过 <!-- fix_needed: ture -->", True, False),
        ("通过 <!-- fix_needed: 0 -->", True, False),
        ("通过 <!-- FIX_NEEDED= FALSE -->", True, False),
        # Two markers that disagree: the safe side, and not structured.
        ("通过 <!-- fix_needed: true --> <!-- fix_needed: false -->",
         True, False),
    ]

    def review(self, text):
        out = markers.parse_review("[QA] " + text)
        self.assertIsNotNone(out, text)
        return out

    def test_every_shape_in_the_table(self):
        violations = []
        for text, want, want_structured in self.SHAPES:
            out = self.review(text)
            if (out["fix_needed"], out["fix_structured"]) != (want,
                                                             want_structured):
                violations.append((text, want, want_structured,
                                   out["fix_needed"], out["fix_structured"]))
        self.assertEqual(violations, [])

    def test_two_agreeing_markers_are_readable(self):
        out = self.review("通过 <!-- fix_needed: false --> <!-- "
                          "fix_needed: false -->")
        self.assertFalse(out["fix_needed"])
        self.assertTrue(out["fix_structured"])

    def test_the_matched_text_is_the_marker_itself(self):
        # ``fix_matched`` is for the event log: it must be the marker as
        # written, not an inferred word.
        out = self.review("通过 <!-- fix_needed: false -->")
        self.assertEqual(out["fix_matched"], "fix_needed: false")
        self.assertEqual(self.review("通过")["fix_matched"], "")

    def test_only_the_first_line_is_read(self):
        # A body that quotes the marker later is discussion; a marker after a
        # newline cannot speak for the verdict.
        out = markers.parse_review("[QA] 通过\n<!-- fix_needed: false -->")
        self.assertTrue(out["fix_needed"], "the second line is not a marker")
        self.assertFalse(out["fix_structured"])

    def test_no_wording_makes_a_requirement_look_settled(self):
        # The whole reason the prose layer is gone: these lines read like
        # "nothing left" and none of them carries a marker.
        for text in ("通过；必须修 2 条，P2 的 5 条已全部修完",
                     "通过；必须修 2 条，其余 P2 已全部修完",
                     "通过；必须修 2 条，上一轮的 P1 已全部修完",
                     "通过；必须修，其余已全部修完",
                     "通过；必须修 2 条，已全部修完大部分",
                     "通过；无需修改",
                     "通过；必须修的 2 条已全部修完"):
            out = self.review(text)
            self.assertTrue(out["fix_needed"], text)
            self.assertFalse(out["fix_structured"], text)

    def test_a_pass_with_the_marker_says_nothing_is_left(self):
        out = self.review("通过（P0=0，可关单） <!-- fix_needed: false -->")
        self.assertEqual(out["verdict"], "pass")
        self.assertFalse(out["fix_needed"])
        self.assertTrue(out["fix_structured"])

    def test_the_verdict_path_is_untouched(self):
        # ``fix_needed`` is appended; the four original keys and the way the
        # verdict is decided stay exactly as they were.
        out = self.review("**按停损规则，仅 P0 可退 —— 本轮结论：通过**")
        self.assertEqual(sorted(out), ["fix_matched", "fix_needed",
                                       "fix_structured", "line", "marker",
                                       "matched", "verdict"])
        self.assertEqual(out["verdict"], "pass")
        self.assertEqual(out["matched"], "通过")
        self.assertEqual(out["marker"], markers.QA)

    def test_the_token_does_not_change_the_verdict(self):
        self.assertEqual(self.review("不通过 <!-- fix_needed: false -->")
                         ["verdict"], "reject")
        self.assertEqual(self.review("不通过 <!-- fix_needed: true -->")
                         ["verdict"], "reject")


class _NullStore:
    root = None


class _NullSource:
    def issues(self, limit=20):
        return []


if __name__ == "__main__":
    unittest.main()
