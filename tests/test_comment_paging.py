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
    """``LiveSource`` with ``_get`` replaced by an in-memory paged backend."""

    def __init__(self, rows, *, per_page=3, fail_on_page=None):
        super().__init__("owner/demo")
        self.rows = list(rows)
        self.per_page = per_page
        self.fail_on_page = fail_on_page
        self.requests = []

    def issues(self, limit=20):
        return [1]

    def title(self, issue):
        return "t"

    def _get(self, path, query=None):
        page = int((query or {}).get("page") or 1)
        self.requests.append(dict(query or {}))
        if self.fail_on_page == page:
            raise github_source.SourceError(f"GitHub 502 on {path} (page {page})")
        start = (page - 1) * self.per_page
        chunk = self.rows[start:start + self.per_page]
        return [{"id": cid, "body": f"b{cid}", "user": {"login": "u"},
                 "created_at": ""} for cid in chunk]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.allow = self.root / "allowlist.json"
        self.allow.write_text("{}", encoding="utf-8")
        self.allow.write_text(__import__("json").dumps(GOOD), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def engine(self, source) -> Engine:
        cfg = config.load(None, {"mode": "assist",
                                 "github": {"source": "none", "repo": "owner/demo",
                                            "max_comments_per_issue": 2,
                                            "max_comment_pages": 5}})
        return Engine(cfg, Store(self.state), source, None,
                      project_allowlist=ProjectAllowlist(self.allow))


class PagingContract(Base):
    def test_paging_stops_at_the_cursor_page(self):
        src = PagedLiveSource([1, 2, 3, 4, 5, 6], per_page=2)
        rows = src.paged(1, since_id=3)
        # page 1 -> [1,2]; min 1 <= 3 so the walk stops: everything after it is
        # already newer than the cursor.
        self.assertEqual([c["id"] for c in rows], [1, 2])
        self.assertEqual(len(src.requests), 1)

    def test_paging_walks_until_the_cursor_page(self):
        src = PagedLiveSource([10, 20, 30, 40, 50, 60], per_page=2)
        src.paged(1, since_id=25)
        # page 1: 10,20 (min 10 <= 25) -> stop after the first page
        self.assertEqual(len(src.requests), 1)

    def test_paging_requests_ascending_pages(self):
        src = PagedLiveSource([1, 2, 3, 4], per_page=2)
        src.paged(1, since_id=0)
        self.assertEqual([q.get("page") for q in src.requests], [1, 2, 3])
        self.assertTrue(all(q.get("direction") == "asc" for q in src.requests))

    def test_error_raises_and_returns_no_partial_list(self):
        src = PagedLiveSource([1, 2, 3, 4, 5, 6], per_page=2, fail_on_page=2)
        with self.assertRaises(github_source.SourceError):
            src.paged(1, since_id=0)

    def test_page_cap_is_honoured(self):
        src = PagedLiveSource(list(range(1, 41)), per_page=2)
        rows = src.paged(1, since_id=0, max_pages=3)
        self.assertEqual(len(rows), 6)


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
        src = PagedLiveSource(list(range(300, 306)), per_page=2, fail_on_page=2)
        engine = self.engine(src)
        summary = engine.run_once()
        self.assertTrue(summary["errors"], "a dead page is reported")
        cursor = engine.store.load_cursor().get("issues") or {}
        self.assertEqual(int(cursor.get("owner/demo#1") or 0), 0)
        self.assertEqual(summary["processed"], 0)


if __name__ == "__main__":
    unittest.main()
