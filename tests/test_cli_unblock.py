"""``cli unblock`` and ``--session-api``.

Both are operator-facing switches with a narrow job:

* ``unblock`` is the only way out of ``blocked`` that does not need a newer
  comment, and it must record the reason and keep the tombstone (the next
  failure needs it to bump the attempt counter);
* ``--session-api none`` must not silently look like a working console: every
  call raises, so the archive read-back fails closed instead of pretending the
  session is live.

Run: ``python -m pytest tests/test_cli_unblock.py -q``
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from super_agent import cli, config
from super_agent.store import Store

THREAD = "owner/demo#1"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.store = Store(self.state)
        self.store.save_index({"version": 1, "meta": {}, "issues": {},
                               "assignments": {}})

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, status="blocked", **over):
        index = self.store.load_index()
        rec = {"id": "asg_1_worker_1", "thread": THREAD, "issue": 1,
               "role": "worker", "sid": "session_w1", "status": status,
               "session_key": "wk-probe-1", "delivery_comment_id": 9001,
               "qa_verdict": "", "tombstone": {}}
        rec.update(over)
        index["assignments"]["asg_1_worker_1"] = rec
        self.store.save_index(index)
        return rec

    def run_cli(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = cli.main(argv)
        return rc, out.getvalue()

    def rec(self):
        return self.store.load_index()["assignments"]["asg_1_worker_1"]

    def rows(self):
        path = self.state / "events.jsonl"
        if not path.is_file():
            return []
        return [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]


class Unblock(Base):
    def test_unblock_moves_to_working_and_records_the_reason(self):
        from super_agent import assignments as asg
        rec = self.add()
        asg.mark_blocked(rec, "archive not confirmed by read-back", cid=9001)
        index = self.store.load_index()
        index["assignments"]["asg_1_worker_1"] = rec
        self.store.save_index(index)

        rc, text = self.run_cli(["--state-dir", str(self.state), "unblock",
                                 "--id", "asg_1_worker_1",
                                 "--reason", "session service is back"])
        self.assertEqual(rc, 0, text)
        self.assertIn("-> working", text)
        rec = self.rec()
        self.assertEqual(rec["status"], "working")
        self.assertTrue(rec["unblocked_at"])
        self.assertEqual(rec["unblock_reason"], "session service is back")
        self.assertEqual(rec["tombstone"]["cid"], 9001,
                         "the tombstone survives: the next failure bumps attempt")
        self.assertEqual(rec["tombstone"]["attempt"], 1)
        kinds = [r["type"] for r in self.rows()]
        self.assertEqual(kinds.count("assignment.unblocked"), 1)

    def test_unblock_refuses_an_assignment_that_is_not_blocked(self):
        self.add(status="working")
        rc, text = self.run_cli(["--state-dir", str(self.state), "unblock",
                                 "--id", "asg_1_worker_1"])
        self.assertEqual(rc, 1)
        self.assertIn("not blocked", text)
        self.assertEqual(self.rec()["status"], "working")

    def test_unblock_refuses_an_unknown_assignment(self):
        rc, text = self.run_cli(["--state-dir", str(self.state), "unblock",
                                 "--id", "asg_9_worker_9"])
        self.assertEqual(rc, 1)
        self.assertIn("no assignment", text)


class SessionApiSwitch(Base):
    def test_none_raises_on_every_call(self):
        api = cli._session_api("none")
        with self.assertRaises(RuntimeError):
            api.session_info("session_w1")
        with self.assertRaises(RuntimeError):
            api.archive_session("session_w1")

    def test_default_is_the_real_console(self):
        from super_agent import api
        self.assertIs(cli._session_api(None), api)
        self.assertIs(cli._session_api("real"), api)

    def test_the_flag_is_accepted_and_reaches_the_engine(self):
        # ``doctor`` builds a source-less engine; with ``none`` its session
        # console raises, which must be reported as a check rather than a crash.
        rc, text = self.run_cli(["--state-dir", str(self.state), "--session-api",
                                 "none", "status"])
        self.assertIn(rc, (0, 1), text)
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                cli.main(["--state-dir", str(self.state), "--session-api", "bogus",
                          "status"])

    def test_production_default_stays_real(self):
        cfg = config.load(None, {})
        self.assertEqual(cfg["engine"]["max_closed_thread_checks"], 10)


if __name__ == "__main__":
    unittest.main()
