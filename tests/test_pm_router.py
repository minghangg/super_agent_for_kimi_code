from __future__ import annotations

import unittest

from super_agent import pm_router


def packet(mode, plan_qa):
    return {
        "requirement_id": "owner/demo#1", "requirement_ref": "issuecomment-100",
        "revision": 2, "requirement_hash": "sha256:" + "a" * 64,
        "approval_ref": "issuecomment-101", "assignment": "asg_1_worker_1",
        "review_policy": {"worker_mode": mode, "plan_qa": plan_qa,
                          "implementation_qa": True, "test_level": "L1"},
    }


class Routing(unittest.TestCase):
    def test_t1_start_uses_one_worker_for_plan_and_implementation(self):
        route = pm_router.next_route(packet("plan_and_implement", False), "start")
        self.assertEqual(route["target_role"], "worker")
        self.assertEqual(route["stage"], "implement")
        self.assertIn("同会话", route["task"])
        self.assertIn("revision: 2", route["requirement_marker"])

    def test_t2_follows_independent_plan_qa_and_development(self):
        value = packet("separate", True)
        plan = pm_router.next_route(value, "start")
        self.assertEqual((plan["target_role"], plan["stage"]), ("worker", "plan"))
        review = pm_router.next_route(value, "plan_delivered")
        self.assertEqual((review["target_role"], review["stage"]), ("qa", "plan_qa"))
        development = pm_router.next_route(value, "plan_qa", "pass")
        self.assertEqual((development["target_role"], development["session"]),
                         ("worker", "new"))
        implementation = pm_router.next_route(value, "implementation_delivered")
        self.assertEqual(implementation["stage"], "implementation_qa")
        release = pm_router.next_route(value, "implementation_qa", "pass")
        self.assertEqual(release["target_role"], "supervisor")

    def test_reject_reuses_worker_and_redirect_creates_a_new_one(self):
        value = packet("separate", True)
        reject = pm_router.next_route(value, "plan_qa", "reject")
        redirect = pm_router.next_route(value, "plan_qa", "redirect")
        self.assertEqual(reject["session"], "same")
        self.assertEqual(redirect["session"], "new")

    def test_ambiguity_returns_to_at_not_pm(self):
        route = pm_router.next_route(packet("separate", True),
                                     "implementation_qa", "requirement_ambiguous")
        self.assertEqual(route["target_role"], "at")
        self.assertEqual(route["stage"], "requirement_ambiguous")

    def test_requirement_change_holds_every_old_route(self):
        route = pm_router.next_route(packet("plan_and_implement", False),
                                     "requirement_changed")
        self.assertEqual(route["action"], "hold")
        self.assertEqual(route["stage"], "waiting_requirement")


if __name__ == "__main__":
    unittest.main()
