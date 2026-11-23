from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from super_agent import config, requirement_auth, session_events


class SupervisorKeys(unittest.TestCase):
    def test_no_builtin_or_bare_supervisor_key_is_accepted(self):
        cfg = config.load()
        self.assertEqual(requirement_auth.supervisor_keys(cfg), set())
        self.assertFalse(requirement_auth.supervisor_verifier(cfg)(
            {"body": "<!-- session: supervisor -->"}))

    def test_explicit_supervisor_keys_require_a_random_tail(self):
        cfg = config.load(None, {"engine": {"supervisor_keys": [
            "supervisor", "supervisor-demo-1a2b3c4d"]}})
        self.assertEqual(requirement_auth.supervisor_keys(cfg),
                         {"supervisor-demo-1a2b3c4d"})

    def test_supervisor_sid_does_not_exempt_a_fixed_ledger_key(self):
        cfg = config.load(None, {"engine": {"supervisor_sid": "session_sup"}})
        with mock.patch.object(requirement_auth.session_ledger, "load_sessions",
                               return_value={"supervisor": {"id": "session_sup"}}):
            self.assertEqual(requirement_auth.supervisor_keys(cfg), set())
            self.assertFalse(requirement_auth.supervisor_verifier(cfg)(
                {"session_key": "supervisor"}))

    def test_supervisor_sid_accepts_only_the_random_tailed_alias(self):
        cfg = config.load(None, {"engine": {"supervisor_sid": "session_sup"}})
        sessions = {
            "supervisor": {"id": "session_sup"},
            "supervisor-demo-1a2b3c4d": {"id": "session_sup"},
        }
        with mock.patch.object(requirement_auth.session_ledger, "load_sessions",
                               return_value=sessions):
            self.assertEqual(requirement_auth.supervisor_keys(cfg),
                             {"supervisor-demo-1a2b3c4d"})
            verify = requirement_auth.supervisor_verifier(cfg)
            self.assertTrue(verify({"session_key": "supervisor-demo-1a2b3c4d"}))
            self.assertFalse(verify({"session_key": "supervisor"}))


class HistoricalSupervisor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.events = Path(self.tmp.name) / "events.jsonl"
        rows = [
            {"seq": 1, "at": "2026-11-23T10:00:00+00:00",
             "type": session_events.CREATED, "sid": "session_old",
             "key": "supervisor-old-1a2b3c4d"},
            {"seq": 2, "at": "2026-11-23T10:01:00+00:00",
             "type": session_events.KEY_CHANGED, "sid": "session_old",
             "key": "supervisor-old-1a2b3c4d"},
            {"seq": 3, "at": "2026-11-23T10:02:00+00:00",
             "type": session_events.CREATED, "sid": "session_pm",
             "key": "pm-demo-1a2b3c4d"},
            {"seq": 4, "at": "2026-11-23T10:03:00+00:00",
             "type": session_events.KEY_CHANGED, "sid": "session_pm",
             "key": "pm-demo-1a2b3c4d"},
            {"seq": 5, "at": "2026-11-23T11:00:00+00:00",
             "type": session_events.ARCHIVED, "sid": "session_old"},
            {"seq": 6, "at": "2026-11-23T12:00:00+00:00",
             "type": session_events.CREATED, "sid": "session_new",
             "key": "supervisor-new-5e6f7a8b"},
        ]
        self.events.write_text("\n".join(json.dumps(row) for row in rows) + "\n",
                               encoding="utf-8")
        self.cfg = config.load(None, {"engine": {
            "session_events_path": str(self.events),
            "supervisor_sid": "session_new"}})

    def tearDown(self):
        self.tmp.cleanup()

    def test_old_supervisor_is_verified_at_the_comment_time(self):
        with mock.patch.object(requirement_auth.session_ledger, "load_sessions",
                               return_value={
                                   "supervisor-old-1a2b3c4d": {"id": "session_old"},
                                   "supervisor-new-5e6f7a8b": {"id": "session_new"},
                                   "pm-demo-1a2b3c4d": {"id": "session_pm"},
                               }):
            verify = requirement_auth.supervisor_verifier(self.cfg)
            self.assertTrue(verify({
                "session_key": "supervisor-old-1a2b3c4d",
                "created_at": "2026-11-23T10:30:00+00:00"}))
            self.assertTrue(verify({"session_key": "supervisor-new-5e6f7a8b"}))
            self.assertFalse(verify({
                "session_key": "pm-demo-1a2b3c4d",
                "created_at": "2026-11-23T10:30:00+00:00"}))
            self.assertFalse(verify({
                "session_key": "supervisor-old-1a2b3c4d",
                "created_at": "2026-11-23T11:30:00+00:00"}))


class HistoricalEvidence(HistoricalSupervisor):
    def sessions(self):
        return {
            "supervisor-old-1a2b3c4d": {"id": "session_old"},
            "supervisor-new-5e6f7a8b": {"id": "session_new"},
            "pm-demo-1a2b3c4d": {"id": "session_pm"},
            "supervisor-deadbeef": {"id": "session_worker"},
        }

    def test_tombstone_key_does_not_need_a_key_changed_event(self):
        rows = [json.loads(line) for line in
                self.events.read_text(encoding="utf-8").splitlines() if line]
        rows = [row for row in rows
                if not (row["type"] == session_events.KEY_CHANGED
                        and row.get("key") == "supervisor-old-1a2b3c4d")]
        self.events.write_text("\n".join(json.dumps(row) for row in rows) + "\n",
                               encoding="utf-8")
        with mock.patch.object(requirement_auth.session_ledger, "load_sessions",
                               return_value=self.sessions()):
            verify = requirement_auth.supervisor_verifier(self.cfg)
            self.assertTrue(verify({
                "session_key": "supervisor-old-1a2b3c4d",
                "created_at": "2026-11-23T10:30:00+00:00"}))

    def test_a_worker_does_not_become_a_supervisor_by_changing_alias(self):
        extra = [
            {"seq": 7, "at": "2026-11-23T10:04:00+00:00",
             "type": session_events.CREATED, "sid": "session_worker",
             "key": "wk-demo-1a2b3c4d"},
            {"seq": 8, "at": "2026-11-23T10:05:00+00:00",
             "type": session_events.KEY_CHANGED, "sid": "session_worker",
             "key": "supervisor-deadbeef"},
        ]
        with self.events.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(json.dumps(row) for row in extra) + "\n")
        with mock.patch.object(requirement_auth.session_ledger, "load_sessions",
                               return_value=self.sessions()):
            verify = requirement_auth.supervisor_verifier(self.cfg)
            self.assertFalse(verify({
                "session_key": "supervisor-deadbeef",
                "created_at": "2026-11-23T10:30:00+00:00"}))

    def test_history_beyond_dot_two_remains_verifiable(self):
        rows = [json.loads(line) for line in
                self.events.read_text(encoding="utf-8").splitlines() if line]
        old = rows[:4]
        current = rows[5:]
        self.events.with_name(self.events.name + ".3").write_text(
            "\n".join(json.dumps(row) for row in old) + "\n", encoding="utf-8")
        self.events.write_text("\n".join(json.dumps(row) for row in current) + "\n",
                               encoding="utf-8")
        with mock.patch.object(requirement_auth.session_ledger, "load_sessions",
                               return_value=self.sessions()):
            verify = requirement_auth.supervisor_verifier(self.cfg)
            self.assertTrue(verify({
                "session_key": "supervisor-old-1a2b3c4d",
                "created_at": "2026-11-23T10:30:00+00:00"}))

    def test_unchanged_history_is_not_reloaded_for_every_comment(self):
        with mock.patch.object(requirement_auth.session_ledger, "load_sessions",
                               return_value=self.sessions()):
            with mock.patch.object(requirement_auth.session_events, "load_events",
                                   wraps=requirement_auth.session_events.load_events) as load:
                verify = requirement_auth.supervisor_verifier(self.cfg)
                for _ in range(20):
                    self.assertTrue(verify({
                        "session_key": "supervisor-old-1a2b3c4d",
                        "created_at": "2026-11-23T10:30:00+00:00"}))
                self.assertEqual(load.call_count, 1)


if __name__ == "__main__":
    unittest.main()
