"""Legacy first-line formats, mapped read-only onto envelopes (#92 §C).

Run: ``python -m pytest tests/test_legacy_map.py -q``

The property under test is that the mapping is one-way: it answers "what is
this comment" without ever rewriting it, without upgrading a prose word into a
conclusion, and without quoting a line that may carry a key name.
"""
from __future__ import annotations

import hashlib
import unittest

from super_agent import envelope


class FiveFormats(unittest.TestCase):
    def _map(self, body, **kw):
        base = dict(repo="o/n", issue=92, cid=11)
        base.update(kw)
        return envelope.legacy_map(body, **base)

    def test_delivery_markers_map_to_delivery(self):
        for marker in ("WK", "SC", "ST", "AU", "RV", "PL", "AT"):
            with self.subTest(marker=marker):
                env = self._map(f"[{marker}:probe] 交付")
                self.assertEqual(env["type"], "delivery")
                self.assertEqual(env["marker"], marker)

    def test_the_name_is_kept_as_claimed_never_as_identity(self):
        seen = self._map("[WK:pm-other-1a2b3c4d] 交付")
        self.assertEqual(seen["claimed_name"], "pm-other-1a2b3c4d")
        # ``from_role`` comes from the caller (whitelist), not from the name.
        self.assertEqual(seen["from_role"], "")

    def test_a_qa_line_maps_to_review(self):
        env = self._map("[QA] 复核：**通过**")
        self.assertEqual(env["type"], "review")
        self.assertEqual(env["result_ref"], {"kind": "issue_comment", "cid": 11,
                                             "verdict": "pass"})
        self.assertTrue(envelope.validate(env)[0])

    def test_a_task_book_maps_to_pm_task_with_no_result(self):
        env = self._map("[PM:TASK] ISS-92 任务书")
        self.assertEqual(env["type"], "pm_task")
        self.assertEqual(env["result_ref"], {})

    def test_a_pm_verdict_maps_to_pm_verdict(self):
        env = self._map("[PM] ready #92 <!-- verdict: ready -->",
                        thread="o/n#92")
        self.assertEqual(env["type"], "pm_verdict")
        self.assertEqual(env["result_ref"], {"kind": "issue_comment", "cid": 11,
                                             "state": "ready"})
        self.assertTrue(envelope.validate(env)[0])
        self.assertTrue(env["coords_ok"])

    def test_an_at_verdict_maps_to_at_verdict(self):
        env = self._map("[AT] 可派单 #1 <!-- verdict: ready -->", thread="o/n#1")
        self.assertEqual(env["type"], "at_verdict")
        self.assertEqual(env["result_ref"], {"kind": "issue_comment", "cid": 11,
                                             "state": "ready"})
        self.assertTrue(envelope.validate(env)[0])

    def test_plain_discussion_maps_to_note(self):
        env = self._map("普通讨论，无标记")
        self.assertEqual(env["type"], "note")
        self.assertEqual(env["result_ref"], {})


class ProseIsNeverUpgraded(unittest.TestCase):
    """§P1-1: a word seen in prose is not a conclusion."""

    def _map(self, body):
        return envelope.legacy_map(body, repo="o/n", issue=92, cid=11)

    def test_prose_without_a_coordinate(self):
        env = self._map("[PM] 结论：ready")
        self.assertEqual(env["type"], "pm_verdict")
        self.assertEqual(env["result_ref"], {})
        self.assertFalse(env["coords_ok"])
        self.assertEqual(env["source"], "prose")

    def test_prose_with_a_coordinate_still_has_no_conclusion(self):
        env = self._map("[PM] #92 结论：ready\n<!-- verdict: ready -->")
        self.assertEqual(env["result_ref"], {})
        self.assertTrue(env["coords_ok"])       # has a coordinate ≠ has a verdict
        self.assertEqual(env["source"], "prose")

    def test_a_structured_marker_is_the_only_way_to_a_conclusion(self):
        env = self._map("[PM] ready #92 <!-- verdict: ready -->")
        self.assertEqual(env["source"], "verdict")
        self.assertNotEqual(env["result_ref"], {})

    def test_an_at_conclusion_without_a_coordinate_stays_unreadable(self):
        # ``state=""`` with ``coords_ok=False``: "a conclusion that cannot be
        # used", which folding into ``blocked`` would erase (§C.2 note 2).
        env = self._map("[AT] 通过 <!-- verdict: pass -->")
        self.assertEqual(env["result_ref"], {})
        self.assertFalse(env["coords_ok"])


class UnknownMarkers(unittest.TestCase):
    """§C.3: count plus a reference, never the text."""

    BODY = "[XYZ] 未知首行 <!-- session: pm-secret-1a2b3c4d -->"

    def test_an_unknown_marker_maps_to_no_type(self):
        env = envelope.legacy_map(self.BODY, repo="o/n", issue=92, cid=11)
        self.assertEqual(env["type"], "")
        self.assertEqual(env["result_ref"], {})

    def test_the_envelope_carries_its_own_unknown_marker(self):
        # P2-1: the reference lives on the envelope, so a caller wiring the
        # event does not re-open the body to build it.
        env = envelope.legacy_map(self.BODY, repo="o/n", issue=92, cid=11)
        marker = env.get("unknown_marker")
        self.assertIsNotNone(marker)
        self.assertEqual(marker["kind"], "unknown_marker")
        self.assertEqual(marker["comment_id"], 11)
        self.assertEqual(marker["first_line_sha1"], envelope.first_line_sha1(self.BODY))
        self.assertNotIn("pm-secret", repr(marker))

    def test_a_known_marker_carries_no_unknown_reference(self):
        for body in ("[WK:x] 交付", "[QA] 通过", "普通讨论"):
            with self.subTest(body=body):
                self.assertNotIn("unknown_marker",
                                 envelope.legacy_map(body, repo="o/n", issue=92,
                                                     cid=1))

    def test_the_event_carries_a_sha1_and_no_text(self):
        event = envelope.unknown_marker_event(self.BODY, repo="o/n", issue=92,
                                              cid=11)
        self.assertEqual(event["type"], "envelope.unknown")
        self.assertEqual(event["comment_id"], 11)
        self.assertEqual(event["first_line_sha1"],
                         hashlib.sha1("[XYZ] 未知首行 <!-- session: pm-secret-"
                                      "1a2b3c4d -->".encode("utf-8")).hexdigest())
        blob = repr(event)
        self.assertNotIn("pm-secret", blob)
        self.assertNotIn("XYZ", blob)

    def test_the_sha1_matches_the_first_line_only(self):
        two_lines = "[XYZ] 未知首行\n第二行无关"
        self.assertEqual(envelope.first_line_sha1(two_lines),
                         envelope.first_line_sha1("[XYZ] 未知首行"))

    def test_prose_that_merely_starts_with_a_word_is_not_a_marker(self):
        env = envelope.legacy_map("XYZ 不是标记", repo="o/n", issue=92, cid=1)
        self.assertEqual(env["type"], "note")


class ReadOnly(unittest.TestCase):
    """§C.4: the mapping never writes, rewrites or upgrades."""

    def test_mapping_leaves_the_body_untouched(self):
        body = "[WK:probe] 交付\n<!-- session: wk-probe-1a2b3c4d -->"
        before = body
        envelope.legacy_map(body, repo="o/n", issue=92, cid=11)
        self.assertEqual(body, before)

    def test_the_module_exposes_no_writer(self):
        for name in dir(envelope):
            with self.subTest(name=name):
                self.assertNotIn(name, ("log_event", "save_index", "put",
                                        "emit", "write"))

    def test_an_old_message_is_never_upgraded(self):
        # A prose verdict stays prose on the second read: nothing is cached.
        first = envelope.legacy_map("[PM] 结论：ready", repo="o/n", issue=92, cid=11)
        second = envelope.legacy_map("[PM] 结论：ready", repo="o/n", issue=92, cid=11)
        self.assertEqual(first, second)
        self.assertEqual(first["result_ref"], {})


if __name__ == "__main__":
    unittest.main()
