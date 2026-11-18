"""The two delivery keys: one dedupes, one groups (#92 §P0-1a).

Run: ``python -m pytest tests/test_delivery_identity.py -q``

The bug these exist for is a key collapse: ``cid:57`` and ``ledger:57`` are two
different deliveries of one issue, and a key that keeps only the part after the
colon makes them one. The second delivery is then read as a replay, silently
dropped, and the counter moves instead of the message (#83).
"""
from __future__ import annotations

import unittest

from super_agent import decide_watch as dw


def _rec(evidence: str, *, issue=92, repo="o/n") -> dict:
    return {"v": 1, "project": "proj", "repo": repo, "issue": issue,
            "worker_name": "w", "worker_session": "", "pm_key": "pm-x",
            "pm_sid": "", "kind": "comment", "evidence": evidence,
            "ledger_line": 57, "detected_ts": 1.0, "dl_id": "dl-1"}


class Identity(unittest.TestCase):
    def test_two_evidence_forms_do_not_collide(self):
        a, b = _rec("cid:57"), _rec("ledger:57")
        self.assertNotEqual(dw.delivery_identity(a), dw.delivery_identity(b))

    def test_a_replay_keeps_its_key(self):
        self.assertEqual(dw.delivery_identity(_rec("cid:57")),
                         dw.delivery_identity(_rec("cid:57")))

    def test_a_different_comment_is_a_different_delivery(self):
        self.assertNotEqual(dw.delivery_identity(_rec("cid:57")),
                            dw.delivery_identity(_rec("cid:58")))

    def test_a_different_issue_is_a_different_delivery(self):
        self.assertNotEqual(dw.delivery_identity(_rec("cid:57")),
                            dw.delivery_identity(_rec("cid:57", issue=93)))

    def test_the_evidence_prefix_is_kept_whole(self):
        self.assertTrue(dw.delivery_identity(_rec("cid:57")).endswith("cid:57"))
        self.assertTrue(dw.delivery_identity(_rec("ledger:57")).endswith("ledger:57"))


class Group(unittest.TestCase):
    def test_the_group_is_per_issue(self):
        # Cooldown, the daily cap, the breaker and the summary are all
        # per-issue limits, so two deliveries of one issue share a group.
        self.assertEqual(dw.delivery_group(_rec("cid:57")),
                         dw.delivery_group(_rec("ledger:57")))
        self.assertEqual(dw.delivery_group(_rec("cid:57")), "o/n#92")

    def test_two_issues_are_two_groups(self):
        self.assertNotEqual(dw.delivery_group(_rec("cid:57")),
                            dw.delivery_group(_rec("cid:57", issue=93)))


class CounterExample(unittest.TestCase):
    """The key this replaced. Each assertion goes red on the old expression."""

    def test_the_split_expression_would_collide(self):
        rec_a, rec_b = _rec("cid:57"), _rec("ledger:57")
        old_a = f"{rec_a['repo']}#{rec_a['issue']}#{rec_a['evidence'].split(':', 1)[-1]}"
        old_b = f"{rec_b['repo']}#{rec_b['issue']}#{rec_b['evidence'].split(':', 1)[-1]}"
        self.assertEqual(old_a, old_b)                        # the collapse itself
        self.assertNotEqual(dw.delivery_identity(rec_a),
                            dw.delivery_identity(rec_b))       # and the fix

    def test_a_split_based_identity_would_lose_the_second_delivery(self):
        # Reproduce the production consequence without the store: a seen-set
        # keyed the old way answers "already seen" for the second delivery.
        seen = set()
        a, b = _rec("cid:57"), _rec("ledger:57")
        old_key = lambda r: f"{r['repo']}#{r['issue']}#{r['evidence'].split(':', 1)[-1]}"
        seen.add(old_key(a))
        self.assertIn(old_key(b), seen)                       # silently dropped
        seen.clear()
        seen.add(dw.delivery_identity(a))
        self.assertNotIn(dw.delivery_identity(b), seen)       # both delivered


class SelfTestRegistration(unittest.TestCase):
    """V must actually be registered, not merely defined as a helper.

    The run itself is asserted in ``test_decide_watch`` (23/23); what is checked
    here is that the tag reaches the tally, so a future edit that drops the
    ``check("V", …)`` call cannot leave this issue's fix untested.
    """

    def test_v_is_registered_in_the_self_test(self):
        import inspect

        src = inspect.getsource(dw.self_test)
        self.assertIn('check("V"', src)
        self.assertIn("delivery_identity", src)


if __name__ == "__main__":
    unittest.main()
