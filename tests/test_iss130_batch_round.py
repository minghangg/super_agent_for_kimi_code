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
    """A listing served page by page; ``missing`` hides one page entirely."""

    def __init__(self, ids, *, per_page=100, missing=(), complete=True,
                 contiguous=None):
        self.ids = list(ids)
        self.per_page = int(per_page)
        self.missing = set(missing)
        self.claimed_complete = complete
        self.claimed_contiguous = complete if contiguous is None else contiguous
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

    def paged(self, issue, *, since_id=0, max_pages=10):
        """Every page the walk would fetch, with the hidden ones left out.

        A real walk records a page **entry** for each request it made, and the
        ``rows`` count is what tells a short page from a full one. The fixture
        mirrors that: pages are numbered from 1 upwards, and ``missing`` removes
        an entry entirely - which is what a page that never arrived looks like.
        """
        wanted = [int(cid) for cid in self.ids if int(cid) > int(since_id or 0)]
        pages = []
        page = 1
        cursor = 0
        while cursor < len(wanted):
            chunk = wanted[cursor:cursor + self.per_page]
            cursor += len(chunk)
            if page not in self.missing:
                pages.append({"page": page, "rows": len(chunk), "status": "ok"})
            page += 1
        return PagedComments(
            [{"id": cid, "issue": 1, "body": "", "author": ""} for cid in wanted],
            complete=self.claimed_complete, contiguous=self.claimed_contiguous,
            pages=pages, reason="fixture")


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


class GapDetection(Base):
    def test_a_jumping_id_list_does_not_rebuild(self):
        """Counter-example 2: page 1 is complete, ids 9001/9005/9009 are not."""
        source = Source([9001, 9005, 9009])
        engine = self.engine(source)
        out = engine.run_once()
        self.assertNotIn(THREAD, out.get("rebuild_needed") or [])
        self.assertEqual(self.cursor_of(engine), 9009)

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
