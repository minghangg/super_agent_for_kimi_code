"""Assignment lifecycle and the one-shot ``waiting_review`` notification.

Every test injects its own session stream through
``engine.session_events_path``: ``--state-dir`` does **not** isolate the
lifecycle stream (``session_events.resolve_events_path`` looks at
``SESS_EVENTS_STATE_DIR``/``LWA_STATE_DIR``/cwd), so relying on the directory
would silently read the live workbench state.

The second half covers the QA verdict side: the transitions that used to be
missing entirely, the gate in front of ``completed``, the hold states, and the
blocked exit - including the two shapes QA flagged (the same delivery failing
twice, and the ``no sid`` branch that never consumed a delivery at all).

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
        engine = Engine(cfg, Store(self.state), self.source, bus or self.bus,
                        project_allowlist=ProjectAllowlist(self.allow),
                        session_api=self.sessions)
        # Close reconciliation is exercised in tests/test_close_reconcile.py.
        # Here the round must end where the lifecycle does, at waiting_release:
        # these fakes have no provable issue state, so the real reconciler would
        # (correctly) demote the thread to close_reconcile.
        engine._round_closed_threads = lambda index: []
        return engine


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
        # The real chain: an archive made through the session API writes **no**
        # ``session.archived`` row, so the stream still says ``archived=False``
        # here. Only the server knows. The old version of this test wrote the
        # archive event by hand, which is why it stayed green while the engine
        # kept forking a second assignment on the same sid.
        self.bind("s")
        self.sessions.archived.add("s")
        self.assertNotIn(session_events.ARCHIVED,
                         [json.loads(line)["type"] for line in
                          self.events.read_text("utf-8").splitlines()])
        engine = self.engine()
        engine.run_once()
        self.assertEqual(self.index(engine)["assignments"], {})
        kinds = [json.loads(x)["type"] for x in
                 (self.state / "events.jsonl").read_text("utf-8").splitlines()]
        self.assertIn("assignment.archived_offstream", kinds,
                      "the cross-check must leave a trace")

    def test_stream_archived_session_still_creates_nothing(self):
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

    def test_unarchived_worker_is_still_a_live_binding(self):
        # Counter-example to the fix: asking the server must not filter out a
        # worker that is genuinely live.
        self.bind("session_w1")
        engine = self.engine()
        engine.run_once()
        self.assertEqual(sorted(self.index(engine)["assignments"]),
                         ["asg_1_worker_1"])

    def test_completed_then_archived_does_not_fork_a_second_assignment(self):
        # QA's exact chain: round 1 binds, delivers, passes and archives _1;
        # round 2 still sees the same sid in the stream (the API-side archive
        # wrote no event). Before the cross-check that forked ``_2`` on the very
        # same sid and the thread never reached a terminal state. Archiving now
        # stops at ``waiting_release``: the Issue is not proved closed yet, so
        # the terminal write is simulated here.
        self.bind("session_w1")
        self.source.add(9001, "[WK:wk-probe-1] done")
        self.source.add(9100, "[QA] **通过**", author="qa")
        engine = self.engine()
        engine.run_once()
        first = self.index(engine)["assignments"]
        self.assertEqual(sorted(first), ["asg_1_worker_1"])
        self.assertEqual(first["asg_1_worker_1"]["status"], "waiting_release")
        self.assertIn("session_w1", self.sessions.archived)

        index = self.index(engine)
        asg.complete(index["assignments"]["asg_1_worker_1"])
        engine.store.save_index(index)

        engine.run_once()
        engine.run_once()
        assignments = self.index(engine)["assignments"]
        self.assertEqual(sorted(assignments), ["asg_1_worker_1"])
        self.assertEqual([rec["sid"] for rec in assignments.values()],
                         ["session_w1"])
        self.assertEqual(engine.health_record()["active_assignments"], 0)
        check = [c for c in engine.doctor()["checks"]
                 if c["name"] == "assignments"][0]
        self.assertTrue(check["ok"], check["detail"])

    def test_session_info_unreachable_fails_closed_and_is_logged(self):
        self.bind("session_w1")

        class Unreachable(FakeSessions):
            def session_info(self, sid):
                raise RuntimeError("session service down")

        engine = self.engine()
        engine.session_api = Unreachable()
        engine.run_once()
        self.assertEqual(self.index(engine)["assignments"], {},
                         "unreachable server means closed, so no dispatch target")
        rows = [json.loads(x) for x in
                (self.state / "events.jsonl").read_text("utf-8").splitlines()]
        unreachable = [r for r in rows
                       if r["type"] == "assignment.archived_unreachable"]
        self.assertEqual(len(unreachable), 1)
        self.assertIn("session_w1", unreachable[0]["sid"])

    def test_missing_stream_creates_nothing_and_degrades(self):
        """A declared stream that is absent fails closed (ISS-79).

        The old assertion here was the bug: ``assertFalse(engine.degraded, "a
        missing stream is not an error")``. A missing stream *is* the fault -
        it reads as "no events today", so the engine ran a healthy-looking
        round with no session truth at all. The fault must show in
        ``degraded``, in ``health.json`` and in the event log.
        """
        self.events.unlink(missing_ok=True)
        engine = self.engine()
        engine.run_once()
        self.assertEqual(self.index(engine)["assignments"], {})
        self.assertTrue(engine.degraded, "a missing declared stream is a fault")
        health = json.loads((self.state / "health.json").read_text("utf-8"))
        self.assertTrue(health["degraded"])
        self.assertIn("session stream missing", health["last_error"])
        self.assertEqual(health["active_assignments"], 0)
        kinds = [json.loads(x)["type"] for x in
                 (self.state / "events.jsonl").read_text("utf-8").splitlines()]
        self.assertIn("session.stream_missing", kinds)

    def test_rotated_stream_is_not_missing(self):
        """Only ``.1`` on disk is still data: rotation must not degrade a round.

        The judgement is ``segments()``, never ``path.is_file()``: the writer
        rotates the current file to ``.1`` and briefly leaves no ``events.jsonl``,
        and calling that missing would raise a fault once per rotation.
        """
        self.bind("session_w1")
        rotated = self.events.with_name(self.events.name + ".1")
        self.events.replace(rotated)
        self.assertFalse(self.events.is_file())
        engine = self.engine()
        engine.run_once()
        self.assertFalse(engine.degraded)
        self.assertEqual(sorted(self.index(engine)["assignments"]),
                         ["asg_1_worker_1"], "the rotated segment still binds")
        health = json.loads((self.state / "health.json").read_text("utf-8"))
        self.assertFalse(health["degraded"])

    def test_unreadable_stream_degrades_instead_of_aborting_the_round(self):
        """A segment that is not UTF-8 must reach health, not escape the round.

        QA's finding (ISS-79 P1-1): ``open(..., encoding="utf-8")`` raises
        ``UnicodeDecodeError``, which is a ``ValueError`` and therefore slipped
        past the ``except OSError`` guard. The exception left ``run_round``
        before its own ``write_health``, so ``health.json`` kept the previous,
        healthy snapshot while every round failed.
        """
        self.events.write_bytes(b"\xff\xfe\x00bad\n")
        engine = self.engine()
        engine.run_once()
        self.assertTrue(engine.degraded)
        health = json.loads((self.state / "health.json").read_text("utf-8"))
        self.assertTrue(health["degraded"], "health must not stay on the old snapshot")
        self.assertIn("not decodable as UTF-8", health["last_error"])
        self.assertNotIn("missing", health["last_error"],
                         "an encoding fault is not a missing file")
        kinds = [json.loads(x)["type"] for x in
                 (self.state / "events.jsonl").read_text("utf-8").splitlines()]
        self.assertIn("session.stream_unreadable", kinds)
        self.assertNotIn("session.stream_missing", kinds)

    def test_unreadable_survives_the_stop_write(self):
        """The last health write of a process must not erase the fault.

        ``cli once`` writes health twice: once at the end of the round and once
        more after it, with ``running=False``. The second call passes no
        ``error``, so unless ``health_record`` folds in ``_stream_error`` it
        replaces a degraded snapshot with a healthy one.
        """
        self.events.write_bytes(b"\xff\xfe\x00bad\n")
        engine = self.engine()
        engine.run_once()
        engine.write_health(running=False)
        health = json.loads((self.state / "health.json").read_text("utf-8"))
        self.assertTrue(health["degraded"])
        self.assertIn("not decodable as UTF-8", health["last_error"])

    def test_missing_and_unreadable_are_told_apart(self):
        """The two faults need different repairs, so they need different words."""
        engine = self.engine()
        self.events.write_bytes(b"\xff\xfe\x00bad\n")
        engine.run_once()
        unhealthy = engine.health_record()["last_error"]
        engine = self.engine()
        self.events.unlink()
        engine.run_once()
        missing = engine.health_record()["last_error"]
        self.assertIn("not decodable", unhealthy)
        self.assertIn("missing", missing)
        self.assertNotEqual(unhealthy, missing)

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
        # No review yet, so the gate holds the record where the delivery put it:
        # waiting_review, not archived and not completed.
        self.assertEqual(rec["status"], "waiting_review")
        self.assertEqual(rec["delivery_comment_id"], 9001)
        self.assertEqual(rec["matched_by"], "marker")
        self.assertEqual(self.sessions.archived, set())

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
        # Two workers on the same issue, neither named by the marker: the
        # sole-worker fallback cannot apply, so the delivery must escalate.
        rows = [
            {"seq": 1, "at": "2026-11-09T10:00:00+00:00",
             "type": session_events.CREATED, "sid": "session_w1", "key": "wk-probe-1"},
            {"seq": 2, "at": "2026-11-09T10:00:01+00:00",
             "type": session_events.BOUND, "sid": "session_w1", "key": THREAD,
             "role": "worker", "issue": 1},
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

    def test_two_workers_same_role_stay_separate_and_match_by_issue(self):
        # The two bugs f5d5550 fixed, end to end on the real chain:
        # (a) bindings must be matched by *issue*, not by the event's key (the
        #     key is the worker's name, so a key comparison never matches);
        # (b) two live workers of the same role are two assignments, not one.
        self.bind("session_w2", name="wk-probe-2")
        rows = [
            {"seq": 1, "at": "2026-11-09T10:00:00+00:00",
             "type": session_events.CREATED, "sid": "session_w1", "key": "wk-probe-1"},
            {"seq": 2, "at": "2026-11-09T10:00:01+00:00",
             "type": session_events.BOUND, "sid": "session_w1", "key": THREAD,
             "role": "worker", "issue": 1},
            {"seq": 3, "at": "2026-11-09T10:00:02+00:00",
             "type": session_events.CREATED, "sid": "session_w2", "key": "wk-probe-2"},
            {"seq": 4, "at": "2026-11-09T10:00:03+00:00",
             "type": session_events.BOUND, "sid": "session_w2", "key": THREAD,
             "role": "worker", "issue": 1},
            # A worker bound to a *different* issue must not join this thread.
            {"seq": 5, "at": "2026-11-09T10:00:04+00:00",
             "type": session_events.CREATED, "sid": "session_other", "key": "wk-other"},
            {"seq": 6, "at": "2026-11-09T10:00:05+00:00",
             "type": session_events.BOUND, "sid": "session_other", "key": "owner/demo#2",
             "role": "worker", "issue": 2},
        ]
        self.events.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                               encoding="utf-8")
        engine = self.engine()
        engine.run_once()
        assignments = self.index(engine)["assignments"]
        self.assertEqual(sorted(assignments),
                         ["asg_1_worker_1", "asg_1_worker_2"])
        self.assertEqual(sorted(rec["sid"] for rec in assignments.values()),
                         ["session_w1", "session_w2"])
        self.assertEqual(sorted(rec["session_key"] for rec in assignments.values()),
                         ["wk-probe-1", "wk-probe-2"])

    def test_delivery_by_marker_reaches_the_right_worker_of_two(self):
        rows = [
            {"seq": 1, "at": "2026-11-09T10:00:00+00:00",
             "type": session_events.CREATED, "sid": "session_w1", "key": "wk-probe-1"},
            {"seq": 2, "at": "2026-11-09T10:00:01+00:00",
             "type": session_events.BOUND, "sid": "session_w1", "key": THREAD,
             "role": "worker", "issue": 1},
            {"seq": 3, "at": "2026-11-09T10:00:02+00:00",
             "type": session_events.CREATED, "sid": "session_w2", "key": "wk-probe-2"},
            {"seq": 4, "at": "2026-11-09T10:00:03+00:00",
             "type": session_events.BOUND, "sid": "session_w2", "key": THREAD,
             "role": "worker", "issue": 1},
        ]
        self.events.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                               encoding="utf-8")
        self.source.add(9011, "[WK:wk-probe-2] done")
        engine = self.engine()
        engine.run_once()
        assignments = self.index(engine)["assignments"]
        by_key = {rec["session_key"]: rec for rec in assignments.values()}
        self.assertEqual(by_key["wk-probe-2"]["status"], "waiting_review")
        self.assertEqual(by_key["wk-probe-2"]["matched_by"], "marker")
        self.assertEqual(by_key["wk-probe-1"]["status"], "working",
                         "the other worker's assignment must stay untouched")

    @staticmethod
    def memory_record(**over):
        rec = {"id": "asg_1_worker_1", "thread": THREAD, "role": "worker",
               "sid": "session_w1", "status": "working",
               "delivery_comment_id": None, "delivery_cids": [],
               "delivery_marker": "", "tombstone": {}}
        rec.update(over)
        return rec

    def test_three_deliveries_are_appended_in_order(self):
        # Every delivery an assignment received is kept, oldest first: a PM
        # verdict may name any of them, not only the newest one.
        rec = self.memory_record()
        for cid in (9001, 9002, 9003):
            rec["status"] = "working"
            self.assertTrue(asg.on_delivery(rec, cid, "wk-probe-1",
                                            "marker")["changed"])
        self.assertEqual(asg.assignment_cids(rec), [9001, 9002, 9003],
                         "arrival order is the fact, never sorted")
        self.assertEqual(rec["delivery_comment_id"], 9003,
                         "the single value stays the newest delivery")

    def test_the_same_cid_is_not_appended_twice(self):
        # A replay of an old delivery is not news: no second copy, no second
        # waiting_review notice, and the newest marker does not move backwards.
        rec = self.memory_record()
        for cid in (9001, 9002, 9003):
            rec["status"] = "working"
            asg.on_delivery(rec, cid, "wk-probe-1", "marker")
        rec["status"] = "waiting_review"
        moved = asg.on_delivery(rec, 9001, "wk-probe-1", "marker")
        self.assertFalse(moved["changed"])
        self.assertEqual(asg.assignment_cids(rec), [9001, 9002, 9003])
        self.assertEqual(rec["delivery_comment_id"], 9003)

    def test_a_single_delivery_keeps_the_list_at_one(self):
        rec = self.memory_record()
        asg.on_delivery(rec, 9001, "wk-probe-1", "marker")
        self.assertEqual(asg.assignment_cids(rec), [9001])
        self.assertEqual(rec["delivery_cids"], [9001],
                         "the history starts at the first delivery")


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

    def test_uncertain_notification_is_never_recorded_as_notified(self):
        # QA's real-probe shape: the bus raised, so the event never left the
        # process, yet the archive pass used the *attempt* counter as proof of
        # delivery and wrote ``notified_at``. A pass still archives the worker
        # (it did deliver), but it must not claim the supervisor was told.
        self.bind("session_w1")
        self.source.add(9001, "[WK:owner/demo#1] done")
        self.source.add(9100, "[QA] **通过**", author="qa")
        engine = self.engine(bus=FakeBus(fail=True))
        engine.run_once()
        rec = self.index(engine)["assignments"]["asg_1_worker_1"]
        self.assertEqual(rec["status"], "waiting_release")
        self.assertEqual(rec["notified_at"], "",
                         "a failed send is not a notification")
        self.assertFalse(rec["tombstone"]["notified"])
        kinds = [json.loads(x)["type"] for x in
                 (self.state / "events.jsonl").read_text("utf-8").splitlines()]
        self.assertEqual(kinds.count("assignment.notify_unconfirmed"), 1)

    def test_successful_notification_is_recorded_as_notified(self):
        self.bind("session_w1")
        self.source.add(9001, "[WK:owner/demo#1] done")
        self.source.add(9100, "[QA] **通过**", author="qa")
        engine = self.engine()
        engine.run_once()
        rec = self.index(engine)["assignments"]["asg_1_worker_1"]
        self.assertTrue(rec["notified_at"])
        self.assertTrue(rec["tombstone"]["notified"])

    def test_disabled_dispatch_writes_no_attempt(self):
        self.bind("session_w1")
        self.source.add(9001, "[WK:owner/demo#1] done")
        engine = self.engine(mode="observe")
        engine.run_once()
        self.assertEqual(engine.store.action_attempted(
            action_id("asg_1_worker_1", "bus_inject", "9001")), 0)


class DoctorAndHealth(Base):
    def test_disabled_crosscheck_leaves_one_visible_trace(self):
        self.bind("session_w1")
        engine = self.engine()
        engine.archived_crosscheck = False
        engine.run_once()
        rows = [json.loads(x) for x in
                (self.state / "events.jsonl").read_text("utf-8").splitlines()]
        off = [r for r in rows if r["type"] == "engine.crosscheck_disabled"]
        self.assertEqual(len(off), 1, rows)
        self.assertEqual(off[0]["sid"], "session_w1")

        # Once per engine, not once per sid or per round: a second round keeps
        # the single trace instead of flooding the stream.
        engine.run_once()
        rows = [json.loads(x) for x in
                (self.state / "events.jsonl").read_text("utf-8").splitlines()]
        self.assertEqual(len([r for r in rows
                              if r["type"] == "engine.crosscheck_disabled"]), 1)

        # The default stays on, and an engine with it on writes no such event.
        on = self.engine()
        self.assertTrue(on.archived_crosscheck)
        on.run_once()
        rows = [json.loads(x) for x in
                (self.state / "events.jsonl").read_text("utf-8").splitlines()]
        self.assertEqual(len([r for r in rows
                              if r["type"] == "engine.crosscheck_disabled"]), 1)

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


class Verdicts(unittest.TestCase):
    """The QA verdict side of the machine, on records built in memory."""

    @staticmethod
    def record(**over):
        rec = {"id": "asg_1_worker_1", "thread": THREAD, "role": "worker",
               "sid": "session_w1", "status": "waiting_review",
               "delivery_comment_id": 9001, "last_review_comment_id": None,
               "qa_verdict": "", "tombstone": {}}
        rec.update(over)
        return rec

    def test_pass_moves_to_waiting_release(self):
        rec = self.record()
        out = asg.on_review(rec, {"verdict": "pass"}, review_cid=9100)
        self.assertTrue(out["changed"])
        self.assertEqual(rec["status"], "waiting_release")
        self.assertEqual(rec["qa_verdict"], "pass")
        self.assertEqual(rec["review_comment_id"], 9100)

    def test_reject_returns_the_same_session_to_working(self):
        rec = self.record()
        out = asg.on_review(rec, {"verdict": "reject"}, review_cid=9100)
        self.assertTrue(out["changed"])
        self.assertEqual(rec["status"], "working")
        # The thread context is kept: the worker is resumed, not replaced.
        self.assertEqual(rec["sid"], "session_w1")
        self.assertEqual(rec["delivery_comment_id"], 9001)

    def test_unreadable_verdict_asks_a_person_and_never_guesses(self):
        rec = self.record()
        out = asg.on_review(rec, {"verdict": ""}, review_cid=9100)
        self.assertEqual(out["status"], "waiting_person")
        self.assertEqual(rec["qa_verdict"], "")

    def test_unverified_source_is_a_warning_not_a_refusal(self):
        rec = self.record()
        out = asg.on_review(rec, {"verdict": "reject"}, review_cid=9100,
                            verified=False)
        self.assertTrue(out["changed"])
        self.assertEqual(rec["status"], "working")
        self.assertFalse(rec["review_verified"])

    def test_review_is_idempotent_on_the_comment_id(self):
        rec = self.record()
        asg.on_review(rec, {"verdict": "reject"}, review_cid=9100)
        self.assertFalse(asg.on_review(rec, {"verdict": "reject"},
                                       review_cid=9100)["changed"])

    def test_gate_refuses_without_a_pass(self):
        ok, why = asg.qa_gate(self.record(qa_verdict=""))
        self.assertFalse(ok)
        self.assertIn("no qa pass", why)
        ok, why = asg.qa_gate(self.record(qa_verdict="reject"))
        self.assertFalse(ok)
        self.assertIn("rejected", why)

    def test_manual_status_flip_cannot_complete(self):
        # The hole the gate closes: a hand-edited ``waiting_release`` with no
        # verdict must not reach ``completed``.
        rec = self.record(status="waiting_release", qa_verdict="")
        out = asg.complete(rec)
        self.assertTrue(out["refused"])
        self.assertEqual(rec["status"], "waiting_release")
        rec["qa_verdict"] = "pass"
        self.assertEqual(asg.complete(rec)["status"], "completed")

    def test_late_delivery_in_a_hold_state_is_only_audited(self):
        for status in ("waiting_release", "close_reconcile"):
            rec = self.record(status=status, qa_verdict="pass")
            out = asg.on_delivery(rec, 9999, "wk-probe-1", "marker")
            self.assertFalse(out["changed"])
            self.assertEqual(out["reason"], "late delivery after a QA pass")
            self.assertEqual(rec["status"], status)

    def test_completed_assignment_audits_a_late_event(self):
        rec = self.record(status="completed", qa_verdict="pass")
        self.assertTrue(asg.late_event(rec, "delivery", cid=9999)["audit"])
        self.assertEqual(rec["status"], "completed")

    def test_close_reconcile_refuses_without_a_pass(self):
        rec = self.record(status="waiting_release", qa_verdict="")
        out = asg.enter_close_reconcile(rec, "binding still registered")
        self.assertTrue(out["refused"])
        self.assertEqual(rec["status"], "waiting_release")

    def test_reopen_needs_all_three(self):
        rec = self.record(status="close_reconcile", qa_verdict="pass",
                          review_comment_id=9100, delivery_comment_id=9200)
        # A newer reject alone is not enough.
        self.assertFalse(asg.reopen(rec, review_cid=9300, delivery_cid=9400,
                                    archived=None)["changed"])
        # Reject and delivery, but the session is archived.
        self.assertFalse(asg.reopen(rec, review_cid=9300, delivery_cid=9400,
                                    archived=True)["changed"])
        out = asg.reopen(rec, review_cid=9300, delivery_cid=9400, archived=False)
        self.assertTrue(out["changed"])
        self.assertEqual(rec["status"], "working")
        self.assertEqual(rec["qa_verdict"], "reject")

    def test_reopen_appends_the_new_delivery(self):
        # ``reopen`` writes the same field ``on_delivery`` writes, so it must
        # append too: one path that overwrites would silently drop the history.
        rec = self.record(status="close_reconcile", qa_verdict="pass",
                          review_comment_id=9100, delivery_comment_id=9200,
                          delivery_cids=[9001, 9200])
        out = asg.reopen(rec, review_cid=9300, delivery_cid=9400, archived=False)
        self.assertTrue(out["changed"])
        self.assertEqual(asg.assignment_cids(rec), [9001, 9200, 9400])
        self.assertEqual(rec["delivery_comment_id"], 9400)

    def test_legacy_record_reads_as_one_cid(self):
        # The pre-change shape on disk: no ``delivery_cids`` at all. The
        # fallback is what makes the existing records readable with no
        # migration script.
        legacy = {"id": "asg_1_worker_1", "thread": THREAD, "role": "worker",
                  "status": "completed", "delivery_comment_id": 9001}
        self.assertEqual(asg.assignment_cids(legacy), [9001])
        self.assertEqual(
            asg.assignment_cids({"id": "asg_1_worker_1", "role": "worker"}),
            [], "no field and no single value is simply no delivery")
        # The first natural write merges the old newest value in as item one.
        merged = dict(legacy)
        asg._append_delivery(merged, 9200)
        self.assertEqual(merged["delivery_cids"], [9001, 9200])
        self.assertEqual(merged["delivery_comment_id"], 9200)

    def test_owners_of_delivery_is_assignment_scoped(self):
        # A cid is unique per repository, not per thread: the scan must filter
        # by thread, by role and by "this assignment received it".
        index = self.owner_index()
        owners = asg.owners_of_delivery(index, THREAD, 9001)
        self.assertEqual(sorted(r["id"] for r in owners),
                         ["asg_1_worker_1", "asg_1_worker_2"])
        self.assertEqual(asg.owners_of_delivery(index, THREAD, 9999), [])
        self.assertEqual(
            [r["id"] for r in asg.owners_of_delivery(index, THREAD, "9002")],
            ["asg_1_worker_1"], "a string cid is the same cid")
        self.assertEqual(
            [r["id"] for r in asg.owners_of_delivery(index, "owner/demo#2", 9001)],
            ["asg_2_worker_1"], "another thread keeps its own holders")

    def test_newest_delivery_owner_skips_closed_assignments(self):
        index = self.owner_index()
        states = ("working", "waiting_review", "close_reconcile")
        self.assertEqual(
            asg.newest_delivery_owner(index, THREAD, states)["id"],
            "asg_1_worker_1", "the closed holder of 9001 is not the newest")
        self.assertIsNone(
            asg.newest_delivery_owner(index, THREAD, ("working",)),
            "no live holder answers None, not 'the closed one will do'")

    @staticmethod
    def owner_index():
        return {"assignments": {
            "asg_1_worker_1": {
                "id": "asg_1_worker_1", "thread": THREAD, "role": "worker",
                "status": "waiting_review", "delivery_comment_id": 9002,
                "delivery_cids": [9001, 9002]},
            "asg_1_worker_2": {
                "id": "asg_1_worker_2", "thread": THREAD, "role": "worker",
                "status": "completed", "delivery_comment_id": 9001},
            "asg_1_qa_1": {
                "id": "asg_1_qa_1", "thread": THREAD, "role": "qa",
                "status": "waiting_review", "delivery_comment_id": 9001},
            "asg_2_worker_1": {
                "id": "asg_2_worker_1", "thread": "owner/demo#2",
                "role": "worker", "status": "waiting_review",
                "delivery_comment_id": 9001}}}


class BlockedExit(unittest.TestCase):
    def test_a_blocked_assignment_waits_for_a_newer_delivery(self):
        rec = Verdicts.record(status="blocked")
        asg.mark_blocked(rec, "archive not confirmed by read-back", cid=9001)
        self.assertFalse(asg.on_delivery(rec, 9001, "wk-probe-1",
                                         "marker")["changed"])
        self.assertTrue(asg.on_delivery(rec, 9002, "wk-probe-1",
                                        "marker")["changed"])

    def test_the_same_delivery_failing_twice_bumps_the_attempt(self):
        # blocked -> unblock -> the same delivery fails again: the cid stays,
        # the attempt grows. Overwriting the tombstone would make the "newer
        # than" guard impossible to ever satisfy again.
        rec = Verdicts.record(status="waiting_review")
        asg.mark_blocked(rec, "archive not confirmed by read-back", cid=9001)
        self.assertEqual(rec["tombstone"]["attempt"], 1)
        self.assertTrue(asg.unblock(rec, "retry after a fix")["changed"])
        self.assertEqual(rec["status"], "working")
        asg.mark_blocked(rec, "archive not confirmed by read-back", cid=9001)
        self.assertEqual(rec["status"], "blocked")
        self.assertEqual(rec["tombstone"]["attempt"], 2)
        self.assertEqual(rec["tombstone"]["cid"], 9001)
        self.assertTrue(rec["unblocked_at"])

    def test_no_sid_block_does_not_consume_the_delivery(self):
        # A block caused by a missing sid happened before the archive, so
        # finishing the sid must let the very same delivery out.
        rec = Verdicts.record(status="working", sid="", delivery_comment_id=None)
        asg.mark_blocked(rec, asg.NO_SID_REASON, cid=None)
        rec["sid"] = "session_w1"
        out = asg.on_delivery(rec, 9001, "wk-probe-1", "marker")
        self.assertTrue(out["changed"])
        self.assertEqual(rec["status"], "waiting_review")

    def test_unblock_refuses_a_live_assignment(self):
        rec = Verdicts.record(status="working")
        self.assertTrue(asg.unblock(rec, "why")["refused"])
        self.assertEqual(rec["status"], "working")

    def test_flow_hold_refuses_delivery_until_supervisor_unblocks(self):
        rec = Verdicts.record(status="working")
        index = {"assignments": {rec["id"]: rec}}
        self.assertEqual(asg.pause_for_flow(index, THREAD, "flow.changed:at->standard"),
                         [rec["id"]])
        out = asg.on_delivery(rec, 9001, "wk-probe-1", "marker")
        self.assertTrue(out["refused"])
        self.assertEqual(rec["status"], "blocked")
        self.assertTrue(asg.unblock(rec, "supervisor approved")["changed"])
        self.assertNotIn("flow_hold_reason", rec)
        self.assertEqual(rec["status"], "working")


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

    # -- #ISS-91: the sole rescue is worker-only, pinned as a pair ------------
    #
    # A long-lived role is not a participant role (``PARTICIPANT_ROLES`` is
    # worker/qa), so it never owns an assignment row. Once the delivery prefix
    # set widened, ``[SC:name]`` reached this function and the sole fallback
    # wrote it onto the single worker - a false green. The two cases below are
    # only meaningful together: one proves the rescue still fires for WK, the
    # other proves it does not fire for a widened prefix.
    def one_live_worker(self):
        return {"assignments": {
            "asg_1_worker_1": {"id": "asg_1_worker_1", "thread": THREAD,
                               "role": "worker", "status": "working",
                               "session_key": "wk-other"}}}

    def test_sole_rescue_still_fires_for_wk(self):
        rec, how = asg.match_delivery(self.one_live_worker(), THREAD, "wk-1",
                                      "WK")
        self.assertEqual((rec["id"], how), ("asg_1_worker_1", "sole"))

    def test_sole_rescue_never_matches_a_long_lived_prefix(self):
        for marker in ("SC", "ST", "AU", "RV", "AT", "PL"):
            with self.subTest(marker=marker):
                rec, how = asg.match_delivery(self.one_live_worker(), THREAD,
                                              "scout-1", marker)
                self.assertIsNone(rec, marker)
                self.assertIn("no live worker", how)

    def test_a_widened_prefix_still_matches_its_own_session_key(self):
        # The exact path is not marker-gated: a binding that does exist is used.
        index = {"assignments": {
            "asg_1_scout_1": {"id": "asg_1_scout_1", "thread": THREAD,
                              "role": "scout", "status": "working",
                              "session_key": "scout-1"},
            "asg_1_worker_1": {"id": "asg_1_worker_1", "thread": THREAD,
                               "role": "worker", "status": "working",
                               "session_key": "wk-1"}}}
        rec, how = asg.match_delivery(index, THREAD, "scout-1", "SC")
        self.assertEqual((rec["id"], how), ("asg_1_scout_1", "marker"))

    def test_the_marker_argument_defaults_to_wk(self):
        # Old callers keep the old behaviour: no argument means a WK delivery.
        rec, how = asg.match_delivery(self.one_live_worker(), THREAD, "wk-1")
        self.assertEqual((rec["id"], how), ("asg_1_worker_1", "sole"))


if __name__ == "__main__":
    unittest.main()
