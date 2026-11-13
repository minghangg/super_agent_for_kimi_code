"""The ``[PM]`` three-state verdict, read from the first non-empty line.

The properties pinned here, and the reason each one is a counter-example
rather than a happy path:

* the state word is decided by **position**, never by table order - ``不可关单``
  contains ``可关单``, and a prefix-checked table turns every refusal into a
  release;
* ``ready`` needs **three** things at once (the structured marker, a usable
  coordinate, no blocked word beside it). One of them missing is not "ready",
  because announcing a release is the only answer that costs real work when it
  is wrong;
* the default is fail-closed: an unreadable line answers ``""``, which callers
  treat as ``blocked``, and the answer never silently becomes a release;
* "no coordinate, no verdict": a line that names no issue cannot answer
  ``ready``, and it says so in ``coords_error`` instead of leaving a caller to
  guess.

Run: ``python -m pytest tests/test_markers_pm.py -q``
"""
from __future__ import annotations

import unittest

from super_agent import markers


class PMMarker(unittest.TestCase):
    def test_pm_and_supervisor_prefixes_both_parse(self):
        for body in ("[PM] 修复中 #42 <!-- verdict: working -->",
                     "[supervisor] 修复中 #42 <!-- verdict: working -->",
                     "［PM］ 修复中 #42 <!-- verdict: working -->",
                     "  [pm]  修复中 #42 <!-- verdict: working -->"):
            out = markers.parse_pm_verdict(body)
            self.assertIsNotNone(out, body)
            self.assertEqual(out["marker"], "PM", body)
            self.assertEqual(out["state"], "working", body)

    def test_other_markers_are_not_pm_comments(self):
        self.assertIsNone(markers.parse_pm_verdict("[QA] 通过 #42"))
        self.assertIsNone(markers.parse_pm_verdict("[WK:wk-1] done #42"))
        self.assertIsNone(markers.parse_pm_verdict("plain text #42 "))
        self.assertIsNone(markers.parse_pm_verdict(""))

    def test_quoted_marker_in_the_body_is_not_a_pm_comment(self):
        self.assertIsNone(markers.parse_pm_verdict("reply\n[PM] 修复中 #42"))

    def test_leading_blank_lines_are_skipped(self):
        out = markers.parse_pm_verdict("\n\n  \n[PM] 修复中 #42 "
                                       "<!-- verdict: working -->")
        self.assertEqual(out["state"], "working")


class ThreeStates(unittest.TestCase):
    READY = "[PM] 可关单 #42 <!-- ready: true -->"

    def test_ready_needs_the_marker(self):
        out = markers.parse_pm_verdict(self.READY)
        self.assertEqual(out["state"], "ready")
        self.assertEqual(out["source"], "marker")
        self.assertTrue(out["structured"])
        self.assertTrue(out["coords_ok"])
        self.assertEqual(out["issue"], 42)

    def test_blocked_from_the_marker(self):
        out = markers.parse_pm_verdict("[PM] 受阻 #42 <!-- verdict: blocked -->")
        self.assertEqual(out["state"], "blocked")

    def test_working_from_the_marker(self):
        out = markers.parse_pm_verdict("[PM] 修复中 #42 <!-- verdict: working -->")
        self.assertEqual(out["state"], "working")

    def test_unmarked_prose_never_decides_a_state(self):
        # The contract: no marker means "blocked / not finished", for **all
        # three** words. The word is still reported so a caller can ask for the
        # marker, but it decides nothing.
        for word, body in (("修复中", "[PM] 修复中 #42"),
                           ("受阻", "[PM] 受阻 #42"),
                           ("可关单", "[PM] 可关单 #42")):
            out = markers.parse_pm_verdict(body)
            self.assertEqual(out["state"], "", body)
            self.assertEqual(out["source"], "prose", body)
            self.assertEqual(out["matched"], word, body)
            self.assertFalse(out["structured"], body)

    def test_ready_without_the_marker_is_fail_closed(self):
        # The wording that used to be read as "release it". Without the
        # declaration the answer is "not ready", and the prose word is kept so
        # a caller can ask the PM to add the marker.
        out = markers.parse_pm_verdict("[PM] 可关单 #42")
        self.assertEqual(out["state"], "")
        self.assertEqual(out["matched"], "可关单")
        self.assertFalse(out["structured"])

    def test_ready_false_marker_is_blocked(self):
        out = markers.parse_pm_verdict("[PM] ready #42 <!-- ready: false -->")
        self.assertEqual(out["state"], "blocked")
        self.assertEqual(out["source"], "marker")

    def test_conflicting_legacy_markers_are_not_a_declaration(self):
        out = markers.parse_pm_verdict(
            "[PM] 修复中 #42 <!-- ready: true --> <!-- ready: false -->")
        self.assertEqual(out["state"], "")
        self.assertEqual(out["source"], "prose")
        self.assertFalse(out["structured"])

    def test_marker_and_blocked_word_never_reads_as_ready(self):
        # Two statements that disagree: the safe side wins.
        out = markers.parse_pm_verdict("[PM] 受阻 #42 <!-- ready: true -->")
        self.assertEqual(out["state"], "blocked")

    def test_no_readable_state_is_empty(self):
        out = markers.parse_pm_verdict("[PM] 通过 #42")
        self.assertEqual(out["state"], "")
        self.assertEqual(out["matched"], "")
        self.assertEqual(out["source"], "none")


class PositionNotOrder(unittest.TestCase):
    """``"可关单" in "不可关单"`` is true - the trap ``markers.py`` warns about."""

    def test_negated_ready_word_is_not_ready(self):
        for body in ("[PM] 不可关单 #42", "[PM] 未可关单 #42",
                     "[PM] 无可关单 #42"):
            out = markers.parse_pm_verdict(body)
            self.assertEqual(out["state"], "", body)
            self.assertEqual(out["matched"], "", body)

    def test_negated_blocked_word_is_not_blocked(self):
        out = markers.parse_pm_verdict("[PM] 未受阻 #42")
        self.assertEqual(out["state"], "")

    def test_a_tail_after_the_word_is_not_a_verdict(self):
        out = markers.parse_pm_verdict("[PM] 修复中项目 #42")
        self.assertEqual(out["state"], "")

    def test_first_position_wins_between_states(self):
        # Two states on one line: the earlier word is the answer, not the
        # longer one and not the table's first entry. (With no marker the state
        # is ``""`` either way; the order is observable through ``matched``.)
        out = markers.parse_pm_verdict("[PM] 受阻，修复中 #42")
        self.assertEqual(out["matched"], "受阻")
        out = markers.parse_pm_verdict("[PM] 修复中，受阻 #42")
        self.assertEqual(out["matched"], "修复中")


class Coordinates(unittest.TestCase):
    def test_bare_number(self):
        out = markers.parse_pm_verdict("[PM] 修复中 #42 <!-- verdict: working -->")
        self.assertEqual(out["issue"], 42)
        self.assertTrue(out["coords_ok"])
        self.assertEqual(out["coords_error"], "")

    def test_qualified_key(self):
        out = markers.parse_pm_verdict(
            "[PM] 修复中 owner/demo#42 <!-- verdict: working -->")
        self.assertEqual(out["issue"], 42)
        self.assertTrue(out["coords_ok"])

    def test_missing_coordinate_cannot_be_ready(self):
        out = markers.parse_pm_verdict("[PM] 可关单 <!-- ready: true -->")
        self.assertEqual(out["state"], "")
        self.assertFalse(out["coords_ok"])
        self.assertEqual(out["coords_error"], "no issue coordinate")

    def test_non_numeric_coordinate_cannot_be_ready(self):
        out = markers.parse_pm_verdict("[PM] 可关单 ## 42 <!-- ready: true -->")
        self.assertEqual(out["state"], "")
        self.assertFalse(out["coords_ok"])
        self.assertEqual(out["coords_error"], "issue coordinate is not a number")

    def test_coordinate_with_trailing_punctuation(self):
        out = markers.parse_pm_verdict("[PM] 可关单 #42，见附件 <!-- ready: true -->")
        self.assertEqual(out["issue"], 42)

    def test_a_qualified_coordinate_still_counts(self):
        out = markers.parse_pm_verdict(
            "[PM] 可关单 owner/demo#7 <!-- ready: true -->")
        self.assertEqual(out["issue"], 7)

    def test_a_coordinate_in_the_body_is_not_the_coordinate(self):
        # P1-2: only the first line names the subject. A ``#99`` mentioned in
        # the body used to become the coordinate and file the release against
        # an issue the PM never spoke about.
        out = markers.parse_pm_verdict(
            "[PM] 可关单 <!-- verdict: ready -->\n\n顺带一提，上一单 #99 也有问题")
        self.assertIsNone(out["issue"])
        self.assertEqual(out["coords_error"], "no issue coordinate")
        self.assertEqual(out["state"], "")

    def test_a_coordinate_after_blank_lines_is_still_the_body(self):
        out = markers.parse_pm_verdict(
            "[PM] 受阻 <!-- verdict: blocked -->\n\n见 #99")
        self.assertIsNone(out["issue"])
        self.assertEqual(out["state"], "blocked")

    def test_missing_coordinate_is_reported_even_when_not_ready(self):
        out = markers.parse_pm_verdict("[PM] 修复中 <!-- verdict: working -->")
        self.assertEqual(out["state"], "working")
        self.assertFalse(out["coords_ok"])
        self.assertEqual(out["coords_error"], "no issue coordinate")


class ContractV1Marker(unittest.TestCase):
    """``<!-- verdict: ready|blocked|working -->`` - the contract form.

    The failure this closes: a PM who writes exactly what the contract says
    used to reach ``state == ""`` (no marker recognised), which callers treat
    as ``blocked``. In other words, the release bell rang as the exception
    bell. ``blocked``/``working`` survived on the prose word alone; only
    ``ready`` was systematically wrong, so it is the case tested hardest.
    """

    def test_v1_ready(self):
        out = markers.parse_pm_verdict("[PM] 可关单 #42 <!-- verdict: ready -->")
        self.assertEqual(out["state"], "ready")
        self.assertEqual(out["issue"], 42)
        self.assertEqual(out["source"], "verdict")
        self.assertTrue(out["structured"])

    def test_v1_blocked(self):
        out = markers.parse_pm_verdict("[PM] 受阻 #42 <!-- verdict: blocked -->")
        self.assertEqual(out["state"], "blocked")
        self.assertEqual(out["source"], "verdict")

    def test_v1_working(self):
        out = markers.parse_pm_verdict("[PM] 修复中 #42 <!-- verdict: working -->")
        self.assertEqual(out["state"], "working")
        self.assertEqual(out["source"], "verdict")

    def test_v1_wins_when_both_agree(self):
        out = markers.parse_pm_verdict(
            "[PM] 可关单 #42 <!-- verdict: ready --> <!-- ready: true -->")
        self.assertEqual(out["state"], "ready")
        self.assertEqual(out["source"], "verdict")

    def test_v1_and_legacy_that_disagree_answer_blocked(self):
        for body in (
                "[PM] 可关单 #42 <!-- verdict: blocked --> <!-- ready: true -->",
                "[PM] 可关单 #42 <!-- ready: false --> <!-- verdict: ready -->",
                "[PM] 可关单 #42 <!-- verdict: ready --> <!-- verdict: blocked -->"):
            out = markers.parse_pm_verdict(body)
            self.assertEqual(out["state"], "blocked", body)
            self.assertEqual(out["source"], "verdict", body)

    def test_v1_ready_without_a_coordinate_is_not_ready(self):
        out = markers.parse_pm_verdict("[PM] 可关单 <!-- verdict: ready -->")
        self.assertEqual(out["state"], "")
        self.assertTrue(out["coords_error"])
        self.assertEqual(out["coords_error"], "no issue coordinate")

    def test_the_legacy_form_still_works(self):
        out = markers.parse_pm_verdict("[PM] 可关单 #42 <!-- ready: true -->")
        self.assertEqual(out["state"], "ready")
        self.assertEqual(out["source"], "marker")

    def test_a_marker_quoted_mid_line_is_not_a_declaration(self):
        # The same trailing-anchor rule the QA token uses: a mention is not a
        # declaration, or this protocol could not be described in a comment.
        for body in ("[PM] 可关单 #42 verdict: ready",
                     "[PM] 可关单 #42 <!--describes: ready -->",
                     "[PM] 修复中 #42 <!-- ready: true -->"):
            out = markers.parse_pm_verdict(body)
            self.assertNotEqual(out["source"], "verdict", body)

    def test_v1_works_with_a_full_width_prefix(self):
        out = markers.parse_pm_verdict("［PM］ 可关单 #42 <!-- verdict: ready -->")
        self.assertEqual(out["state"], "ready")
        self.assertEqual(out["source"], "verdict")


class DeliveryCoordinate(unittest.TestCase):
    """A-case: ``<!-- verdict: ready delivery: 9300 -->``.

    The coordinate is optional - every comment written before the field exists
    is still a valid declaration - and a malformed one is reported, never
    silently turned back into a guess.
    """

    def test_the_coordinate_is_read(self):
        out = markers.parse_pm_verdict("[PM] 可关单 #42 "
                                       "<!-- verdict: ready delivery: 9300 -->")
        self.assertEqual(out["state"], "ready")
        self.assertEqual(out["delivery"], 9300)
        self.assertEqual(out["delivery_error"], "")
        self.assertEqual(out["coords_error"], "")

    def test_blocked_and_working_carry_it_too(self):
        for state in ("blocked", "working"):
            out = markers.parse_pm_verdict(
                f"[PM] x #42 <!-- verdict: {state} delivery: 9300 -->")
            self.assertEqual(out["state"], state)
            self.assertEqual(out["delivery"], 9300)

    def test_the_old_format_has_no_coordinate_and_no_error(self):
        out = markers.parse_pm_verdict("[PM] 可关单 #42 <!-- verdict: ready -->")
        self.assertEqual(out["state"], "ready")
        self.assertIsNone(out["delivery"])
        self.assertEqual(out["delivery_error"], "")

    def test_a_malformed_coordinate_is_reported(self):
        out = markers.parse_pm_verdict("[PM] 可关单 #42 "
                                       "<!-- verdict: ready delivery: abc -->")
        self.assertIsNone(out["delivery"])
        self.assertEqual(out["delivery_error"],
                         "delivery coordinate is not a number")
        self.assertEqual(out["state"], "", "and it is not a release")
        self.assertTrue(out["coords_error"])

    def test_a_coordinate_outside_the_marker_is_a_mention(self):
        out = markers.parse_pm_verdict(
            "[PM] 可关单 #42 <!-- verdict: ready --> 见 delivery: 9300")
        self.assertIsNone(out["delivery"])
        self.assertTrue(out["delivery_error"])

    def test_the_legacy_marker_has_no_coordinate_field(self):
        out = markers.parse_pm_verdict("[PM] 可关单 #42 <!-- ready: true -->")
        self.assertEqual(out["state"], "ready")
        self.assertIsNone(out["delivery"])

    def test_every_wrong_position_is_reported(self):
        # P0: the "before the marker" shape used to return (None, "") - a
        # silent fall back to the time guess. All three wrong positions share
        # one message because all three are "stated but not inside this
        # marker", and the reader's fix is the same.
        cases = [
            "[PM] 可关单 #42 delivery: 9300 <!-- verdict: ready -->",
            "[PM] 可关单 #42 <!-- verdict: ready --> 见 delivery: 9300",
            "[PM] 可关单 #42 <!-- verdict: ready --> <!-- delivery: 9300 -->",
        ]
        for body in cases:
            out = markers.parse_pm_verdict(body)
            self.assertIsNone(out["delivery"], body)
            self.assertEqual(out["delivery_error"],
                             "delivery coordinate is not at the end of the "
                             "verdict marker", body)
            self.assertEqual(out["state"], "", body)

    def test_a_bad_value_before_the_marker_is_still_a_bad_value(self):
        out = markers.parse_pm_verdict(
            "[PM] 可关单 #42 delivery: abc <!-- verdict: ready -->")
        self.assertIsNone(out["delivery"])
        self.assertEqual(out["delivery_error"],
                         "delivery coordinate is not a number")

    def test_a_coordinate_does_not_change_the_state(self):
        for body, want in (
                ("[PM] 可关单 #42 <!-- verdict: ready delivery: 9300 -->", "ready"),
                ("[PM] 可关单 #42 <!-- verdict: blocked delivery: 9300 -->",
                 "blocked")):
            self.assertEqual(markers.parse_pm_verdict(body)["state"], want)


class Selftest(unittest.TestCase):
    """The one-command demonstration: ``python -m tests.test_markers_pm``."""

    DEMO = (
        "[PM] 可关单 #42 <!-- verdict: ready -->",
        "[PM] 受阻 #42 <!-- verdict: blocked -->",
        "[PM] 修复中 #42 <!-- verdict: working -->",
        "[PM] 可关单 #42 <!-- ready: true -->",
        "[PM] 可关单 #42",
        "[PM] 不可关单 #42 <!-- verdict: ready -->",
    )

    def test_demo_covers_all_three_states_and_both_fail_closed_answers(self):
        states = [markers.parse_pm_verdict(b)["state"] for b in self.DEMO]
        self.assertEqual(states, ["ready", "blocked", "working", "ready", "",
                                  "blocked"])


def _selftest() -> int:
    print(f"markers.parse_pm_verdict - {len(Selftest.DEMO)} lines")
    for body in Selftest.DEMO:
        out = markers.parse_pm_verdict(body)
        print(f"  {body}\n    -> state={out['state']!r} "
              f"issue={out['issue']} coords_ok={out['coords_ok']} "
              f"structured={out['structured']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
