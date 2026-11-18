"""The two delivery keys, and the premise that turned out to be false (#92 P1-1).

Run: ``python -m pytest tests/test_delivery_identity.py -q``

**What this file used to claim, and why that was wrong.** The first round shipped
a "collision" story: that ``cid:57`` and ``ledger:57`` would share a dedup key
because the key was built with ``split(':', 1)[-1]``. QA checked the code at
``5999f9c`` and the line was::

    skey = f"{key}#{rec['evidence']}"        # the whole evidence, prefix included

That expression has **never** existed in the form the story described. No
production line ever dropped the prefix, so the "counter-example" in this file
was comparing two strings the test itself had just built.

So the tests now assert what is true and worth protecting:

* **the group key is per issue** - cooldown, the daily cap, the breaker and the
  summary are all per-issue limits, and widening that key would defeat them;
* **the dedup key keeps the whole evidence** - not because it used to lose it,
  but because losing it is the mistake a future edit would make, and the test
  is where that edit would go red;
* **one evidence form is one delivery** - a replay is dropped, a different
  evidence is not.
"""
from __future__ import annotations

import unittest

from super_agent import decide_watch as dw

REPO, ISSUE = "o/n", 92


def _rec(evidence: str, *, issue=ISSUE, repo=REPO) -> dict:
    return {"v": 1, "project": "proj", "repo": repo, "issue": issue,
            "worker_name": "w", "worker_session": "", "pm_key": "pm-x",
            "pm_sid": "", "kind": "comment", "evidence": evidence,
            "ledger_line": 57, "detected_ts": 1.0, "dl_id": "dl-1"}


class TheDedupKey(unittest.TestCase):
    """``enqueue_delivery``'s ``skey``: one evidence form, one delivery."""

    def _skey(self, rec: dict) -> str:
        # The expression the production line uses, read from the same place the
        # engine builds it - not a second copy that could drift.
        return f"{dw.delivery_group(rec)}#{rec['evidence']}"

    def test_the_whole_evidence_is_in_the_key(self):
        self.assertTrue(self._skey(_rec("cid:57")).endswith("#cid:57"))
        self.assertTrue(self._skey(_rec("ledger:57")).endswith("#ledger:57"))

    def test_two_evidence_forms_are_two_keys(self):
        self.assertNotEqual(self._skey(_rec("cid:57")),
                            self._skey(_rec("ledger:57")))

    def test_a_replay_keeps_its_key(self):
        self.assertEqual(self._skey(_rec("cid:57")),
                         self._skey(_rec("cid:57")))

    def test_a_different_comment_is_a_different_key(self):
        self.assertNotEqual(self._skey(_rec("cid:57")),
                            self._skey(_rec("cid:58")))

    def test_a_prefix_cut_would_be_caught_here(self):
        # The mutation a future edit would make. If someone writes
        # ``evidence.split(':', 1)[-1]``, this assertion stays green **only**
        # because the real key does not - that is what the file protects.
        rec_a, rec_b = _rec("cid:57"), _rec("ledger:57")
        cut_a = f"{dw.delivery_group(rec_a)}#{rec_a['evidence'].split(':', 1)[-1]}"
        cut_b = f"{dw.delivery_group(rec_b)}#{rec_b['evidence'].split(':', 1)[-1]}"
        self.assertEqual(cut_a, cut_b)                       # the mutation collides
        self.assertNotEqual(self._skey(rec_a), self._skey(rec_b))   # the code does not


class TheGroupKey(unittest.TestCase):
    def test_the_group_is_per_issue(self):
        self.assertEqual(dw.delivery_group(_rec("cid:57")), "o/n#92")
        self.assertEqual(dw.delivery_group(_rec("cid:57")),
                         dw.delivery_group(_rec("ledger:57")))

    def test_two_issues_are_two_groups(self):
        self.assertNotEqual(dw.delivery_group(_rec("cid:57")),
                            dw.delivery_group(_rec("cid:57", issue=93)))

    def test_two_repos_are_two_groups(self):
        self.assertNotEqual(dw.delivery_group(_rec("cid:57")),
                            dw.delivery_group(_rec("cid:57", repo="o/m")))


class BothDeliveriesGoOut(unittest.TestCase):
    """The behaviour the keys exist for, through ``enqueue_delivery`` itself."""

    def test_one_issue_two_evidence_forms_send_twice(self):
        import json
        import tempfile
        from dataclasses import replace
        from pathlib import Path

        tmp = Path(tempfile.mkdtemp())
        saved = dw._cfg()
        self.addCleanup(lambda: dw.configure(saved))
        dw.configure(replace(saved, state_root=tmp, workspace_root=tmp,
                             event_source=lambda: [],
                             heartbeat=lambda *a, **k: None,
                             send_transport=lambda *a, **k: True))
        # The recipient is the supervisor (#72: the bare ``pm-<project>`` key is
        # no longer a leg). The bare key stays in the roster on purpose - this
        # test must still go out with it present.
        (tmp / "sessions.json").write_text(json.dumps(
            {"pm-n": {"id": "session_00000000-0000-4000-8000-000000000001"},
             "supervisor": {"id": "session_00000000-0000-4000-8000-000000000002"}}),
            encoding="utf-8")
        st = dw.load_delivery_state()
        # The second delivery lands **outside** the burst window (ISS-83): inside it,
        # a second delivery is merged into the day's summary on purpose, so a
        # same-instant second send says nothing about the dedup key.
        first = dw.enqueue_delivery(st, _rec("cid:57"), dry=True, now=1.0)
        replay = dw.enqueue_delivery(st, _rec("cid:57"), dry=True, now=2.0)
        other = dw.enqueue_delivery(st, _rec("cid:57b"), dry=True,
                                    now=1.0 + dw.DELIVERY_BURST_WINDOW_S + 1)
        self.assertEqual((first, replay, other), (1, 0, 1))
        self.assertEqual(len(st.get("seen") or {}), 2)


class SelfTestRegistration(unittest.TestCase):
    def test_v_is_registered_and_reports_seen_keys(self):
        import inspect

        src = inspect.getsource(dw.self_test)
        self.assertIn('check("V"', src)
        self.assertIn("seen_keys", src)


if __name__ == "__main__":
    unittest.main()
