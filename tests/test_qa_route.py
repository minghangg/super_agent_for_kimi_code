"""Routing a ``[QA]`` verdict: always to the project PM, never to a worker.

The properties pinned here:

* a verdict comment is read as a review even when the configured role is the
  one that wrote it (the marker beats role noise, exactly like ``[WK:]``);
* the dispatch goes to ``event.agent.project_pm`` with ``origin_role="qa"``;
* ``_dispatch`` refuses ``origin_role=qa`` + ``target_role=worker`` without
  moving the external-action counter or injecting anything - a claim about the
  engine's own route decision, not about every process on the machine;
* a suppressed dispatch (observe mode / a refused allowlist) is retried next
  round under the same action id, and a delivered one is never sent twice;
* an unverified verdict - no qa session key on this thread - is still applied,
  and the warning names doctor as its only consumer.

Run: ``python -m pytest tests/test_qa_route.py -q``
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from super_agent import config, session_events
from super_agent.allowlist import ProjectAllowlist
from super_agent.engine import Engine
from super_agent.store import Store, action_id

THREAD = "owner/demo#1"

GOOD = {
    "schema_version": 1,
    "defaults": {"mode": "observe", "creation_mode": "pm_created"},
    "projects": {
        "demo": {"repo": "owner/demo", "cwd": ".", "mode": "assist",
                 "creation_mode": "pm_created", "issues": [1],
                 "actions": ["bus_inject", "collect", "reconcile", "archive"],
                 "max_active_writers": 1, "roles": ["worker", "qa"]},
    },
}


class FakeBus:
    def __init__(self, *, fail=False):
        self.injected = []
        self.fail = fail

    def healthz(self):
        return {"ok": True, "ready": True, "connected": True, "seq": 1}

    def inject(self, event_type, payload):
        if self.fail:
            raise RuntimeError("bus down")
        self.injected.append((event_type, payload))
        return {"ok": True, "seq": len(self.injected)}

    def events(self):
        return [e for e, _ in self.injected]


class Comments:
    def __init__(self):
        self.rows = []

    def add(self, cid, body, author="worker"):
        self.rows.append({"id": cid, "issue": 1, "body": body,
                          "author": author, "created_at": ""})

    def issues(self, limit=20):
        return [1]

    def title(self, issue):
        return "probe"

    def comments(self, issue):
        return sorted(self.rows, key=lambda c: c["id"])

    def paged(self, issue, *, since_id=0, max_pages=10):
        return self.comments(issue)


class FakeSessions:
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
        self.state = self.root / "state"
        self.events = self.root / "lifecycle" / "events.jsonl"
        self.events.parent.mkdir(parents=True, exist_ok=True)
        self.allow = self.root / "allowlist.json"
        self.allow.write_text(json.dumps(GOOD), encoding="utf-8")
        self.source = Comments()
        self.bus = FakeBus()
        self.sessions = FakeSessions()

    def tearDown(self):
        self.tmp.cleanup()

    def bind(self, sid, *, role="worker", issue=1, name="wk-probe-1", key=THREAD):
        row = {"seq": 1, "at": "2026-11-10T10:00:00+00:00",
               "type": session_events.CREATED, "sid": sid, "key": name}
        bound = {"seq": 2, "at": "2026-11-10T10:00:01+00:00",
                 "type": session_events.BOUND, "sid": sid, "key": key,
                 "role": role, "issue": issue}
        self.events.write_text(json.dumps(row) + "\n" + json.dumps(bound) + "\n",
                               encoding="utf-8")

    def bind_both(self):
        """One worker and one qa on the same thread."""
        rows = [
            {"seq": 1, "at": "2026-11-10T10:00:00+00:00",
             "type": session_events.CREATED, "sid": "session_w1", "key": "wk-probe-1"},
            {"seq": 2, "at": "2026-11-10T10:00:01+00:00",
             "type": session_events.BOUND, "sid": "session_w1", "key": THREAD,
             "role": "worker", "issue": 1},
            {"seq": 3, "at": "2026-11-10T10:00:02+00:00",
             "type": session_events.CREATED, "sid": "session_q1", "key": "qa-demo"},
            {"seq": 4, "at": "2026-11-10T10:00:03+00:00",
             "type": session_events.BOUND, "sid": "session_q1", "key": THREAD,
             "role": "qa", "issue": 1},
        ]
        self.events.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                               encoding="utf-8")

    def engine(self, *, role="supervisor", mode="assist", actions=None) -> Engine:
        if actions is not None:
            data = json.loads(json.dumps(GOOD))
            data["projects"]["demo"]["actions"] = actions
            self.allow.write_text(json.dumps(data), encoding="utf-8")
        cfg = config.load(None, {
            "mode": mode, "role": role,
            "github": {"source": "none", "repo": "owner/demo"},
            "engine": {"session_events_path": str(self.events),
                       "allowlist": ["issue_comment.created"]},
        })
        return Engine(cfg, Store(self.state), self.source, self.bus,
                      project_allowlist=ProjectAllowlist(self.allow),
                      session_api=self.sessions)

    def rows(self):
        path = self.state / "events.jsonl"
        if not path.is_file():
            return []
        return [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]

    def review_chain(self, *, verdict="[QA] **不通过（退回改）** — still broken"):
        """Worker delivers, then QA judges. Returns the assignment record."""
        self.bind_both()
        self.source.add(9001, "[WK:wk-probe-1] done")
        self.source.add(9100, verdict, author="qa")
        engine = self.engine()
        engine.run_once()
        rec = engine.store.load_index()["assignments"]["asg_1_worker_1"]
        return engine, rec


class VerdictGoesToPm(Base):
    def test_reject_moves_the_assignment_and_routes_to_the_project_pm(self):
        engine, rec = self.review_chain()
        self.assertEqual(rec["status"], "working")
        self.assertEqual(rec["qa_verdict"], "reject")
        self.assertEqual(rec["sid"], "session_w1")
        kinds = self.bus.events()
        self.assertIn("event.agent.project_pm", kinds)
        self.assertNotIn("event.agent.worker_task", kinds)
        payload = [p for e, p in self.bus.injected
                   if e == "event.agent.project_pm"][0]
        self.assertEqual(payload["verdict"], "reject")
        self.assertEqual(payload["project"], "demo")
        self.assertEqual(payload["review_comment_id"], 9100)

    def test_pass_moves_the_assignment_to_waiting_release(self):
        engine, rec = self.review_chain(verdict="[QA] **通过**，可关单")
        self.assertEqual(rec["status"], "waiting_release")
        payload = [p for e, p in self.bus.injected
                   if e == "event.agent.project_pm"][0]
        self.assertEqual(payload["verdict"], "pass")

    def test_a_verdict_beats_role_noise(self):
        # The engine's own role is ``qa`` here, so a shared-login comment would
        # otherwise be ignored as role-authored noise and the verdict lost.
        self.bind_both()
        self.source.add(9001, "[WK:wk-probe-1] done")
        self.source.add(9100, "[QA] 结论：不通过——still broken", author="qa")
        engine = self.engine(role="qa")
        engine.run_once()
        rec = engine.store.load_index()["assignments"]["asg_1_worker_1"]
        self.assertEqual(rec["status"], "working")

    def test_unverified_verdict_still_applies_and_names_doctor(self):
        # No qa session key on this thread at all.
        self.bind("session_w1")
        self.source.add(9001, "[WK:wk-probe-1] done")
        self.source.add(9100, "[QA] REJECT", author="someone")
        engine = self.engine()
        engine.run_once()
        rec = engine.store.load_index()["assignments"]["asg_1_worker_1"]
        self.assertEqual(rec["status"], "working")
        warn = [r for r in self.rows() if r["type"] == "review.unverified"]
        self.assertEqual(len(warn), 1)
        self.assertEqual(warn[0]["consumers"], ["doctor"])

    def test_review_without_a_worker_assignment_escalates(self):
        # A qa session is bound but no delivery ever arrived, so the verdict
        # has nothing to judge: escalate rather than dispatch a decision.
        self.bind("session_w1")
        self.source.add(9100, "[QA] PASS", author="qa")
        engine = self.engine()
        summary = engine.run_once()
        self.assertEqual(summary["needs_person"], 1)
        self.assertEqual([e for e in self.bus.events()
                          if e == "event.agent.project_pm"], [])
        rows = [r for r in self.rows() if r["type"] == "review.unmatched"]
        self.assertEqual(len(rows), 1)


class RoutingRetries(Base):
    def test_observe_mode_writes_a_route_record_and_no_injection(self):
        self.bind_both()
        self.source.add(9001, "[WK:wk-probe-1] done")
        self.source.add(9100, "[QA] REJECT", author="qa")
        engine = self.engine(mode="observe")
        engine.run_once()
        self.assertEqual(self.bus.injected, [])
        routes = engine.store.load_index()["routes"]
        self.assertEqual(len(routes), 1)
        key = f"route:{THREAD}:9100"
        self.assertFalse(routes[key]["ok"])
        self.assertEqual(engine.store.action_attempted(
            action_id(f"route:{THREAD}:9100", "bus_inject", "9100")), 0)

    def test_refused_allowlist_is_retried_next_round(self):
        engine = self.engine(actions=["collect", "reconcile"])
        self.bind_both()
        self.source.add(9001, "[WK:wk-probe-1] done")
        self.source.add(9100, "[QA] REJECT", author="qa")
        engine.run_once()
        self.assertEqual(self.bus.injected, [])
        # Restore the allowlist: the next round retries under the same id.
        self.allow.write_text(json.dumps(GOOD), encoding="utf-8")
        engine.projects.poll()
        engine.run_once()
        self.assertEqual([e for e in self.bus.events()
                          if e == "event.agent.project_pm"], ["event.agent.project_pm"])

    def test_a_delivered_verdict_is_never_sent_twice(self):
        engine, _rec = self.review_chain()
        engine.run_once()
        engine.run_once()
        self.assertEqual([e for e in self.bus.events()
                          if e == "event.agent.project_pm"].count(
                              "event.agent.project_pm"), 1)

    def test_an_uncertain_verdict_is_not_resent(self):
        self.bus.fail = True
        _engine, _rec = self.review_chain()
        base = action_id(f"route:{THREAD}:9100", "bus_inject", "9100")
        state = _engine.store.load_outbox_state()
        self.assertEqual(state["actions"][base]["status"], "uncertain")


class CounterfactEngineNeverRoutesQaToWorker(Base):
    """The refusal, as narrowly as it is true.

    Only ``_dispatch`` is under test: a process calling the session API on its
    own is outside this gate, so the assertion is about this combination of
    origin and target roles, not about every route on the machine.
    """

    def test_qa_to_worker_is_refused_before_anything_else(self):
        self.bind_both()
        engine = self.engine()
        out = engine._dispatch({
            "kind": "bus_inject", "event": "event.agent.worker_task",
            "issue": 1, "input_ref": "9100", "thread": THREAD,
            "origin_role": "qa", "target_role": "worker",
            "payload": {"sid": "session_w1", "verdict": "reject"},
        })
        self.assertFalse(out["dispatched"])
        self.assertTrue(out["refused"])
        refused = [r for r in self.rows() if r["type"] == "route.refused"]
        self.assertEqual(len(refused), 1)
        self.assertEqual(self.bus.injected, [])
        self.assertEqual(engine.store.load_counters()["external_actions"], 0)

    def test_the_same_target_from_the_pm_is_allowed(self):
        # Counter-example: the gate is about the origin, not about the target.
        self.bind_both()
        engine = self.engine()
        out = engine._dispatch({
            "kind": "bus_inject", "event": "event.agent.worker_task",
            "issue": 1, "input_ref": "9200", "thread": THREAD,
            "origin_role": "pm", "target_role": "worker",
            "payload": {"sid": "session_w1", "verdict": ""},
        })
        self.assertTrue(out["dispatched"])
        self.assertEqual(len(self.bus.injected), 1)


if __name__ == "__main__":
    unittest.main()
