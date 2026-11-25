"""Bounded batches, backpressure, rebuild and round completeness (#130 D).

Seven counter-examples decide this file, and each one is written so the **wrong**
implementation fails it:

1. a full queue must not lose a comment - backpressure is a refusal, and the
   cursor does not move past a row that was not consumed;
2. ids that jump while the pages are complete must not trigger a rebuild - a
   deleted comment is not a hole;
3. a really missing page must trigger one;
4. ``rebuild_issue`` must not move the cursor before it has proved it read the
   whole listing;
5. a queue that still holds rows must not advance ``run_count`` - the counter is
   what the close streak and the stalls read;
6. a failed issue is not archived and not closed;
7. ``round_complete`` is the conjunction of collection and consumption - never
   collection alone.

Run: ``python -m pytest tests/test_iss130_batch_round.py -q``
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from super_agent import config
from super_agent.allowlist import ProjectAllowlist
from super_agent.batch import (BatchCursor, CollectedBatch, CollectedRound,
                               PageRecord, ScanPolicy, first_hole, merge_spans,
                               spans_contiguous)
from super_agent.batch_queue import Backpressure, BoundedBatchQueue, contiguous_with
from super_agent.engine import Engine
from super_agent.github_source import NullSource, PagedComments
from super_agent.paged_walk import walk_listing
from super_agent.store import Store

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


class Source:
    """A listing served page by page, walked by the **real** ``walk_listing``.

    The first delivery of this issue had its own ``paged`` that numbered pages
    from 1 upwards. The production walk does not: it reads page 1 to find
    ``rel="last"``, then walks back from the newest page and stops on the first
    page at/below the cursor. That difference is exactly why P0-1 (a stable
    false "missing page 2") passed its own tests - the fixture never produced
    the page list the engine actually sees (#130 QA §七). So the fixture serves
    pages like GitHub does and lets the real walk decide.
    """

    def __init__(self, ids, *, per_page=100, missing=(), complete=None,
                 contiguous=None):
        self.ids = list(ids)
        self.per_page = int(per_page)
        self.missing = set(missing)
        # ``None`` means "the walk's own answer stands". A fixture that forces a
        # default here would mask the very claims the engine under test reads.
        self.claimed_complete = complete
        self.claimed_contiguous = contiguous
        self.requests = []
        self.fail_issues = False

    def issues(self, limit=20):
        if self.fail_issues:
            raise RuntimeError("source down")
        return [1]

    def title(self, issue):
        return "probe"

    def flow(self, issue):
        return ""

    def issue_state(self, issue):
        return "open"

    def comments(self, issue):
        return self.paged(issue)

    def page_of(self, page):
        """``(rows_on_page, rows, headers)`` - one request, as GitHub answers."""
        start = (page - 1) * self.per_page
        chunk = self.ids[start:start + self.per_page]
        total = max(1, -(-len(self.ids) // self.per_page))
        headers = {}
        if page < total:
            headers["Link"] = (f'<https://api/issue/1/comments?page={page + 1}>; '
                               f'rel="next", <https://api/issue/1/comments'
                               f'?page={total}>; rel="last"')
        if page in self.missing:
            return 0, [], headers        # the request never arrived
        return (len(chunk),
                [{"id": int(cid), "issue": 1, "body": "", "author": ""}
                 for cid in chunk], headers)

    def paged(self, issue, *, since_id=0, max_pages=10):
        """The real walk over the fixture's pages.

        ``complete``/``contiguous`` are overrides for the cases where the
        *backend* states something the page list does not show - the shape a
        claim-based consumer must handle. Left as ``None`` the walk's own answer
        wins, which is what makes this fixture able to reproduce P0-1.
        """
        walked = walk_listing(self.page_of, page_size=self.per_page,
                              since_id=int(since_id or 0), max_pages=max_pages)
        if self.claimed_complete is not None:
            walked.pagination_complete = bool(self.claimed_complete)
        if self.claimed_contiguous is not None:
            walked.contiguous = bool(self.claimed_contiguous)
        return walked


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        allow = self.root / "allowlist.json"
        allow.write_text(json.dumps(GOOD), encoding="utf-8")
        self.allow = allow

    def tearDown(self):
        self.tmp.cleanup()

    def engine(self, source=None, **gh):
        cfg = config.load(None, {
            "mode": "assist",
            "github": {"source": "none", "repo": "owner/demo", **gh},
            "engine": {"archived_crosscheck": False},
        })
        return Engine(cfg, Store(self.state), source or NullSource(), None,
                      project_allowlist=ProjectAllowlist(self.allow))

    def cursor_of(self, engine):
        return engine.store.load_cursor()["issues"].get(THREAD)


class BatchData(unittest.TestCase):
    """The pure layer, where "ids jump" and "a page is missing" must differ."""

    def test_overlapping_and_abutting_spans_merge(self):
        self.assertEqual(merge_spans([(5, 7), (7, 3)]), [(3, 7)])
        self.assertEqual(merge_spans([(3, 7), (9, 9)]), [(3, 7), (9, 9)],
                         "page 8 is missing, so these are two runs")

    def test_one_run_is_contiguous_and_a_gap_is_not(self):
        self.assertTrue(spans_contiguous([(1, 3), (4, 6)]))
        self.assertFalse(spans_contiguous([(1, 3), (5, 6)]))
        self.assertEqual(first_hole([(1, 3), (5, 6)]), 4)

    def test_a_jumping_id_list_is_not_a_gap(self):
        """Counter-example 2 at the unit level: ids 1, 2, 9 are one page."""
        batch = CollectedBatch(THREAD, [{"id": 1}, {"id": 2}, {"id": 9}],
                               pages=[{"page": 1, "rows": 3}], complete=True)
        self.assertFalse(batch.needs_rebuild())
        self.assertTrue(batch.collection_complete)

    def test_a_missing_middle_page_is_a_gap(self):
        """Counter-example 3 at the unit level."""
        batch = CollectedBatch(THREAD, [{"id": 1}], complete=False,
                               pages=[{"page": 1, "rows": 1},
                                      {"page": 3, "rows": 1}])
        self.assertTrue(batch.needs_rebuild())
        self.assertEqual(batch.missing_page(), 2)

    def test_duplicate_ids_collapse(self):
        batch = CollectedBatch(THREAD, [{"id": 5}, {"id": 5}, {"id": 6}])
        self.assertEqual([row["id"] for row in batch.rows], [5, 6])

    def test_round_complete_is_both_halves(self):
        """Counter-example 7: collection alone is not a complete round."""
        round_result = CollectedRound(1)
        round_result.add(THREAD, CollectedBatch(THREAD, [{"id": 1}],
                                                complete=True))
        self.assertTrue(round_result.collection_complete)
        self.assertFalse(round_result.consumption_complete)
        self.assertFalse(round_result.round_complete)

    def test_cursor_prefix_never_jumps_a_pending_row(self):
        cursor = BatchCursor()
        cursor.observe(THREAD, [{"id": 10}, {"id": 11}, {"id": 12}])
        cursor.consume(THREAD, [10])
        self.assertEqual(cursor.value(THREAD), 10,
                         "11 is pending, so 12 may not be claimed")
        cursor.consume(THREAD, [11, 12])
        self.assertEqual(cursor.value(THREAD), 12)


class Queue(unittest.TestCase):
    def test_a_full_queue_refuses_instead_of_dropping(self):
        """Counter-example 1: nothing is dropped, the batch is refused whole."""
        q = BoundedBatchQueue(ScanPolicy(max_batches=1, max_batch_rows=100,
                                         max_issue_rows=100))
        q.put(CollectedBatch("a", [{"id": 1}], complete=True))
        with self.assertRaises(Backpressure) as caught:
            q.put(CollectedBatch("b", [{"id": 2}], complete=True))
        self.assertEqual(caught.exception.reason, "queue full")
        self.assertEqual(q.size(), 1, "the accepted batch is still queued")
        self.assertEqual(q.put(CollectedBatch("a", [{"id": 1}], complete=True)), 0,
                         "a re-offered row is not a second row")

    def test_the_per_issue_cap_refuses_only_that_issue(self):
        q = BoundedBatchQueue(ScanPolicy(max_batches=5, max_batch_rows=100,
                                         max_issue_rows=2))
        q.put(CollectedBatch("a", [{"id": 1}, {"id": 2}], complete=True))
        with self.assertRaises(Backpressure):
            q.put(CollectedBatch("a", [{"id": 3}], complete=True))
        self.assertEqual(q.put(CollectedBatch("b", [{"id": 9}], complete=True)), 1)

    def test_history_below_the_cursor_is_not_counted_as_work(self):
        """An AT read fetches the whole listing; its history is not backpressure."""
        q = BoundedBatchQueue(ScanPolicy(max_batches=5, max_batch_rows=3,
                                         max_issue_rows=3))
        batch = CollectedBatch("a", [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}],
                              cursor_before=3, complete=True)
        self.assertEqual(q.put(batch), 1, "only id 4 is above the cursor")

    def test_contiguous_with_answers_page_runs_not_id_runs(self):
        self.assertTrue(contiguous_with([(1, 3), (4, 6)]))
        self.assertFalse(contiguous_with([(1, 3), (5, 6)]))
        self.assertTrue(contiguous_with([(1, 4)], expect_from=1))
        self.assertFalse(contiguous_with([(2, 4)], expect_from=1))


class BackpressureInTheRound(Base):
    """Counter-examples 1 and 5 through the real round."""

    def engine_with_tiny_caps(self, source):
        cfg = config.load(None, {
            "mode": "assist",
            "github": {"source": "none", "repo": "owner/demo",
                       "max_comments_per_issue": 10, "max_comment_pages": 10,
                       "max_issue_rows": 1, "max_batch_rows": 1},
            "engine": {"archived_crosscheck": False},
        })
        return Engine(cfg, Store(self.state), source, None,
                      project_allowlist=ProjectAllowlist(self.allow))

    def test_a_refused_batch_does_not_move_the_cursor(self):
        """Counter-example 1: the rows stay above the cursor for the next round."""
        engine = self.engine_with_tiny_caps(Source([9001, 9002, 9003]))
        out = engine.run_once()
        self.assertTrue(out.get("backpressure"), "the cap must actually refuse")
        self.assertEqual(self.cursor_of(engine) or 0, 0,
                         "a refused batch was not consumed, so the cursor stays")
        self.assertEqual(out["processed"], 0)

    def test_an_incomplete_round_does_not_advance_run_count(self):
        """Counter-example 5."""
        engine = self.engine_with_tiny_caps(Source([9001, 9002]))
        out = engine.run_once()
        self.assertFalse(out["round_complete"])
        self.assertEqual(engine.store.load_index()["meta"].get("run_count", 0), 0)
        self.assertEqual(engine.store.load_index()["meta"].get("round_seq"), 1)

    def test_round_seq_advances_even_when_the_round_is_incomplete(self):
        engine = self.engine_with_tiny_caps(Source([9001, 9002]))
        engine.run_once()
        engine.run_once()
        self.assertEqual(engine.store.load_index()["meta"]["round_seq"], 2)

    def test_a_clean_round_advances_both_counters(self):
        engine = self.engine(Source([9001, 9002]))
        out = engine.run_once()
        self.assertTrue(out["round_complete"])
        meta = engine.store.load_index()["meta"]
        self.assertEqual(meta["run_count"], 1)
        self.assertEqual(meta["round_seq"], 1)
        self.assertEqual(self.cursor_of(engine), 9002)


class Starvation(Base):
    """Counter-example 1's other half: a bound must serve everyone, not a prefix.

    The first delivery bounded the queue but never consumed it, so the same
    ``max_batches`` issues held the slots every round and the tail's cursor
    stayed at 0 for ever. That is not backpressure (#130 QA P0-4).

    The fix is the consumer, not a rotation: with the slot released as soon as
    one issue is done, a round reads **every** live issue, so there is no
    unlucky tail to rotate. The tests below assert that, and assert the refusal
    itself still works when the cap is genuinely smaller than the work.
    """

    def engine_for_many(self, source, *, caps=8):
        cfg = config.load(None, {
            "mode": "assist",
            "github": {"source": "none", "repo": "owner/demo",
                       "max_issues": 20, "max_comments_per_issue": 50,
                       "max_comment_pages": 10, "max_batches": caps},
            "engine": {"archived_crosscheck": False},
        })
        return Engine(cfg, Store(self.state), source, None,
                      project_allowlist=ProjectAllowlist(self.allow))

    def test_every_issue_is_read_with_fewer_slots_than_issues(self):
        """20 live issues, 8 slots: all twenty are read in one round."""
        source = ManySource(20)
        engine = self.engine_for_many(source)
        engine.run_once()
        cursors = engine.store.load_cursor().get("issues") or {}
        read = {thread for thread, value in cursors.items() if int(value or 0) > 0}
        self.assertEqual(len(read), 20,
                         "the slot must be released per issue, not per round")
        self.assertEqual(source.reads[20], 1, "the last issue was read")

    def test_the_queue_is_emptied_by_the_consumer(self):
        """No consumer was the defect; the release is what fixes it."""
        source = ManySource(3)
        engine = self.engine_for_many(source, caps=8)
        engine.run_once()
        cursors = engine.store.load_cursor().get("issues") or {}
        self.assertTrue(all(int(v or 0) > 0 for v in cursors.values()),
                        "an accepted batch must be consumed and released")


class ManySource(Source):
    """``n`` independent issues, each with its own small comment listing."""

    def __init__(self, n, *, per_page=100):
        super().__init__(list(range(9001, 9001 + per_page)), per_page=per_page)
        self.n = int(n)
        self.reads = {issue: 0 for issue in range(1, self.n + 1)}
        self.issue_reads = 0

    def issues(self, limit=20):
        return list(range(1, self.n + 1))[:limit]

    def paged(self, issue, *, since_id=0, max_pages=10):
        self.reads[int(issue)] = self.reads.get(int(issue), 0) + 1
        rows = [{"id": 9001 + int(issue), "issue": int(issue), "body": "",
                 "author": ""}]
        return PagedComments(rows, complete=True, contiguous=True,
                            pages=[{"page": 1, "rows": 1, "status": "ok"}],
                            reason="fixture")


class GapDetection(Base):
    def test_a_jumping_id_list_does_not_rebuild(self):
        """Counter-example 2: page 1 is complete, ids 9001/9005/9009 are not."""
        source = Source([9001, 9005, 9009])
        engine = self.engine(source)
        out = engine.run_once()
        self.assertNotIn(THREAD, out.get("rebuild_needed") or [])
        self.assertEqual(self.cursor_of(engine), 9009)

    def test_the_real_walk_does_not_report_a_skipped_middle_page(self):
        """Counter-example 2 in the shape that actually occurs (QA P0-1).

        The walk reads page 1 for ``rel="last"`` and then walks back from the
        newest page, stopping on the first page at/below the cursor. On a
        six-page listing with a cursor inside page 3 the pages it returns are
        ``[1, 6, 5, 4, 3]`` - page 2 was never needed, and reading that list as
        a dense sequence reported a "missing page 2" on every active issue.
        """
        source = Source(list(range(9001, 9031)), per_page=5)
        walked = source.paged(1, since_id=9013)
        self.assertEqual([p["page"] for p in walked.pages], [1, 6, 5, 4, 3],
                         "the fixture must reproduce the production page order")
        batch = CollectedBatch(THREAD, list(walked), cursor_before=9013,
                              pages=list(walked.pages),
                              complete=walked.pagination_complete,
                              contiguous=walked.contiguous,
                              reason=walked.stop_reason)
        self.assertIsNone(batch.missing_page())
        self.assertFalse(batch.needs_rebuild())
        self.assertTrue(batch.collection_complete,
                        "the unread prefix was proved gap-free")

class EmptyIssueIsNotAFault(Base):
    """Counter-example 2's blind spot: a listing with no comments at all.

    ``walk_listing`` always probes page 1, so an issue nobody has commented on
    produces one legitimate, complete empty read. The first delivery of the
    page-gap judgement treated every zero-row page as a lost request, so a fresh
    issue paused itself on the very first round: ``read.paused`` was written, and
    the comments that arrived afterwards were never read until a person lifted
    the pause by hand. "New issue, no comments yet" is one of the most common
    states there is (#130 QA P0-A).
    """

    def test_an_empty_listing_is_complete(self):
        source = Source([])
        walked = source.paged(1, since_id=0)
        self.assertEqual([(p["page"], p["rows"]) for p in walked.pages],
                         [(1, 0)], "the walk must produce the real empty read")
        self.assertTrue(walked.pagination_complete)
        batch = CollectedBatch(THREAD, list(walked), cursor_before=0,
                              pages=list(walked.pages),
                              complete=walked.pagination_complete,
                              contiguous=walked.contiguous)
        self.assertIsNone(batch.missing_page())
        self.assertFalse(batch.needs_rebuild())
        self.assertTrue(batch.collection_complete)

    def test_an_empty_issue_stays_idle_and_reads_what_arrives_next(self):
        """The production path: empty round 1 must not pause round 2 out."""
        source = Source([])
        engine = self.engine(source)
        out = engine.run_once()
        self.assertEqual(out.get("rebuild_needed") or [], [],
                         "an empty listing is not evidence of a gap")
        rec = engine.store.load_index()["issues"][THREAD]
        self.assertFalse(rec.get("read_paused"),
                         "an issue with no comments is not a fault")
        self.assertNotEqual(rec.get("state"), "needs_person")

        source.ids.append(9001)
        out = engine.run_once()
        self.assertEqual(out["processed"], 1,
                         "the comment that arrived must be read")
        self.assertEqual(self.cursor_of(engine), 9001)

    def test_a_page_that_never_arrived_is_still_a_hole(self):
        """The empty-last-page exception must not excuse a middle page.

        Every page below ``last`` is full by construction, so a zero-row page in
        the middle is a lost request - not an empty tail. The floor the stop
        page provides only excuses pages **below** the cursor.
        """
        source = Source(list(range(9001, 9031)), per_page=5, missing=(3,))
        walked = source.paged(1, since_id=9013)
        self.assertIn(6, [p["page"] for p in walked.pages])
        batch = CollectedBatch(THREAD, list(walked), cursor_before=9013,
                              pages=list(walked.pages),
                              complete=walked.pagination_complete,
                              contiguous=walked.contiguous)
        self.assertEqual(batch.missing_page(), 3)
        self.assertTrue(batch.needs_rebuild())

    def test_a_missing_page_is_reported(self):
        """Counter-example 3: a hole **between** pages the walk did return.

        The backend claims the unread prefix is contiguous - the rows it did
        return reach past the cursor - while page 2 never arrived. That is the
        shape the ids cannot see: 9001 and 9101 look like neighbours.
        """
        source = Source(list(range(9001, 9201)), per_page=50, complete=False,
                        contiguous=True, missing=(2,))
        engine = self.engine(source)
        out = engine.run_once()
        self.assertIn(THREAD, out.get("rebuild_needed") or [])


class RebuildKeepsTheCursor(Base):
    """Counter-example 4."""

    def test_an_incomplete_rebuild_does_not_move_the_cursor(self):
        source = Source(list(range(9001, 9201)), per_page=50, complete=False,
                        missing=(3,))
        engine = self.engine(source)
        index = engine.store.load_index()
        index.setdefault("issues", {})[THREAD] = {"state": "tracked",
                                                 "comments_seen": 5}
        engine.store.save_index(index)
        engine.store.save_cursor({"version": 1, "issues": {THREAD: 9001}})
        before = self.cursor_of(engine)
        out = engine.rebuild_issue(THREAD, "probe")
        self.assertFalse(out["ok"])
        self.assertEqual(self.cursor_of(engine), before,
                         "an unproven rebuild must not move the cursor")
        rows = engine.store.load_index()["issues"][THREAD]["needs_person"]
        self.assertTrue(any("rebuild.incomplete" in str(r.get("reason"))
                            for r in rows), "the fault must be one visible row")

    def test_a_complete_rebuild_moves_it(self):
        engine = self.engine(Source([9001, 9002, 9003]))
        out = engine.rebuild_issue(THREAD, "probe")
        self.assertTrue(out["ok"])
        self.assertEqual(out["cursor_before"], 0)
        self.assertEqual(out["cursor_after"], 9003)
        self.assertEqual(self.cursor_of(engine), 9003)

    def test_the_pause_a_rebuild_writes_survives_the_round(self):
        """Counter-example 3, production path (#130 QA P0-2).

        ``rebuild_issue`` used to save its own index copy, which the round then
        overwrote with the copy it read at the start. The pause existed in the
        event log and nowhere a reader looks, and the next round read the issue
        again - "paused" was a false statement.
        """
        source = Source(list(range(9001, 9201)), per_page=50, missing=(3,))
        engine = self.engine(source)
        out = engine.run_once()
        self.assertIn(THREAD, out.get("rebuild_needed") or [])
        rec = engine.store.load_index()["issues"][THREAD]
        self.assertTrue(any("rebuild.incomplete" in str(row.get("reason"))
                            for row in rec.get("needs_person") or []),
                        "the pause must reach the index, not only the log")
        self.assertTrue(rec.get("read_paused"),
                        "the issue must stop being read until a person lifts it")

    def test_an_incomplete_rebuild_does_not_advance_the_cursor_in_a_round(self):
        """Counter-example 4, production path (#130 QA P0-3).

        The rebuild itself left the cursor alone; ``_round_issue`` then moved it
        to ``max(fresh)`` - the highest row of the very read that had just been
        ruled untrustworthy. The missing middle ends up below the cursor and no
        later round reads it again.
        """
        source = Source(list(range(9001, 9201)), per_page=50, missing=(3,))
        engine = self.engine(source)
        index = engine.store.load_index()
        index.setdefault("issues", {})[THREAD] = {"state": "tracked",
                                                 "comments_seen": 5}
        engine.store.save_index(index)
        engine.store.save_cursor({"version": 1, "issues": {THREAD: 9001}})
        out = engine.run_once()
        self.assertIn(THREAD, out.get("rebuild_needed") or [])
        self.assertEqual(self.cursor_of(engine), 9001,
                         "an untrusted read may not move the cursor")


class FailedIssueIsNotArchived(Base):
    """Counter-example 6."""

    def test_a_failed_source_keeps_the_allowlist_and_archives_nothing(self):
        source = Source([9001])
        source.fail_issues = True
        engine = self.engine(source)
        index = engine.store.load_index()
        index.setdefault("assignments", {})["asg_1_worker_1"] = {
            "id": "asg_1_worker_1", "thread": THREAD, "issue": 1,
            "role": "worker", "status": "waiting_review",
            "delivery_comment_id": 9001, "sid": "session_w1"}
        engine.store.save_index(index)
        out = engine.run_once()
        self.assertEqual(out.get("allowlist_kept"), "source failed this round")
        self.assertEqual(engine.store.load_index()["assignments"]
                         ["asg_1_worker_1"]["status"], "waiting_review",
                         "a thread whose read failed must not move")


if __name__ == "__main__":
    unittest.main()
