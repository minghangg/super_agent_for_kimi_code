"""The message envelope: 14 fields, closed domains, fail-closed validation (#92).

Run: ``python -m pytest tests/test_envelope.py -q``
"""
from __future__ import annotations

import unittest

from super_agent import envelope


class Build(unittest.TestCase):
    def test_all_fourteen_fields_are_present(self):
        env = envelope.build(repo="o/r", issue=92, cid=1234, type="delivery",
                             thread="o/r#92", from_role="worker")
        self.assertEqual(set(env), set(envelope.FIELDS))

    def test_message_id_is_stable_and_distinct(self):
        same = envelope.message_id("o/r", 92, 1234)
        self.assertEqual(same, envelope.message_id("o/r", 92, 1234))
        self.assertNotEqual(same, envelope.message_id("o/r", 92, 1235))
        self.assertNotEqual(same, envelope.message_id("o/r", 93, 1234))
        # A repo containing '#' must not collide with a shorter one: the
        # separator is JSON's, not a character the name may carry.
        self.assertNotEqual(envelope.message_id("a#1", 2, 3),
                            envelope.message_id("a", 2, 3))
        self.assertEqual(len(same), 32)

    def test_a_string_issue_number_is_still_numeric(self):
        self.assertEqual(envelope.message_id("o/r", "92", 1234),
                         envelope.message_id("o/r", 92, 1234))

    def test_payload_ref_defaults_to_this_comment(self):
        env = envelope.build(repo="o/r", issue=92, cid=7, type="note",
                             thread_scope="global")
        self.assertEqual(env["payload_ref"], {"kind": "issue_comment", "cid": 7})
        self.assertEqual(env["result_ref"], {})

    def test_build_reads_no_body(self):
        # The signature has no ``body`` parameter at all: identity cannot be
        # spelled by the comment it describes (#92 §D.2).
        import inspect
        params = set(inspect.signature(envelope.build).parameters)
        self.assertNotIn("body", params)

    def test_scope_is_inferred_from_a_thread(self):
        env = envelope.build(repo="o/r", issue=92, cid=1, type="delivery",
                             thread="o/r#92")
        self.assertEqual(env["thread_scope"], "issue")

    def test_scope_is_not_inferred_without_a_thread(self):
        env = envelope.build(repo="o/r", issue=92, cid=1, type="note")
        self.assertEqual(env["thread_scope"], "")
        ok, reason = envelope.validate(env)
        self.assertFalse(ok)
        self.assertEqual(reason, "missing_thread_scope")


class ScopeRules(unittest.TestCase):
    """The four rules of #92 §附, one positive and one negative each."""

    def _env(self, **kw):
        base = dict(repo="o/r", issue=92, cid=1, type="delivery", project="proj")
        base.update(kw)
        return envelope.build(**base)

    def test_rule_1_issue_scope_needs_a_thread(self):
        ok, _ = envelope.validate(self._env(thread="o/r#92"))
        self.assertTrue(ok)
        ok, reason = envelope.validate(self._env(thread_scope="issue"))
        self.assertFalse(ok)
        self.assertEqual(reason, "thread_required")

    def test_rule_2_other_scopes_forbid_a_thread(self):
        for scope in ("project", "global", "none"):
            with self.subTest(scope=scope):
                self.assertTrue(envelope.validate(
                    self._env(thread="", thread_scope=scope))[0])
                ok, reason = envelope.validate(
                    self._env(thread="o/r#92", thread_scope=scope))
                self.assertFalse(ok)
                self.assertEqual(reason, "thread_not_allowed")

    def test_rule_3_type_is_not_restricted_by_scope(self):
        # A delivery may belong to a project; a dispatch may be global.
        self.assertTrue(envelope.validate(
            self._env(type="delivery", thread="", thread_scope="project"))[0])
        self.assertTrue(envelope.validate(
            self._env(type="pm_task", thread="", thread_scope="global"))[0])

    def test_rule_4_missing_scope_with_empty_thread_is_refused(self):
        env = self._env()
        env["thread_scope"] = ""
        ok, reason = envelope.validate(env)
        self.assertFalse(ok)
        self.assertEqual(reason, "missing_thread_scope")

    def test_project_scope_needs_a_project(self):
        ok, reason = envelope.validate(self._env(thread="", thread_scope="project",
                                                 project=""))
        self.assertFalse(ok)
        self.assertEqual(reason, "project_required")
        # ...but a global row has no project yet, and must still validate.
        self.assertTrue(envelope.validate(
            self._env(thread="", thread_scope="global", project=""))[0])


class Rejections(unittest.TestCase):
    """#92 §A.5: fail-closed, visible, and each one can go red."""

    def _ok(self, **kw):
        base = dict(repo="o/r", issue=92, cid=1, type="delivery", thread="o/r#92")
        base.update(kw)
        return envelope.build(**base)

    def test_a_missing_required_field_is_refused(self):
        env = self._ok()
        del env["message_id"]
        self.assertEqual(envelope.validate(env),
                         (False, "missing_message_id"))
        env = self._ok()
        del env["created_at"]
        self.assertEqual(envelope.validate(env), (False, "missing_created_at"))
        env = self._ok()
        env["payload_ref"] = {}
        self.assertEqual(envelope.validate(env), (False, "missing_payload_ref"))

    def test_b_a_forged_role_is_refused_when_it_is_not_an_enum(self):
        env = self._ok(from_role="worker")
        self.assertTrue(envelope.validate(env)[0])
        # The whitelist answers "" for an unverified key; anything invented is
        # not a role and is refused outright.
        self.assertTrue(envelope.validate(self._ok(from_role=""))[0])
        ok, reason = envelope.validate(self._ok(from_role="root"))
        self.assertFalse(ok)
        self.assertEqual(reason, "unknown_from_role")

    def test_c_an_unknown_type_is_refused_and_countable(self):
        ok, reason = envelope.validate(self._ok(type="gossip"))
        self.assertFalse(ok)
        self.assertEqual(reason, "unknown_type")
        event = envelope.rejected_event(self._ok(type="gossip"), reason)
        self.assertEqual(event["type"], "envelope.rejected")
        self.assertEqual(event["reason"], "unknown_type")
        self.assertEqual(event["comment_id"], 1)
        self.assertNotIn("body", event)

    def test_d_another_schema_version_is_refused_explicitly(self):
        ok, reason = envelope.validate(self._ok(schema_version=2))
        self.assertFalse(ok)
        self.assertEqual(reason, "unknown_schema")

    def test_e_a_malformed_result_ref_is_refused(self):
        ok, reason = envelope.validate(
            self._ok(result_ref={"kind": "verdict", "state": "ready"}))
        self.assertFalse(ok)
        self.assertEqual(reason, "bad_result_ref")
        self.assertTrue(envelope.validate(
            self._ok(result_ref={"kind": "issue_comment", "cid": 9}))[0])


class RoleMapping(unittest.TestCase):
    """§P0-2 归一表: the whitelist decides, the marker never self-certifies."""

    def test_a_verified_key_names_its_role(self):
        for key, want in (("supervisor", "supervisor"),
                          ("pm-demo-1a2b3c4d", "pm"),
                          ("qa-lwa-ISS-92-1a2b3c4d", "qa"),
                          ("wk-ISS-92-dev", "worker"),
                          ("worker-x", "worker"),
                          ("scout-x", "scout"),
                          ("steward-x", "steward"),
                          ("auditor-x", "auditor"),
                          ("reviewer-x", "reviewer"),
                          ("analyst-x", "analyst"),
                          ("at-x", "analyst")):
            with self.subTest(key=key):
                self.assertEqual(envelope.from_role_of(key, verified=True), want)

    def test_an_unverified_key_is_empty_even_with_a_marker(self):
        # ``[WK:<some pm key>]`` must not certify itself (§D.2 反例 A).
        self.assertEqual(
            envelope.from_role_of("pm-other-1a2b3c4d", "WK", verified=False), "")
        self.assertEqual(envelope.from_role_of("", "WK", verified=False), "")

    def test_every_enumerated_role_is_reachable(self):
        # The whole point of widening ``role_of``: no enum value is dead.
        reachable = {envelope.from_role_of(k, verified=True)
                     for k in ("pm-x", "qa-x", "wk-x", "scout-x", "steward-x",
                               "analyst-x", "auditor-x", "reviewer-x",
                               "supervisor")}
        self.assertEqual(reachable, set(envelope.FROM_ROLES))


if __name__ == "__main__":
    unittest.main()
