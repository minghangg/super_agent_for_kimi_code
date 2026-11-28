"""#142 QA P0-1: ``health.recovered`` must fire on the transition, not every round.

Two defects lived here, and both were invisible to a test that re-implemented
the decision instead of driving it:

1. five sites set ``Engine.degraded`` and nothing cleared it, so one transient
   fault pinned ``degraded: true`` for the life of the process;
2. after that was fixed, the recovery row compared ``engine.degraded`` with the
   previous snapshot's ``degraded`` - and those are **different quantities**.
   :meth:`Engine.health_record` folds ``error``, ``_index_error``,
   ``_stream_error`` and ``_bus_last_error`` into the published flag, while
   ``engine.degraded`` is set by three of those sites only. A source failure
   (``error`` non-empty every round) therefore looked like a recovery every
   round: QA measured three false rows over four rounds.

So every test here calls the real ``run_once()`` and reads the real
``events.jsonl``. None of them re-derives the decision - an earlier revision of
this file did, which is exactly why it passed while the engine was wrong.

Run: ``python -m pytest tests/test_iss142_degraded_recovery.py -q``
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from super_agent import config, health
from super_agent.engine import Engine
from super_agent.github_source import NullSource
from super_agent.store import Store

GOOD = {
    "schema_version": 1,
    "defaults": {"mode": "observe", "creation_mode": "pm_created"},
    "projects": {
        "demo": {"repo": "owner/demo", "cwd": ".",
                 "mode": "observe", "creation_mode": "pm_created",
                 "issues": [1], "actions": ["collect", "reconcile"],
                 "max_active_writers": 1},
    },
}


class FlakySource(NullSource):
    """A source whose failure the caller controls, for whole-round accounting."""

    def __init__(self) -> None:
        self.fail = True
        self.asked = 0

    def issues(self, limit: int = 100) -> list:
        self.asked += 1
        if self.fail:
            raise ConnectionError("github unreachable")
        return []


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.allow_path = self.root / "allowlist.json"
        self.allow_path.write_text(json.dumps(GOOD), encoding="utf-8")
        self.events = self.root / "sessions" / "events.jsonl"
        self.events.parent.mkdir(parents=True, exist_ok=True)
        self.events.write_text("", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def engine(self, source=None) -> Engine:
        cfg = config.load(None, {
            "github": {"source": "none"},
            "engine": {"archived_crosscheck": False,
                       "session_events_path": str(self.events),
                       "allowlist": ["issue_comment.created"]}})
        return Engine(cfg, Store(self.state), source or NullSource())

    def snapshot(self) -> dict:
        return health.read(self.state / "health.json")

    def recovered_rows(self) -> list:
        path = self.state / "events.jsonl"
        if not path.is_file():
            return []
        return [row for row in
                (json.loads(line) for line in
                 path.read_text("utf-8").splitlines() if line.strip())
                if row.get("type") == "health.recovered"]

    def recovered_count(self) -> int:
        return len(self.recovered_rows())


class PersistentSourceFailureIsNotARecovery(Base):
    """The counter-example QA measured: ``error`` non-empty, flag never set.

    ``run_once`` × 4 with ``issues()`` raising every round. The snapshot is
    degraded throughout because ``health_record`` folds ``error`` in, while
    ``engine.degraded`` stays false. A flag-vs-snapshot comparison wrote three
    ``health.recovered`` rows here; the same-source comparison writes none.
    """

    def test_no_recovery_row_while_the_source_keeps_failing(self):
        source = FlakySource()
        engine = self.engine(source)
        for _ in range(4):
            engine.run_once()
            self.assertTrue(self.snapshot()["degraded"],
                            "degraded must stay true every round")
            self.assertEqual(self.recovered_count(), 0,
                             "degraded -> degraded is never a recovery")
        snap = self.snapshot()
        self.assertTrue(snap["degraded"], "a live source failure is degraded")
        self.assertIn("ConnectionError", snap["last_error"])
        self.assertEqual(self.recovered_count(), 0,
                         "degraded -> degraded is not a recovery")


class TransientFailureRecoversExactlyOnce(Base):
    """The positive path: two failing rounds, then the third recovers.

    Pinned to the round number, not just the count: a row written one round
    early would still add up to 1 here, and that is exactly the off-by-one the
    old flag-vs-snapshot comparison produced.
    """

    def test_two_failures_then_one_recovery_in_the_third_round(self):
        source = FlakySource()
        engine = self.engine(source)

        for expected_round in (1, 2):
            engine.run_once()
            self.assertTrue(self.snapshot()["degraded"],
                            f"round {expected_round} is still failing")
            self.assertEqual(self.recovered_count(), 0,
                             "no recovery row while the fault is live")

        source.fail = False
        engine.run_once()
        self.assertFalse(self.snapshot()["degraded"], "round 3 cleared the fault")
        self.assertEqual(self.recovered_count(), 1,
                         "the transition is dated once")

        rounds = [r["round_seq"] for r in self.recovered_rows()]
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0], 3,
                         "the row belongs to the round that recovered, round 3")

        for _ in range(3):
            engine.run_once()
        self.assertEqual(self.recovered_count(), 1,
                         "recovery is a transition, not a state")


class DegradedFlagIsRoundScoped(Base):
    """The original defect: the memory flag used to be set and never cleared."""

    def test_the_flag_does_not_survive_into_a_clean_round(self):
        engine = self.engine()
        engine.begin_round()
        engine.degraded = True
        self.assertTrue(engine.degraded)
        engine.begin_round()
        self.assertFalse(engine.degraded, "a new round starts clean")


if __name__ == "__main__":
    unittest.main()
