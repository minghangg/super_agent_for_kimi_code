"""Compatibility facade for the GitHub source package (issue #134 step 1).

The implementations live in :mod:`super_agent.github.source`. This module keeps
the 21 test files and the engine's historical imports working unchanged; it is
a re-export, not a second implementation. No method here can mutate the remote
repository — there is deliberately no POST/PATCH/PUT/DELETE path.
"""
from .github.source import (BrokenSource, FixtureSource, LiveSource, NullSource,
                            PagedBatch, PagedComments, SourceError, build,
                            comments_from, new_comments, paged_batch,
                            paged_result, walk_complete)

__all__ = [
    "BrokenSource", "FixtureSource", "LiveSource", "NullSource", "SourceError",
    "PagedBatch", "PagedComments", "build", "comments_from", "new_comments",
    "paged_batch", "paged_result", "walk_complete",
]
