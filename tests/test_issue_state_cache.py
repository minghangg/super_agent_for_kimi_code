"""#134 step 5: only the closed terminal state may be cached.

Counterexamples 10 and 11 are the reason this is a set, not a dict with a TTL:

* caching ``unknown`` after one failed probe makes a transient outage permanent;
* caching ``open`` hides the close and keeps a finished thread alive forever.

A reopened issue re-enters ``issues()``, so a cached ``closed`` is safe for the
process lifetime.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from super_agent import config
from super_agent.engine import Engine
from super_agent.store import Store


class StateSource:
    def __init__(self, *states):
        self.states = list(states)
        self.calls = 0

    def issue_state(self, issue):
        self.calls += 1
        if not self.states:
            raise RuntimeError("offline")
        state = self.states.pop(0)
        if state == "raise":
            raise RuntimeError("offline")
        return state


def make_engine(source, tmp):
    cfg = config.load(None, {"github": {"source": "none"}})
    return Engine(cfg, Store(Path(tmp)), source, None)


class IssueStateCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_closed_is_cached_without_a_second_probe(self):
        src = StateSource("closed", "open")
        engine = make_engine(src, self.tmp.name)
        self.assertEqual(engine._issue_state(7), "closed")
        self.assertEqual(engine._issue_state(7), "closed")
        self.assertEqual(src.calls, 1)

    def test_unknown_is_never_cached(self):
        src = StateSource("raise", "open")
        engine = make_engine(src, self.tmp.name)
        self.assertEqual(engine._issue_state(7), "unknown")
        self.assertEqual(engine._issue_state(7), "open")
        self.assertEqual(src.calls, 2)

    def test_open_is_never_cached(self):
        src = StateSource("open", "closed")
        engine = make_engine(src, self.tmp.name)
        self.assertEqual(engine._issue_state(7), "open")
        self.assertEqual(engine._issue_state(7), "closed")
        self.assertEqual(src.calls, 2)


if __name__ == "__main__":
    unittest.main()
