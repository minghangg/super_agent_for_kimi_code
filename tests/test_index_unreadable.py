"""``index.json`` that cannot be read must never be overwritten by an empty one.

``load_index`` is tolerant on purpose - ``status``, ``doctor`` and ``brief``
have to be able to *report* a fault instead of dying on it. The bug was that
"could not read" was indistinguishable from "empty": the round wrote the
skeleton back at its end, and one truncated file erased ``assignments`` and
``release_frames``, neither of which the event log can rebuild.

The damage is made **inside the process**, after ``Store`` is constructed:
``check_migration`` refuses a broken file at startup, so a CLI-level fixture
would never reach the code under test.

Run: ``python -m pytest tests/test_index_unreadable.py -q``
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from super_agent import cli, config
from super_agent.engine import Engine
from super_agent.github_source import NullSource
from super_agent.store import IndexRefused, Store

THREAD = "owner/repo#7"
RELEASE_KEY = f"release:{THREAD}:500"
GOOD = {
    "version": 1,
    "meta": {"run_count": 3},
    "issues": {THREAD: {"state": "tracked", "comments_seen": 1}},
    "assignments": {
        "asg_7_wk_1": {"thread": THREAD, "status": "working",
                       "issue": 7, "role": "wk", "delivery_comment_id": None},
        "asg_7_qa_1": {"thread": THREAD, "status": "waiting_review",
                       "issue": 7, "role": "qa", "delivery_comment_id": None},
    },
    "pm_verdicts": {f"{THREAD}|1": {"verdict": "pass"}},
    "release_frames": {RELEASE_KEY: {
        "key": RELEASE_KEY, "thread": THREAD, "comment_id": 500, "source": "pm"}},
    "pm_frames": {f"{THREAD}|500": {"cid": 500}},
}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.good_text = json.dumps(GOOD)
        self.write(self.good_text)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, text) -> None:
        (self.root / "index.json").write_text(
            text if isinstance(text, str) else json.dumps(text), encoding="utf-8")

    def truncate(self) -> bytes:
        """Cut the file in half, the way a torn write leaves it."""
        text = (self.root / "index.json").read_text(encoding="utf-8")
        half = text[: len(text) // 2].encode("utf-8")
        (self.root / "index.json").write_bytes(half)
        return half

    def store(self) -> Store:
        return Store(self.root)

    def engine(self, store: Store) -> Engine:
        cfg = config.load(None, {"github": {"source": "none"},
                                 "engine": {"allow_empty_cursor": True}})
        return Engine(cfg, store, NullSource(), None)

    def kept(self) -> list[str]:
        return sorted(p.name for p in self.root.glob("index.json.corrupt.*"))

    def events(self) -> list[dict]:
        path = self.root / "events.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def types(self) -> list[str]:
        return [e.get("type") for e in self.events()]

    def health(self) -> dict:
        return json.loads((self.root / "health.json").read_text(encoding="utf-8"))

    def count(self, table: str) -> int:
        data = json.loads((self.root / "index.json").read_text(encoding="utf-8"))
        return len(data.get(table) or {})


class TruncatedFile(Base):
    """The core case: a half-written file erased the in-flight assignments."""

    def setUp(self):
        super().setUp()
        self.store_ = self.store()
        self.engine_ = self.engine(self.store_)
        self.corrupt = self.truncate()

    def test_the_round_does_not_write_the_skeleton_back(self):
        self.engine_.run_round()
        self.assertFalse((self.root / "index.json").is_file(),
                         "the file was moved aside, never rewritten")
        self.assertEqual(len(self.kept()), 1, "exactly one kept copy")
        self.assertEqual((self.root / self.kept()[0]).read_bytes(), self.corrupt)

    def test_degraded_and_the_event_are_written(self):
        self.engine_.run_round()
        snap = self.health()
        self.assertTrue(snap["degraded"])
        self.assertIn("JSONDecodeError", snap["last_error"])
        self.assertIn("index.unreadable", self.types())
        self.assertIn("index.write_back_skipped", self.types())
        event = next(e for e in self.events() if e["type"] == "index.unreadable")
        self.assertEqual(event["level"], "warn")
        self.assertEqual(event["consumers"], ["doctor", "brief", "health"])
        self.assertTrue(event["quarantined"].startswith("index.json.corrupt."))

    def test_health_is_written_this_round(self):
        self.engine_.run_round()
        self.assertIn("run.started", self.types())
        self.assertIn("run.completed", self.types(),
                      "a skipped write-back must not skip the rest of the round")

    def test_save_index_refuses_directly(self):
        self.store_.load_index()
        with self.assertRaises(IndexRefused):
            self.store_.save_index({"version": 1})


class NonObjectTopLevel(Base):
    def test_an_array_is_a_fault_like_a_truncation(self):
        store = self.store()
        engine = self.engine(store)
        self.write([1, 2, 3])
        before = (self.root / "index.json").read_bytes()
        engine.run_round()
        self.assertIn("list", store.index_unreadable)
        self.assertEqual(len(self.kept()), 1)
        self.assertEqual((self.root / self.kept()[0]).read_bytes(), before)
        self.assertTrue(self.health()["degraded"])
        self.assertIn("index.unreadable", self.types())


class EmptyVsMissing(Base):
    """An empty object is a real (empty) state; a missing file is a fresh root."""

    def test_empty_object_is_written_back_with_the_skeleton(self):
        store = self.store()
        engine = self.engine(store)
        self.write({})
        self.assertEqual(store.index_problem(), "")
        engine.run_round()
        self.assertEqual(self.kept(), [])
        saved = json.loads((self.root / "index.json").read_text(encoding="utf-8"))
        for key in ("version", "meta", "issues", "assignments"):
            self.assertIn(key, saved)
        self.assertFalse(self.health()["degraded"])

    def test_missing_file_is_a_fresh_root(self):
        (self.root / "index.json").unlink()
        store = self.store()
        self.assertEqual(store.index_problem(), "")
        self.assertTrue(store.index_writable)
        self.engine(store).run_round()
        self.assertTrue((self.root / "index.json").is_file())
        self.assertFalse(self.health()["degraded"])


class SecondWritePath(Base):
    """``cli._flush_state`` is the other path that wrote an empty index back."""

    def test_exit_flush_does_not_overwrite_the_unreadable_file(self):
        store = self.store()
        engine = self.engine(store)
        corrupt = self.truncate()
        engine.run_round()
        kept = self.kept()
        self.assertEqual(len(kept), 1)
        cli._flush_state(store)
        self.assertEqual(self.kept(), kept, "no second kept copy")
        self.assertFalse((self.root / "index.json").is_file(),
                         "the exit flush wrote an empty index back")
        self.assertEqual((self.root / kept[0]).read_bytes(), corrupt)


class RestartAfterQuarantine(Base):
    """The next process must not read "no index" as a fresh root."""

    def test_missing_index_beside_a_kept_copy_stays_refused(self):
        store = self.store()
        self.truncate()
        self.engine(store).run_round()
        self.assertEqual(len(self.kept()), 1)
        fresh = self.store()
        fresh.load_index()
        self.assertTrue(fresh.index_problem().startswith("index.json is missing"))
        self.assertFalse(fresh.index_writable)
        with self.assertRaises(IndexRefused):
            fresh.save_index({"version": 1})

    def test_deleting_the_kept_copy_restores_a_fresh_root(self):
        store = self.store()
        self.truncate()
        self.engine(store).run_round()
        os.unlink(self.root / self.kept()[0])
        fresh = self.store()
        self.assertEqual(fresh.index_problem(), "")
        self.assertTrue(fresh.index_writable)


class RoundTripKeepsCounts(Base):
    """A healthy index survives a round: nothing is dropped by the write-back."""

    def test_counts_are_equal_before_and_after(self):
        before = json.loads((self.root / "index.json").read_text(encoding="utf-8"))
        self.engine(self.store()).run_round()
        after = json.loads((self.root / "index.json").read_text(encoding="utf-8"))
        for table in ("assignments", "pm_verdicts", "release_frames", "pm_frames",
                      "issues"):
            self.assertEqual(len(after.get(table) or {}),
                             len(before.get(table) or {}), table)
        self.assertFalse(self.health()["degraded"])
        self.assertEqual(self.kept(), [])


class ReleaseLedgerSurvives(Base):
    """The kept copy holds the *corrupt* bytes, so only a backup restores the key."""

    def test_a_backup_brings_the_dedup_key_back(self):
        store = self.store()
        self.truncate()
        self.engine(store).run_round()
        self.write(self.good_text)          # the person restores a good copy
        recovered = self.store()
        index = recovered.load_index()
        self.assertTrue(recovered.index_writable)
        self.assertIn(RELEASE_KEY, index.get("release_frames") or {})
        self.assertIsNotNone(self.engine(recovered)._release_announced(
            index, THREAD, 500), "the release still counts as already announced")

    def test_the_kept_copy_is_the_corrupt_bytes(self):
        store = self.store()
        corrupt = self.truncate()
        self.engine(store).run_round()
        self.assertEqual((self.root / self.kept()[0]).read_bytes(), corrupt,
                         "quarantine renames, it does not repair: a torn file is "
                         "still torn, so recovery needs a backup")

    def test_accepting_the_empty_index_loses_the_ledger(self):
        store = self.store()
        self.truncate()
        store.load_index()
        self.assertFalse(store.index_writable)
        store.accept_empty_index()
        store.save_index({"version": 1, "meta": {}, "issues": {},
                          "assignments": {}})
        index = self.store().load_index()
        self.assertNotIn(RELEASE_KEY, index.get("release_frames") or {})
        self.assertIsNone(self.engine(self.store())._release_announced(
            index, THREAD, 500), "with the ledger gone the release is re-sent once")
        self.assertIn("index.accepted_empty", self.types())


class ActiveAssignmentsNotZero(Base):
    """A count of 0 from an unreadable index must not read as "nothing in flight"."""

    def test_the_last_good_count_is_kept(self):
        store = self.store()
        engine = self.engine(store)
        engine.run_round()
        good = self.health()["active_assignments"]
        self.assertEqual(good, 2, "two active assignments in the fixture")
        self.truncate()
        engine.run_round()
        snap = self.health()
        self.assertTrue(snap["degraded"])
        self.assertEqual(snap["active_assignments"], good,
                         "0 would say the in-flight work is gone")


if __name__ == "__main__":
    unittest.main()
