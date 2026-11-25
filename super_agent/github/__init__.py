"""GitHub access package: source backends and the single HTTP client.

Step 1 of issue #134 is a pure extraction; step 2 adds
:mod:`super_agent.github.client`, the one HTTP exit every live request uses.
Public names stay importable from ``super_agent.github_source`` until the final
migration step.
"""
from .cached import CachedLiveSource
from .cache import cache_root
from .source import (BrokenSource, FixtureSource, LiveSource, NullSource,
                     PagedBatch, PagedComments, SourceError, build, comments_from,
                     new_comments, paged_batch, paged_result, walk_complete)

__all__ = [
    "CachedLiveSource", "cache_root",
    "BrokenSource", "FixtureSource", "LiveSource", "NullSource", "SourceError",
    "PagedBatch", "PagedComments", "build", "comments_from", "new_comments",
    "paged_batch", "paged_result", "walk_complete",
]
