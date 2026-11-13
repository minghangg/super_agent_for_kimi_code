"""``[PM]`` verdict routing: three states, one silence, one re-entry guard.

The properties pinned here, and why each is a counter-example:

* ``ready``, ``blocked`` and unreadable each produce a frame, and ``working``
  produces **none** - a frame saying "nothing new" is the noise that teaches a
  reader to ignore the channel;
* the two exceptions share the ``event.agent.supervisor`` path with
  ``exception: true`` in the payload, so the one delivery leg ISS-55 built is
  the only leg, and the reader can tell an exception from a release;
* unreadable or coordinate-less is **blocked**, never silence: a PM answered
  and the engine dropped it is the failure nobody can see;
* once the thread has a PM verdict, the QA pass no longer rings the supervisor
  - the release has one gate, and two wake-ups for one release is the
  double-ring this change exists to stop;
* a PM that stops answering is caught by a counter that a PM verdict resets,
  fires once per hold, and can be configured.

Run: ``python -m pytest tests/test_engine_pm_route.py -q``
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from super_agent import config, markers, session_events
from super_agent.allowlist import ProjectAllowlist
from super_agent.engine import Engine
from super_agent.store import Store

THREAD = "owner/demo#1"
READY = "[PM] 可关单 #1 <!-- ready: true -->"
# The contract spelling (issue #57 cid 6828771423 §二). Both must work, and the
# end-to-end test below proves the contract one reaches the supervisor, not just
# the parser.
READY_V1 = "[PM] 可关单 #1 <!-- verdict: ready -->"
BLOCKED_V1 = "[PM] 受阻 #1 <!-- verdict: blocked -->"
WORKING_V1 = "[PM] 修复中 #1 <!-- verdict: working -->"
# No marker at all: the contract's "blocked / not finished", which is ``""``.
UNMARKED_WORKING = "[PM] 修复中 #1"
BLOCKED = BLOCKED_V1
WORKING = WORKING_V1
UNREADABLE = "[PM] 通过 #1"
PASS = "[QA] 通过（P0=0） <!-- fix_needed: false -->"


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

    def add(self, cid, body, author="pm"):
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


GOOD = {
    "schema_version": 1,
    "defaults": {"mode": "observe", "creation_mode": "pm_created"},
    "projects": {
        "demo": {"repo": "owner/demo", "cwd": ".", "mode": "assist",
                 "creation_mode": "pm_created", "issues": [1],
                 "actions": ["bus_inject", "collect", "reconcile", "archive"],
                 "max_active_writers": 1, "roles": ["worker", "qa", "pm"]},
    },
}


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
        self.sessions_path = self.events.parent / "sessions.json"
        self.sessions_path.write_text(
            json.dumps({"supervisor": {"id": "session_sup"},
                        "pm-demo": {"id": "session_pm"}}), encoding="utf-8")
        self.bind()

    def tearDown(self):
        self.tmp.cleanup()

    def bind(self):
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
            {"seq": 5, "at": "2026-11-10T10:00:04+00:00",
             "type": session_events.CREATED, "sid": "session_pm",
             "key": "pm-demo"},
            {"seq": 6, "at": "2026-11-10T10:00:05+00:00",
             "type": session_events.BOUND, "sid": "session_pm", "key": THREAD,
             "role": "pm", "issue": 1},
        ]
        self.events.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                               encoding="utf-8")

    def engine(self, *, mode="assist", engine_cfg=None, role="supervisor"):
        extra = {"session_events_path": str(self.events),
                 "allowlist": ["issue_comment.created"]}
        extra.update(engine_cfg or {})
        cfg = config.load(None, {
            "mode": mode, "role": role,
            "github": {"source": "none", "repo": "owner/demo"},
            "engine": extra,
        })
        engine = Engine(cfg, Store(self.state), self.source, self.bus,
                        project_allowlist=ProjectAllowlist(self.allow),
                        session_api=self.sessions)
        engine._round_closed_threads = lambda index: []
        return engine

    def rows(self):
        path = self.state / "events.jsonl"
        if not path.is_file():
            return []
        return [json.loads(x) for x in path.read_text("utf-8").splitlines()
                if x.strip()]

    def types(self):
        return [r["type"] for r in self.rows()]

    def pm_rows(self, engine, reason):
        """The raw ``needs_person`` rows for one reason (``status`` drops the
        detail, and the detail is what a person reads)."""
        rec = (engine.store.load_index().get("issues") or {}).get(THREAD) or {}
        return [row for row in rec.get("needs_person") or []
                if str(row.get("reason") or "") == reason]

    def deliver(self):
        self.source.add(9001, "[WK:wk-probe-1] done", author="worker")


class ThreeStateRouting(Base):
    def test_contract_v1_ready_reaches_the_supervisor(self):
        # The whole chain, not just the parser: a PM who writes exactly what
        # issue #57 §二 says must ring the release bell.
        engine = self.engine()
        self.deliver()
        self.source.add(9100, READY_V1)
        engine.run_once()
        payloads = self.bus.payloads("event.agent.supervisor")
        self.assertEqual(len(payloads), 1)
        self.assertFalse(payloads[0]["exception"])
        self.assertEqual(payloads[0]["state"], "ready")
        self.assertIn("可关单", payloads[0]["text"])

    def test_contract_v1_blocked_still_sends_an_exception(self):
        engine = self.engine()
        self.deliver()
        self.source.add(9100, BLOCKED_V1)
        engine.run_once()
        payloads = self.bus.payloads("event.agent.supervisor")
        self.assertEqual(len(payloads), 1)
        self.assertTrue(payloads[0]["exception"])

    def test_contract_v1_working_stays_silent(self):
        engine = self.engine()
        self.deliver()
        self.source.add(9100, WORKING_V1)
        engine.run_once()
        self.assertEqual(self.bus.payloads("event.agent.supervisor"), [])

    def test_ready_calls_the_supervisor(self):
        engine = self.engine()
        self.deliver()
        self.source.add(9100, READY)
        engine.run_once()
        payloads = self.bus.payloads("event.agent.supervisor")
        self.assertEqual(len(payloads), 1)
        self.assertFalse(payloads[0]["exception"])
        self.assertEqual(payloads[0]["state"], "ready")
        self.assertEqual(payloads[0]["sid"], "session_sup")
        self.assertIn("可关单", payloads[0]["text"])

    def test_blocked_sends_an_exception_frame(self):
        engine = self.engine()
        self.deliver()
        self.source.add(9100, BLOCKED)
        engine.run_once()
        payloads = self.bus.payloads("event.agent.supervisor")
        self.assertEqual(len(payloads), 1)
        self.assertTrue(payloads[0]["exception"])
        self.assertEqual(payloads[0]["state"], "blocked")
        self.assertIn("PM 例外", payloads[0]["text"])

    def test_working_is_silent(self):
        engine = self.engine()
        self.deliver()
        self.source.add(9100, WORKING)
        engine.run_once()
        self.assertEqual(self.bus.payloads("event.agent.supervisor"), [])
        self.assertEqual(self.bus.payloads("event.agent.needs_person"), [])
        self.assertIn("pm.verdict", self.types())
        # The verdict is still on record: it is the answer the stall watchdog
        # and the double-ring guard both read.
        self.assertEqual(engine.store.load_index()["pm_verdicts"][THREAD]["state"],
                         "working")

    def test_unreadable_is_treated_as_blocked_not_silence(self):
        engine = self.engine()
        self.deliver()
        self.source.add(9100, UNREADABLE)
        engine.run_once()
        payloads = self.bus.payloads("event.agent.supervisor")
        self.assertEqual(len(payloads), 1, "a dropped PM answer must be visible")
        self.assertTrue(payloads[0]["exception"])
        self.assertEqual(payloads[0]["state"], "")

    def test_ready_without_a_coordinate_never_releases(self):
        engine = self.engine()
        self.deliver()
        self.source.add(9100, "[PM] 可关单 <!-- ready: true -->")
        engine.run_once()
        payloads = self.bus.payloads("event.agent.supervisor")
        self.assertEqual(len(payloads), 1)
        self.assertTrue(payloads[0]["exception"])
        self.assertEqual(payloads[0]["reason"], "no issue coordinate")

    def test_a_pm_verdict_beats_role_noise(self):
        # The engine's own role is ``pm``: the shared login means the verdict
        # would otherwise be dismissed as author noise.
        engine = self.engine(role="pm")
        self.deliver()
        self.source.add(9100, BLOCKED)
        engine.run_once()
        self.assertEqual(len(self.bus.payloads("event.agent.supervisor")), 1)

    def test_observe_mode_records_the_frame_and_sends_nothing(self):
        engine = self.engine(mode="observe")
        self.deliver()
        self.source.add(9100, READY)
        engine.run_once()
        self.assertEqual(self.bus.payloads("event.agent.supervisor"), [])
        frames = engine.store.load_index()["pm_frames"]
        self.assertFalse(frames[f"pm:{THREAD}:9100"]["ok"])

    def test_the_frame_is_retried_after_a_refusal(self):
        engine = self.engine(mode="observe")
        self.deliver()
        self.source.add(9100, READY)
        engine.run_once()
        engine.dispatch_enabled = True
        engine.run_once()
        engine.run_once()
        self.assertEqual(len(self.bus.payloads("event.agent.supervisor")), 1,
                         "at most once, and at least once")


class OneRingForOneRelease(Base):
    def test_qa_pass_then_pm_ready_sends_exactly_one_frame(self):
        engine = self.engine()
        self.deliver()
        self.source.add(9100, PASS, author="qa")
        self.source.add(9200, READY)
        engine.run_once()
        self.assertEqual(len(self.bus.payloads("event.agent.supervisor")), 1,
                         "one release, one wake-up")
        self.assertIn(THREAD, engine.store.load_index()["pm_verdicts"])

    def test_a_pm_ready_in_a_later_round_does_not_ring_again(self):
        # The QA pass rings in round 1. The PM answer arrives in a **later**
        # batch, so a "is there a verdict right now?" question answers "no" in
        # both rounds and rings twice. The event ledger answers "has this
        # release rung?", which is the question that holds across rounds.
        engine = self.engine()
        self.deliver()
        self.source.add(9100, PASS, author="qa")
        engine.run_once()
        self.assertEqual(len(self.bus.payloads("event.agent.supervisor")), 1)
        self.source.add(9200, READY)
        engine.run_once()
        self.assertEqual(len(self.bus.payloads("event.agent.supervisor")), 1,
                         "the PM answer must not ring a second time")
        skipped = [r for r in self.rows()
                   if r["type"] == "route.supervisor_skipped"]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["announced_by"], "qa")

    def test_nothing_rings_until_a_release_is_actually_cleared(self):
        # Counter-example for the guard: a second QA pass on a thread whose
        # release already rang is silence, not a second wake-up.
        engine = self.engine()
        self.deliver()
        self.source.add(9100, PASS, author="qa")
        engine.run_once()
        self.source.add(9200, "[QA] 通过 <!-- fix_needed: false -->",
                        author="qa")
        engine.run_once()
        self.assertEqual(len(self.bus.payloads("event.agent.supervisor")), 1)

    def test_the_suppressed_frame_leaves_no_retryable_record(self):
        # The frame that was skipped must not leave a ``sup:`` record the retry
        # pass would later pick up and send.
        engine = self.engine()
        self.deliver()
        self.source.add(9100, PASS, author="qa")
        engine.run_once()
        self.source.add(9200, READY)
        engine.run_once()
        self.assertEqual(len(self.bus.payloads("event.agent.supervisor")), 1)
        index = engine.store.load_index()
        engine._retry_supervisor_routes(index)
        engine._retry_pm_frames(index)
        self.assertEqual(len(self.bus.payloads("event.agent.supervisor")), 1,
                         "the retry passes send nothing for one release")


class StallWatchdog(Base):
    def hold(self, engine, *, qa_pass=True):
        """A thread held in ``waiting_release`` after a QA pass."""
        index = engine.store.load_index()
        index.setdefault("assignments", {})["asg_1_worker_1"] = {
            "id": "asg_1_worker_1", "thread": THREAD, "issue": 1,
            "role": "worker", "status": "waiting_release", "sid": "session_w1",
            "delivery_comment_id": 9001,
            "qa_verdict": "pass" if qa_pass else ""}
        engine.store.save_index(index)
        return index

    def test_no_frame_before_the_threshold(self):
        engine = self.engine(engine_cfg={"pm_stall_rounds": 3})
        self.hold(engine)
        for _ in range(2):
            engine.run_once()
        self.assertEqual(self.bus.payloads("event.agent.supervisor"), [])

    def test_one_exception_frame_at_the_threshold(self):
        engine = self.engine(engine_cfg={"pm_stall_rounds": 3})
        self.hold(engine)
        for _ in range(3):
            engine.run_once()
        payloads = self.bus.payloads("event.agent.supervisor")
        self.assertEqual(len(payloads), 1)
        self.assertTrue(payloads[0]["exception"])
        self.assertIn("PM 停摆", payloads[0]["text"])

    def test_the_frame_is_sent_once_per_hold(self):
        engine = self.engine(engine_cfg={"pm_stall_rounds": 2})
        self.hold(engine)
        for _ in range(6):
            engine.run_once()
        self.assertEqual(len(self.bus.payloads("event.agent.supervisor")), 1)

    def test_a_pm_verdict_resets_the_counter(self):
        engine = self.engine(engine_cfg={"pm_stall_rounds": 3})
        self.hold(engine)
        for _ in range(2):
            engine.run_once()
        self.source.add(9100, WORKING)
        engine.run_once()
        index = engine.store.load_index()
        self.assertNotIn(THREAD, index.get("pm_stall") or {})
        engine.run_once()
        self.assertEqual(self.bus.payloads("event.agent.supervisor"), [])

    def test_a_thread_without_a_qa_pass_is_not_watched(self):
        engine = self.engine(engine_cfg={"pm_stall_rounds": 1})
        self.hold(engine, qa_pass=False)
        engine.run_once()
        self.assertEqual(self.bus.payloads("event.agent.supervisor"), [])

    def test_the_stall_is_visible_to_a_person(self):
        engine = self.engine(engine_cfg={"pm_stall_rounds": 1})
        self.hold(engine)
        engine.run_once()
        reasons = [e.get("reason") for e in
                   (engine.status()["items"] or [])]
        self.assertIn("pm.stall[user]", reasons)

    def test_a_second_hold_is_its_own_notice_and_the_row_is_not_piled(self):
        # P1-3: the action id carries a per-thread hold sequence, so a second
        # stuck release is reported instead of being swallowed by the first
        # one's attempt counter - and the person-facing row is updated, not
        # duplicated.
        engine = self.engine(engine_cfg={"pm_stall_rounds": 1})
        self.hold(engine)
        engine.run_once()
        self.assertEqual(len(self.bus.payloads("event.agent.supervisor")), 1)
        self.release(engine)                 # leaves the hold
        engine.run_once()
        self.hold(engine)                    # ... and comes back
        engine.run_once()
        self.assertEqual(len(self.bus.payloads("event.agent.supervisor")), 2,
                         "a new hold is a new notice")
        rows = [e for e in (engine.status()["items"] or [])
                if e.get("reason") == "pm.stall[user]"]
        self.assertEqual(len(rows), 1, "one row, not one per hold")

    def test_the_default_threshold_does_not_collide_with_release_timeout(self):
        # P1-3: 15 rounds vs release_timeout_s=1800 (30 rounds at 60s). Equal
        # defaults would put a release-timeout reminder and a stall notice on
        # the same round for the same thread.
        engine = self.engine()
        cfg = engine.cfg.get("engine") or {}
        self.assertNotIn("pm_stall_rounds", cfg)
        timeout_s = float(cfg.get("release_timeout_s") or 1800)
        poll = float((engine.cfg.get("github") or {}).get("poll_interval_s") or 60)
        self.assertLess(15 * poll, timeout_s,
                        "the stall notice must come before the timeout")

    def test_the_two_watchdogs_do_not_fire_on_the_same_round(self):
        engine = self.engine(engine_cfg={"pm_stall_rounds": 2})
        self.hold(engine)
        for _ in range(4):
            engine.run_once()
        kinds = [p.get("kind") for p in
                 self.bus.payloads("event.agent.supervisor")]
        self.assertIn("pm_stall", kinds)
        self.assertNotIn("release_timeout", kinds,
                         "the stall notice is the first one, not a duplicate")

    def release(self, engine):
        index = engine.store.load_index()
        rec = (index.get("assignments") or {}).get("asg_1_worker_1")
        if rec is not None:
            rec["status"] = "completed"
        engine.store.save_index(index)


class PMAuthorCheck(Base):
    """P1-1: a PM ``ready`` wakes a person to close an issue - it cannot be
    taken back, so it needs the same attribution the QA path has."""

    def test_an_unverified_pm_verdict_does_not_ring(self):
        engine = self.engine()
        self.deliver()
        # No pm-* key and no issue_sessions entry: the thread has no PM.
        self.sessions_path.write_text(json.dumps(
            {"supervisor": {"id": "session_sup"}}), encoding="utf-8")
        self.source.add(9100, READY, author="some-random-account")
        engine.run_once()
        self.assertEqual(self.bus.payloads("event.agent.supervisor"), [],
                         "an unverified ready must not ring")
        rows = self.pm_rows(engine, "pm.verdict_unverified[user]")
        self.assertEqual(len(rows), 1)
        self.assertIn("unverified author", str(rows[0].get("detail") or ""))

    def test_a_named_pm_session_key_is_checked_exactly(self):
        engine = self.engine()
        self.deliver()
        self.source.add(9100, READY)
        engine.run_once()
        self.assertEqual(len(self.bus.payloads("event.agent.supervisor")), 1)
        # The engine does not read ``author`` at all: a forged login with a
        # registered key still rings, which is why the check is called weak.
        self.sessions_path.write_text(json.dumps(
            {"supervisor": {"id": "session_sup"}}), encoding="utf-8")


class PMCoorclinateCheck(Base):
    """P1-2: the verdict must speak about this thread's issue."""

    def test_a_verdict_for_another_issue_does_not_ring(self):
        engine = self.engine()
        self.deliver()
        self.source.add(9100, "[PM] 可关单 #99 <!-- verdict: ready -->")
        engine.run_once()
        self.assertEqual(self.bus.payloads("event.agent.supervisor"), [])
        rows = self.pm_rows(engine, "pm.verdict_wrong_issue[user]")
        self.assertEqual(len(rows), 1)
        self.assertIn("#99", str(rows[0].get("detail") or ""))

    def test_a_body_reference_is_not_the_coordinate(self):
        # No first-line coordinate, so this cannot be ready. The body's #99 is
        # a reference and must not become the subject: the frame is an
        # exception about **this** thread, not a release of #99.
        engine = self.engine()
        self.deliver()
        self.source.add(9100, "[PM] 可关单 <!-- verdict: ready -->\n\n和 #99 有关")
        engine.run_once()
        payloads = self.bus.payloads("event.agent.supervisor")
        self.assertEqual(len(payloads), 1)
        self.assertTrue(payloads[0]["exception"])
        self.assertEqual(payloads[0]["issue"], 1,
                         "the frame is about this thread, not #99")

    def test_the_matching_coordinate_rings(self):
        engine = self.engine()
        self.deliver()
        self.source.add(9100, READY)
        engine.run_once()
        self.assertEqual(len(self.bus.payloads("event.agent.supervisor")), 1)


class BothGatesHoldInBothOrders(Base):
    """The two gates must each hold alone, in either arrival order.

    Disabling one side's dedupe must still be caught, so each order is pinned
    twice: the first frame comes from the QA pass, or from the PM verdict.
    """

    def test_qa_first_then_pm_is_one_frame(self):
        engine = self.engine()
        self.deliver()
        self.source.add(9100, PASS, author="qa")
        engine.run_once()
        self.assertEqual(len(self.bus.payloads("event.agent.supervisor")), 1)
        self.source.add(9200, READY)
        engine.run_once()
        self.assertEqual(len(self.bus.payloads("event.agent.supervisor")), 1,
                         "QA first: the PM gate must hold on its own")

    def test_pm_first_then_qa_is_one_frame(self):
        # The PM answers before any QA pass is seen: the PM gate rings, and the
        # QA gate must then stay silent on its own.
        engine = self.engine()
        self.deliver()
        self.source.add(9100, READY)
        engine.run_once()
        self.assertEqual(len(self.bus.payloads("event.agent.supervisor")), 1)
        self.source.add(9200, PASS, author="qa")
        engine.run_once()
        self.assertEqual(len(self.bus.payloads("event.agent.supervisor")), 1,
                         "PM first: the QA gate must hold on its own")

    def test_both_in_one_batch_qa_first(self):
        engine = self.engine()
        self.deliver()
        self.source.add(9100, PASS, author="qa")
        self.source.add(9200, READY)
        engine.run_once()
        self.assertEqual(len(self.bus.payloads("event.agent.supervisor")), 1)

    def test_both_in_one_batch_pm_first(self):
        engine = self.engine()
        self.deliver()
        self.source.add(9200, READY)
        self.source.add(9300, PASS, author="qa")
        engine.run_once()
        self.assertEqual(len(self.bus.payloads("event.agent.supervisor")), 1)


class ReleaseLedgerWrittenAfterTheSend(Base):
    """P1-2: the ledger means "this happened", so it is written after the send.

    Written before, a crash in between leaves "already announced" on record for
    a frame nobody sent - the release is lost and nothing a person reads says
    so. Left unwritten, the next round simply retries.
    """

    def test_a_crash_after_the_decision_still_sends_next_round(self):
        engine = self.engine(mode="observe")
        self.deliver()
        self.source.add(9100, PASS, author="qa")
        engine.run_once()
        # Observe: the decision was taken, the dispatch did not happen, and the
        # ledger must therefore be empty - the same state a crash leaves.
        index = engine.store.load_index()
        self.assertEqual(index.get("release_frames") or {}, {},
                         "nothing landed, so nothing is recorded")
        self.assertTrue(index["supervisor_routes"][f"sup:{THREAD}:9100"])
        engine.dispatch_enabled = True
        engine.run_once()
        self.assertEqual(len(self.bus.payloads("event.agent.supervisor")), 1,
                         "an unwritten ledger is a retry, not a loss")
        self.assertTrue(engine.store.load_index()["release_frames"])

    def test_an_undelivered_release_leaves_a_person_readable_row(self):
        engine = self.engine()
        self.deliver()
        self.sessions_path.write_text(json.dumps({}), encoding="utf-8")
        self.source.add(9100, PASS, author="qa")
        engine.run_once()
        self.assertEqual(self.bus.payloads("event.agent.supervisor"), [])
        index = engine.store.load_index()
        self.assertEqual(index.get("release_frames") or {}, {},
                         "an undelivered release is not recorded as announced")
        rows = [e for e in (engine.status()["items"] or [])]
        self.assertTrue(rows, "a person has something to read")

    def test_a_crash_after_the_pm_decision_also_retries(self):
        engine = self.engine(mode="observe")
        self.deliver()
        self.source.add(9100, READY)
        engine.run_once()
        self.assertEqual(engine.store.load_index().get("release_frames") or {},
                         {})
        engine.dispatch_enabled = True
        engine.run_once()
        self.assertEqual(len(self.bus.payloads("event.agent.supervisor")), 1)
        engine.run_once()
        self.assertEqual(len(self.bus.payloads("event.agent.supervisor")), 1,
                         "and then the ledger stops the repeat")

    def test_the_retry_path_writes_the_ledger_too(self):
        # P0: the retry is a **first** delivery. If it does not write the
        # ledger, the next PM verdict rings again - the hole the QA side never
        # had, because its retry re-enters ``_route_review``.
        engine = self.engine(mode="observe")
        self.deliver()
        self.source.add(9100, READY)
        engine.run_once()
        self.assertEqual(engine.store.load_index().get("release_frames") or {},
                         {})
        engine.dispatch_enabled = True
        index = engine.store.load_index()
        self.assertEqual(engine._retry_pm_frames(index), 1)
        self.assertEqual(len(self.bus.payloads("event.agent.supervisor")), 1)
        self.assertTrue(index.get("release_frames"),
                        "a delivered retry is a delivery: record it")
        self.assertTrue(index["pm_frames"][f"pm:{THREAD}:9100"]["ok"],
                        "and the record must not be retried again")
        engine.store.save_index(index)   # the round's own save
        # Another verdict on the same release must stay silent.
        self.source.add(9200, READY)
        engine.run_once()
        self.assertEqual(len(self.bus.payloads("event.agent.supervisor")), 1,
                         "after the retry, a new ready must not ring again")

    def test_a_retry_on_the_same_round_is_not_repeated(self):
        engine = self.engine(mode="observe")
        self.deliver()
        self.source.add(9100, READY)
        engine.run_once()
        engine.dispatch_enabled = True
        engine.run_once()
        engine.run_once()
        engine.run_once()
        self.assertEqual(len(self.bus.payloads("event.agent.supervisor")), 1,
                         "one retry, not one per round")

    def test_an_exception_retry_writes_no_release_ledger(self):
        engine = self.engine(mode="observe")
        self.deliver()
        self.source.add(9100, BLOCKED_V1)
        engine.run_once()
        engine.dispatch_enabled = True
        engine.run_once()
        self.assertEqual(len(self.bus.payloads("event.agent.supervisor")), 1)
        self.assertEqual(engine.store.load_index().get("release_frames") or {},
                         {}, "an exception frame is not a release")


class ReworkIsANewRelease(Base):
    """P1-1: the key is the delivery this comment judged, not the newest one.

    The rework case has two worker assignments (deliveries 9001 and 9300). A
    key built from "the newest delivery" gives the first release the second
    delivery's key once the rework assignment exists, and the same release
    rings twice.
    """

    def rework(self, engine):
        index = engine.store.load_index()
        index.setdefault("assignments", {}).update({
            "asg_1_worker_1": {
                "id": "asg_1_worker_1", "thread": THREAD, "issue": 1,
                "role": "worker", "status": "waiting_release",
                "sid": "session_w1", "delivery_comment_id": 9001,
                "review_comment_id": 9100, "qa_verdict": "pass"},
            "asg_1_worker_2": {
                "id": "asg_1_worker_2", "thread": THREAD, "issue": 1,
                "role": "worker", "status": "waiting_review",
                "sid": "session_w1", "delivery_comment_id": 9300}})
        engine.store.save_index(index)

    def test_one_release_across_a_rework(self):
        engine = self.engine()
        self.deliver()
        self.source.add(9100, PASS, author="qa")
        engine.run_once()
        self.assertEqual(len(self.bus.payloads("event.agent.supervisor")), 1)
        self.rework(engine)
        self.source.add(9200, READY)
        engine.run_once()
        self.assertEqual(len(self.bus.payloads("event.agent.supervisor")), 1,
                         "the same release must not ring twice after a rework")
        frames = engine.store.load_index().get("release_frames") or {}
        self.assertEqual(list(frames), [f"release:{THREAD}:9001"],
                         "one key, the delivery the verdicts judged")

    def test_a_verdict_for_the_rework_is_a_different_key(self):
        # Counter-example: the guard must not collapse two real releases. A
        # verdict on delivery 9300 is a different release and does ring.
        engine = self.engine()
        self.deliver()
        self.rework(engine)
        self.source.add(9250, PASS, author="qa")
        engine.run_once()
        frames = engine.store.load_index().get("release_frames") or {}
        self.assertEqual(list(frames), [f"release:{THREAD}:9300"])

    def test_an_unlinked_comment_says_so(self):
        engine = self.engine(engine_cfg={"allowlist": ["issue_comment.created"]})
        self.source.add(9100, READY)
        engine.run_once()
        missed = [r for r in self.rows()
                  if r["type"] == "release.key_unlinked"]
        self.assertEqual(len(missed), 1)
        self.assertEqual(missed[0]["comment_id"], 9100)


class DeclaredDeliveryWins(Base):
    """A-case: a verdict may name the delivery it judges.

    Without the coordinate the engine attributes a PM verdict by time ("newest
    delivery that already existed"), which misfires on a reworked thread. The
    field replaces the guess with a statement; the guess stays only for the old
    format.
    """

    def rework(self, engine):
        index = engine.store.load_index()
        index.setdefault("assignments", {}).update({
            "asg_1_worker_1": {
                "id": "asg_1_worker_1", "thread": THREAD, "issue": 1,
                "role": "worker", "status": "waiting_release",
                "sid": "session_w1", "delivery_comment_id": 9001,
                "review_comment_id": 9100, "qa_verdict": "pass"},
            "asg_1_worker_2": {
                "id": "asg_1_worker_2", "thread": THREAD, "issue": 1,
                "role": "worker", "status": "waiting_review",
                "sid": "session_w1", "delivery_comment_id": 9300}})
        engine.store.save_index(index)

    def test_the_declared_delivery_is_used_without_a_time_guess(self):
        engine = self.engine()
        self.deliver()
        self.rework(engine)
        # 9200 is older than delivery 9300, so the time guess would answer 9001.
        self.source.add(9200, "[PM] 可关单 #1 <!-- verdict: ready delivery: 9300 -->")
        engine.run_once()
        frames = engine.store.load_index().get("release_frames") or {}
        self.assertEqual(list(frames), [f"release:{THREAD}:9300"],
                         "the coordinate decides, not the clock")

    def test_the_old_format_still_uses_the_time_guess(self):
        # Regression: comments written before the field must keep working.
        engine = self.engine()
        self.deliver()
        self.rework(engine)
        self.source.add(9200, READY)
        engine.run_once()
        frames = engine.store.load_index().get("release_frames") or {}
        self.assertEqual(list(frames), [f"release:{THREAD}:9001"])

    def test_a_declared_delivery_that_does_not_exist_is_reported(self):
        # An unknown coordinate is not a release and not a silent guess: the
        # engine stops and puts a readable row in front of a person.
        engine = self.engine()
        self.deliver()
        self.source.add(9100, "[PM] 可关单 #1 <!-- verdict: ready delivery: 9999 -->")
        engine.run_once()
        self.assertEqual(self.bus.payloads("event.agent.supervisor"), [],
                         "an unknown delivery must not ring")
        rows = self.pm_rows(engine, "pm.verdict[user]")
        self.assertEqual(len(rows), 1)
        self.assertIn("9999", str(rows[0].get("detail") or ""))
        logged = [r for r in self.rows() if r["type"] == "pm.delivery_unknown"]
        self.assertEqual(len(logged), 1)

    def test_a_malformed_delivery_is_reported_not_silently_dropped(self):
        engine = self.engine()
        self.deliver()
        self.source.add(9100, "[PM] 可关单 #1 <!-- verdict: ready delivery: abc -->")
        engine.run_once()
        self.assertEqual(self.bus.payloads("event.agent.supervisor"), [],
                         "an unreadable coordinate must not fall back to a guess")
        rows = self.pm_rows(engine, "pm.verdict[user]")
        self.assertEqual(len(rows), 1)
        self.assertIn("delivery coordinate", str(rows[0].get("detail") or ""))
        logged = [r for r in self.rows() if r["type"] == "pm.delivery_bad"]
        self.assertEqual(len(logged), 1)

    def test_the_same_declared_delivery_rings_once(self):
        engine = self.engine()
        self.deliver()
        self.rework(engine)
        self.source.add(9200, "[PM] 可关单 #1 <!-- verdict: ready delivery: 9300 -->")
        engine.run_once()
        self.source.add(9400, "[PM] 可关单 #1 <!-- verdict: ready delivery: 9300 -->")
        engine.run_once()
        self.assertEqual(len(self.bus.payloads("event.agent.supervisor")), 1,
                         "one delivery, one release, whichever comment says so")


class NoSilentOutcome(Base):
    """P1-1: every dispatch path either records the release or leaves a row.

    The gap this closes: ``_dispatch`` returns ``dispatched=True, ok=False``
    when the bus raises after the send. Neither "wrote the ledger" nor "not
    dispatched, retry later" matched, so the frame was sent and **nothing**
    was recorded - a bell that rang and left no trace.
    """

    def make_bus_fail(self):
        bus = self.bus
        original = bus.inject

        def inject(event_type, payload):
            original(event_type, payload)      # the send happens ...
            raise RuntimeError("bus dropped after accepting the event")
        bus.inject = inject

    def test_a_send_that_raises_still_lands_in_needs_person(self):
        engine = self.engine()
        self.deliver()
        self.make_bus_fail()
        self.source.add(9100, READY)
        engine.run_once()
        index = engine.store.load_index()
        self.assertEqual(index.get("release_frames") or {}, {},
                         "unconfirmed is not 'it happened'")
        rows = [e for e in (index["issues"][THREAD]["needs_person"] or [])
                if "not confirmed" in str(e.get("detail") or "")]
        self.assertEqual(len(rows), 1,
                         "an unconfirmed send must leave something to read")
        logged = [r for r in self.rows()
                  if r["type"] == "route.supervisor_undeliverable"]
        self.assertEqual(len(logged), 1)

    def test_an_exception_that_raises_also_lands_in_needs_person(self):
        engine = self.engine()
        self.deliver()
        self.make_bus_fail()
        self.source.add(9100, BLOCKED_V1)
        engine.run_once()
        index = engine.store.load_index()
        rows = [e for e in (index["issues"][THREAD]["needs_person"] or [])
                if "not confirmed" in str(e.get("detail") or "")]
        self.assertEqual(len(rows), 1,
                         "an unconfirmed exception frame also leaves a row")

    def test_an_exception_that_never_left_also_lands_in_needs_person(self):
        # Third branch: nothing was dispatched. For a release that is "retry
        # later", but for an exception frame the PM asked for a look and the
        # look did not happen - that needs a row of its own.
        engine = self.engine(mode="observe")
        self.deliver()
        self.source.add(9100, BLOCKED_V1)
        engine.run_once()
        self.assertEqual(self.bus.payloads("event.agent.supervisor"), [])
        rows = self.pm_rows(engine, "pm.verdict[user]")
        self.assertEqual(len(rows), 1,
                         "an unsent exception is a person's to notice")

    def test_a_retry_writes_the_record_back_through_the_table(self):
        # The loop's ``record`` is a stale reference: ``_route_pm_frame`` stores
        # a fresh dict under the same key. Writing through the old reference
        # drops ``ok`` and the next round retries a frame that already landed.
        engine = self.engine(mode="observe")
        self.deliver()
        self.source.add(9100, READY)
        engine.run_once()
        index = engine.store.load_index()
        index.pop("release_frames", None)
        engine.dispatch_enabled = True
        self.assertEqual(engine._retry_pm_frames(index), 1)
        self.assertTrue(index["pm_frames"][f"pm:{THREAD}:9100"]["ok"],
                        "the outcome must be written through the table")
        self.assertEqual(engine._retry_pm_frames(index), 0,
                         "and a recorded success is not retried")


class GuardDiscriminatesDeclaredFromGuess(Base):
    """P1-2 precision: "ignored the coordinate" vs "used it in a guess".

    ``_delivery_for`` has five levels. Two of them can both answer 9300 for the
    same input, so a test that only asserts the resulting key cannot tell an
    exact coordinate from a lucky guess. This one pins the *mechanism*: the
    coordinate must win **before** any assignment is consulted, which is
    observable as "a coordinate for a delivery that does not exist still yields
    that number, and stops".
    """

    def test_the_declared_delivery_wins_even_with_no_assignment_for_it(self):
        engine = self.engine()
        self.deliver()
        engine.run_once()
        index = engine.store.load_index()
        # 9300 is newer than the only delivery (9001), so the time guess cannot
        # produce it. Only level 1 can.
        self.assertEqual(
            engine._delivery_for(index, THREAD, 9200, declared=9300), 9300)
        self.assertEqual(
            engine._delivery_for(index, THREAD, 9200, declared=None), 9001,
            "the same input without a coordinate answers the guess")

    def test_the_coordinate_short_circuits_instead_of_tie_breaking(self):
        # The shape that separates "ignore the coordinate" from "use it only
        # when nothing else answers": the declared value is one **no** other
        # level could produce (older and smaller than every delivery), so only
        # a level-1 short circuit can return it.
        engine = self.engine()
        self.deliver()
        engine.run_once()
        index = engine.store.load_index()
        index["assignments"]["asg_1_worker_1"]["delivery_comment_id"] = 9300
        index["assignments"]["asg_1_worker_1"]["review_comment_id"] = 9200
        self.assertEqual(
            engine._delivery_for(index, THREAD, 9200, declared=5000), 5000,
            "a declared delivery wins even when it is older than the ledger")
        self.assertEqual(
            engine._delivery_for(index, THREAD, 9200, declared=None), 9300,
            "and without it the assignment lookup answers")

    def test_a_coordinate_is_not_rescued_by_the_assignment_lookup(self):
        engine = self.engine()
        self.deliver()
        engine.run_once()
        index = engine.store.load_index()
        # 9100 is the review of delivery 9001. A coordinate of 9300 must win
        # over what levels 3/4 would answer, proving level 1 short-circuits.
        self.assertEqual(
            engine._delivery_for(index, THREAD, 9100, declared=9300), 9300)


class Selftest(unittest.TestCase):
    """One command: ``python -m tests.test_engine_pm_route``."""

    def test_the_demo_inputs_parse_to_the_states_the_router_needs(self):
        states = [markers.parse_pm_verdict(b)["state"] for b in
                  (READY_V1, BLOCKED_V1, WORKING_V1, UNREADABLE)]
        self.assertEqual(states, ["ready", "blocked", "working", ""])


def _selftest() -> int:
    print("pm verdict -> engine route")
    for body, route in ((READY_V1, "event.agent.supervisor (release)"),
                        (BLOCKED_V1, "event.agent.supervisor (exception)"),
                        (WORKING_V1, "no frame"),
                        (READY, "event.agent.supervisor (release, legacy)"),
                        (UNREADABLE, "event.agent.supervisor (exception)")):
        out = markers.parse_pm_verdict(body)
        print(f"  {body}\n    -> state={out['state']!r} source={out['source']!r} "
              f"route={route}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
