from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from super_agent import requirements as req

RID = "owner/demo#7"


def package(**changes):
    value = {
        "schema_version": 1, "requirement_id": RID, "revision": 1,
        "status": "ready", "objective": "ship one deterministic result",
        "context_refs": ["issue-7"], "scope": ["one module"],
        "out_of_scope": ["UI"], "constraints": ["no dependency"],
        "acceptance": ["the approved example passes"],
        "negative_tests": ["an unapproved dispatch is refused"],
        "risk_tier": "T1",
        "review_policy": {"plan_qa": False, "implementation_qa": True,
                          "worker_mode": "plan_and_implement", "test_level": "L1"},
        "open_questions": [], "approval_ref": "", "updated_at": "before",
    }
    value.update(changes)
    value["requirement_hash"] = req.requirement_hash(value)
    return value


def comment(cid, kind, value):
    return {"id": cid, "body": req.fence(kind, value)}


def approved_projection(value=None):
    value = value or package()
    approval = {"requirement_id": RID, "revision": value["revision"],
                "requirement_hash": value["requirement_hash"],
                "package_ref": "issuecomment-100", "approved_at": "now"}
    return req.project_comments([
        comment(100, req.PACKAGE_FENCE, value),
        comment(101, req.APPROVAL_FENCE, approval),
    ])


class Contract(unittest.TestCase):
    def test_hash_ignores_publication_metadata(self):
        one = package()
        two = copy.deepcopy(one)
        two["updated_at"] = "after"
        two["status"] = "approved"
        two["approval_ref"] = "issuecomment-101"
        self.assertEqual(req.requirement_hash(one), req.requirement_hash(two))

    def test_hash_covers_acceptance_and_boundaries(self):
        one = package()
        for field in ("objective", "scope", "out_of_scope", "acceptance",
                      "negative_tests", "review_policy", "open_questions"):
            two = copy.deepcopy(one)
            if isinstance(two[field], list):
                two[field] = two[field] + ["changed"]
            elif isinstance(two[field], dict):
                two[field]["test_level"] = "L2"
            else:
                two[field] = "changed"
            self.assertNotEqual(req.requirement_hash(one), req.requirement_hash(two), field)

    def test_ready_with_an_open_question_is_refused(self):
        value = package(open_questions=["which boundary?"])
        with self.assertRaisesRegex(req.RequirementError, "open_questions"):
            req.validate_package(value)

    def test_t2_cannot_be_downgraded_to_save_a_review(self):
        value = package(risk_tier="T2")
        with self.assertRaisesRegex(req.RequirementError, "T2 requires"):
            req.validate_package(value)

    def test_acceptance_and_negative_tests_are_mandatory(self):
        for field in ("acceptance", "negative_tests"):
            value = package(**{field: []})
            with self.assertRaisesRegex(req.RequirementError, field):
                req.validate_package(value)

    def test_interaction_options_are_semantic_and_validated(self):
        interaction = {"choice_mode": "single", "allow_custom": True,
                       "options": [{"id": "a", "label": "A"},
                                   {"id": "b", "label": "B"}]}
        value = package(interaction=interaction)
        normalized = req.validate_package(value)
        self.assertEqual(normalized["interaction"]["options"][0]["label"], "A")
        changed = copy.deepcopy(interaction)
        changed["options"][0]["label"] = "different"
        self.assertNotEqual(req.requirement_hash(value),
                            req.requirement_hash(package(interaction=changed)))
        duplicated = copy.deepcopy(interaction)
        duplicated["options"][1]["id"] = "a"
        with self.assertRaisesRegex(req.RequirementError, "unique"):
            req.validate_package(package(interaction=duplicated))


class Projection(unittest.TestCase):
    def test_unapproved_ready_cannot_make_a_packet(self):
        projection = req.project_comments([comment(100, req.PACKAGE_FENCE, package())])
        self.assertEqual(req.status(projection, RID), "ready")
        with self.assertRaisesRegex(req.RequirementError, "not approved"):
            req.dispatch_packet(projection, RID, "asg_7_worker_1")

    def test_exact_approval_makes_a_dispatch_only_packet(self):
        projection = approved_projection()
        current = req.current(projection, RID)
        self.assertEqual(current["status"], "approved")
        self.assertEqual(current["approval_ref"], "issuecomment-101")
        packet = req.dispatch_packet(projection, RID, "asg_7_worker_1")
        self.assertEqual(packet["revision"], 1)
        self.assertEqual(packet["requirement_ref"], "issuecomment-100")
        self.assertNotIn("objective", packet)
        self.assertNotIn("acceptance", packet)

    def test_wrong_hash_or_package_ref_does_not_approve(self):
        value = package()
        for field, wrong in (("requirement_hash", "sha256:" + "0" * 64),
                             ("package_ref", "issuecomment-999")):
            approval = {"requirement_id": RID, "revision": 1,
                        "requirement_hash": value["requirement_hash"],
                        "package_ref": "issuecomment-100"}
            approval[field] = wrong
            projection = req.project_comments([
                comment(100, req.PACKAGE_FENCE, value),
                comment(101, req.APPROVAL_FENCE, approval)])
            self.assertEqual(req.status(projection, RID), "ready")

    def test_new_revision_invalidates_the_old_approval(self):
        projection = approved_projection()
        newer = package(revision=2, objective="a changed objective")
        projection = req.project_comments(
            [comment(102, req.PACKAGE_FENCE, newer)], projection)
        self.assertEqual(req.current(projection, RID)["revision"], 2)
        self.assertEqual(req.status(projection, RID), "ready")
        self.assertEqual(req.current(projection, RID)["approval_ref"], "")

    def test_same_revision_with_different_semantics_invalidates_dispatch(self):
        projection = approved_projection()
        conflicting = package(objective="silently changed")
        projection = req.project_comments(
            [comment(102, req.PACKAGE_FENCE, conflicting)], projection)
        self.assertEqual(req.status(projection, RID), "invalid")
        self.assertEqual(req.current(projection, RID)["status"], "invalid")
        with self.assertRaises(req.RequirementError):
            req.dispatch_packet(projection, RID, "asg_7_worker_1")

    def test_unreadable_newer_revision_fail_closes_the_old_approval(self):
        for raw_revision in (2, "2"):
            with self.subTest(raw_revision=raw_revision):
                projection = approved_projection()
                invalid = package(revision=raw_revision, status="ready",
                                  open_questions=["unfinished"])
                projection = req.project_comments(
                    [comment(102, req.PACKAGE_FENCE, invalid)], projection)
                self.assertEqual(req.current(projection, RID)["revision"], 2)
                self.assertEqual(req.status(projection, RID), "invalid")
                with self.assertRaises(req.RequirementError):
                    req.dispatch_packet(projection, RID, "asg_7_worker_1")

    def test_full_replay_is_idempotent_and_projection_is_rebuildable(self):
        with tempfile.TemporaryDirectory() as tmp:
            projection = approved_projection()
            path = req.save_projection(tmp, projection)
            loaded = req.load_projection(tmp)
            replay = req.project_comments([
                comment(100, req.PACKAGE_FENCE, package()),
                comment(101, req.APPROVAL_FENCE, {
                    "requirement_id": RID, "revision": 1,
                    "requirement_hash": package()["requirement_hash"],
                    "package_ref": "issuecomment-100"})], loaded)
            self.assertEqual(len(replay["approvals"] if "approvals" in replay else
                                 replay["requirements"][RID]["approvals"]), 1)
            self.assertTrue(path.is_file())
            self.assertEqual(req.status(replay, RID), "approved")

    def test_stale_assignment_does_not_match_the_new_package(self):
        value = package(revision=2)
        assignment = {"requirement_id": RID, "revision": 1,
                      "requirement_hash": package()["requirement_hash"]}
        ok, reason = req.version_matches(assignment, value)
        self.assertFalse(ok)
        self.assertIn("mismatch", reason)


if __name__ == "__main__":
    unittest.main()
