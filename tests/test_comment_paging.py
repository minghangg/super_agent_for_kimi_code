"""Paged comment reading: stop at the cursor, never advance past unread rows.

The old engine asked for one page (100 rows) and then sliced the tail
(``[-max_comments:]``). An issue with more than ``max_comments`` new comments
lost the middle: the slice kept the newest ones and the cursor jumped to their
max, so the skipped ids were never read again.

These tests pin the three properties that replace it:

* paging asks for the oldest page first and stops once a page reaches the cursor;
* a failure raises ``SourceError`` and returns nothing (no partial list);
* the engine takes the oldest N new comments and advances the cursor only to
  the highest id it actually processed.

Run: ``python -m pytest tests/test_comment_paging.py -q``
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from super_agent import config, github_source
from super_agent.allowlist import ProjectAllowlist
from super_agent.engine import Engine
from super_agent.store import Store

GOOD = {
    "schema_version": 1,
    "defaults": {"mode": "observe", "creation_mode": "pm_created"},
    "projects": {
        "demo": {"repo": "owner/demo", "cwd": ".",
                 "mode": "assist", "creation_mode": "pm_created",
                 "issues": [1], "actions": ["bus_inject"], "max_active_writers": 1},
    },
}


class PagedLiveSource(github_source.LiveSource):
    """``LiveSource`` over an in-memory listing that behaves like the real API.

    Two measured facts are reproduced here too: **``direction`` is ignored**
    (the per-issue endpoint answers oldest-first regardless) and the page count
    is announced through ``Link``. A stub that reverses on ``direction`` would
    hide the very defect ISS-127 is about - it did, once.
    """

    def __init__(self, rows, *, per_page=3, fail_on_page=None, last_link=True):
        super().__init__("owner/demo")
        self.rows = sorted(rows)
        self.per_page = per_page
        self.page_size = per_page
        self.fail_on_page = fail_on_page
        self.last_link = last_link
        self.requests = []

    def issues(self, limit=20):
        return [1]

    def title(self, issue):
        return "t"

    def flow(self, issue):
        return ""

    def page_count(self) -> int:
        return max(1, -(-len(self.rows) // self.per_page))

    def _get_with_headers(self, path, query=None):
        query = dict(query or {})
        page = int(query.get("page") or 1)
        self.requests.append(query)
        if self.fail_on_page == page:
            raise github_source.SourceError(f"GitHub 502 on {path} (page {page})")
        start = (page - 1) * self.per_page
        chunk = self.rows[start:start + self.per_page]
        body = [{"id": cid, "body": f"b{cid}", "user": {"login": "u"},
                 "created_at": ""} for cid in chunk]
        headers = {}
        total = self.page_count()
        if self.last_link and total > 1:
            headers["Link"] = (f'<https://api.github.com/repos/owner/demo/issues/1/'
                               f'comments?per_page={self.per_page}&page={total}>; '
                               f'rel="last"')
        return body, headers

    def _get(self, path, query=None):
        return self._get_with_headers(path, query)[0]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.allow = self.root / "allowlist.json"
        self.allow.write_text("{}", encoding="utf-8")
        self.allow.write_text(__import__("json").dumps(GOOD), encoding="utf-8")
        # ``--state-dir`` does not isolate the lifecycle stream, so pin it to an
        # empty file inside the temp dir: otherwise the engine reads the live
        # workbench stream and its assignments depend on this machine's state.
        self.events = self.root / "sessions" / "events.jsonl"
        self.events.parent.mkdir(parents=True, exist_ok=True)
        self.events.write_text("", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def engine(self, source) -> Engine:
        # No session API is injected here, so the archived cross-check stays off
        # (it would otherwise ask a server). The lifecycle stream is still
        # pinned to the temp dir: an unset path resolves to the live workbench
        # stream, which would leak this machine's bindings into the round.
        cfg = config.load(None, {"mode": "assist",
                                 "github": {"source": "none", "repo": "owner/demo",
                                            "max_comments_per_issue": 2,
                                            "max_comment_pages": 5},
                                 "engine": {"archived_crosscheck": False,
                                            "session_events_path": str(self.events)}})
        return Engine(cfg, Store(self.state), source, None,
                      project_allowlist=ProjectAllowlist(self.allow))


class PagingContract(Base):
    def test_a_settled_page_stops_the_walk(self):
        src = PagedLiveSource(list(range(1, 13)), per_page=4)
        rows = src.paged(1, since_id=8)
        # listing is 3 pages (4 rows each). Probe page 1 for Link, then walk
        # back: page 3 = 9..12 (min 9 > 8, keep going), page 2 = 5..8
        # (min 5 <= 8 ⇒ everything above 8 already collected).
        self.assertEqual([q.get("page") for q in src.requests], [1, 3, 2])
        self.assertEqual([c["id"] for c in rows], list(range(5, 13)))
        # Stopping at the cursor proves the unread prefix is contiguous. It does
        # NOT prove the older pages were read - page 1 was never collected.
        self.assertTrue(rows.contiguous)
        self.assertFalse(rows.pagination_complete)

    def test_a_mixed_page_does_not_stop_the_walk(self):
        """The defect this rule exists for: one old row must not end the walk."""
        src = PagedLiveSource(list(range(1, 9)), per_page=3)
        rows = src.paged(1, since_id=5)
        # 3 pages. page 3 = 7,8 (min 7 > 5) -> page 2 = 4,5,6 (min 4 <= 5: stop)
        self.assertEqual([q.get("page") for q in src.requests], [1, 3, 2])
        ids = [c["id"] for c in rows]
        self.assertIn(6, ids)
        self.assertIn(8, ids)

    def test_the_walk_starts_at_the_last_page(self):
        src = PagedLiveSource([1, 2, 3, 4, 5, 6], per_page=2)
        src.paged(1, since_id=0)
        self.assertEqual([q.get("page") for q in src.requests], [1, 3, 2])
        self.assertNotIn("direction", src.requests[0],
                         "the per-issue endpoint ignores direction: never send it")

    def test_error_raises_and_returns_no_partial_list(self):
        """A failure is never folded into a short list: 1 row is not "the end"."""
        src = PagedLiveSource(list(range(1, 13)), per_page=4, fail_on_page=2)
        with self.assertRaises(github_source.SourceError):
            src.paged(1, since_id=0)

    def test_page_cap_is_honoured_and_reported_incomplete(self):
        src = PagedLiveSource(list(range(1, 41)), per_page=2)
        rows = src.paged(1, since_id=0, max_pages=3)
        # 20 pages; budget 3 = probe + 2 real pages, all at the newest end.
        self.assertEqual(len(rows), 4)
        self.assertFalse(rows.pagination_complete,
                         "running out of pages is not proof the listing ended")
        self.assertFalse(rows.contiguous,
                         "nothing at or below the cursor was reached")

    def test_a_full_page_is_not_proof_of_the_end(self):
        """Counter-example 3: one page of 100 fresh rows is not a finished walk."""
        src = PagedLiveSource(list(range(1, 101)), per_page=100)
        rows = src.paged(1, since_id=0, max_pages=1)
        self.assertFalse(rows.pagination_complete,
                         "a full page plus an exhausted budget proves nothing")
        self.assertEqual([q.get("page") for q in src.requests], [1])

    def test_a_full_page_is_followed_by_the_next_page(self):
        """...so the walk keeps going until a page at/below the cursor."""
        src = PagedLiveSource(list(range(1, 201)), per_page=100)
        rows = src.paged(1, since_id=120)
        # probe page 1, then newest page 2 = 101..200 (min 101 <= 120 ⇒ stop)
        self.assertEqual([q.get("page") for q in src.requests], [1, 2])
        self.assertTrue(rows.contiguous)
        self.assertFalse(rows.pagination_complete,
                         "page 1's own rows were never collected")
        self.assertIn("cursor", rows.stop_reason)

    def test_the_walk_is_a_list(self):
        """Counter-example 2: old callers index/slice the result, so it is a list."""
        src = PagedLiveSource([1, 2, 3, 4], per_page=100)
        rows = src.paged(1)
        self.assertIsInstance(rows, list)
        self.assertEqual(rows[-1]["id"], 4)
        self.assertEqual([c["id"] for c in rows[:2]], [1, 2])


class CommentsFromIsTypeTolerant(unittest.TestCase):
    """Counter-example 2: a two-way unpack would break every plain-list backend."""

    class PlainList:
        def paged(self, issue, *, since_id=0, max_pages=10):
            return [{"id": 1}, {"id": 2}]

    class Decorated(github_source.PagedComments):
        pass

    def test_a_plain_list_backend_is_not_unpacked(self):
        rows = github_source.comments_from(self.PlainList(), 1)
        self.assertEqual([c["id"] for c in rows], [1, 2])

    def test_the_decorated_result_still_reports_completeness(self):
        decorated = github_source.PagedComments([{"id": 1}], complete=True)
        self.assertTrue(github_source.walk_complete(decorated))
        self.assertFalse(github_source.walk_complete([{"id": 1}]),
                         "a plain list proves nothing either way")

    def test_an_empty_walk_is_not_complete_by_default(self):
        self.assertFalse(github_source.walk_complete(
            github_source.NullSource().paged(1)))


class EngineCursor(Base):
    def test_engine_takes_oldest_n_and_does_not_skip(self):
        rows = list(range(100, 110))          # 10 new comments, cap is 2
        src = PagedLiveSource(rows, per_page=100)
        engine = self.engine(src)
        summary = engine.run_once()
        cursor = engine.store.load_cursor()["issues"]
        self.assertEqual(summary["processed"], 2)
        cur = cursor[engine._thread_key(1)]
        self.assertEqual(cur, 101, "cursor only moves to the last processed id")

        # Second round picks up exactly where the first stopped: no gap.
        engine.run_once()
        self.assertEqual(engine.store.load_cursor()["issues"][engine._thread_key(1)], 103)

    def test_seen_comment_ids_are_contiguous(self):
        rows = list(range(200, 206))
        src = PagedLiveSource(rows, per_page=100)
        engine = self.engine(src)
        engine.run_once()
        idx = engine.store.load_index()
        self.assertEqual(idx["issues"][engine._thread_key(1)]["comments_seen"], 2)

    def test_failed_page_leaves_the_cursor_alone(self):
        """Counter-example 4: a dead page is an error, never an empty listing."""
        src = PagedLiveSource(list(range(300, 320)), per_page=8, fail_on_page=2)
        engine = self.engine(src)
        summary = engine.run_once()
        self.assertTrue(summary["errors"], "a dead page is reported")
        cursor = engine.store.load_cursor().get("issues") or {}
        self.assertEqual(int(cursor.get("owner/demo#1") or 0), 0)
        self.assertEqual(summary["processed"], 0)


if __name__ == "__main__":
    unittest.main()
