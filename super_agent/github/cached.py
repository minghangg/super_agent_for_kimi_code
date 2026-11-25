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

from .. import flow as flow_mod
from . import cache


class CachedLiveSource:
    """Read-through cache around any source with the LiveSource method set."""

    def __init__(self, backend, *, ttl_s: float = 300.0,
                 root=None):
        self.backend = backend
        self.ttl_s = float(ttl_s)
        self.root = root
        self._labels: dict | None = None

    def _cache_root(self):
        return cache.cache_root() if self.root is None else self.root

    def issues(self, limit: int = 20) -> list[int]:
        """Always ask the backend, then refresh the snapshot (#134 §3).

        The listing is "every round", not "within a TTL": a cached answer here
        would keep a closed issue in the live set and hide a new one after a
        restart (P0-1). ``limit`` truncates only the **returned** prefix; the
        snapshot keeps the full page so ``doctor()``'s ``limit=1`` probe cannot
        narrow every later round.

        The snapshot is written **after** the read succeeds, so a failed refresh
        keeps the previous file byte-identical instead of replacing it with an
        empty listing.

        Labels come from the same request, so a later ``flow()`` answers from
        the snapshot instead of one detail GET per issue (P1-1).
        """
        rows, labels = self._read_listing()
        cache.write_issues(rows, labels=labels, root=self._cache_root())
        self._labels = labels
        return [int(x) for x in rows[:max(0, int(limit))]]

    def probe(self, issue: int) -> str:
        """``doctor()``'s reachability check: no snapshot write (P0-1).

        A probe asks for ``issues(limit=1)`` and only needs "can we reach the
        source". Letting it write the snapshot made one row stand in for the
        whole listing, which then narrowed the next round to a single issue.
        """
        return str(self.issue_state(issue))

    def _read_listing(self) -> tuple[list[int], dict]:
        """``(numbers, labels)`` from the backend, whatever it can offer.

        ``LiveSource`` exposes both in one request; a backend that only has
        ``issues()`` still refreshes the numbers, and its ``flow()`` keeps its
        own behaviour.
        """
        reader = getattr(self.backend, "issues_with_labels", None)
        if callable(reader):
            rows, labels = reader(2 ** 31)
            return [int(x) for x in rows], labels
        rows = [int(x) for x in self.backend.issues(limit=2 ** 31)]
        return rows, {}

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
        """Answer from the snapshot's labels when it has this issue (#134 P1-1).

        ``LiveSource.flow`` falls back to ``GET /issues/{n}`` whenever its
        in-process label map has no entry -- which is exactly the state after a
        restart. Reading the snapshot here keeps a cached round at one list
        request instead of one detail request per issue.
        """
        number = int(issue)
        labels = getattr(self, "_labels", None)
        if labels is None:
            labels = cache.read_labels(self._cache_root())
            self._labels = labels
        if number in labels:
            return flow_mod.from_labels(labels[number])
        return self.backend.flow(issue)
