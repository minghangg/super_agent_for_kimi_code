"""Assignment lifecycle and the one-shot ``waiting_review`` notification.

Every test injects its own session stream through
``engine.session_events_path``: ``--state-dir`` does **not** isolate the
lifecycle stream (``session_events.resolve_events_path`` looks at
``SESS_EVENTS_STATE_DIR``/``LWA_STATE_DIR``/cwd), so relying on the directory
would silently read the live workbench state.

Run: ``python -m pytest tests/test_assignments.py -q``
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from super_agent import config, session_events
from super_agent import assignments as asg
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
                 "actions": ["bus_inject", "collect", "reconcile"],
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


class Comments:
    def __init__(self, rows):
        self.rows = list(rows)

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
    """Archive + read-back without touching the session service."""

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
        self.source = Comments([])
        self.bus = FakeBus()
        self.sessions = FakeSessions()

    def tearDown(self):
        self.tmp.cleanup()

    def bind(self, sid, *, role="worker", issue=1, key=THREAD, name="wk-probe-1"):
        rows = [
            {"seq": 1, "at": "2026-11-09T10:00:00+00:00",
             "type": session_events.CREATED, "sid": sid, "key": name},
            {"seq": 2, "at": "2026-11-09T10:00:01+00:00",
             "type": session_events.BOUND, "sid": sid, "key": key,
             "role": role, "issue": issue},
        ]
        self.events.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                               encoding="utf-8")

    def engine(self, bus=None, mode="assist") -> Engine:
        cfg = config.load(None, {
            "mode": mode,
            "github": {"source": "none", "repo": "owner/demo"},
            "engine": {"session_events_path": str(self.events),
                       "allowlist": ["issue_comment.created"]},
        })
        return Engine(cfg, Store(self.state), self.source, bus or self.bus,
                      project_allowlist=ProjectAllowlist(self.allow),
                      session_api=self.sessions)

    def index(self, engine):
        return engine.store.load_index()


class Sync(Base):
    def test_bind_creates_one_working_assignment(self):
        self.bind("session_w1")
        engine = self.engine()
        engine.run_once()
        index = self.index(engine)
        self.assertEqual(sorted(index["assignments"]), ["asg_1_worker_1"])
        rec = index["assignments"]["asg_1_worker_1"]
        self.assertEqual(rec["status"], "working")
        self.assertEqual(rec["sid"], "session_w1")
        self.assertEqual(rec["session_key"], "wk-probe-1")

    def test_repeated_rounds_do_not_fork_a_second_assignment(self):
        self.bind("session_w1")
        engine = self.engine()
        engine.run_once()
        engine.run_once()
        self.assertEqual(len(self.index(engine)["assignments"]), 1)

    def test_archived_session_creates_nothing(self):
        rows = [
            {"seq": 1, "at": "2026-11-09T10:00:00+00:00",
             "type": session_events.CREATED, "sid": "s", "key": "wk-probe-1"},
            {"seq": 2, "at": "2026-11-09T10:00:01+00:00",
             "type": session_events.BOUND, "sid": "s", "key": THREAD,
             "role": "worker", "issue": 1},
            {"seq": 3, "at": "2026-11-09T10:00:02+00:00",
             "type": session_events.ARCHIVED, "sid": "s"},
        ]
        self.events.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                               encoding="utf-8")
        engine = self.engine()
        engine.run_once()
        self.assertEqual(self.index(engine)["assignments"], {})

    def test_unreadable_stream_creates_nothing_and_degrades(self):
        self.events.unlink(missing_ok=True)
        engine = self.engine()
        engine.run_once()
        self.assertEqual(self.index(engine)["assignments"], {})
        self.assertFalse(engine.degraded, "a missing stream is not an error")
        health = json.loads((self.state / "health.json").read_text("utf-8"))
        self.assertEqual(health["active_assignments"], 0)

    def test_reader_failure_is_recorded_and_fails_closed(self):
        self.bind("session_w1")
        engine = self.engine()
        real = session_events.load_events

        def boom(path):
            raise RuntimeError("stream exploded")

        session_events.load_events = boom
        try:
            engine.run_once()
        finally:
            session_events.load_events = real
        self.assertTrue(engine.degraded)
        self.assertEqual(self.index(engine)["assignments"], {})


class Delivery(Base):
    def test_marker_match_moves_to_waiting_review(self):
        self.bind("session_w1")
        self.source.add(9001, "[WK:wk-probe-1] all green")
        engine = self.engine()
        engine.run_once()
        rec = self.index(engine)["assignments"]["asg_1_worker_1"]
        # The delivery is archived in the same round that noticed it, so the
        # observable result of "matched" is the completed record.
        self.assertEqual(rec["status"], "completed")
        self.assertEqual(rec["delivery_comment_id"], 9001)
        self.assertEqual(rec["matched_by"], "marker")

    def test_named_worker_marker_matches_its_own_session(self):
        self.bind("session_w1")
        self.source.add(9002, "[WK:wk-probe-1] done")
        engine = self.engine()
        engine.run_once()
        rec = self.index(engine)["assignments"]["asg_1_worker_1"]
        self.assertEqual(rec["matched_by"], "marker")

    def test_sole_worker_fallback(self):
        self.bind("session_w1")
        self.source.add(9003, "[WK:someone-else] done")
        engine = self.engine()
        engine.run_once()
        self.assertEqual(self.index(engine)["assignments"]["asg_1_worker_1"]["matched_by"],
                         "sole")

    def test_unmatched_delivery_escalates_and_is_not_dropped(self):
        self.bind("session_w1", role="qa")
        self.bind("session_w2", role="worker", name="wk-probe-2")
        # qa + worker: two candidates, neither named by the marker.
        rows = [
            {"seq": 1, "at": "2026-11-09T10:00:00+00:00",
             "type": session_events.CREATED, "sid": "session_w1", "key": "wk-probe-1"},
            {"seq": 2, "at": "2026-11-09T10:00:01+00:00",
             "type": session_events.BOUND, "sid": "session_w1", "key": THREAD,
             "role": "qa", "issue": 1},
            {"seq": 3, "at": "2026-11-09T10:00:02+00:00",
             "type": session_events.CREATED, "sid": "session_w2", "key": "wk-probe-2"},
            {"seq": 4, "at": "2026-11-09T10:00:03+00:00",
             "type": session_events.BOUND, "sid": "session_w2", "key": THREAD,
             "role": "worker", "issue": 1},
        ]
        self.events.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                               encoding="utf-8")
        self.source.add(9010, "[WK:nobody] done")
        engine = self.engine()
        summary = engine.run_once()
        self.assertEqual(summary["needs_person"], 1)
        events = [json.loads(x) for x in
                  (self.state / "events.jsonl").read_text("utf-8").splitlines()]
        self.assertEqual([e["type"] for e in events].count("assignment.unmatched"), 1)


class Notification(Base):
    def test_exactly_one_waiting_review_event_and_attempt(self):
        self.bind("session_w1")
        self.source.add(9001, "[WK:owner/demo#1] done")
        engine = self.engine()
        engine.run_once()
        engine.run_once()             # a second round must not notify again
        events = [json.loads(x) for x in
                  (self.state / "events.jsonl").read_text("utf-8").splitlines()]
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds.count("assignment.waiting_review"), 1)
        self.assertEqual(len(self.bus.injected), 1)
        state = engine.store.load_outbox_state()
        base = action_id("asg_1_worker_1", "bus_inject", "9001")
        self.assertEqual(state["attempts"][base], 1)

    def test_refused_dispatch_retries_next_round(self):
        # No bus_inject in the allowlist: the dispatch is refused, no attempt
        # is recorded, so the next round is allowed to try again.
        allow = dict(GOOD)
        allow = json.loads(json.dumps(GOOD))
        allow["projects"]["demo"]["actions"] = ["collect", "reconcile"]
        self.allow.write_text(json.dumps(allow), encoding="utf-8")
        self.bind("session_w1")
        self.source.add(9001, "[WK:owner/demo#1] done")
        engine = self.engine()
        engine.run_once()
        self.assertEqual(self.bus.injected, [])
        self.assertEqual(engine.store.action_attempted(
            action_id("asg_1_worker_1", "bus_inject", "9001")), 0)
        self.allow.write_text(json.dumps(GOOD), encoding="utf-8")
        self.assertEqual(engine.projects.poll()["used"], "new",
                         "a valid edit must be picked up before the next round")
        engine.run_once()
        self.assertEqual(len(self.bus.injected), 1)

    def test_bus_failure_is_uncertain_and_not_resent(self):
        self.bind("session_w1")
        self.source.add(9001, "[WK:owner/demo#1] done")
        engine = self.engine(bus=FakeBus(fail=True))
        engine.run_once()
        state = engine.store.load_outbox_state()
        base = action_id("asg_1_worker_1", "bus_inject", "9001")
        self.assertEqual(state["attempts"][base], 1)
        self.assertIn(base, state["actions"], "an uncertain action stays visible")
        self.assertEqual(state["actions"][base]["status"], "uncertain")

        engine.run_once()
        # The uncertain notification is never re-sent; the archive pass may add
        # its own (separate) attempt, so only the notification counter matters.
        self.assertEqual(engine.store.action_attempted(
            action_id("asg_1_worker_1", "bus_inject", "9001")), 1,
            "an uncertain action is never re-sent blindly")

    def test_disabled_dispatch_writes_no_attempt(self):
        self.bind("session_w1")
        self.source.add(9001, "[WK:owner/demo#1] done")
        engine = self.engine(mode="observe")
        engine.run_once()
        self.assertEqual(engine.store.action_attempted(
            action_id("asg_1_worker_1", "bus_inject", "9001")), 0)


class DoctorAndHealth(Base):
    def test_active_assignments_counts_only_live_ones(self):
        self.bind("session_w1")
        engine = self.engine()
        engine.run_once()
        self.assertEqual(engine.health_record()["active_assignments"], 1)
        index = self.index(engine)
        index["assignments"]["asg_1_worker_1"]["status"] = "completed"
        engine.store.save_index(index)
        self.assertEqual(engine.health_record()["active_assignments"], 0)

    def test_doctor_warns_on_non_terminal_only(self):
        self.bind("session_w1")
        engine = self.engine()
        engine.run_once()
        check = [c for c in engine.doctor()["checks"]
                 if c["name"] == "assignments"][0]
        self.assertFalse(check["ok"])
        self.assertEqual(check["level"], "warn")
        self.assertIn("asg_1_worker_1", check["detail"])

        index = self.index(engine)
        index["assignments"]["asg_1_worker_1"]["status"] = "completed"
        engine.store.save_index(index)
        check = [c for c in engine.doctor()["checks"]
                 if c["name"] == "assignments"][0]
        self.assertTrue(check["ok"])


class Matching(unittest.TestCase):
    def test_marker_wins_over_sole(self):
        index = {"assignments": {
            "asg_1_worker_1": {"id": "asg_1_worker_1", "thread": THREAD,
                               "role": "worker", "status": "working",
                               "session_key": "a"},
            "asg_1_worker_2": {"id": "asg_1_worker_2", "thread": THREAD,
                               "role": "worker", "status": "working",
                               "session_key": "b"}}}
        rec, how = asg.match_delivery(index, THREAD, "b")
        self.assertEqual((rec["id"], how), ("asg_1_worker_2", "marker"))

    def test_two_workers_without_a_marker_is_unmatched(self):
        index = {"assignments": {
            "asg_1_worker_1": {"id": "asg_1_worker_1", "thread": THREAD,
                               "role": "worker", "status": "working",
                               "session_key": "a"},
            "asg_1_worker_2": {"id": "asg_1_worker_2", "thread": THREAD,
                               "role": "worker", "status": "working",
                               "session_key": "b"}}}
        rec, how = asg.match_delivery(index, THREAD, "zzz")
        self.assertIsNone(rec)

    def test_terminal_assignment_is_not_a_candidate(self):
        index = {"assignments": {
            "asg_1_worker_1": {"id": "asg_1_worker_1", "thread": THREAD,
                               "role": "worker", "status": "completed",
                               "session_key": "a"}}}
        rec, _ = asg.match_delivery(index, THREAD, "a")
        self.assertIsNone(rec)


if __name__ == "__main__":
    unittest.main()
