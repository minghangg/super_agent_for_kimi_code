"""#134 step 3 counterexamples: cache hits, failures, root, backpressure, rebuild.

The cache is a read-through mirror. These tests fail the tempting wrong
versions: writing on failure, using ``lwa_root``, adding a ``cache_dir`` key,
short-circuiting the engine's queue, or serving a rebuild from disk.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from super_agent import batch_queue, config
from super_agent.batch import CollectedBatch, ScanPolicy
from super_agent.github import cache, cached

ROWS = [{"id": 1, "body": "a"}, {"id": 2, "body": "b"}]


class Backend:
    def __init__(self, rows=None, *, fail=False):
        self.rows, self.fail, self.calls = rows or ROWS, fail, 0
        self.paged_calls = 0

    def issues(self, limit=20):
        return [1, 2]

    def comments(self, issue):
        self.calls += 1
        if self.fail:
            raise RuntimeError("offline")
        return list(self.rows)

    def paged(self, issue, *, since_id=0, max_pages=10):
        self.paged_calls += 1
        return list(self.rows)

    def title(self, issue):
        return "t"

    def issue_state(self, issue):
        return "closed"

    def flow(self, issue):
        return "standard"


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_6_cache_hit_makes_no_network_call(self):
        backend = Backend()
        src = cached.CachedLiveSource(backend, root=self.root)
        self.assertEqual([c["id"] for c in src.comments(1)], [1, 2])
        self.assertEqual([c["id"] for c in src.comments(1)], [1, 2])
        self.assertEqual(backend.calls, 1)

    def test_7_network_failure_writes_nothing(self):
        backend = Backend(fail=True)
        src = cached.CachedLiveSource(backend, root=self.root)
        with self.assertRaises(RuntimeError):
            src.comments(1)
        self.assertFalse((self.root / "comments" / "1.json").exists())

    def test_8_root_uses_data_github_cache(self):
        self.assertTrue(str(cache.cache_root()).replace("\\", "/").endswith(
            "/data/github_cache"))

    def test_9_no_cache_dir_config_key(self):
        self.assertNotIn("cache_dir", config.DEFAULTS["github"])

    def test_12_cache_hit_still_faces_backpressure(self):
        backend = Backend(rows=[{"id": i, "body": "x"} for i in range(1, 6)])
        src = cached.CachedLiveSource(backend, root=self.root)
        src.comments(1)                      # warm the cache
        rows = src.comments(1)               # this one is a cache hit
        policy = ScanPolicy(max_batches=1, max_batch_rows=2, max_issue_rows=2)
        q = batch_queue.BoundedBatchQueue(policy)
        batch = CollectedBatch("o/r#1", rows, cursor_before=0)
        with self.assertRaises(batch_queue.Backpressure):
            q.put(batch)
        self.assertEqual([r["thread"] for r in q.refused], ["o/r#1"])

    def test_13_rebuild_never_uses_the_cache(self):
        backend = Backend()
        src = cached.CachedLiveSource(backend, root=self.root)
        src.comments(1)                      # warm the cache
        src.paged(1, since_id=0, max_pages=10)
        self.assertEqual(backend.paged_calls, 1)
        self.assertEqual(backend.calls, 1)

    def test_failed_state_is_never_cached(self):
        backend = Backend()
        src = cached.CachedLiveSource(backend, root=self.root)
        self.assertEqual(src.issue_state(1), "closed")
        self.assertFalse((self.root / "issues.json").exists())


if __name__ == "__main__":
    unittest.main()
