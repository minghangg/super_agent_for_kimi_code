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
from super_agent.engine_parts import doctor
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


class FlakyIssues:
    """A backend whose listing changes between rounds, and can fail."""

    def __init__(self, rounds, labels=None, state="open"):
        self.rounds = list(rounds)
        self.labels = labels or {}
        self.fail = False
        self.fail_state = False
        self.state = state
        self.calls = 0
        self.detail_calls = 0

    def issues(self, limit=20):
        return self.issues_with_labels(limit)[0]

    def issues_with_labels(self, limit=20):
        self.calls += 1
        if self.fail:
            raise RuntimeError("offline")
        rows = self.rounds[min(self.calls, len(self.rounds)) - 1]
        out = [int(x) for x in rows]
        return out[:max(0, int(limit))], dict(self.labels)

    def comments(self, issue):
        return []

    def paged(self, issue, *, since_id=0, max_pages=10):
        return []

    def title(self, issue):
        return ""

    def issue_state(self, issue):
        if self.fail_state:
            raise RuntimeError("offline")
        return self.state

    def flow(self, issue):
        self.detail_calls += 1
        return ""


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

    # ---- QA 退回 cid 7162406901 的 P0-1 / P1-1 回归 ----

    def test_p0_1_issues_refreshes_every_round(self):
        """A cached listing must never answer a later round."""
        lead = FlakyIssues([[1, 2, 3], [1, 2, 3, 4, 5, 6], [1, 2, 3, 4, 5, 6]])
        src = cached.CachedLiveSource(lead, root=self.root)
        self.assertEqual(src.issues(20), [1, 2, 3])
        self.assertEqual(src.issues(20), [1, 2, 3, 4, 5, 6])
        self.assertEqual(lead.calls, 2)

    def test_p0_1_a_limit_probe_cannot_narrow_the_snapshot(self):
        """``doctor()``'s ``issues(limit=1)`` must not write one row."""
        lead = FlakyIssues([[7, 8, 9]])
        src = cached.CachedLiveSource(lead, root=self.root)
        self.assertEqual(src.issues(limit=1), [7])
        self.assertEqual(cache.read_issues(self.root), [7, 8, 9])
        self.assertEqual(src.issues(limit=20), [7, 8, 9])

    def test_p0_1_a_failed_refresh_keeps_the_previous_snapshot(self):
        lead = FlakyIssues([[1, 2]])
        src = cached.CachedLiveSource(lead, root=self.root)
        src.issues(20)
        before = (self.root / "issues.json").read_bytes()
        lead.fail = True
        with self.assertRaises(RuntimeError):
            src.issues(20)
        self.assertEqual((self.root / "issues.json").read_bytes(), before)

    def test_p1_1_flow_uses_snapshot_labels_without_a_detail_request(self):
        lead = FlakyIssues([[1]], labels={1: [{"name": "flow:at"}]})
        src = cached.CachedLiveSource(lead, root=self.root)
        src.issues(20)
        self.assertEqual(src.flow(1), "at")
        self.assertEqual(lead.detail_calls, 0)

    # ---- QA P0-4: doctor 直调 github_check 的契约 ----

    def test_p0_4_github_check_consumes_the_probe_state(self):
        """``doctor`` reads a state string; ``int()`` made every live check fail."""
        for state in ("open", "closed", "unknown"):
            with self.subTest(state=state):
                backend = FlakyIssues([[1]], state=state)
                src = cached.CachedLiveSource(backend, root=self.root)
                row = doctor.github_check(src, "live")
                self.assertEqual(row[0], "github")
                self.assertTrue(row[1], row)
                self.assertIn(state, row[2])

    def test_p0_4_github_check_reports_a_broken_probe_as_down(self):
        backend = FlakyIssues([[1]])
        backend.fail_state = True
        src = cached.CachedLiveSource(backend, root=self.root)
        row = doctor.github_check(src, "live")
        self.assertFalse(row[1])
        self.assertEqual(row[4], "github-down")

    def test_p0_4_github_check_does_not_touch_the_snapshot(self):
        backend = FlakyIssues([[1, 2, 3]])
        src = cached.CachedLiveSource(backend, root=self.root)
        doctor.github_check(src, "live")
        self.assertFalse((self.root / "issues.json").exists())

    def test_p1_2_replay_listing_is_not_cached(self):
        backend = Backend()
        src = cached.CachedLiveSource(backend, root=self.root)
        src.comments(1)
        src.paged(1, since_id=0, max_pages=10)
        self.assertEqual(backend.paged_calls, 1)


if __name__ == "__main__":
    unittest.main()
