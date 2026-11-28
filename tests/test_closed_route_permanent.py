"""ISS-84: a closed issue ends its route; an archived sid only ends its log line.

Two different kinds of "nothing will happen", kept apart on purpose:

* **issue closed** is a business end state, so the route is marked
  ``permanent`` once and the retry pass stops probing it. Reopening needs a
  person to clear the mark - the loop never asks again.
* **target session archived** is a reversible operational act (RULES §33:
  restore, then deliver), so the route is *not* marked. The refusal repeats
  every round; only the ``dispatch.archived_sid`` event is written once.

Run: ``python -m pytest tests/test_closed_route_permanent.py -q``
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from super_agent import config, github_source
from super_agent.allowlist import ProjectAllowlist
from super_agent.engine import Engine
from super_agent.store import Store

THREAD = "owner/demo#7"
KEY = "route:owner/demo#7:3"
GOOD = {
    "schema_version": 1,
    "defaults": {"mode": "observe", "creation_mode": "pm_created"},
    "projects": {
        "demo": {"repo": "owner/demo", "cwd": ".", "mode": "assist",
                 "creation_mode": "pm_created", "issues": [7],
                 "actions": ["bus_inject", "collect"],
                 "roles": ["worker", "qa", "pm"]},
    },
}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.allow = self.root / "allowlist.json"
        self.allow.write_text(json.dumps(GOOD), encoding="utf-8")
        (self.root / "sessions.json").write_text(json.dumps({
            "pm-demo-3f7c1a90": {"id": "session_pm"}}), encoding="utf-8")
        (self.root / "events.jsonl").write_text(json.dumps({
            "seq": 1, "at": "2026-11-10T10:00:00+00:00",
            "type": "session.created", "sid": "session_pm",
            "key": "pm-demo-3f7c1a90"}) + "\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def engine(self) -> Engine:
        cfg = config.load(None, {"mode": "assist",
                                 "github": {"source": "none", "repo": "owner/demo"},
                                 "engine": {"archived_crosscheck": False,
                                            "session_state_dir": str(self.root)}})
        return Engine(cfg, Store(self.state), github_source.NullSource(), None,
                      project_allowlist=ProjectAllowlist(self.allow))

    def index(self, *, ok=False) -> dict:
        return {"routes": {KEY: {"thread": THREAD, "comment_id": 3,
                                 "verdict": "pass", "action_id": "seed",
                                 "ok": ok}},
                "assignments": {"asg_7_worker_1": {
                    "thread": THREAD, "issue": 7, "role": "worker",
                    "status": "working"}},
                "issues": {}}

    def events(self, engine: Engine, etype: str) -> list:
        path = engine.store.events_path
        if not path.is_file():
            return []
        rows = [json.loads(x) for x in path.read_text("utf-8").splitlines()
                if x.strip()]
        return [r for r in rows if r.get("type") == etype]


class ClosedIssueStops(Base):
    """正例: closed marks once, then the route is never a candidate again."""

    def test_closed_route_is_marked_and_logged_once_over_eight_rounds(self):
        engine = self.engine()
        engine._issue_state = lambda issue: "closed"
        index = self.index()
        for _ in range(3):
            self.assertEqual(engine._retry_routes(index), 0)
        route = index["routes"][KEY]
        self.assertTrue(route["permanent"])
        self.assertEqual(route["permanent_reason"], "issue closed")
        self.assertIn("permanent_at", route)
        self.assertIn("skipped_closed_at", route, "the observation is kept")
        self.assertEqual(len(self.events(engine, "route.skipped_closed_issue")), 1)
        for _ in range(5):
            engine._retry_routes(index)
        self.assertEqual(len(self.events(engine, "route.skipped_closed_issue")), 1)

    def test_the_engine_never_asks_github_again_once_marked(self):
        engine = self.engine()
        asked = {"n": 0}

        def state(issue):
            asked["n"] += 1
            return "closed"

        engine._issue_state = state
        index = self.index()
        engine._retry_routes(index)
        self.assertEqual(asked["n"], 1)
        for _ in range(4):
            engine._retry_routes(index)
        self.assertEqual(asked["n"], 1, "``permanent`` is what stops the probe")


class UnknownIsNotClosed(Base):
    """反例 1: a broken probe keeps retrying and never marks."""

    def test_unknown_goes_on_to_the_review_and_marks_nothing(self):
        engine = self.engine()
        engine._issue_state = lambda issue: "unknown"
        calls = {"n": 0}

        def review(*args, **kwargs):
            calls["n"] += 1
            return {"dispatched": False, "reason": "not allowlisted"}

        engine._route_review = review
        index = self.index()
        for _ in range(3):
            engine._retry_routes(index)
        self.assertEqual(calls["n"], 3, "unknown is retried every round")
        route = index["routes"][KEY]
        self.assertNotIn("permanent", route)
        self.assertNotIn("skipped_closed_at", route)
        self.assertEqual(self.events(engine, "route.skipped_closed_issue"), [])


class OpenIssueStillDelivers(Base):
    """反例 2: an open, deliverable issue is sent and marked ``ok``."""

    def test_open_route_delivers_and_is_not_permanent(self):
        engine = self.engine()
        engine._issue_state = lambda issue: "open"
        injected = []

        class Bus:
            def healthz(self):
                return {"ok": True, "ready": True, "connected": True, "seq": 1}

            def inject(self, event_type, payload):
                injected.append((event_type, payload))
                return {"ok": True, "seq": len(injected)}

        engine.bus = Bus()
        engine.dispatch_enabled = True
        index = self.index()
        index["routes"][KEY]["verdict"] = "pass"
        result = engine._route_review(
            index, THREAD, 7, {"id": 3, "body": "", "author": ""},
            {"verdict": "pass"}, log_unverified=False)
        self.assertTrue(result.get("dispatched"), result)
        self.assertEqual([e for e, _ in injected], ["event.agent.project_pm"])
        route = index["routes"][KEY]
        self.assertTrue(route["ok"])
        self.assertNotIn("permanent", route)


class ArchiveIsNotPermanent(Base):
    """反例 3: archived sid - one event, same refusal, no permanent mark."""

    def test_three_rounds_write_one_event_and_keep_refusing(self):
        engine = self.engine()
        engine._sid_archived = lambda sid: True
        action = {"kind": "bus_inject", "event": "event.agent.project_pm",
                  "issue": 7, "input_ref": "3", "assignment_id": KEY,
                  "payload": {"sid": "session_dead"}}
        for _ in range(3):
            out = engine._dispatch(dict(action))
            self.assertFalse(out["dispatched"])
            self.assertTrue(out["refused"])
            self.assertEqual(out["reason"], "target session is archived")
        self.assertEqual(len(self.events(engine, "dispatch.archived_sid")), 1)
        self.assertEqual(engine.store.load_counters()["external_actions"], 0)

    def test_a_different_action_on_the_same_sid_is_its_own_row(self):
        engine = self.engine()
        engine._sid_archived = lambda sid: True
        common = {"kind": "bus_inject", "issue": 7, "input_ref": "3",
                  "payload": {"sid": "session_dead"}}
        engine._dispatch({**common, "assignment_id": "asg_7_worker_1"})
        engine._dispatch({**common, "assignment_id": "route:owner/demo#7:3"})
        engine._dispatch({**common, "assignment_id": "asg_7_worker_1"})
        self.assertEqual(len(self.events(engine, "dispatch.archived_sid")), 2)

    def test_a_route_refused_for_archive_keeps_no_permanent_key(self):
        """The PM route path refuses the same way and also stays unmarked."""
        engine = self.engine()
        engine._issue_state = lambda issue: "open"
        engine._sid_archived = lambda sid: True
        engine.dispatch_enabled = True
        # ``_route_action`` puts the PM sid under ``pm_sid``, so the archive
        # guard sees it through the payload the route really builds.
        original = engine._route_action

        def with_sid(*args, **kwargs):
            action = original(*args, **kwargs)
            action["payload"]["sid"] = "session_dead"
            return action

        engine._route_action = with_sid
        index = self.index()
        for _ in range(3):
            engine._retry_routes(index)
        route = index["routes"][KEY]
        self.assertNotIn("permanent", route)
        self.assertTrue(route["refused"])
        self.assertEqual(len(self.events(engine, "dispatch.archived_sid")), 1)


class ClearingTheMarkRevives(Base):
    """反例 4: the stop is the mark, not anything hard-coded."""

    def test_deleting_permanent_makes_the_route_a_candidate_again(self):
        engine = self.engine()
        engine._issue_state = lambda issue: "closed"
        index = self.index()
        engine._retry_routes(index)
        route = index["routes"][KEY]
        self.assertTrue(route["permanent"])
        for key in ("permanent", "permanent_reason", "permanent_at"):
            route.pop(key, None)
        calls = {"n": 0}

        def review(*args, **kwargs):
            calls["n"] += 1
            return {"dispatched": False, "reason": "not allowlisted"}

        engine._route_review = review
        engine._issue_state = lambda issue: "open"
        engine._retry_routes(index)
        self.assertEqual(calls["n"], 1, "a cleared mark is evaluated at once")
        self.assertNotIn("permanent", index["routes"][KEY])
