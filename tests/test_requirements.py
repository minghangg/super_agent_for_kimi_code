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


def project(comments, previous=None, *, at=True, supervisor=True):
    return req.project_comments(
        comments, previous,
        package_verifier=lambda comment: at,
        approval_verifier=lambda comment: supervisor)


def approved_projection(value=None):
    value = value or package()
    approval = {"requirement_id": RID, "revision": value["revision"],
                "requirement_hash": value["requirement_hash"],
                "package_ref": "issuecomment-100", "approved_at": "now"}
    return project([
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
        projection = project([comment(100, req.PACKAGE_FENCE, package())])
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

    def test_reprojecting_an_approved_revision_keeps_the_exact_approval(self):
        projection = approved_projection()
        replayed = project([], projection)
        current = req.current(replayed, RID)
        self.assertEqual(current["status"], "approved")
        self.assertEqual(current["approval_ref"], "issuecomment-101")
        self.assertTrue(current["approval_source_verified"])
        entry = replayed["requirements"][RID]
        self.assertEqual(entry["approval"]["approval_ref"], "issuecomment-101")
        packet = req.dispatch_packet(replayed, RID, "asg_7_worker_1")
        self.assertEqual(packet["approval_ref"], "issuecomment-101")

    def test_wrong_hash_or_package_ref_does_not_approve(self):
        value = package()
        for field, wrong in (("requirement_hash", "sha256:" + "0" * 64),
                             ("package_ref", "issuecomment-999")):
            approval = {"requirement_id": RID, "revision": 1,
                        "requirement_hash": value["requirement_hash"],
                        "package_ref": "issuecomment-100"}
            approval[field] = wrong
            projection = project([
                comment(100, req.PACKAGE_FENCE, value),
                comment(101, req.APPROVAL_FENCE, approval)])
            self.assertEqual(req.status(projection, RID), "ready")

    def test_new_revision_invalidates_the_old_approval(self):
        projection = approved_projection()
        newer = package(revision=2, objective="a changed objective")
        projection = project(
            [comment(102, req.PACKAGE_FENCE, newer)], projection)
        self.assertEqual(req.current(projection, RID)["revision"], 2)
        self.assertEqual(req.status(projection, RID), "ready")
        self.assertEqual(req.current(projection, RID)["approval_ref"], "")

    def test_same_revision_with_different_semantics_invalidates_dispatch(self):
        projection = approved_projection()
        conflicting = package(objective="silently changed")
        projection = project(
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
                projection = project(
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
            replay = project([
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


class SourceRecovery(unittest.TestCase):
    def comments(self):
        value = package()
        approval = {"requirement_id": RID, "revision": 1,
                    "requirement_hash": value["requirement_hash"],
                    "package_ref": "issuecomment-100"}
        return [comment(100, req.PACKAGE_FENCE, value),
                comment(101, req.APPROVAL_FENCE, approval)]

    def project(self, comments, previous, allowed):
        return req.project_comments(
            comments, previous,
            package_verifier=lambda comment: True,
            approval_verifier=lambda comment: allowed)

    def test_previously_seen_unverified_approval_recovers_after_rotation(self):
        allowed = False
        first = self.project(self.comments(), None, allowed)
        first["seen_comment_ids"] = [100, 101]  # the legacy failure poisoned the cursor
        recovered = self.project(self.comments(), first, True)
        self.assertEqual(req.status(recovered, RID), "approved")
        self.assertEqual(req.current(recovered, RID)["approval_ref"],
                         "issuecomment-101")
        errors = recovered["requirements"][RID]["errors"]
        self.assertFalse([e for e in errors if e.get("source_unverified")])
        self.assertIn(101, recovered["seen_comment_ids"])

    def test_unverified_retry_does_not_duplicate_errors_or_advance_seen(self):
        first = self.project(self.comments(), None, False)
        second = self.project(self.comments(), first, False)
        errors = second["requirements"][RID]["errors"]
        self.assertEqual(len([e for e in errors if e.get("source_unverified")]), 1)
        self.assertNotIn(101, second["seen_comment_ids"])


class SourceSecurity(unittest.TestCase):
    def test_unverified_package_and_approval_never_authorise_dispatch(self):
        value = package()
        approval = {"requirement_id": RID, "revision": 1,
                    "requirement_hash": value["requirement_hash"],
                    "package_ref": "issuecomment-100"}
        forged = {"id": 100, "body": req.fence(req.PACKAGE_FENCE, value)
                  + "\n" + req.fence(req.APPROVAL_FENCE, approval)}
        projection = project([forged], at=False, supervisor=False)
        self.assertNotEqual(req.status(projection, RID), "approved")
        with self.assertRaises(req.RequirementError):
            req.dispatch_packet(projection, RID, "asg_7_worker_1")
        errors = projection["requirements"][RID]["errors"]
        self.assertEqual(len([e for e in errors if e.get("source_unverified")]), 2)

    def test_unverified_pm_revision_does_not_replace_or_pause_current_package(self):
        projection = approved_projection()
        malicious = package(revision=2, objective="PM rewrite")
        projection = project(
            [comment(102, req.PACKAGE_FENCE, malicious)], projection, at=False)
        self.assertEqual(req.current(projection, RID)["revision"], 1)
        self.assertEqual(req.current(projection, RID)["objective"], package()["objective"])
        self.assertEqual(req.status(projection, RID), "approved")

    def test_draft_package_cannot_be_promoted_by_an_approval(self):
        value = package(status="draft")
        approval = {"requirement_id": RID, "revision": 1,
                    "requirement_hash": value["requirement_hash"],
                    "package_ref": "issuecomment-100"}
        projection = project([
            comment(100, req.PACKAGE_FENCE, value),
            comment(101, req.APPROVAL_FENCE, approval)])
        self.assertEqual(req.status(projection, RID), "draft")
        with self.assertRaises(req.RequirementError):
            req.dispatch_packet(projection, RID, "asg_7_worker_1")

    def test_no_verifier_is_deny_by_default(self):
        projection = req.project_comments([comment(100, req.PACKAGE_FENCE, package())])
        self.assertIsNone(req.current(projection, RID))


if __name__ == "__main__":
    unittest.main()
