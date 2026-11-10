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


class MustFixParsing(unittest.TestCase):
    """The ``fix_needed`` bit, against the 28 first lines the workbench produces.

    The assertion that matters is the pair ``(verdict, fix_needed)``: a pass
    that still demands work must not reach the supervisor, and a pass that says
    "nothing to fix" must. The five hard cases are named one by one so a
    regression points at the exact shape instead of a table index.
    """

    # ``(first line without the [QA] prefix, verdict, fix_needed)``
    SHAPES = [
        ("结论：通过；无需修改", "pass", False),
        ("结论：通过；无需修复", "pass", False),
        ("结论：通过；P1 已修，无需修改", "pass", False),
        ("有条件通过（1 条 P1 必须修，其余可随下轮）", "pass", True),
        ("未修复项为 P3", "", False),
        ("结论：通过；尚未修复的 P2 可随下轮", "pass", False),
        ("P2 待修可随下轮", "", False),
        ("通过（P0=0，可关单）", "pass", False),
        ("#24 独立复核：**通过**（P0=0 / P1=3 / P2=2）", "pass", False),
        ("结论：通过；必须修的 P1 已在本轮修完", "pass", False),
        ("退回改（2 条 P0）", "reject", False),
        ("## A. P1 是否修好", "", False),
        ("**通过**（P0=0 / P1=1 必须修）", "pass", True),
        ("结论：有条件通过（1 条 P1 必须修）", "pass", True),
        ("通过；无须再修", "pass", False),
        ("通过，不必修", "pass", False),
        ("通过；必须修项为0", "pass", False),
        ("通过；没有必须修的项", "pass", False),
        ("通过；无需再修", "pass", False),
        ("有条件通过（P1 需修）", "pass", True),
        ("结论：通过；仍需修的 P1 见正文", "pass", True),
        ("有条件通过（1 条 P1 仍须修）", "pass", True),
        ("通过（P0=0 / P1=0 / P2=1）", "pass", False),
        ("#47 第二轮复核：**通过**（P0=0 / P1=0）", "pass", False),
        ("## 开发轮独立测试（ISS-47）", "", False),
        ("## ISS-53 方案轮复核（零代码改动轮）", "", False),
        ("通过；P2 未修好但不阻塞", "pass", False),
        ("方案复核：有条件通过——P1 必须修", "pass", True),
        # A partial fix is still a fix. Every line here writes a requirement
        # and a completion word in the same clause; reading the completion
        # word as "nothing left" sends a false "ready to close", which is the
        # one direction this bit must never take.
        ("有条件通过（2 条必须修，已修 1 条）", "pass", True),
        ("必须修的 P0-1 已修，P0-2 未修", "", True),
        ("通过；必须修；尚未全部修完", "pass", True),
        ("必须修 2 条，已修 1 条", "", True),
        ("必须修，其中 1 条已修", "", True),
        ("必须修 3 条（已修 0 条）", "", True),
        ("必须修（未修完）", "", True),
        ("必须修 2 条，其中 1 条已修", "", True),
        ("通过；2 条必须修，0 条已修", "pass", True),
        # The count relation has to survive the shapes a review actually writes:
        # with no measure word, with no punctuation between the two counts, with
        # a bracketed total, with a Chinese numeral, and with ``处`` instead of
        # ``条``. Every one of these used to fall through to "a completion word
        # is visible, so nothing is left".
        ("必须修 2 条，已修 1", "", True),
        ("必须修2条已修1条", "", True),
        ("必须修 2 条 已修 1 条", "", True),
        ("两条必须修，已修一条", "", True),
        ("必须修（2 条），已修 1", "", True),
        ("必须修 2 条，已完成 1 条", "", True),
        ("必须修 3 条（已完成 1 条，2 条待办）", "", True),
        ("必须修改 2 处，已修 1 处", "", True),
        ("必须修 2 条 / 已修 1 条", "", True),
        # The safe side of the same relation must stay settled: all of them
        # fixed, and more fixed than required.
        ("必须修 2 条，已修 2 条", "", False),
        ("必须修 1 条，已修 3 条", "", False),
        # A counted requirement with no fixed count is pending - unless the line
        # says *all* of it is done, which is a statement about the total.
        ("通过；必须修的 2 条已全部修完", "pass", False),
        ("通过；必须修 2 条，尚未全部修完", "pass", True),
    ]

    def review(self, text):
        out = markers.parse_review("[QA] " + text)
        self.assertIsNotNone(out, text)
        return out

    def assertShape(self, text, verdict, fix_needed):
        out = self.review(text)
        self.assertEqual(out["verdict"], verdict, text)
        self.assertEqual(out["fix_needed"], fix_needed, text)
        self.assertEqual(bool(out["fix_matched"]), fix_needed, text)

    def test_every_shape_in_the_table(self):
        for text, verdict, fix_needed in self.SHAPES:
            with self.subTest(text=text):
                self.assertShape(text, verdict, fix_needed)

    def test_hard_1_no_need_to_modify(self):
        self.assertShape("结论：通过；无需修改", "pass", False)

    def test_hard_2_no_need_to_fix(self):
        self.assertShape("结论：通过；无需修复", "pass", False)

    def test_hard_3_already_fixed_and_nothing_left(self):
        self.assertShape("结论：通过；P1 已修，无需修改", "pass", False)

    def test_hard_4_conditional_pass_with_a_must_fix(self):
        self.assertShape("有条件通过（1 条 P1 必须修，其余可随下轮）", "pass", True)

    def test_hard_5_state_word_is_not_a_requirement(self):
        # The word table deliberately holds no state words (``未修复``/``待修``):
        # "P3 is unfixed" is not "the release must wait".
        self.assertShape("未修复项为 P3", "", False)

    def test_hard_6_partial_fix_is_still_a_fix(self):
        # The completion guard used to read "a completion word within 12
        # characters" as "nothing left". A review that states its own
        # arithmetic - "2 required, 1 fixed" - then produced a false
        # "ready to close". Completion is a count relation, not a distance.
        for text in ("有条件通过（2 条必须修，已修 1 条）",
                     "必须修的 P0-1 已修，P0-2 未修",
                     "通过；必须修；尚未全部修完",
                     "必须修 2 条，已修 1 条",
                     "必须修，其中 1 条已修",
                     "必须修 3 条（已修 0 条）",
                     "必须修（未修完）",
                     "必须修 2 条，其中 1 条已修",
                     "必须修 2 条，已修 1",
                     "必须修2条已修1条",
                     "必须修 2 条 已修 1 条",
                     "两条必须修，已修一条",
                     "必须修（2 条），已修 1",
                     "必须修 2 条，已完成 1 条",
                     "必须修 3 条（已完成 1 条，2 条待办）",
                     "必须修改 2 处，已修 1 处",
                     "必须修 2 条 / 已修 1 条"):
            with self.subTest(text=text):
                out = self.review(text)
                self.assertTrue(out["fix_needed"], text)
                self.assertEqual(out["fix_matched"], "必须修", text)

    def test_hard_7_a_counted_requirement_needs_a_count_relation(self):
        # "2 required, 2 fixed" and "1 required, 3 fixed" are the safe side of
        # the same arithmetic; a fix that only ever answers True would pass the
        # partial cases above and fail these.
        for text in ("必须修 2 条，已修 2 条",
                     "必须修 1 条，已修 3 条",
                     "必须修 2 条（已修 2 条）"):
            with self.subTest(text=text):
                self.assertFalse(self.review(text)["fix_needed"], text)

    def test_hard_8_no_count_relation_is_a_remainder_not_a_completion(self):
        # A total with no fixed count says how much is left, not that it is
        # done. Only an explicit "all of it" claim settles the total, and its
        # negation has to win over it.
        self.assertTrue(self.review("必须修 2 条，已修完")["fix_needed"])
        self.assertTrue(
            self.review("通过；必须修 2 条，尚未全部修完")["fix_needed"])
        # ``未修复`` is not a substring of ``未全部修复``, so the "all" claim
        # needs its own negation window rather than an entry in the word list.
        for text in ("通过；必须修 2 条，尚未全部修复",
                     "通过；必须修 2 条，未全部修复",
                     "通过；必须修 2 条，没有全部修复"):
            with self.subTest(text=text):
                self.assertTrue(self.review(text)["fix_needed"], text)
        self.assertFalse(
            self.review("通过；必须修的 2 条已全部修完")["fix_needed"])

    def test_hard_9_the_generative_sweep(self):
        """Requirement words x separators x completion words x measure words.

        The point is the **ratio**, not any single line: every combination that
        states "N required, M fixed" with ``N > M`` must answer
        ``fix_needed``, and every one with ``N <= M`` must not. A point fix
        passes the named shapes and still leaks here.
        """
        requirements = ("必须修", "仍需修", "仍须修", "需要修", "须修", "需修",
                        "还需要修")
        separators = ("，", " ", "/", "；", "、")
        completions = ("已修", "已修完", "已完成", "已处理", "已补", "已改")
        measures = ("条", "处", "项", "")
        checked = 0
        for req in requirements:
            for sep in separators:
                for done in completions:
                    for measure in measures:
                        for required in range(1, 5):
                            for fixed in range(0, 5):
                                text = (f"{req} {required} {measure}{sep}"
                                        f"{done} {fixed} {measure}")
                                want = required > fixed
                                with self.subTest(text=text):
                                    self.assertEqual(
                                        self.review(text)["fix_needed"], want,
                                        text)
                                checked += 1
        self.assertGreaterEqual(checked, 500)

    def test_hard_10_whole_word_negation_still_wins(self):
        # The new arithmetic must not break the ordinary pass: these five were
        # the original reason the bit exists.
        for text in ("结论：通过；无需修改",
                     "结论：通过；无需修复",
                     "通过；无须再修",
                     "通过，不必修",
                     "非必须修，已全部处理"):
            with self.subTest(text=text):
                self.assertFalse(self.review(text)["fix_needed"], text)

    def test_state_words_are_not_requirements(self):
        for text in ("尚未修复的 P2 可随下轮", "P2 待修可随下轮", "未修复项为 P3"):
            with self.subTest(text=text):
                self.assertFalse(self.review(text)["fix_needed"], text)

    def test_a_completed_fix_is_not_a_pending_fix(self):
        self.assertFalse(
            self.review("必须修的 P1 已在本轮修完")["fix_needed"])

    def test_a_glued_single_character_negation_voids_the_hit(self):
        self.assertFalse(self.review("非必须修，已全部处理")["fix_needed"])

    def test_an_absence_after_a_modifier_is_not_a_requirement(self):
        self.assertFalse(self.review("通过；没有必须修的项")["fix_needed"])

    def test_the_verdict_path_is_untouched(self):
        # ``fix_needed`` is appended; the four original keys and the way the
        # verdict is decided stay exactly as they were.
        out = self.review("**按停损规则，仅 P0 可退 —— 本轮结论：通过**")
        self.assertEqual(sorted(out), ["fix_matched", "fix_needed", "line",
                                       "marker", "matched", "verdict"])
        self.assertEqual(out["verdict"], "pass")
        self.assertEqual(out["matched"], "通过")
        self.assertEqual(out["marker"], markers.QA)


class _NullStore:
    root = None


class _NullSource:
    def issues(self, limit=20):
        return []


if __name__ == "__main__":
    unittest.main()
