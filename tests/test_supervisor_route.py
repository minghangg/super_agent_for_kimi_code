"""Supervisor notification: a shippable verdict reaches one more reader than the PM.

The properties pinned here, and the reason each one is a counter-example rather
than a happy path:

* a thread held by **close reconciliation** is reminded once - the scan covers
  both held states, because an open Issue demotes ``waiting_release`` in the
  same round and scanning only that state watches an empty set;
* supervisor records live in their own top-level table, so the PM retry pass
  never reads, re-sends or supersedes them;
* the decision is taken **before** the PM early return, so a second round (or a
  crash between record and dispatch) cannot turn at-most-once into at-most-zero;
* an unreachable supervisor is never silent, and its ``needs_person`` row lands
  in the ``user`` bucket (a local configuration gap, not a decision);
* this is a protocol: the payload is asserted, "the person received it" is not.

Run: ``python -m pytest tests/test_supervisor_route.py -q``
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from super_agent import brief, config, markers, session_events
from super_agent.allowlist import ProjectAllowlist
from super_agent.engine import Engine
from super_agent.store import Store, action_id

THREAD = "owner/demo#1"
# The structured marker is the only thing that says "nothing is left"; without
# it the answer is the safe side (work remains) and no release is announced.
PASS_BODY = "[QA] 通过（P0=0，可关单） <!-- fix_needed: false -->"
PASS_UNMARKED = "[QA] 通过（P0=0，可关单）"
REJECT_BODY = "[QA] 退回改（2 条 P0）"

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
    def __init__(self):
        self.injected = []

    def healthz(self):
        return {"ok": True, "ready": True, "connected": True, "seq": 1}

    def inject(self, event_type, payload):
        self.injected.append((event_type, payload))
        return {"ok": True, "seq": len(self.injected)}

    def events(self):
        return [e for e, _ in self.injected]

    def payloads(self, event_type):
        return [p for e, p in self.injected if e == event_type]


class FakeSessions:
    def __init__(self):
        self.archived = set()

    def archive_session(self, sid):
        self.archived.add(sid)
        return True, {"ids": [sid]}

    def session_info(self, sid):
        return {"archived": sid in self.archived}


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

    def issue_state(self, issue):
        return "open"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.events = self.root / "lifecycle" / "events.jsonl"
        self.events.parent.mkdir(parents=True, exist_ok=True)
        self.sessions_path = self.events.parent / "sessions.json"
        self.allow = self.root / "allowlist.json"
        self.allow.write_text(json.dumps(GOOD), encoding="utf-8")
        self.source = Comments()
        self.bus = FakeBus()
        self.sessions = FakeSessions()
        self.bind_both()
        self.write_sessions({"supervisor": {"id": "session_sup"}})

    def fresh(self):
        """Restart from a clean state directory, inside one test method."""
        self.tmp.cleanup()
        self.setUp()

    def tearDown(self):
        self.tmp.cleanup()

    def write_sessions(self, data):
        self.sessions_path.write_text(json.dumps(data), encoding="utf-8")

    def bind_both(self):
        rows = [
            {"seq": 1, "at": "2026-11-10T10:00:00+00:00",
             "type": session_events.CREATED, "sid": "session_w1",
             "key": "wk-probe-1"},
            {"seq": 2, "at": "2026-11-10T10:00:01+00:00",
             "type": session_events.BOUND, "sid": "session_w1", "key": THREAD,
             "role": "worker", "issue": 1},
            {"seq": 3, "at": "2026-11-10T10:00:02+00:00",
             "type": session_events.CREATED, "sid": "session_q1",
             "key": "qa-demo"},
            {"seq": 4, "at": "2026-11-10T10:00:03+00:00",
             "type": session_events.BOUND, "sid": "session_q1", "key": THREAD,
             "role": "qa", "issue": 1},
        ]
        self.events.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                               encoding="utf-8")

    def engine(self, *, mode="assist", engine_cfg=None) -> Engine:
        extra = {"session_events_path": str(self.events),
                 "allowlist": ["issue_comment.created"]}
        extra.update(engine_cfg or {})
        cfg = config.load(None, {
            "mode": mode,
            "github": {"source": "none", "repo": "owner/demo"},
            "engine": extra,
        })
        return Engine(cfg, Store(self.state), self.source, self.bus,
                      project_allowlist=ProjectAllowlist(self.allow),
                      session_api=self.sessions)

    def deliver_and_pass(self, engine, body=PASS_BODY):
        self.source.add(9001, "[WK:wk-probe-1] done")
        self.source.add(9100, body, author="qa")
        engine.run_once()

    def rows(self):
        path = self.state / "events.jsonl"
        if not path.is_file():
            return []
        return [json.loads(x) for x in path.read_text("utf-8").splitlines()
                if x.strip()]

    def index(self, engine):
        return engine.store.load_index()


class ShippableVerdictReachesTheSupervisor(Base):
    def test_pass_and_no_fix_goes_to_the_pm_and_the_supervisor(self):
        engine = self.engine()
        self.deliver_and_pass(engine)
        self.assertEqual(len(self.bus.payloads("event.agent.project_pm")), 1)
        payloads = self.bus.payloads("event.agent.supervisor")
        self.assertEqual(len(payloads), 1, "one line per shippable verdict")
        payload = payloads[0]
        for key in ("issue", "review_comment_id", "verdict", "fix_needed",
                    "at", "text", "sid"):
            self.assertIn(key, payload)
        self.assertEqual(payload["issue"], 1)
        self.assertEqual(payload["review_comment_id"], 9100)
        self.assertEqual(payload["verdict"], "pass")
        self.assertFalse(payload["fix_needed"])
        self.assertIn("【可关单】#1", payload["text"])
        self.assertEqual(payload["sid"], "session_sup")

    def test_a_reject_goes_to_the_pm_only(self):
        engine = self.engine()
        self.deliver_and_pass(engine, body=REJECT_BODY)
        self.assertEqual(len(self.bus.payloads("event.agent.project_pm")), 1)
        self.assertEqual(self.bus.payloads("event.agent.supervisor"), [])

    def test_a_pass_with_a_must_fix_stays_with_the_pm(self):
        # No marker, so the safe side: the line reads like a conditional pass
        # and is treated exactly like one that does not.
        engine = self.engine()
        self.deliver_and_pass(engine, body="[QA] 有条件通过（1 条 P1 必须修）")
        self.assertEqual(len(self.bus.payloads("event.agent.project_pm")), 1)
        self.assertEqual(self.bus.payloads("event.agent.supervisor"), [])

    def test_an_unmarked_pass_is_not_announced(self):
        # The failure this change removes: a plain pass line with no marker used
        # to be read as "nothing to fix" from its wording alone.
        engine = self.engine()
        self.deliver_and_pass(engine, body=PASS_UNMARKED)
        self.assertEqual(len(self.bus.payloads("event.agent.project_pm")), 1)
        self.assertEqual(self.bus.payloads("event.agent.supervisor"), [])
        self.assertFalse(self.index(engine).get("supervisor_routes") or {})


class SupervisorRecordsHaveTheirOwnTable(Base):
    def test_supervisor_records_never_enter_the_routes_table(self):
        engine = self.engine()
        self.deliver_and_pass(engine)
        index = self.index(engine)
        sup = index.get("supervisor_routes") or {}
        self.assertEqual(list(sup), [f"sup:{THREAD}:9100"])
        self.assertTrue(sup[f"sup:{THREAD}:9100"]["sup_eligible"])
        routes = index.get("routes") or {}
        # The PM route for the same cid is expected; a *supervisor* record there
        # is not, and the ``sup:`` prefix is the discriminator (the cid alone is
        # shared by design).
        self.assertEqual(list(routes), [f"route:{THREAD}:9100"])
        self.assertFalse([k for k in routes if k.startswith("sup:")])

    def test_retry_routes_never_touches_supervisor_records(self):
        engine = self.engine(mode="observe")
        self.deliver_and_pass(engine)
        index = self.index(engine)
        sup_key = f"sup:{THREAD}:9100"
        self.assertIn(sup_key, index["supervisor_routes"])
        self.assertFalse(index["supervisor_routes"][sup_key]["ok"])

        # Simulate the one shape that used to leak: the attempt counters are
        # gone and the PM record is gone, so ``_retry_routes`` has nothing of
        # its own left - it must not pick the supervisor record up.
        state = engine.store.load_outbox_state()
        state["attempts"] = {}
        state["actions"] = {}
        engine.store.save_outbox_state(state)
        index["routes"] = {}
        sent = engine._retry_routes(index)
        self.assertEqual(sent, 0)
        self.assertEqual(self.bus.payloads("event.agent.supervisor"), [])
        self.assertNotIn(sup_key, index.get("routes") or {})
        # And no PM dismissal row was resolved with a supervisor id.
        written = engine.store.load_index()
        rows = ((written.get("issues") or {}).get(THREAD) or {}).get(
            "needs_person") or []
        for entry in rows:
            row = (written.get("needs_person_resolved") or {}).get(
                f"{THREAD}|{entry.get('comment_id')}") or {}
            self.assertNotIn("sup:", str(row.get("superseded_by") or ""))


class DecidedBeforeThePmEarlyReturn(Base):
    def test_supervisor_is_decided_before_the_pm_early_return(self):
        engine = self.engine()
        self.deliver_and_pass(engine)
        index = self.index(engine)
        sup_key = f"sup:{THREAD}:9100"
        # Drop the supervisor record so the block has work to do, keep the PM
        # attempt counter above zero: the PM path returns early here.
        del index["supervisor_routes"][sup_key]
        review = markers.parse_review(PASS_BODY)
        pm_base = action_id(f"route:{THREAD}:9100", "bus_inject", "9100")
        self.assertGreater(engine.store.action_attempted(pm_base), 0)
        result = engine._route_review(index, THREAD, 1,
                                      {"id": 9100, "body": PASS_BODY,
                                       "author": "qa"}, review)
        self.assertFalse(result["dispatched"])
        record = index["supervisor_routes"].get(sup_key)
        self.assertIsNotNone(record, "the supervisor block must still run")
        self.assertTrue(record["sup_eligible"])
        self.assertEqual(len(self.bus.payloads("event.agent.supervisor")), 1,
                         "an attempted send is not repeated")

    def test_first_round_supervisor_refusal_is_retried_once(self):
        engine = self.engine(mode="observe")
        self.deliver_and_pass(engine)
        self.assertEqual(self.bus.payloads("event.agent.supervisor"), [])
        self.assertFalse(
            self.index(engine)["supervisor_routes"][f"sup:{THREAD}:9100"]["ok"])

        engine.dispatch_enabled = True
        engine.run_once()
        self.assertEqual(len(self.bus.payloads("event.agent.supervisor")), 1)
        engine.run_once()
        self.assertEqual(len(self.bus.payloads("event.agent.supervisor")), 1,
                         "the retry is at most once, not at most twice")

    def test_crash_after_record_before_dispatch_keeps_the_decision(self):
        engine = self.engine()
        index = self.index(engine)
        sup_key = f"sup:{THREAD}:9100"
        sup_base = action_id(sup_key, "bus_inject", "9100")
        # The record a crash between the write and the dispatch would leave.
        index["supervisor_routes"] = {sup_key: {
            "thread": THREAD, "comment_id": 9100, "at": "2026-11-10T10:00:00Z",
            "verdict": "pass", "fix_needed": False, "sup_eligible": True,
            "action_id": sup_base}}
        index["assignments"] = {"asg_1_worker_1": {
            "id": "asg_1_worker_1", "thread": THREAD, "issue": 1,
            "role": "worker", "status": "waiting_release",
            "sid": "session_w1", "delivery_comment_id": 9001}}

        self.assertEqual(engine._retry_supervisor_routes(index), 1)
        self.assertTrue(index["supervisor_routes"][sup_key]["ok"])
        self.assertEqual(len(self.bus.payloads("event.agent.supervisor")), 1)
        self.assertEqual(engine._retry_supervisor_routes(index), 0)


class UnmarkedReviewSendsNothing(Base):
    """No marker means "work is left", so a release is never announced.

    Every wording shape the earlier revisions tried to decide from is kept
    here: they are still real review lines, and each one used to reach the
    supervisor with a "【可关单】" line while work was pending. The answer no
    longer depends on how they read - none of them carries the marker, so none
    of them is announced. That is the whole point of the change, and it is the
    one property a wording table could never hold.
    """

    UNMARKED = (
        # The shapes that made the bit exist in the first place.
        "通过（P0=0，可关单）",
        "结论：通过；无需修改",
        "结论：通过；无需修复",
        "结论：通过；P1 已修，无需修改",
        "通过；无须再修",
        "通过，不必修",
        "通过；无需再修",
        "通过；必须修项为0",
        "通过；没有必须修的项",
        "结论：通过；必须修的 P1 已在本轮修完",
        # Counted requirements, partial fixes, all-done phrases.
        "有条件通过（1 条 P1 必须修，其余可随下轮）",
        "有条件通过（2 条必须修，已修 1 条）",
        "必须修 2 条，已修 1 条",
        "必须修 3 条（已修 0 条）",
        "必须修（未修完）",
        "通过；必须修的 2 条已全部修完",
        # The negation family: each of these reached the supervisor while work
        # was still pending, under two character tables that disagreed.
        "必须修 2 条，并非全部修完",
        "必须修 2 条，非全部修完",
        "必须修 2 条，非全部修复",
        "必须修 2 条，并非全部完成",
        "必须修，未均已修完",
        "必须修，尚全部修完",
        "必须修，没全部修完",
        "必须修 3 条，已全部修完 2 条",
        # The shapes that killed the wording layer outright: the phrase is not
        # wrong, its subject is a different batch. No regex answers "which
        # batch", which is why the answer is now written down instead of read.
        "有条件通过（必须修 2 条，P2 的 5 条已全部修完）",
        "有条件通过（必须修 2 条，其余 P2 已全部修完）",
        "有条件通过（必须修 2 条，上一轮的 P1 已全部修完）",
        "有条件通过（必须修 2 条，其他都已全部修完）",
        "有条件通过（必须修，P2 的 5 条已全部修完）",
        "有条件通过（必须修，其余已全部修完）",
        "有条件通过（必须修 2 条，已全部修完大部分）",
    )

    def test_an_unmarked_review_sends_nothing_to_the_supervisor(self):
        for body in self.UNMARKED:
            with self.subTest(body=body):
                self.fresh()
                engine = self.engine()
                self.deliver_and_pass(engine, body="[QA] " + body)
                self.assertEqual(
                    self.bus.payloads("event.agent.supervisor"), [], body)
                self.assertEqual(
                    len(self.bus.payloads("event.agent.project_pm")), 1, body)
                index = self.index(engine)
                self.assertFalse(
                    (index.get("supervisor_routes") or {}), body)

    def test_a_marker_that_does_not_parse_is_an_absent_marker(self):
        for body in ("通过（P0=0） <!-- fix_need: false -->",
                     "通过（P0=0） <!-- fix_needed= false -->",
                     "通过（P0=0） <!-- fix_needed: ture -->",
                     "通过（P0=0） <!-- FIX_NEEDED= FALSE -->"):
            with self.subTest(body=body):
                self.fresh()
                engine = self.engine()
                self.deliver_and_pass(engine, body="[QA] " + body)
                self.assertEqual(
                    self.bus.payloads("event.agent.supervisor"), [], body)
                self.assertEqual(
                    len(self.bus.payloads("event.agent.project_pm")), 1, body)

    def test_two_markers_that_disagree_send_nothing(self):
        # The line does not say one thing; the safe side is "work is left".
        self.fresh()
        engine = self.engine()
        self.deliver_and_pass(
            engine,
            body="[QA] 通过 <!-- fix_needed: true --> <!-- fix_needed: false -->")
        self.assertEqual(self.bus.payloads("event.agent.supervisor"), [])
        self.assertEqual(len(self.bus.payloads("event.agent.project_pm")), 1)


class TheMarkerIsTheOnlyWayToAnnounce(Base):
    """The positive direction: only a readable ``fix_needed: false`` announces."""

    def test_the_marker_with_false_announces_the_release(self):
        self.fresh()
        engine = self.engine()
        self.deliver_and_pass(engine)
        payloads = self.bus.payloads("event.agent.supervisor")
        self.assertEqual(len(payloads), 1)
        self.assertFalse(payloads[0]["fix_needed"])
        self.assertEqual(payloads[0]["verdict"], "pass")

    def test_the_marker_with_true_sends_nothing(self):
        self.fresh()
        engine = self.engine()
        self.deliver_and_pass(
            engine, body="[QA] 通过（P0=0） <!-- fix_needed: true -->")
        self.assertEqual(self.bus.payloads("event.agent.supervisor"), [])
        self.assertEqual(len(self.bus.payloads("event.agent.project_pm")), 1)

    def test_a_marked_reject_still_sends_nothing(self):
        # ``fix_needed: false`` says "no work left", not "the verdict is pass";
        # the supervisor is still told only about a release.
        self.fresh()
        engine = self.engine()
        self.deliver_and_pass(
            engine, body="[QA] 退回改（2 条 P0） <!-- fix_needed: false -->")
        self.assertEqual(self.bus.payloads("event.agent.supervisor"), [])
        self.assertEqual(len(self.bus.payloads("event.agent.project_pm")), 1)



class ReleaseTimeout(Base):
    """The held state is ``close_reconcile``; nothing here is hand-assembled."""

    def test_a_thread_held_by_close_reconcile_is_reminded_once(self):
        engine = self.engine()
        self.deliver_and_pass(engine)
        index = self.index(engine)
        rec = index["assignments"]["asg_1_worker_1"]
        self.assertEqual(rec["status"], "close_reconcile",
                         "an open Issue demotes the release in the same round")
        self.assertEqual(len([p for p in self.bus.payloads("event.agent.supervisor")
                             if p.get("kind") != "release_timeout"]), 1,
                         "the pass line is the only other supervisor event")
        # No reminder yet: the thread has just been written.
        self.assertEqual([p for p in self.bus.payloads("event.agent.supervisor")
                          if p.get("kind") == "release_timeout"], [])

        old = (datetime.now(timezone.utc) - timedelta(seconds=1900)).isoformat(
            timespec="seconds")
        index["close_reconcile"][THREAD]["first_checked_at"] = old
        engine.store.save_index(index)
        engine.run_once()
        engine.run_once()
        timeout = [p for p in self.bus.payloads("event.agent.supervisor")
                   if p.get("kind") == "release_timeout"]
        self.assertEqual(len(timeout), 1, "reminded once, not every round")
        self.assertEqual(timeout[0]["issue"], 1)
        self.assertEqual(timeout[0]["sid"], "session_sup")
        self.assertIn("超时未关", timeout[0]["text"])
        self.assertEqual(self.index(engine)["assignments"]["asg_1_worker_1"]
                         ["status"], "close_reconcile",
                         "a reminder never moves the assignment")

    def test_completed_thread_is_never_reminded(self):
        engine = self.engine()
        self.deliver_and_pass(engine)
        index = self.index(engine)
        index["assignments"]["asg_1_worker_1"]["status"] = "completed"
        index["close_reconcile"] = {}
        engine.store.save_index(index)
        engine.run_once()
        self.assertEqual([p for p in self.bus.payloads("event.agent.supervisor")
                          if p.get("kind") == "release_timeout"], [])

    def test_release_timeout_without_a_start_time_is_reported_not_sent(self):
        engine = self.engine()
        self.deliver_and_pass(engine)
        index = self.index(engine)
        # Wipe every start the reminder may fall back on. ``_round_closed_threads``
        # would recreate ``first_checked_at`` on the next round, so the scan is
        # driven directly here - the shape under test is "no start at all".
        index["close_reconcile"] = {}
        for rec in (index.get("assignments") or {}).values():
            rec.pop("tombstone", None)
            rec.pop("archived_at", None)
        engine.store.save_index(index)
        engine._remind_release_timeouts(index)
        self.assertEqual([p for p in self.bus.payloads("event.agent.supervisor")
                          if p.get("kind") == "release_timeout"], [])
        skipped = [r for r in self.rows() if r["type"] == "release_timeout.skipped"]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["reason"], "no usable start time")
        rows = [e for e in (index["issues"][THREAD]["needs_person"] or [])
                if str(e.get("reason") or "") ==
                "route.supervisor_undeliverable[user]"]
        self.assertEqual(len(rows), 1)

    def test_release_timeout_reminder_does_not_need_waiting_release(self):
        # The regression this guards: scanning only ``waiting_release`` watches
        # a set that is empty by design, so the reminder never fires.
        engine = self.engine()
        self.deliver_and_pass(engine)
        index = self.index(engine)
        statuses = {str(r.get("status") or "")
                    for r in (index.get("assignments") or {}).values()}
        self.assertNotIn("waiting_release", statuses)
        self.assertIn("close_reconcile", statuses)

    def test_a_second_release_cycle_does_not_inherit_the_first_start(self):
        # ``close_reconcile[thread]`` outlives a completed thread, so a start
        # carried forward would date the second release to the first one and
        # fire "held for 313 days" on its very first round. The cycle start is
        # reset when the previous status was not ``pending``.
        engine = self.engine()
        self.deliver_and_pass(engine)
        index = self.index(engine)
        old = "2026-01-01T00:00:00+00:00"
        index["close_reconcile"][THREAD]["status"] = "completed"
        index["close_reconcile"][THREAD]["first_checked_at"] = old
        # A new assignment on the same thread enters the hold again.
        index["assignments"]["asg_1_worker_2"] = {
            "id": "asg_1_worker_2", "thread": THREAD, "issue": 1,
            "role": "worker", "status": "waiting_release", "sid": "session_w1",
            "delivery_comment_id": 9002, "qa_verdict": "pass"}
        engine.store.save_index(index)
        engine.run_once()
        fresh = self.index(engine)["close_reconcile"][THREAD]["first_checked_at"]
        self.assertNotEqual(fresh, old, "the second cycle keeps its own start")
        timeout = [p for p in self.bus.payloads("event.agent.supervisor")
                   if p.get("kind") == "release_timeout"]
        self.assertEqual(timeout, [], "a fresh cycle is not overdue")

    def test_a_missing_start_is_logged_once_and_queued(self):
        # The scan runs every round; an unconditional event per round buries
        # the queue it is writing to.
        engine = self.engine()
        self.deliver_and_pass(engine)
        index = self.index(engine)
        index["close_reconcile"] = {}
        for rec in (index.get("assignments") or {}).values():
            rec.pop("tombstone", None)
            rec.pop("archived_at", None)
        for _ in range(3):
            engine._remind_release_timeouts(index)
        skipped = [r for r in self.rows() if r["type"] == "release_timeout.skipped"]
        self.assertEqual(len(skipped), 1, "one line, not one per round")
        rows = [e for e in (index["issues"][THREAD]["needs_person"] or [])
                if str(e.get("reason") or "") ==
                "route.supervisor_undeliverable[user]"]
        self.assertEqual(len(rows), 1, "one row, not one per round")


class UndeliverableSupervisor(Base):
    def test_undeliverable_supervisor_lands_in_the_user_bucket(self):
        engine = self.engine()
        self.write_sessions({"someone-else": {"id": "session_x"}})
        self.deliver_and_pass(engine)
        self.assertEqual(self.bus.payloads("event.agent.supervisor"), [])
        index = self.index(engine)
        rows = [e for e in (index["issues"][THREAD]["needs_person"] or [])
                if str(e.get("reason") or "").startswith(
                    "route.supervisor_undeliverable")]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"],
                         "route.supervisor_undeliverable[user]")
        self.assertEqual(brief._classify(rows[0]["reason"]),
                         brief.PRIORITY_USER)
        logged = [r for r in self.rows()
                  if r["type"] == "route.supervisor_undeliverable"]
        self.assertEqual(len(logged), 1)
        self.assertNotIn("[user]", logged[0]["type"])
        # Never attempted, so a later round may still deliver it.
        sup_base = action_id(f"sup:{THREAD}:9100", "bus_inject", "9100")
        self.assertEqual(engine.store.action_attempted(sup_base), 0)

    def test_configured_sid_wins_and_overlay_keys_are_tried_in_order(self):
        engine = self.engine(engine_cfg={"supervisor_sid": "session_fixed"})
        self.assertEqual(engine._supervisor_sid(), "session_fixed")
        engine = self.engine(engine_cfg={
            "supervisor_keys": ["missing", "backup"]})
        self.write_sessions({"missing": {}, "backup": {"id": "session_b"}})
        self.assertEqual(engine._supervisor_sid(), "session_b")
        engine = self.engine(engine_cfg={"supervisor_keys": ["missing"]})
        self.assertEqual(engine._supervisor_sid(), "")

    def test_a_retry_that_cannot_resolve_the_issue_is_not_silent(self):
        # Dropping the record quietly leaves ``ok=False`` forever with nothing
        # to look at - the same silence this bit exists to remove.
        engine = self.engine()
        index = self.index(engine)
        sup_key = f"sup:{THREAD}:9100"
        index["assignments"] = {}
        index["supervisor_routes"] = {sup_key: {
            "thread": THREAD, "comment_id": 9100, "at": "2026-11-10T10:00:00Z",
            "verdict": "pass", "fix_needed": False, "sup_eligible": True,
            "action_id": action_id(sup_key, "bus_inject", "9100")}}
        self.assertEqual(engine._retry_supervisor_routes(index), 0)
        logged = [r for r in self.rows()
                  if r["type"] == "route.supervisor_undeliverable"]
        self.assertEqual(len(logged), 1)
        self.assertIn("resolve the issue", logged[0]["detail"])
        rows = [e for e in (index["issues"][THREAD]["needs_person"] or [])
                if str(e.get("reason") or "") ==
                "route.supervisor_undeliverable[user]"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(brief._classify(rows[0]["reason"]),
                         brief.PRIORITY_USER)

    def test_two_different_failures_on_one_comment_keep_two_rows(self):
        # A ``None`` delivery id is not an identity: two assignments sharing it
        # must not collapse into one row, and two different reasons for the
        # same comment are two problems, not one.
        engine = self.engine()
        index = self.index(engine)
        engine._note_undeliverable(index, THREAD, None, "no supervisor sid",
                                   assignment="asg_1_worker_1")
        engine._note_undeliverable(index, THREAD, None, "no supervisor sid",
                                   assignment="asg_1_worker_2")
        self.assertEqual(len(index["issues"][THREAD]["needs_person"]), 2)
        engine._note_undeliverable(index, THREAD, None, "dispatch refused",
                                   assignment="asg_1_worker_1")
        self.assertEqual(len(index["issues"][THREAD]["needs_person"]), 3)

        # ... and the same failure across rounds still writes one row.
        for _ in range(3):
            engine._note_undeliverable(index, THREAD, None,
                                       "dispatch refused",
                                       assignment="asg_1_worker_1")
        self.assertEqual(len(index["issues"][THREAD]["needs_person"]), 3)
        logged = [r for r in self.rows()
                  if r["type"] == "route.supervisor_undeliverable"]
        self.assertEqual(len(logged), 3, "one line per distinct failure")


class ConfigSurface(unittest.TestCase):
    def test_defaults_and_validation(self):
        engine = config.DEFAULTS["engine"]
        self.assertEqual(engine["supervisor_sid"], "")
        self.assertEqual(engine["supervisor_keys"], ["supervisor"])
        self.assertEqual(engine["release_timeout_s"], 1800)
        with self.assertRaises(config.ConfigError):
            config.load(None, {"engine": {"release_timeout_s": 0}})
        with self.assertRaises(config.ConfigError):
            config.load(None, {"engine": {"release_timeout_s": "soon"}})
        with self.assertRaises(config.ConfigError):
            config.load(None, {"engine": {"supervisor_keys": "supervisor"}})
        with self.assertRaises(config.ConfigError):
            config.load(None, {"engine": {"supervisor_keys": ["ok", 3]}})
        cfg = config.load(None, {"engine": {"supervisor_keys": ["a", "b"]}})
        self.assertEqual(cfg["engine"]["supervisor_keys"], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
