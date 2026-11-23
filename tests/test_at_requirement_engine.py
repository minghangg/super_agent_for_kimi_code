from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from super_agent import at_serve, config, flow as flow_mod, requirements as req, session_events
from super_agent.allowlist import ProjectAllowlist
from super_agent.engine import Engine
from super_agent.store import Store

THREAD = "owner/demo#1"
AT_KEY = "at-demo-3f7c1a90"
QA_KEY = "qa-demo-3f7c1a90"
PM_KEY = "pm-demo-3f7c1a90"
SUP_KEY = "supervisor-demo-7c31ade2"


def package(revision=1, *, questions=None, objective="build X"):
    value = {
        "schema_version": 1, "requirement_id": THREAD, "revision": revision,
        "status": "clarifying" if questions else "ready", "objective": objective,
        "context_refs": ["issue-1"], "scope": ["engine"],
        "out_of_scope": ["UI"], "constraints": ["serial"],
        "acceptance": ["approved example passes"],
        "negative_tests": ["stale revision is refused"], "risk_tier": "T1",
        "review_policy": {"plan_qa": False, "implementation_qa": True,
                          "worker_mode": "plan_and_implement", "test_level": "L1"},
        "open_questions": list(questions or []), "approval_ref": "",
        "updated_at": "test",
    }
    value["requirement_hash"] = req.requirement_hash(value)
    return value


def at_comment(cid, value, state="ready"):
    label = "有疑问" if state == "questions" else "可派单"
    return {"id": cid, "issue": 1, "author": "at", "session_key": AT_KEY,
            "body": f"[AT] {label} #1 <!-- verdict: {state} -->\n"
                    f"{req.fence(req.PACKAGE_FENCE, value)}\n"
                    f"<!-- session: {AT_KEY} -->"}


def approval(cid, value, package_cid):
    return {"id": cid, "issue": 1, "author": "supervisor",
            "body": req.fence(req.APPROVAL_FENCE, {
                "requirement_id": THREAD, "revision": value["revision"],
                "requirement_hash": value["requirement_hash"],
                "package_ref": f"issuecomment-{package_cid}"})
                + f"\n<!-- session: {SUP_KEY} -->"}


class Bus:
    def __init__(self):
        self.rows = []

    def healthz(self):
        return {"ok": True, "ready": True, "connected": True}

    def inject(self, event, payload):
        self.rows.append((event, payload))
        return {"ok": True, "seq": len(self.rows)}

    def events(self, name):
        return [p for e, p in self.rows if e == name]


class Source:
    def __init__(self):
        self.rows = []

    def add(self, row):
        self.rows.append(row)

    def issues(self, limit=20):
        return [1]

    def title(self, issue):
        return "AT mode"

    def comments(self, issue):
        return sorted(self.rows, key=lambda row: row["id"])

    def paged(self, issue, *, since_id=0, max_pages=10):
        return [row for row in self.comments(issue) if row["id"] > since_id]

    def issue_state(self, issue):
        return "open"


class Sessions:
    def __init__(self):
        self.archived = set()

    def archive_session(self, sid):
        self.archived.add(sid)
        return True, {"ids": [sid]}

    def session_info(self, sid):
        return {"archived": sid in self.archived}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "store"
        self.shared = self.root / "sessions"
        self.shared.mkdir()
        self.events = self.shared / "events.jsonl"
        self.allow = self.root / "allowlist.json"
        self.allow.write_text(json.dumps({
            "schema_version": 1,
            "defaults": {"mode": "assist", "creation_mode": "pm_created"},
            "projects": {"demo": {
                "repo": "owner/demo", "cwd": ".", "mode": "assist",
                "creation_mode": "pm_created", "issues": [1], "at_mode": True,
                "actions": ["bus_inject", "collect", "reconcile", "archive"],
                "roles": ["worker", "qa", "pm", "at"]}}}), encoding="utf-8")
        (self.shared / "sessions.json").write_text(json.dumps({
            SUP_KEY: {"id": "session_sup"}, AT_KEY: {"id": "session_at"},
            QA_KEY: {"id": "session_qa"}, PM_KEY: {"id": "session_pm"},
        }), encoding="utf-8")
        (self.shared / "issue_sessions.json").write_text(json.dumps({
            THREAD: {"at": {"name": AT_KEY, "session": "session_at"}}}),
            encoding="utf-8")
        rows = []
        seq = 0
        for sid, key, role in (("session_w1", "wk-probe-1", "worker"),
                               ("session_qa", QA_KEY, "qa"),
                               ("session_pm", PM_KEY, "pm"),
                               ("session_at", AT_KEY, "at")):
            seq += 1
            rows.append({"seq": seq, "type": session_events.CREATED,
                         "sid": sid, "key": key})
            seq += 1
            rows.append({"seq": seq, "type": session_events.BOUND,
                         "sid": sid, "key": THREAD, "role": role, "issue": 1})
        self.events.write_text("\n".join(json.dumps(row) for row in rows) + "\n",
                               encoding="utf-8")
        self.source = Source()
        self.bus = Bus()

    def tearDown(self):
        self.tmp.cleanup()

    def engine(self):
        cfg = config.load(None, {
            "mode": "assist", "role": "supervisor",
            "github": {"source": "none", "repo": "owner/demo"},
            "engine": {"session_events_path": str(self.events),
                       "archived_crosscheck": False,
                       "supervisor_sid": "session_sup",
                       "allowlist": ["issue_comment.created"]}})
        engine = Engine(cfg, Store(self.state), self.source, self.bus,
                        project_allowlist=ProjectAllowlist(self.allow),
                        session_api=Sessions())
        engine._round_closed_threads = lambda index: []
        return engine

    def publish(self, value, cid, *, approved=True):
        self.source.add(at_comment(cid, value,
                                   "questions" if value["open_questions"] else "ready"))
        if approved:
            self.source.add(approval(cid + 1, value, cid))

    def index(self, engine):
        return engine.store.load_index()


class Gates(Base):
    def test_live_worker_is_not_an_assignment_before_approval(self):
        value = package()
        self.publish(value, 100, approved=False)
        engine = self.engine()
        engine.run_once()
        index = self.index(engine)
        self.assertEqual(index.get("assignments"), {})
        reasons = [row["reason"] for row in index["issues"][THREAD]["needs_person"]]
        self.assertIn("requirement.not_approved[user]", reasons)

    def test_approval_binds_the_exact_hash_and_policy(self):
        value = package()
        self.publish(value, 100)
        engine = self.engine()
        engine.run_once()
        rec = self.index(engine)["assignments"]["asg_1_worker_1"]
        self.assertEqual(rec["requirement_id"], THREAD)
        self.assertEqual(rec["revision"], 1)
        self.assertEqual(rec["requirement_hash"], value["requirement_hash"])
        self.assertEqual(rec["approval_ref"], "issuecomment-101")
        self.assertEqual(rec["review_policy"]["worker_mode"], "plan_and_implement")

    def test_delivery_without_a_version_reference_does_not_advance(self):
        value = package()
        self.publish(value, 100)
        engine = self.engine()
        engine.run_once()
        self.source.add({"id": 9001, "issue": 1, "author": "worker",
                         "body": "[WK:wk-probe-1] done"})
        engine.run_once()
        rec = self.index(engine)["assignments"]["asg_1_worker_1"]
        self.assertEqual(rec["status"], "working")
        self.assertIsNone(rec["delivery_comment_id"])

    def test_versioned_delivery_and_qa_advance_the_same_assignment(self):
        value = package()
        self.publish(value, 100)
        engine = self.engine()
        engine.run_once()
        marker = req.reference_marker(value)
        self.source.add({"id": 9001, "issue": 1, "author": "worker",
                         "body": f"[WK:wk-probe-1] done\n{marker}"})
        engine.run_once()
        rec = self.index(engine)["assignments"]["asg_1_worker_1"]
        self.assertEqual(rec["status"], "waiting_review")
        notice = self.bus.events("event.agent.waiting_review")[-1]
        self.assertEqual(notice["requirement_hash"], value["requirement_hash"])
        self.source.add({"id": 9100, "issue": 1, "author": "qa",
                         "session_key": QA_KEY,
                         "body": f"[QA] 通过 <!-- fix_needed: false -->\n"
                                 f"<!-- session: {QA_KEY} -->\n{marker}"})
        engine.run_once()
        rec = self.index(engine)["assignments"]["asg_1_worker_1"]
        self.assertEqual(rec["status"], "waiting_release")
        self.assertEqual(rec["qa_verdict"], "pass")

    def test_qa_only_assignment_accepts_its_five_part_review(self):
        value = package()
        self.publish(value, 100)
        rows = [json.loads(line) for line in
                self.events.read_text(encoding="utf-8").splitlines() if line]
        rows = [row for row in rows if row.get("role") != "worker"
                and row.get("key") != "wk-probe-1"]
        self.events.write_text("\n".join(json.dumps(row) for row in rows) + "\n",
                               encoding="utf-8")
        engine = self.engine()
        engine.run_once()
        marker = req.reference_marker(value)
        self.source.add({"id": 9100, "issue": 1, "author": "qa",
                         "session_key": QA_KEY,
                         "body": f"[{QA_KEY}]-[完工]-[{PM_KEY}]-[demo] "
                                 f"<!-- fix_needed: false -->\n"
                                 f"<!-- session: {QA_KEY} -->\n{marker}"})
        engine.run_once()
        qa = self.index(engine)["assignments"]["asg_1_qa_1"]
        self.assertEqual(qa["status"], "waiting_release")
        self.assertEqual(qa["review_comment_id"], 9100)
        self.assertEqual(qa["qa_verdict"], "pass")

    def test_stale_qa_hash_cannot_pass_the_current_work(self):
        value = package()
        self.publish(value, 100)
        engine = self.engine()
        engine.run_once()
        self.source.add({"id": 9001, "issue": 1, "author": "worker",
                         "body": f"[WK:wk-probe-1] done\n{req.reference_marker(value)}"})
        engine.run_once()
        old = dict(value, requirement_hash="sha256:" + "0" * 64)
        self.source.add({"id": 9100, "issue": 1, "author": "qa",
                         "session_key": QA_KEY,
                         "body": f"[QA] 通过 <!-- fix_needed: false -->\n"
                                 f"<!-- session: {QA_KEY} -->\n{req.reference_marker(old)}"})
        engine.run_once()
        rec = self.index(engine)["assignments"]["asg_1_worker_1"]
        self.assertEqual(rec["status"], "waiting_review")
        self.assertNotEqual(rec.get("qa_verdict"), "pass")

    def test_new_revision_pauses_old_work_and_new_approval_forks_assignment(self):
        first = package()
        self.publish(first, 100)
        engine = self.engine()
        engine.run_once()
        second = package(2, questions=["which boundary?"], objective="changed X")
        self.publish(second, 200, approved=False)
        engine.run_once()
        old = self.index(engine)["assignments"]["asg_1_worker_1"]
        self.assertEqual(old["status"], "waiting_requirement")
        questions = list((self.state / "atserve" / "questions").glob("*.json"))
        self.assertEqual(len(questions), 1)
        record = json.loads(questions[0].read_text(encoding="utf-8"))
        self.assertEqual(record["revision"], 2)
        self.assertEqual(record["requirement_hash"], second["requirement_hash"])

        ready = package(3, objective="changed X")
        self.publish(ready, 300)
        engine.run_once()
        assignments = self.index(engine)["assignments"]
        self.assertEqual(assignments["asg_1_worker_1"]["status"], "waiting_requirement")
        self.assertEqual(assignments["asg_1_worker_2"]["status"], "working")
        self.assertEqual(assignments["asg_1_worker_2"]["revision"], 3)

    def test_prepared_old_outbox_is_closed_without_an_external_send(self):
        first = package()
        self.publish(first, 100)
        engine = self.engine()
        engine.run_once()
        aid = engine.store.begin_action(
            "asg_1_worker_1:bus_inject:9001", verb="bus_inject",
            assignment_id="asg_1_worker_1", input_ref="9001", issue=1,
            payload={"thread": THREAD, "requirement_id": THREAD, "revision": 1,
                     "requirement_hash": first["requirement_hash"]})["action_id"]
        second = package(2, objective="changed X")
        self.publish(second, 200)
        engine.run_once()
        before = len(self.bus.rows)
        results = engine.replay_prepared()
        self.assertEqual(len(self.bus.rows), before)
        self.assertTrue(results[0]["refused"])
        self.assertIn("superseded", results[0]["reason"])
        self.assertNotIn(aid, engine.store.load_outbox_state()["actions"])


class FlowBranches(Base):
    def test_standard_flow_keeps_legacy_assignments_without_at_versions(self):
        self.publish(package(), 100)
        self.source.flow = lambda issue: flow_mod.STANDARD
        engine = self.engine()
        engine.run_once()
        index = self.index(engine)
        self.assertEqual(index["assignments"]["asg_1_worker_1"]["status"], "working")
        self.assertFalse(index["assignments"]["asg_1_worker_1"].get("requirement_id"))
        self.assertEqual(index["issues"][THREAD]["flow"], "standard")

    def test_flow_at_uses_the_approved_requirement_gate(self):
        self.publish(package(), 100)
        self.source.flow = lambda issue: flow_mod.AT
        engine = self.engine()
        engine.run_once()
        index = self.index(engine)
        self.assertEqual(index["assignments"]["asg_1_worker_1"]["status"], "working")
        self.assertEqual(index["issues"][THREAD]["flow"], "at")

    def test_conflicting_flow_labels_fail_closed(self):
        self.publish(package(), 100)

        def conflict(issue):
            raise flow_mod.FlowError("both flow labels")
        self.source.flow = conflict
        engine = self.engine()
        engine.run_once()
        index = self.index(engine)
        self.assertEqual(index.get("assignments"), {})
        self.assertEqual(index["issues"][THREAD]["flow"], "invalid")
        self.assertIn("flow.conflict[user]",
                      [row["reason"] for row in index["issues"][THREAD]["needs_person"]])

    def test_at_to_standard_blocks_old_work_for_explicit_recovery(self):
        value = package()
        self.publish(value, 100)
        engine = self.engine()
        engine.run_once()
        engine.store.begin_action(
            "asg_1_worker_1:bus_inject:9001", verb="bus_inject",
            assignment_id="asg_1_worker_1", input_ref="9001", issue=1,
            payload={"thread": THREAD, "requirement_id": THREAD, "revision": 1,
                     "requirement_hash": value["requirement_hash"]})
        self.source.flow = lambda issue: flow_mod.STANDARD
        engine.run_once()
        index = self.index(engine)
        assignment = index["assignments"]["asg_1_worker_1"]
        self.assertEqual(assignment["status"], "blocked")
        self.assertEqual(assignment["flow_previous_status"], "working")
        self.assertEqual(assignment["flow_hold_reason"], "flow.changed:at->standard")
        self.assertIn("flow.switch_pause[user]",
                      [row["reason"] for row in index["issues"][THREAD]["needs_person"]])
        self.source.add({"id": 9001, "issue": 1, "author": "worker",
                         "body": "[WK:wk-probe-1] delivery during flow switch"})
        engine.run_once()
        assignment = self.index(engine)["assignments"]["asg_1_worker_1"]
        self.assertEqual(assignment["status"], "blocked")
        self.assertIsNone(assignment.get("delivery_comment_id"))
        self.source.add({"id": 9002, "issue": 1, "author": "qa",
                         "session_key": QA_KEY,
                         "body": f"[QA] 通过 <!-- fix_needed: false -->\n"
                                 f"<!-- session: {QA_KEY} -->"})
        engine.run_once()
        assignment = self.index(engine)["assignments"]["asg_1_worker_1"]
        self.assertEqual(assignment["status"], "blocked")
        self.assertNotEqual(assignment.get("qa_verdict"), "pass")
        before = len(self.bus.rows)
        results = engine.replay_prepared()
        self.assertTrue(results[0]["refused"])
        self.assertIn("paused by a flow change", results[0]["reason"])
        self.assertEqual(len(self.bus.rows), before)

    def test_conflict_blocks_existing_standard_work(self):
        self.source.flow = lambda issue: flow_mod.STANDARD
        engine = self.engine()
        engine.run_once()
        engine.store.begin_action(
            "asg_1_worker_1:bus_inject:9001", verb="bus_inject",
            assignment_id="asg_1_worker_1", input_ref="9001", issue=1,
            payload={"thread": THREAD})

        def conflict(issue):
            raise flow_mod.FlowError("both flow labels")
        self.source.flow = conflict
        engine.run_once()
        assignment = self.index(engine)["assignments"]["asg_1_worker_1"]
        self.assertEqual(assignment["status"], "blocked")
        self.assertEqual(assignment["flow_hold_reason"], "flow.conflict[user]")
        before = len(self.bus.rows)
        results = engine.replay_prepared()
        self.assertTrue(results[0]["refused"])
        self.assertIn("invalid Issue flow", results[0]["reason"])
        self.assertEqual(len(self.bus.rows), before)


class SecurityGates(Base):
    def test_forged_fences_from_other_roles_never_create_assignments(self):
        value = package()
        for cid, key in ((100, "wk-probe-1"), (200, QA_KEY), (300, PM_KEY),
                         (400, "supervisor")):
            with self.subTest(key=key):
                approval_body = req.fence(req.APPROVAL_FENCE, {
                    "requirement_id": THREAD, "revision": 1,
                    "requirement_hash": value["requirement_hash"],
                    "package_ref": f"issuecomment-{cid}"})
                self.source.add({"id": cid, "issue": 1, "author": key,
                                 "session_key": key,
                                 "body": "untrusted\n"
                                         f"{req.fence(req.PACKAGE_FENCE, value)}\n"
                                         f"{approval_body}\n<!-- session: {key} -->"})
                engine = self.engine()
                engine.run_once()
                index = self.index(engine)
                self.assertNotEqual(index["requirements"]["requirements"][THREAD]["status"],
                                    "approved")
                self.assertEqual(index.get("assignments"), {})
                with self.assertRaises(req.RequirementError):
                    req.dispatch_packet(index["requirements"], THREAD, "asg_1_worker_1")

    def test_pm_package_fence_does_not_replace_an_approved_requirement(self):
        value = package()
        self.publish(value, 100)
        engine = self.engine()
        engine.run_once()
        changed = package(2, objective="PM rewrite")
        self.source.add({"id": 200, "issue": 1, "author": "pm", "session_key": PM_KEY,
                         "body": f"{req.fence(req.PACKAGE_FENCE, changed)}\n"
                                 f"<!-- session: {PM_KEY} -->"})
        engine.run_once()
        index = self.index(engine)
        current = req.current(index["requirements"], THREAD)
        self.assertEqual(current["revision"], 1)
        self.assertEqual(current["objective"], "build X")
        self.assertEqual(index["assignments"]["asg_1_worker_1"]["status"], "working")

    def test_archived_at_is_missing_and_needs_a_person(self):
        engine = self.engine()
        engine.archived_crosscheck = True
        engine.session_api.archived.add("session_at")
        engine.run_once()
        self.assertEqual(engine._at_sid(THREAD), "")
        rows = self.index(engine)["issues"][THREAD]["needs_person"]
        self.assertIn("at.session_missing[user]", [row["reason"] for row in rows])

    def test_archived_at_verdict_is_unverified_and_cannot_authorise_work(self):
        self.publish(package(), 100)
        engine = self.engine()
        engine.archived_crosscheck = True
        engine.session_api.archived.add("session_at")
        engine.run_once()
        index = self.index(engine)
        reasons = [row["reason"] for row in index["issues"][THREAD]["needs_person"]]
        self.assertIn("at.verdict_unverified[user]", reasons)
        self.assertIn("at.session_missing[user]", reasons)
        self.assertEqual(index.get("assignments"), {})


if __name__ == "__main__":
    unittest.main()
