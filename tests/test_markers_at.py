"""The ``[AT]`` three-state verdict, read from the first non-empty line.

The properties pinned here, and why each one is a counter-example:

* **``questions`` is a state, not a word.** It is the reason the AT keeps its
  own book in the engine, so a parser that cannot answer it is not enough;
* the state word is decided by **position**, never by table order - ``不可派单``
  contains ``可派单``, and a prefix-checked table turns every refusal into a
  dispatch decision;
* ``ready`` needs the structured marker **and** a usable coordinate. One of
  them missing is not "ready", because dispatching on a guess is the failure
  this marker exists to stop;
* the default is fail-closed: an unreadable line answers ``""``;
* **no ``delivery`` field.** An AT verdict judges whether work can be
  dispatched, not which delivery it saw - the field would invite a time-based
  guess the PM side removed on purpose.

Run: ``python -m pytest tests/test_markers_at.py -q``
"""
from __future__ import annotations

import unittest

from super_agent import markers


class Prefixes(unittest.TestCase):
    def test_at_prefixes_both_bracket_widths(self):
        for body in ("[AT] 可派单 #42 <!-- verdict: ready -->",
                     "［AT］ 可派单 #42 <!-- verdict: ready -->",
                     "  [at]  可派单 #42 <!-- verdict: ready -->"):
            out = markers.parse_at_verdict(body)
            self.assertIsNotNone(out, body)
            self.assertEqual(out["marker"], "AT", body)
            self.assertEqual(out["state"], "ready", body)

    def test_other_markers_are_not_at_comments(self):
        for body in ("[PM] 可关单 #42 <!-- verdict: ready -->",
                     "[QA] 通过 #42", "[WK:wk-1] done #42", "plain #42"):
            self.assertIsNone(markers.parse_at_verdict(body), body)

    def test_a_quoted_marker_is_not_an_at_comment(self):
        self.assertIsNone(markers.parse_at_verdict(
            "reply\n[AT] 可派单 #42 <!-- verdict: ready -->"))

    def test_the_at_parser_does_not_read_the_pm_marker(self):
        self.assertIsNone(markers.parse_at_verdict(
            "[PM] 修复中 #42 <!-- verdict: working -->"))


class ThreeStates(unittest.TestCase):
    def test_ready(self):
        out = markers.parse_at_verdict("[AT] 可派单 #42 <!-- verdict: ready -->")
        self.assertEqual(out["state"], "ready")
        self.assertTrue(out["structured"])
        self.assertEqual(out["source"], "verdict")
        self.assertEqual(out["issue"], 42)

    def test_questions(self):
        out = markers.parse_at_verdict(
            "[AT] 有疑问 #42 <!-- verdict: questions -->")
        self.assertEqual(out["state"], "questions")
        self.assertTrue(out["structured"])

    def test_blocked(self):
        out = markers.parse_at_verdict("[AT] 受阻 #42 <!-- verdict: blocked -->")
        self.assertEqual(out["state"], "blocked")

    def test_an_unmarked_line_decides_nothing(self):
        for word in ("可派单", "有疑问", "受阻"):
            out = markers.parse_at_verdict(f"[AT] {word} #42")
            self.assertEqual(out["state"], "", word)
            self.assertEqual(out["source"], "prose", word)
            self.assertFalse(out["structured"], word)

    def test_a_negation_is_not_its_state(self):
        # ``不可派单`` must never read as ``可派单``: the hit is skipped.
        out = markers.parse_at_verdict("[AT] 不可派单 #42 <!-- verdict: blocked -->")
        self.assertEqual(out["state"], "blocked")
        out = markers.parse_at_verdict("[AT] 不可派单 #42")
        self.assertEqual(out["state"], "")
        # A fully negated word is not a "prose" hit either: the same answer the
        # PM parser gives, so one negation rule covers both markers.
        self.assertEqual(out["source"], "none")

    def test_a_denial_beside_a_ready_declaration_is_blocked(self):
        out = markers.parse_at_verdict(
            "[AT] 不可派单 #42 <!-- verdict: ready -->")
        self.assertEqual(out["state"], "blocked")

    def test_disagreeing_prose_and_marker_is_blocked(self):
        out = markers.parse_at_verdict(
            "[AT] 有疑问 #42 <!-- verdict: ready -->")
        self.assertEqual(out["state"], "blocked")

    def test_two_disagreeing_markers_are_blocked(self):
        out = markers.parse_at_verdict(
            "[AT] 有疑问 #42 <!-- verdict: questions --> <!-- verdict: ready -->")
        self.assertEqual(out["state"], "blocked")

    def test_ready_without_a_coordinate_is_not_ready(self):
        out = markers.parse_at_verdict("[AT] 可派单 <!-- verdict: ready -->")
        self.assertEqual(out["state"], "")
        self.assertFalse(out["coords_ok"])
        self.assertEqual(out["coords_error"], "no issue coordinate")

    def test_any_state_without_a_coordinate_is_empty(self):
        for state in ("ready", "questions", "blocked"):
            out = markers.parse_at_verdict(f"[AT] {state} <!-- verdict: {state} -->")
            self.assertEqual(out["state"], "", state)

    def test_the_shape_has_no_delivery_field(self):
        out = markers.parse_at_verdict("[AT] 可派单 #42 <!-- verdict: ready -->")
        self.assertNotIn("delivery", out)
        self.assertNotIn("delivery_error", out)

    def test_a_body_coordinate_is_not_the_subject(self):
        out = markers.parse_at_verdict(
            "[AT] 可派单 #42 <!-- verdict: ready -->\n\n另外 #99 也要看")
        self.assertEqual(out["issue"], 42)

    def test_the_legacy_pm_marker_is_not_read(self):
        # No AT comment predates this protocol, so there is no legacy spelling
        # to honour - inventing one would give the marker two meanings.
        out = markers.parse_at_verdict("[AT] 可派单 #42 <!-- ready: true -->")
        self.assertEqual(out["state"], "")
        self.assertEqual(out["structured"], False)


if __name__ == "__main__":
    unittest.main()
