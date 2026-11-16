"""Shape rules for role session keys (``pm-<project>-<random>``).

The property under test is the one issue #87 round 2 depends on: a key name can
be *spelled* from public information (project name, date) or it can carry a tail
nobody can spell. Only the second is a credential.

Run: ``python -m pytest tests/test_sessionkeys.py -q``
"""
from __future__ import annotations

import unittest

from super_agent import sessionkeys


class RandomTail(unittest.TestCase):
    def test_eight_hex_characters_is_a_tail(self):
        self.assertEqual(sessionkeys.random_suffix("pm-demo-3f7c1a90"), "3f7c1a90")
        self.assertTrue(sessionkeys.has_random_suffix("qa-lwa-ISS-87-0a1b2c3d"))

    def test_a_bare_project_name_has_no_tail(self):
        for name in ("pm-demo", "pm-longer_project_name", "qa", "qa-ISS-19"):
            with self.subTest(name=name):
                self.assertEqual(sessionkeys.random_suffix(name), "")
                self.assertFalse(sessionkeys.has_random_suffix(name))

    def test_a_date_is_not_a_tail(self):
        # ``20261116`` is eight digits and lowercase-hex-shaped by accident.
        for name in ("pm-demo-20261116", "pm-demo-20261113",
                     "qa-lwa-ISS-86-20261231"):
            with self.subTest(name=name):
                self.assertFalse(sessionkeys.has_random_suffix(name))

    def test_a_word_is_not_a_tail(self):
        self.assertFalse(sessionkeys.has_random_suffix("pm-demo-deadbeefx"))
        self.assertFalse(sessionkeys.has_random_suffix("pm-demo-1234567"))

    def test_the_tail_must_be_the_last_segment(self):
        self.assertFalse(sessionkeys.has_random_suffix("pm-3f7c1a90-demo"))


class RoleAndScope(unittest.TestCase):
    def test_role_comes_from_the_prefix(self):
        self.assertEqual(sessionkeys.role_of("pm-demo-3f7c1a90"), "pm")
        self.assertEqual(sessionkeys.role_of("qa"), "qa")
        self.assertEqual(sessionkeys.role_of("at-demo-3f7c1a90"), "at")
        self.assertEqual(sessionkeys.role_of("wk-probe"), "")
        # ``pmx`` is not a ``pm`` key: the separator is what separates.
        self.assertEqual(sessionkeys.role_of("pmx-demo"), "")

    def test_scope_matches_the_bare_and_the_suffixed_name(self):
        self.assertTrue(sessionkeys.scoped_by("pm-demo", "pm", "demo"))
        self.assertTrue(sessionkeys.scoped_by("pm-demo-3f7c1a90", "pm", "demo"))
        self.assertFalse(sessionkeys.scoped_by("pm-demolition", "pm", "demo"))
        self.assertFalse(sessionkeys.scoped_by("pm-other-3f7c1a90", "pm", "demo"))
        self.assertFalse(sessionkeys.scoped_by("qa-demo-3f7c1a90", "pm", "demo"))


class PreferredKey(unittest.TestCase):
    """The recipient rule: newest key of *this* project, never file order."""

    DB = {
        "pm-alpha-1a2b3c4d": {"id": "s1", "updated": "2026-11-04T08:41"},
        "pm-demo-11112222": {"id": "s2", "updated": "2026-11-14T21:17"},
        "pm-demo-3f7c1a90": {"id": "s3", "updated": "2026-11-16T03:39"},
        "supervisor": {"id": "s4"},
    }

    def test_the_newest_key_of_this_project_wins(self):
        self.assertEqual(sessionkeys.preferred_key(self.DB, "pm", "demo"),
                         "pm-demo-3f7c1a90")

    def test_file_order_is_not_the_rule(self):
        # ``pm-alpha-…`` is first in the dict and must not answer "demo".
        self.assertEqual(sessionkeys.preferred_key(self.DB, "pm", "alpha"),
                         "pm-alpha-1a2b3c4d")

    def test_a_project_with_no_key_answers_empty(self):
        # Never another project's key: that is the misdelivery, not a fallback.
        self.assertEqual(sessionkeys.preferred_key(self.DB, "pm", "gridinone"), "")

    def test_without_a_project_the_newest_key_wins(self):
        self.assertEqual(sessionkeys.preferred_key(self.DB, "pm"), "pm-demo-3f7c1a90")

    def test_an_unreadable_ledger_answers_empty(self):
        self.assertEqual(sessionkeys.preferred_key({}, "pm", "demo"), "")
        self.assertEqual(sessionkeys.preferred_key(None, "pm", "demo"), "")


if __name__ == "__main__":
    unittest.main()
