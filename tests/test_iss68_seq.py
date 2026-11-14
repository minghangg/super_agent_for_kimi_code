"""ISS-68: a verdict must reach the assignment that is waiting for it.

The two halves of the failure are the same tree:

* **E59** - the pass-word table is ``通过``/``有条件通过``/``pass``. A QA that
  writes ``可过`` gets no verdict, and ``on_review`` parks the delivery in
  ``waiting_person``.
* **E58** - the candidate filter did not list the states a verdict could be
  stuck in. The corrected ``通过`` then found no assignment, became
  ``review.unmatched``, and the release never rang. E59 creates the state, E58
  makes it terminal.

Every test here drives the **real round** (``Engine.run_once``) from a session
stream and comments, so the states are ones the engine can actually build.

Rows:

* **A** - ``可过`` parks; a later ``通过`` releases: one supervisor frame, one
  PM frame, a ``release_frames`` key, no ``review.unmatched``;
* **A-negative** - ``可过`` then ``可关单`` stays parked with no frame at all;
* **B** - the normal order is word for word unchanged, and at-most-once holds;
* **C** - the word table is not loosened;
* **D** - no delivery on record is still unmatched;
* **D'** - ``close_reconcile`` is still unmatched;
* **E** - a refused verdict in a hold state writes a visible ``[user]`` event.

Run: ``python -m pytest tests/test_iss68_seq.py -q``
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from super_agent import assignments as asg
from super_agent import config, markers, session_events
from super_agent.allowlist import ProjectAllowlist
from super_agent.engine import REVIEW_TARGET_STATES, Engine
from super_agent.store import Store, action_id

THREAD = "owner/demo#1"
ASG = "asg_1_worker_1"
SUP = "event.agent.supervisor"
PM = "event.agent.project_pm"
UNREADABLE = "[QA] 结论：可过"
PASS_LINE = "[QA] 结论：通过 <!-- fix_needed: false -->"
HOLD_LINE = "[QA] 结论：可关单"
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

    def count(self, event_type):
        return self.events().count(event_type)

    def count_for(self, event_type, cid):
        """Frames of one kind about one comment.

        A QA comment is routed to the PM whatever its verdict reads, so a
        thread with two verdicts has two PM frames. Counting per comment is
        the only way to ask "did *this* release ring once".
        """
        return sum(1 for e, payload in self.injected
                   if e == event_type
                   and int(payload.get("review_comment_id") or 0) == int(cid))


class FakeSessions:
    def __init__(self):
        self.archived = set()
        self.archive_calls = []

    def archive_session(self, sid):
        self.archive_calls.append(sid)
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

    def bind_both(self):
        rows = [
            {"seq": 1, "at": "2026-11-14T05:00:00+00:00",
             "type": session_events.CREATED, "sid": "session_w1",
             "key": "wk-probe-1"},
            {"seq": 2, "at": "2026-11-14T05:00:01+00:00",
             "type": session_events.BOUND, "sid": "session_w1", "key": THREAD,
             "role": "worker", "issue": 1},
            {"seq": 3, "at": "2026-11-14T05:00:02+00:00",
             "type": session_events.CREATED, "sid": "session_q1",
             "key": "qa-demo"},
            {"seq": 4, "at": "2026-11-14T05:00:03+00:00",
             "type": session_events.BOUND, "sid": "session_q1", "key": THREAD,
             "role": "qa", "issue": 1},
        ]
        self.events.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                               encoding="utf-8")

    def engine(self) -> Engine:
        cfg = config.load(None, {
            "mode": "assist",
            "github": {"source": "none", "repo": "owner/demo"},
            "engine": {"session_events_path": str(self.events),
                       "allowlist": ["issue_comment.created"],
                       "supervisor_sid": "session_sup"},
        })
        engine = Engine(cfg, Store(self.state), self.source, self.bus,
                        project_allowlist=ProjectAllowlist(self.allow),
                        session_api=self.sessions)
        # Close reconciliation needs a provable Issue state these fakes lack;
        # it is exercised in tests/test_close_reconcile.py.
        engine._round_closed_threads = lambda index: []
        return engine

    def rows(self):
        path = self.state / "events.jsonl"
        if not path.is_file():
            return []
        return [json.loads(x)
                for x in path.read_text("utf-8").splitlines() if x.strip()]

    def types(self):
        return [r["type"] for r in self.rows()]

    def index(self, engine):
        return engine.store.load_index()

    def rec(self, engine):
        return self.index(engine)["assignments"][ASG]

    def deliver_then_park(self, *, verdict=UNREADABLE):
        """The real chain: deliver, judge badly, let the round finish.

        ``可过`` is unreadable, so ``on_review`` writes ``waiting_person`` -
        exactly the state #65 was in. The archive pass then declines to archive
        (``qa_gate`` has no pass), which is the other half of "the assignment is
        still open".
        """
        self.bind_both()
        self.source.add(9001, "[WK:wk-probe-1] done")
        self.source.add(9100, verdict, author="qa")
        engine = self.engine()
        engine.run_once()
        return engine

    def feed(self, engine, cid, body, *, author="qa"):
        """One more comment, one more round."""
        self.source.add(cid, body, author=author)
        engine.run_once()
        return engine

    def edit(self, engine, cid, body):
        """Edit a comment in place and rewind the cursor so the round sees it.

        This is what a QA does with ``gh issue comment --edit``: the id does not
        move, so the cursor must be rewound for the engine to read it again -
        which is exactly the shape that used to deadlock.
        """
        for row in self.source.rows:
            if int(row["id"]) == int(cid):
                row["body"] = body
                break
        cursor = engine.store.load_cursor()
        issues = cursor.setdefault("issues", {})
        issues[THREAD] = min(int(issues.get(THREAD) or 0), int(cid) - 1)
        engine.store.save_cursor(cursor)
        engine.run_once()
        return engine


class VariantA_RealChain(Base):
    """Variant A: the chain #65 actually walked, end to end."""

    def test_unreadable_parks_and_a_later_pass_releases(self):
        engine = self.deliver_then_park()
        parked = self.rec(engine)
        self.assertEqual(parked["status"], "waiting_person")
        self.assertEqual(parked.get("qa_verdict") or "", "")
        self.assertEqual(self.bus.count(SUP), 0)

        self.feed(engine, 9200, PASS_LINE)
        rec = self.rec(engine)
        self.assertEqual(rec["status"], "waiting_release")
        self.assertEqual(rec["qa_verdict"], "pass")
        self.assertEqual(rec["review_comment_id"], 9200)
        self.assertNotIn("review.unmatched", self.types())
        self.assertNotIn("assignment.qa_refused_after_hold", self.types())

    def test_the_release_frame_lands_exactly_once(self):
        engine = self.deliver_then_park()
        self.feed(engine, 9200, PASS_LINE)
        self.assertEqual(self.bus.count_for(SUP, 9200), 1)
        self.assertEqual(self.bus.count_for(PM, 9200), 1)
        # The parked comment was still routed to the PM (a verdict is a
        # verdict), but it never reached the supervisor: it has no pass.
        self.assertEqual(self.bus.count_for(SUP, 9100), 0)
        frames = self.index(engine).get("release_frames") or {}
        self.assertEqual(sorted(frames), [f"release:{THREAD}:9001"])

    def test_a_second_round_does_not_ring_again(self):
        engine = self.deliver_then_park()
        self.feed(engine, 9200, PASS_LINE)
        engine.run_once()
        self.assertEqual(self.bus.count_for(SUP, 9200), 1)
        self.assertEqual(self.bus.count_for(PM, 9200), 1)

    def test_the_parked_comment_is_the_watermark(self):
        # The unreadable comment still counts as answered: it left the thread
        # in the state that says so, and a replay of it must not look newer.
        engine = self.deliver_then_park()
        self.assertEqual(self.rec(engine)["last_review_comment_id"], 9100)

    def test_a_failed_archive_also_leaves_a_reachable_state(self):
        # The other way into the hole: the archive ran and failed, so the
        # assignment is ``blocked`` with a delivery on record. The verdict must
        # still reach it. (This path never changed; pinned so it cannot.)
        self.sessions_archive_broken()
        engine = self.deliver_then_park(verdict=PASS_LINE)
        self.assertEqual(self.rec(engine)["status"], "blocked")
        self.feed(engine, 9200, PASS_LINE)
        self.assertNotIn("review.unmatched", self.types())

    def sessions_archive_broken(self):
        class Broken(FakeSessions):
            def archive_session(self, sid):
                self.archive_calls.append(sid)
                return False, {"error": "boom"}

            def session_info(self, sid):
                return {"archived": False}
        self.sessions = Broken()


class VariantANegative_FailClosed(Base):
    """The state is reachable now; that must not make an unreadable line work."""

    def test_a_second_unreadable_verdict_stays_parked(self):
        engine = self.deliver_then_park()
        self.feed(engine, 9200, HOLD_LINE)
        rec = self.rec(engine)
        self.assertEqual(rec["status"], "waiting_person")
        self.assertEqual(rec.get("qa_verdict") or "", "")
        # No release, in either direction: the pass word table never matched, so
        # the supervisor was never told anything.
        self.assertEqual(self.bus.count(SUP), 0)
        self.assertEqual(self.bus.count_for(SUP, 9200), 0)

    def test_the_refusal_is_visible_not_silent(self):
        engine = self.deliver_then_park()
        self.feed(engine, 9200, HOLD_LINE)
        rows = [r for r in self.rows()
                if r["type"] == "assignment.qa_refused_after_hold"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["comment_id"], 9200)
        self.assertEqual(rows[0]["status"], "waiting_person")
        self.assertEqual(rows[0]["consumers"], ["user"])
        self.assertIn("no readable verdict", rows[0]["reason"])

    def test_an_older_readable_verdict_cannot_promote(self):
        engine = self.deliver_then_park()
        # A comment below the watermark: history, not a decision.
        self.feed(engine, 9002, PASS_LINE)
        self.assertEqual(self.rec(engine)["status"], "waiting_person")
        self.assertEqual(self.bus.count(SUP), 0)

    def test_an_unreadable_replay_changes_nothing(self):
        engine = self.deliver_then_park()
        rec = self.rec(engine)
        moved = asg.on_review(rec, {"verdict": ""}, review_cid=9100)
        self.assertFalse(moved["changed"])
        self.assertEqual(rec["status"], "waiting_person")


class VariantB_NormalOrderUnchanged(Base):
    """Variant B: QA before the archive - the old path, word for word."""

    def test_pass_before_archive_takes_the_same_route_once(self):
        self.bind_both()
        self.source.add(9001, "[WK:wk-probe-1] done")
        self.source.add(9100, PASS_LINE, author="qa")
        engine = self.engine()
        engine.run_once()
        rec = self.rec(engine)
        self.assertEqual(rec["status"], "waiting_release")
        self.assertEqual(rec["qa_verdict"], "pass")
        self.assertEqual(rec["review_comment_id"], 9100)
        self.assertEqual(self.bus.count(PM), 1)
        self.assertEqual(self.bus.count(SUP), 1)
        base = action_id(f"route:{THREAD}:9100", "bus_inject", "9100")
        self.assertEqual(engine.store.action_attempted(base), 1)

    def test_a_second_round_does_not_redispatch(self):
        self.bind_both()
        self.source.add(9001, "[WK:wk-probe-1] done")
        self.source.add(9100, PASS_LINE, author="qa")
        engine = self.engine()
        engine.run_once()
        engine.run_once()
        base = action_id(f"route:{THREAD}:9100", "bus_inject", "9100")
        self.assertEqual(engine.store.action_attempted(base), 1)
        self.assertEqual(self.bus.count(PM), 1)
        self.assertEqual(self.bus.count(SUP), 1)
        routes = [k for k in self.index(engine).get("routes") or {}
                  if k.startswith(f"route:{THREAD}:")]
        self.assertEqual(routes, [f"route:{THREAD}:9100"])

    def test_a_reject_before_archive_still_returns_the_worker(self):
        self.bind_both()
        self.source.add(9001, "[WK:wk-probe-1] done")
        self.source.add(9100, "[QA] 结论：不通过 <!-- fix_needed: true -->",
                        author="qa")
        engine = self.engine()
        engine.run_once()
        rec = self.rec(engine)
        self.assertEqual(rec["status"], "working")
        self.assertEqual(rec["qa_verdict"], "reject")
        self.assertEqual(self.bus.count(SUP), 0)


class VariantC_WordTableNotLoosened(unittest.TestCase):
    """Variant C: E59 is a charter fix, so the engine table must not move."""

    def test_ke_guo_is_still_unreadable(self):
        got = markers.parse_review(UNREADABLE)
        self.assertIsNotNone(got)
        self.assertEqual(got["verdict"], "")

    def test_ke_guan_dan_is_still_unreadable(self):
        self.assertEqual(markers.parse_review(HOLD_LINE)["verdict"], "")

    def test_tong_guo_is_a_pass(self):
        self.assertEqual(markers.parse_review(PASS_LINE)["verdict"], "pass")

    def test_you_tiao_jian_tong_guo_is_a_pass_with_work_left(self):
        got = markers.parse_review("[QA] 结论：有条件通过 <!-- fix_needed: true -->")
        self.assertEqual(got["verdict"], "pass")
        self.assertTrue(got["fix_needed"])

    def test_pass_words_are_exactly_the_documented_three(self):
        self.assertEqual(markers.PASS_WORDS, ("有条件通过", "通过", "pass"))


class VariantD_NoDeliveryNoCandidate(Base):
    """Variant D: the delivery requirement is what the widening must not touch."""

    def test_no_delivery_still_unmatched_in_every_candidate_state(self):
        for status in REVIEW_TARGET_STATES:
            with self.subTest(status=status):
                self.setUp()
                self.bind_both()
                engine = self.engine()
                index = self.index(engine)
                index.setdefault("assignments", {})[ASG] = {
                    "id": ASG, "thread": THREAD, "issue": 1, "role": "worker",
                    "sid": "session_w1", "session_key": "wk-probe-1",
                    "status": status, "qa_verdict": "",
                    "delivery_comment_id": None, "tombstone": {},
                }
                engine.store.save_index(index)
                self.source.add(9200, PASS_LINE, author="qa")
                summary = engine.run_once()
                self.assertEqual(summary["needs_person"], 1)
                self.assertIn("review.unmatched", self.types())
                self.assertEqual(self.rec(engine)["status"], status)
                self.tearDown()

    def test_the_state_list_is_the_documented_one(self):
        self.assertEqual(REVIEW_TARGET_STATES,
                         ("working", "waiting_review", "blocked",
                          "waiting_release", "waiting_person"))

    def test_close_reconcile_is_not_a_candidate(self):
        self.bind_both()
        engine = self.engine()
        index = self.index(engine)
        index.setdefault("assignments", {})[ASG] = {
            "id": ASG, "thread": THREAD, "issue": 1, "role": "worker",
            "sid": "session_w1", "session_key": "wk-probe-1",
            "status": "close_reconcile", "qa_verdict": "pass",
            "delivery_comment_id": 9001, "review_comment_id": 9100,
            "last_review_comment_id": 9100, "tombstone": {"archived": True},
        }
        engine.store.save_index(index)
        self.source.add(9200, PASS_LINE, author="qa")
        engine.run_once()
        self.assertIn("review.unmatched", self.types())
        self.assertEqual(self.rec(engine)["status"], "close_reconcile")


class VariantE_HoldRefusalIsVisible(Base):
    """Variant E: a held assignment that refuses a verdict says so out loud."""

    def test_a_reject_after_the_archive_is_recorded(self):
        self.bind_both()
        self.source.add(9001, "[WK:wk-probe-1] done")
        self.source.add(9100, PASS_LINE, author="qa")
        engine = self.engine()
        engine.run_once()
        self.assertEqual(self.rec(engine)["status"], "waiting_release")
        self.feed(engine, 9200, "[QA] 结论：不通过 <!-- fix_needed: true -->")
        rec = self.rec(engine)
        self.assertEqual(rec["status"], "waiting_release")
        self.assertEqual(rec["qa_verdict"], "pass")
        rows = [r for r in self.rows()
                if r["type"] == "assignment.qa_refused_after_hold"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["comment_id"], 9200)
        self.assertEqual(rows[0]["verdict"], "reject")
        self.assertEqual(rows[0]["reason"], "review after the hold state")
        self.assertEqual(rows[0]["consumers"], ["user"])

    def test_completed_is_still_unmatched_which_is_also_visible(self):
        # ``completed`` is not a candidate on purpose: a late event there is
        # audited, never restarted. Its refusal is visible through
        # ``review.unmatched`` + ``needs_person`` - the state before this issue,
        # for every state. Nothing here made it quieter.
        self.bind_both()
        engine = self.engine()
        index = self.index(engine)
        index.setdefault("assignments", {})[ASG] = {
            "id": ASG, "thread": THREAD, "issue": 1, "role": "worker",
            "sid": "session_w1", "session_key": "wk-probe-1",
            "status": "completed", "qa_verdict": "pass",
            "delivery_comment_id": 9001, "review_comment_id": 9100,
            "last_review_comment_id": 9100, "tombstone": {"archived": True},
        }
        engine.store.save_index(index)
        self.source.add(9200, PASS_LINE, author="qa")
        summary = engine.run_once()
        self.assertIn("review.unmatched", self.types())
        self.assertEqual(summary["needs_person"], 1)
        self.assertEqual(self.rec(engine)["status"], "completed")


class VariantE1_EditInPlaceDeadlock(Base):
    """E1: editing the parked comment used to ring the bell and never close."""

    def test_editing_the_same_cid_to_pass_releases_and_closes(self):
        engine = self.deliver_then_park()
        self.assertEqual(self.rec(engine)["status"], "waiting_person")
        # Same comment, same id, readable now.
        self.edit(engine, 9100, PASS_LINE)
        rec = self.rec(engine)
        self.assertEqual(rec["status"], "waiting_release")
        self.assertEqual(rec["qa_verdict"], "pass")
        self.assertEqual(rec["review_comment_id"], 9100)
        # The gate: the whole point of the deadlock was that this refused.
        ok, why = asg.qa_gate(rec)
        self.assertTrue(ok, why)
        index = self.index(engine)
        done = asg.complete(index["assignments"][ASG])
        self.assertTrue(done["changed"], done.get("reason"))
        self.assertEqual(index["assignments"][ASG]["status"], "completed")

    def test_the_frame_is_not_sent_for_a_refused_verdict(self):
        # Before the fix the route ran whatever ``changed`` said, so a refused
        # verdict still told the supervisor a release was ready.
        engine = self.deliver_then_park()
        before = self.bus.count(SUP)
        self.feed(engine, 9200, HOLD_LINE)
        self.assertEqual(self.bus.count(SUP), before)
        frames = self.index(engine).get("release_frames") or {}
        self.assertEqual(frames, {})

    def test_a_replay_of_the_edited_comment_changes_nothing(self):
        engine = self.deliver_then_park()
        self.edit(engine, 9100, PASS_LINE)
        sup = self.bus.count(SUP)
        events = self.types().count("assignment.qa_review")
        engine.run_once()
        self.assertEqual(self.rec(engine)["status"], "waiting_release")
        self.assertEqual(self.bus.count(SUP), sup)
        self.assertEqual(self.types().count("assignment.qa_review"), events)


class VariantP1_RefusalVisibility(Base):
    """P1-2/P1-3: a refusal must be in the queue a person reads, once."""

    def test_stale_pass_writes_both_events(self):
        # A ``waiting_person`` assignment whose delivery is newer than the
        # review: the verdict is stale, and stale must still be in the refused
        # bucket - ``review.stale`` is a doctor row, not a person's row.
        self.bind_both()
        engine = self.engine()
        index = self.index(engine)
        index.setdefault("assignments", {})[ASG] = {
            "id": ASG, "thread": THREAD, "issue": 1, "role": "worker",
            "sid": "session_w1", "session_key": "wk-probe-1",
            "status": "waiting_person", "qa_verdict": "",
            "delivery_comment_id": 9300, "last_review_comment_id": 9100,
            "tombstone": {}}
        engine.store.save_index(index)
        self.source.add(9200, PASS_LINE, author="qa")
        engine.run_once()
        types = self.types()
        self.assertIn("review.stale", types)
        self.assertIn("assignment.qa_refused_after_hold", types)

    def test_the_refusal_reaches_needs_person(self):
        engine = self.deliver_then_park()
        self.feed(engine, 9200, HOLD_LINE)
        rows = (self.index(engine)["issues"][THREAD].get("needs_person") or [])
        mine = [r for r in rows if int(r.get("comment_id") or 0) == 9200]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["reason"], "review.refused_after_hold [user]")

    def test_three_reads_give_one_row_and_one_event(self):
        engine = self.deliver_then_park()
        self.feed(engine, 9200, HOLD_LINE)
        for _ in range(2):
            cursor = engine.store.load_cursor()
            cursor["issues"][THREAD] = 9199
            engine.store.save_cursor(cursor)
            engine.run_once()
        rows = (self.index(engine)["issues"][THREAD].get("needs_person") or [])
        mine = [r for r in rows if int(r.get("comment_id") or 0) == 9200]
        self.assertEqual(len(mine), 1)
        self.assertEqual(self.types().count("assignment.qa_refused_after_hold"), 1)

    def test_the_summary_counter_is_untouched(self):
        # ``summary["needs_person"]`` counts triage escalations, not queue rows:
        # a refused verdict is routed, not escalated. This pins that the new
        # row did not move the number.
        engine = self.deliver_then_park()
        self.source.add(9200, HOLD_LINE, author="qa")
        summary = engine.run_once()
        self.assertEqual(summary["needs_person"], 0)


class VariantE2_OlderCommentEdit(Base):
    """#65's real shape: the comment that gets fixed is **below** the watermark.

    The QA parks the thread, later comments move the watermark past it, and the
    fix lands on the parked comment itself - an id that is *older* than
    ``last_review_comment_id``. A "must be newer" rule refuses exactly this.
    """

    def park_with_three_reviews(self):
        """Park on 9100, then read 9200 and 9300 so the watermark moves past it."""
        self.bind_both()
        self.source.add(9001, "[WK:wk-probe-1] done")
        self.source.add(9100, UNREADABLE, author="qa")
        self.source.add(9200, HOLD_LINE, author="qa")
        self.source.add(9300, HOLD_LINE, author="qa")
        engine = self.engine()
        engine.run_once()
        return engine

    def test_editing_an_older_comment_still_releases(self):
        engine = self.park_with_three_reviews()
        rec = self.rec(engine)
        self.assertEqual(rec["status"], "waiting_person")
        self.assertEqual(int(rec["last_review_comment_id"]), 9300)
        # Now fix the **oldest** of the three, which is below the watermark.
        self.edit(engine, 9100, PASS_LINE)
        rec = self.rec(engine)
        self.assertEqual(rec["status"], "waiting_release")
        self.assertEqual(rec["qa_verdict"], "pass")
        self.assertEqual(int(rec["review_comment_id"]), 9100)
        ok, why = asg.qa_gate(rec)
        self.assertTrue(ok, why)
        index = self.index(engine)
        self.assertTrue(asg.complete(index["assignments"][ASG])["changed"])

    def test_an_older_comment_that_did_not_change_is_still_refused(self):
        # The exception is the **edit**, not the age: re-reading 9100 unchanged
        # must not promote the thread (nothing about it moved).
        engine = self.park_with_three_reviews()
        cursor = engine.store.load_cursor()
        cursor["issues"][THREAD] = 9099
        engine.store.save_cursor(cursor)
        engine.run_once()
        self.assertEqual(self.rec(engine)["status"], "waiting_person")
        self.assertIn("assignment.qa_refused_after_hold", self.types())


class VariantE3_ReleaseEditAndUndo(Base):
    """The same edit inside ``waiting_release``, and an undo that leaves a trace."""

    def test_editing_the_comment_inside_waiting_release_still_records_the_pass(self):
        # A hold with no pass on record is what the archive leaves when a
        # verdict was never readable; editing that comment must fill the gate.
        self.bind_both()
        engine = self.engine()
        index = self.index(engine)
        index.setdefault("assignments", {})[ASG] = {
            "id": ASG, "thread": THREAD, "issue": 1, "role": "worker",
            "sid": "session_w1", "session_key": "wk-probe-1",
            "status": "waiting_release", "qa_verdict": "",
            "delivery_comment_id": 9001, "review_comment_id": None,
            "last_review_comment_id": 9100,
            "review_readings": {"9100": ""},
            "tombstone": {"archived": True}}
        engine.store.save_index(index)
        self.source.add(9100, PASS_LINE, author="qa")
        engine.run_once()
        rec = self.rec(engine)
        self.assertEqual(rec["status"], "waiting_release")
        self.assertEqual(rec["qa_verdict"], "pass")
        self.assertTrue(asg.qa_gate(rec)[0])

    def test_turning_a_pass_into_a_reject_leaves_a_trace(self):
        engine = self.deliver_then_park()
        self.edit(engine, 9100, PASS_LINE)
        self.assertEqual(self.rec(engine)["qa_verdict"], "pass")
        # Undo: the same comment now says 不通过.
        self.edit(engine, 9100, "[QA] 结论：不通过 <!-- fix_needed: true -->")
        rec = self.rec(engine)
        types = self.types()
        # A hold cannot be un-archived by a comment, so the reject is refused -
        # but it is **not** silent: event, queue row and PM frame all exist.
        self.assertEqual(rec["status"], "waiting_release")
        self.assertIn("assignment.qa_refused_after_hold", types)
        self.assertIn("route.refused_decided", types)
        rows = (self.index(engine)["issues"][THREAD].get("needs_person") or [])
        mine = [r for r in rows if int(r.get("comment_id") or 0) == 9100]
        self.assertTrue(mine, "the undo must be queued for a person")

    def test_the_supervisor_is_not_woken_by_a_refusal(self):
        engine = self.deliver_then_park()
        self.edit(engine, 9100, PASS_LINE)
        sup = self.bus.count(SUP)
        self.edit(engine, 9100, "[QA] 结论：不通过 <!-- fix_needed: true -->")
        self.assertEqual(self.bus.count(SUP), sup)
        frames = self.index(engine).get("release_frames") or {}
        self.assertEqual(sorted(frames), [f"release:{THREAD}:9001"])

    def test_the_pm_hears_about_the_refusal(self):
        engine = self.deliver_then_park()
        self.edit(engine, 9100, PASS_LINE)
        before = self.bus.count(PM)
        self.edit(engine, 9100, "[QA] 结论：不通过 <!-- fix_needed: true -->")
        self.assertGreater(self.bus.count(PM), before)
        refused = [p for e, p in self.bus.injected
                   if e == PM and p.get("refused")]
        self.assertTrue(refused, "the PM must see refused: true")
        self.assertEqual(refused[-1]["review_comment_id"], 9100)


class VariantE4_CleanupOnCompleted(Base):
    """A completed thread must stop asking for a person."""

    def test_completing_the_thread_clears_the_refused_rows(self):
        engine = self.deliver_then_park()
        self.feed(engine, 9200, HOLD_LINE)
        index = self.index(engine)
        self.assertTrue(index.get("review_refused"))
        rec = index["assignments"][ASG]
        # Drive the thread to ``completed`` the way the reconciler does: the
        # four conditions are stubbed green, the two-round streak is honoured.
        rec["qa_verdict"] = "pass"
        rec["status"] = "waiting_release"
        engine.store.save_index(index)
        engine.close_conditions = lambda thread, idx: {
            "conditions": {"issue_closed": True, "binding_cleared": True,
                           "sid_archived": True, "no_running_task": True},
            "detail": {}}
        engine._round_closed_threads = lambda idx: [
            engine._reconcile_thread(idx, THREAD)]
        engine.run_once()
        engine.run_once()
        index = self.index(engine)
        self.assertEqual(self.rec(engine)["status"], "completed")
        self.assertEqual(index.get("review_refused") or {}, {})
        rows = (index["issues"][THREAD].get("needs_person") or [])
        self.assertEqual([r for r in rows
                          if str(r.get("reason") or "").startswith(
                              "review.refused_after_hold")], [])

    def test_another_threads_rows_survive(self):
        engine = self.deliver_then_park()
        self.feed(engine, 9200, HOLD_LINE)
        index = self.index(engine)
        index.setdefault("review_refused", {})["refused:other/repo#9:1:x"] = {
            "thread": "other/repo#9", "comment_id": 1, "reason": "x"}
        engine.store.save_index(index)
        engine._clear_refused(index, THREAD)
        left = index.get("review_refused") or {}
        self.assertEqual(sorted(left), ["refused:other/repo#9:1:x"])


class VariantE5_ReplayIsNotNews(Base):
    """A re-read comment that did not change must not add rows or frames."""

    def test_rereading_answered_comments_adds_nothing(self):
        engine = self.deliver_then_park()
        self.source.add(9200, HOLD_LINE, author="qa")
        self.source.add(9300, HOLD_LINE, author="qa")
        engine.run_once()
        index = self.index(engine)
        before = (len(index.get("review_refused") or {}),
                  len(index["issues"][THREAD]["needs_person"]),
                  self.bus.count(PM))
        # Rewind past all three verdicts: the round reads them again unchanged.
        cursor = engine.store.load_cursor()
        cursor["issues"][THREAD] = 9099
        engine.store.save_cursor(cursor)
        engine.run_once()
        index = self.index(engine)
        after = (len(index.get("review_refused") or {}),
                 len(index["issues"][THREAD]["needs_person"]),
                 self.bus.count(PM))
        self.assertEqual(after, before)
        self.assertEqual(self.rec(engine)["status"], "waiting_person")

    def test_an_edited_comment_settles_its_own_refusal(self):
        # A real refusal, then the fix: the comment is first read while the
        # assignment already holds a pass, so it cannot be applied (a hold takes
        # no reject); editing it to a pass settles that row.
        self.bind_both()
        engine = self.engine()
        self.source.add(9001, "[WK:wk-probe-1] done")
        self.source.add(9100, PASS_LINE, author="qa")
        engine.run_once()
        self.assertEqual(self.rec(engine)["status"], "waiting_release")
        # First reading of 9200: a reject in a hold state, refused and queued.
        self.feed(engine, 9200, "[QA] 结论：不通过 <!-- fix_needed: true -->")
        index = self.index(engine)
        mine = [k for k in (index.get("review_refused") or {})
                if k.startswith(f"refused:{THREAD}:9200:")]
        self.assertTrue(mine, "the reject was refused and queued")
        # The QA fixes that same comment into a pass: its refusal settles.
        self.edit(engine, 9200, PASS_LINE)
        index = self.index(engine)
        left = [k for k in (index.get("review_refused") or {})
                if k.startswith(f"refused:{THREAD}:9200:")]
        self.assertEqual(left, [])
        rows = [r for r in index["issues"][THREAD]["needs_person"]
                if str(r.get("comment_id")) == "9200"]
        self.assertEqual(rows, [])
        self.assertEqual(self.rec(engine)["qa_verdict"], "pass")

    def park_two_reviews(self):
        self.bind_both()
        self.source.add(9001, "[WK:wk-probe-1] done")
        self.source.add(9100, UNREADABLE, author="qa")
        self.source.add(9200, HOLD_LINE, author="qa")
        engine = self.engine()
        engine.run_once()
        return engine


class HoldStateRules(unittest.TestCase):
    """The two exits, on records built in memory (the state machine alone)."""

    @staticmethod
    def record(**over):
        rec = {"id": ASG, "thread": THREAD, "role": "worker",
               "sid": "session_w1", "status": "waiting_release",
               "delivery_comment_id": 9001, "last_review_comment_id": 9100,
               "review_comment_id": 9100, "qa_verdict": "pass",
               "tombstone": {"archived": True}}
        rec.update(over)
        return rec

    # --- waiting_release ---------------------------------------------------
    def test_a_late_pass_is_recorded_and_refused(self):
        rec = self.record(qa_verdict="", review_comment_id=None)
        out = asg.on_review(rec, {"verdict": "pass"}, review_cid=9200)
        self.assertTrue(out["changed"])
        self.assertEqual(rec["status"], "waiting_release")
        self.assertEqual(rec["qa_verdict"], "pass")
        self.assertEqual(rec["review_comment_id"], 9200)

    def test_a_reject_there_is_refused_with_a_flag(self):
        rec = self.record()
        out = asg.on_review(rec, {"verdict": "reject"}, review_cid=9200)
        self.assertFalse(out["changed"])
        self.assertTrue(out["refused"])
        self.assertEqual(rec["status"], "waiting_release")
        self.assertEqual(rec["qa_verdict"], "pass")

    def test_close_reconcile_and_completed_refuse_with_a_flag(self):
        for status in ("close_reconcile", "completed"):
            rec = self.record(status=status)
            out = asg.on_review(rec, {"verdict": "pass"}, review_cid=9200)
            self.assertFalse(out["changed"])
            self.assertTrue(out["refused"])

    # --- waiting_person ----------------------------------------------------
    def test_a_newer_pass_leaves_waiting_person(self):
        rec = self.record(status="waiting_person", qa_verdict="")
        out = asg.on_review(rec, {"verdict": "pass"}, review_cid=9200)
        self.assertTrue(out["changed"])
        self.assertEqual(rec["status"], "waiting_release")
        self.assertEqual(rec["qa_verdict"], "pass")
        self.assertEqual(rec["review_comment_id"], 9200)

    def test_a_newer_reject_leaves_waiting_person(self):
        rec = self.record(status="waiting_person", qa_verdict="")
        out = asg.on_review(rec, {"verdict": "reject"}, review_cid=9200)
        self.assertEqual(rec["status"], "working")
        self.assertEqual(rec["qa_verdict"], "reject")

    def test_an_unreadable_verdict_stays_put_and_says_why(self):
        rec = self.record(status="waiting_person", qa_verdict="")
        out = asg.on_review(rec, {"verdict": ""}, review_cid=9200)
        self.assertFalse(out["changed"])
        self.assertTrue(out["refused"])
        self.assertEqual(out["reason"], "no readable verdict after waiting_person")
        self.assertEqual(rec["status"], "waiting_person")
        self.assertEqual(rec.get("qa_verdict") or "", "")

    def test_an_older_verdict_stays_put(self):
        rec = self.record(status="waiting_person", qa_verdict="")
        out = asg.on_review(rec, {"verdict": "pass"}, review_cid=9000)
        self.assertFalse(out["changed"])
        self.assertTrue(out["refused"])
        self.assertEqual(out["reason"], "review is not newer than the recorded one")

    def test_the_watermark_never_moves_backwards(self):
        rec = self.record(status="waiting_person", qa_verdict="")
        asg.on_review(rec, {"verdict": ""}, review_cid=9000)
        self.assertEqual(rec["last_review_comment_id"], 9100)

    def test_a_pass_older_than_the_delivery_is_stale(self):
        rec = self.record(status="waiting_person", qa_verdict="",
                          delivery_comment_id=9300)
        out = asg.on_review(rec, {"verdict": "pass"}, review_cid=9200)
        self.assertFalse(out["changed"])
        self.assertTrue(out["refused"])
        self.assertEqual(out["reason"], "review.stale")


if __name__ == "__main__":
    unittest.main()
