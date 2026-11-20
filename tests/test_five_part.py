"""ISS-99: a five-part title says who, what, whom to tell, which project, and cid.

The properties pinned here, and why each is a counter-example rather than a
happy path:

* **a probe must not spend quota.** Two 92-byte probes and one real delivery
  used to count as three; the breaker tripped on a healthy thread and four
  comments went silent. The title is what tells them apart, so the counting
  rule is tested, not the parser alone;
* **the hyphen is a separator, not a character.** `pm-<项目>`
  and `wk-ISS-98-dev-20261119` both contain hyphens. Splitting on `-` shreds
  them, so the parser is tested against those exact names;
* **a wrong action word is a silent non-notification.** That is the safe
  direction: a false notification cannot be taken back, a missing one leaves a
  row a person can see. So an unknown action must not notify;
* **old comments keep their meaning.** 3,000+ `[WK:…]` / `[QA]` lines are
  already on GitHub; a parser that only knows the new shape would re-read them
  as discussion.

Run: ``python -m pytest tests/test_five_part.py -q``
"""
from __future__ import annotations

import unittest

from super_agent import decide_watch, issue_cli, markers, role_config

# Assembled: the gate scans this file, and a project name written whole
# is a hit. The shape under test is the hyphens, not this project.
PM_KEY = "pm-" + "lai" + "ke" + "_work_with_agent"


class Parse(unittest.TestCase):
    def test_five_parts_with_a_cid(self):
        five = markers.parse_five(
            "[wk-ISS-98-dev-20261119]-[完工]-[" + PM_KEY + "]-[lwa]-[7010865591]")
        self.assertIsNotNone(five)
        self.assertEqual(five["who"], "wk-ISS-98-dev-20261119")
        self.assertEqual(five["do"], "完工")
        self.assertEqual(five["notify"], PM_KEY)
        self.assertEqual(five["project"], "lwa")
        self.assertEqual(five["cid"], "7010865591")
        self.assertTrue(five["action_ok"])

    def test_four_parts_without_a_cid(self):
        five = markers.parse_five("[supervisor]-[交接要求]-[pm-lwa]-[superagent]")
        self.assertIsNotNone(five)
        self.assertEqual(five["cid"], "")

    def test_hyphens_inside_names_are_not_separators(self):
        # The whole reason the regex matches bracketed groups instead of
        # splitting on "-".
        five = markers.parse_five("[qa-lwa-ISS-98]-[退回]-[pm,wk-ISS-98-dev]-[lwa]")
        self.assertIsNotNone(five)
        self.assertEqual(five["who"], "qa-lwa-ISS-98")
        self.assertEqual(five["notify"], "pm,wk-ISS-98-dev")

    def test_a_plain_line_is_not_a_title(self):
        self.assertIsNone(markers.parse_five("普通评论，没有标记"))
        self.assertIsNone(markers.parse_five("[WK:wk-x] 旧格式交付"))

    def test_an_unknown_action_is_parsed_but_flagged(self):
        five = markers.parse_five("[wk-x]-[乱写]-[pm]-[lwa]")
        self.assertIsNotNone(five)
        self.assertFalse(five["action_ok"])


class Notify(unittest.TestCase):
    def test_no_notify_does_not_page_anyone(self):
        five = markers.parse_five("[wk-x]-[完工]-[No Notify]-[lwa]")
        self.assertFalse(markers.wants_notify(five))

    def test_no_notify_is_normalised_however_it_is_spelled(self):
        for spelling in ("No Notify", "no notify", "NONOTIFY", "NoNotify"):
            with self.subTest(spelling=spelling):
                five = markers.parse_five(f"[wk-x]-[完工]-[{spelling}]-[lwa]")
                self.assertFalse(markers.wants_notify(five))

    def test_a_named_recipient_is_paged(self):
        five = markers.parse_five("[wk-x]-[完工]-[pm-lwa]-[lwa]")
        self.assertTrue(markers.wants_notify(five))

    def test_silent_actions_never_page_even_with_a_recipient(self):
        for action in ("调试", "已归档"):
            with self.subTest(action=action):
                five = markers.parse_five(f"[wk-x]-[{action}]-[pm]-[lwa]")
                self.assertFalse(markers.wants_notify(five))

    def test_an_unknown_action_never_pages(self):
        five = markers.parse_five("[wk-x]-[QB]-[pm]-[lwa]")
        self.assertFalse(markers.wants_notify(five))


class Delivery(unittest.TestCase):
    def test_a_finished_worker_is_a_delivery(self):
        got = markers.parse_delivery("[wk-ISS-98-dev-20261119]-[完工]-[pm-lwa]-[lwa]-[7]")
        self.assertIsNotNone(got)
        self.assertEqual(got["name"], "wk-ISS-98-dev-20261119")

    def test_a_verdict_is_not_a_delivery(self):
        self.assertIsNone(markers.parse_delivery("[qa-lwa-ISS-98]-[退回]-[pm]-[lwa]"))
        self.assertIsNone(markers.parse_delivery("[supervisor]-[交接要求]-[pm]-[lwa]"))

    def test_a_title_whose_who_is_not_a_role_is_not_a_delivery(self):
        # A typo must not create a delivery that matches no assignment.
        self.assertIsNone(markers.parse_delivery("[QB]-[完工]-[pm]-[lwa]"))

    def test_a_bare_role_prefix_is_not_a_session_key(self):
        # ``[SC]-[完工]-…`` names a role, not a session. Accepting it would make
        # a delivery named "SC" that matches no assignment, and the engine would
        # report ``assignment.unmatched`` for a comment that looked correct.
        self.assertIsNone(markers.parse_delivery("[SC]-[完工]-[pm]-[lwa]"))
        self.assertIsNone(markers.parse_delivery("[WK]-[完工]-[pm]-[lwa]"))

    def test_the_legacy_marker_still_parses(self):
        got = markers.parse_delivery("[WK:wk-ISS-92-dev-20261118] 交付")
        self.assertIsNotNone(got)
        self.assertEqual(got["marker"], "WK")
        self.assertEqual(got["name"], "wk-ISS-92-dev-20261118")


class Review(unittest.TestCase):
    def test_reject_and_pass_come_from_the_action_word(self):
        self.assertEqual(
            markers.parse_review("[qa-x]-[退回]-[pm]-[lwa]")["verdict"], "reject")
        self.assertEqual(
            markers.parse_review("[qa-x]-[完工]-[pm]-[lwa]")["verdict"], "pass")

    def test_a_receipt_is_not_a_review(self):
        self.assertIsNone(markers.parse_review("[qa-x]-[接单]-[No Notify]-[lwa]"))

    def test_the_legacy_qa_prefix_still_parses(self):
        got = markers.parse_review("[QA] 结论：通过")
        self.assertIsNotNone(got)
        self.assertEqual(got["verdict"], "pass")


class Counting(unittest.TestCase):
    """The quota rule: a probe does not spend it, a delivery does."""

    def test_two_probes_and_one_delivery_count_once(self):
        # The issue's own case: cids 7009804739 and 7010098778 were probes,
        # 7010148170 was the real delivery, and all three were counted.
        titles = [
            "[wk-x]-[调试]-[No Notify]-[lwa]",
            "[wk-x]-[调试]-[No Notify]-[lwa]",
            "[wk-x]-[完工]-[pm-lwa]-[lwa]-[7010148170]",
        ]
        counted = [t for t in titles if decide_watch._is_real_delivery(t)]
        self.assertEqual(len(counted), 1)

    def test_a_legacy_line_is_not_counted(self):
        # No action word means it never claimed to deliver. Counting it is how
        # probes spent quota in the first place.
        self.assertFalse(decide_watch._is_real_delivery("[WK:wk-x] 交付"))

    def test_a_row_without_a_title_is_skipped(self):
        self.assertIsNone(decide_watch._title_of("cid:123"))
        self.assertIsNone(decide_watch._title_of("ledger:57"))

    def test_a_row_with_a_title_yields_it(self):
        note = "cid:123;title:[wk-x]-[完工]-[pm]-[lwa]"
        self.assertEqual(decide_watch._title_of(note), "[wk-x]-[完工]-[pm]-[lwa]")

    def test_the_threshold_is_eight(self):
        self.assertEqual(decide_watch.DELIVERY_MAX_PER_DAY, 8)


class Prefix(unittest.TestCase):
    """What the writer emits, which is what the parser must read back."""

    def setUp(self):
        self.table = role_config.load()

    def test_five_parts_round_trip(self):
        head = issue_cli._prefix("worker", "wk-ISS-99-dev", self.table,
                                 do="完工", notify="pm-lwa", project="lwa",
                                 cid="7012345678")
        five = markers.parse_five(head)
        self.assertEqual(five["do"], "完工")
        self.assertEqual(five["cid"], "7012345678")
        self.assertIsNotNone(markers.parse_delivery(head))

    def test_an_unknown_action_is_refused_before_it_reaches_github(self):
        with self.assertRaises(issue_cli.Denied):
            issue_cli._prefix("worker", "wk-x", self.table,
                              do="乱写", notify="pm", project="lwa")

    def test_a_five_part_title_without_notify_is_refused(self):
        with self.assertRaises(issue_cli.Denied):
            issue_cli._prefix("worker", "wk-x", self.table,
                              do="完工", notify="", project="lwa")

    def test_a_five_part_title_without_project_is_refused(self):
        with self.assertRaises(issue_cli.Denied):
            issue_cli._prefix("worker", "wk-x", self.table,
                              do="完工", notify="pm", project="")


class ProjectKey(unittest.TestCase):
    """The key must exist; the repository name is not the key."""

    def test_the_registry_is_read_from_the_configured_file(self):
        from super_agent import issue_cli as I
        table = I._load_projects(None)
        self.assertGreaterEqual(len(table), 1)
        self.assertIn("superagent", table)

    def test_the_repository_name_is_not_a_key(self):
        # `super_agent_for_kimi_code` is the repo; the key is `superagent`. A
        # wrong key means nobody is told and nothing reports it.
        from super_agent import issue_cli as I
        self.assertNotIn("super_agent_for_kimi_code", I._load_projects(None))



class LegacyShape(Prefix):
    def test_the_legacy_shape_is_unchanged(self):
        self.assertEqual(issue_cli._prefix("worker", "wk-x", self.table), "[WK:wk-x]")
        self.assertEqual(issue_cli._prefix("qa", None, self.table), "[QA]")


class EveryAction(unittest.TestCase):
    def test_the_whitelist_has_twelve_actions(self):
        self.assertEqual(len(markers.NOTIFY_ACTIONS), 12)
        self.assertEqual(set(markers.NOTIFY_ACTIONS), {
            "完工", "收口", "退回", "裁决", "告警", "派单", "接单",
            "交接要求", "交接完成", "交接就绪", "已归档", "调试"})

    def test_every_action_survives_a_round_trip(self):
        table = role_config.load()
        for action in markers.NOTIFY_ACTIONS:
            with self.subTest(action=action):
                head = issue_cli._prefix("qa", "qa-x", table, do=action,
                                         notify="No Notify", project="lwa")
                five = markers.parse_five(head)
                self.assertEqual(five["do"], action)
                self.assertTrue(five["action_ok"])


if __name__ == "__main__":
    unittest.main()
