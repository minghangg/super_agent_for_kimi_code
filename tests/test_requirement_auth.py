from __future__ import annotations

import unittest
from unittest import mock

from super_agent import config, requirement_auth


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


if __name__ == "__main__":
    unittest.main()
