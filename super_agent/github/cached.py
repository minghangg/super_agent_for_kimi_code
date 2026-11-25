"""A cached read-through wrapper for :class:`LiveSource` (#134 step 3).

The engine only sees the six source methods it already uses. This wrapper keeps
that surface identical: ``issues()`` and ``comments()`` consult the local cache
first, but return the same shapes, and ``paged()`` delegates to the real backend
without consulting the cache. Rebuild therefore cannot be satisfied from disk.

Cache hits do **not** bypass #130 backpressure. They happen inside
``comments()``, before the engine has a listing; the engine still hands every
returned row to ``BoundedBatchQueue.put()``, where a full queue refuses them.
"""
from __future__ import annotations

from . import cache


class CachedLiveSource:
    """Read-through cache around any source with the LiveSource method set."""

    def __init__(self, backend, *, ttl_s: float = 300.0,
                 root=None):
        self.backend = backend
        self.ttl_s = float(ttl_s)
        self.root = root

    def _cache_root(self):
        return cache.cache_root() if self.root is None else self.root

    def issues(self, limit: int = 20) -> list[int]:
        rows = cache.read_issues(self._cache_root())
        if rows is None:
            rows = list(self.backend.issues(limit=limit))
            cache.write_issues(rows, root=self._cache_root())
        return [int(x) for x in rows[:max(0, int(limit))]]

    def comments(self, issue: int) -> list[dict]:
        rows = cache.read_comments(issue, self.ttl_s, root=self._cache_root())
        if rows is None:
            rows = list(self.backend.comments(issue))
            cache.write_comments(issue, rows, root=self._cache_root())
        return rows

    def paged(self, issue: int, *, since_id: int = 0, max_pages: int = 10):
        """Never cached: rebuild must walk real pages."""
        return self.backend.paged(issue, since_id=since_id, max_pages=max_pages)

    def title(self, issue: int) -> str:
        return self.backend.title(issue)

    def issue_state(self, issue: int) -> str:
        """No state cache: ``unknown``/``open`` must never become permanent."""
        return self.backend.issue_state(issue)

    def flow(self, issue: int) -> str:
        return self.backend.flow(issue)
