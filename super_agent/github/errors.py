"""The one exception type the GitHub layer raises.

Split out of ``source.py`` so the fixture backend can raise it without importing
the live backend (which imports the fixture): one small module, no cycle.

``github_source.SourceError`` and ``github.SourceError`` still name this class -
both packages re-export it.
"""
from __future__ import annotations


class SourceError(RuntimeError):
    pass
