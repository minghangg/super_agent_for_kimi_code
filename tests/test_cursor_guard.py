"""The cursor is the only thing that stops a full re-read (issue #87).

Once a verdict has to name its session key, re-reading an issue's whole history
is not "some extra work": every verdict written before the key existed comes
back as ``unverified``. ``cursor.json`` is what records how far the engine got,
and until this guard it was the one file whose loss was silent -
``load_cursor()`` returned ``{"version": 1, "issues": {}}`` and the round
re-scanned everything.

Three distinctions decide the guard, and each has a counter-example below:

* **a missing cursor on a fresh install is fine** - there is no issue to
  re-read, so refusing would make the first run impossible;
* **a lost cursor next to a populated index is not fine** - the index proves
  comments were read, and the file that proves how far is gone;
* **a per-issue ``0`` is not a fault** - it is where a fresh issue starts. The
  plan's audit found 14 of them, all with ``comments_seen == 0`` and no
  ``comment.processed`` event. Refusing those would stop the engine on a
  healthy state.

Run: ``python -m pytest tests/test_cursor_guard.py -q``
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from super_agent import cli, config
from super_agent.engine import Engine
from super_agent.github_source import NullSource
from super_agent.store import CursorRefused, Store

THREAD = "owner/demo#1"


class FakeBus:
    def healthz(self):
        return {"ok": True, "ready": True, "connected": True, "boot_id": "test",
                "seq": 1}

    def inject(self, event_type, payload):
        return {"ok": True, "seq": 1}


def _index_with_one_issue() -> dict:
    return {"version": 1, "meta": {}, "assignments": {},
            "issues": {THREAD: {"state": "tracked", "comments_seen": 3,
                                "last_comment_id": 9003, "needs_person": []}}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "state"
        self.store = Store(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def write_cursor(self, body) -> None:
        text = body if isinstance(body, str) else json.dumps(body)
        self.store.cursor_path.write_text(text, encoding="utf-8")

    def populate(self, issues: dict | None = None) -> None:
        self.store.save_index(_index_with_one_issue())
        self.write_cursor({"version": 1,
                           "issues": {THREAD: 9003} if issues is None else issues})


class FreshInstall(Base):
    def test_no_cursor_and_no_index_is_fine(self):
        self.assertEqual(self.store.cursor_problem(), "")

    def test_a_populated_cursor_is_fine(self):
        self.populate()
        self.assertEqual(self.store.cursor_problem(), "")

    def test_a_per_issue_zero_is_fine(self):
        """The 14 zeros the plan audited: new issues, not lost data."""
        self.store.save_index(_index_with_one_issue())
        self.write_cursor({"version": 1,
                           "issues": {THREAD: 0, "owner/demo#2": 0}})
        self.assertEqual(self.store.cursor_problem(), "")

    def test_strict_read_returns_the_normal_shape(self):
        self.populate()
        self.assertEqual(self.store.load_cursor(strict=True)["issues"],
                         {THREAD: 9003})


class Refusals(Base):
    def test_a_lost_cursor_beside_a_populated_index_is_refused(self):
        self.store.save_index(_index_with_one_issue())
        problem = self.store.cursor_problem()
        self.assertIn("missing", problem)
        self.assertIn("would re-read their whole history", problem)

    def test_an_index_that_tracked_no_comment_does_not_refuse(self):
        """An issue is in the index but no comment was ever read.

        ``comments_seen == 0`` means the round wrote the row and stopped: the
        cursor has nothing to replay, so a missing file is not lost data. This
        is the case ``test_issue_three_state`` tracks before any comment
        exists, and the one the ticket warned not to refuse.
        """
        index = _index_with_one_issue()
        index["issues"][THREAD]["comments_seen"] = 0
        index["issues"][THREAD]["last_comment_id"] = 0
        self.store.save_index(index)
        self.assertEqual(self.store.cursor_problem(), "")

    def test_an_emptied_cursor_is_refused(self):
        self.store.save_index(_index_with_one_issue())
        self.write_cursor({"version": 1, "issues": {}})
        problem = self.store.cursor_problem()
        self.assertIn("no issue entries", problem)

    def test_a_cursor_without_an_issues_object_is_refused(self):
        self.store.save_index(_index_with_one_issue())
        self.write_cursor({"version": 1})
        self.assertIn("'issues' object", self.store.cursor_problem())

    def test_an_unparseable_cursor_is_refused_by_the_loader(self):
        self.store.save_index(_index_with_one_issue())
        self.write_cursor('{"issues": ')
        with self.assertRaises(CursorRefused):
            self.store.load_cursor(strict=True)

    def test_the_tolerant_read_still_answers(self):
        """doctor and reporter must be able to describe the fault."""
        self.store.save_index(_index_with_one_issue())
        self.assertEqual(self.store.load_cursor()["issues"], {})
        self.assertNotEqual(self.store.cursor_problem(), "")
    def test_a_round_refuses_the_lost_cursor(self):
        self.populate()
        self.store.cursor_path.unlink()
        engine = Engine(config.load(None, {"github": {"source": "none"}}),
                        self.store, NullSource(), FakeBus(),
                        clock=lambda: 1_000_000.0)
        with self.assertRaises(CursorRefused):
            engine.run_once()

    def test_the_explicit_flag_allows_the_rescan(self):
        self.populate()
        self.store.cursor_path.unlink()
        cfg = config.load(None, {"github": {"source": "none"},
                                 "engine": {"allow_empty_cursor": True}})
        engine = Engine(cfg, self.store, NullSource(), FakeBus(),
                        clock=lambda: 1_000_000.0)
        engine.run_once()          # must not raise
        self.assertEqual(engine.store.load_index()["meta"]["run_count"], 1)


class DoctorAndCli(Base):
    def config_path(self) -> Path:
        path = Path(self.tmp.name) / "engine.json"
        path.write_text(json.dumps({
            "github": {"source": "none"},
            "state_dir": str(self.root),
        }), encoding="utf-8")
        return path

    def doctor(self, *extra: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["--config", str(self.config_path()), "doctor", *extra])
        return rc, out.getvalue(), err.getvalue()

    def test_the_doctor_fails_on_a_lost_cursor(self):
        self.populate()
        self.store.cursor_path.unlink()
        rc, out, err = self.doctor()
        self.assertEqual(rc, 1, err)
        self.assertIn("[FAIL]", out)
        self.assertIn("missing", out)
        self.assertNotIn("Traceback", err)

    def test_the_doctor_is_quiet_on_a_healthy_cursor(self):
        self.populate()
        rc, out, err = self.doctor()
        self.assertNotIn("cursor refused", err)
        self.assertNotIn("invalid cursors", out)

    def test_once_refuses_before_any_action(self):
        self.populate()
        self.store.cursor_path.unlink()
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = cli.main(["--config", str(self.config_path()), "once"])
        self.assertEqual(rc, 4)
        self.assertIn("cursor refused", err.getvalue())
        self.assertIn("--allow-empty-cursor", err.getvalue())

    def test_once_runs_with_the_explicit_flag(self):
        self.populate()
        self.store.cursor_path.unlink()
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = cli.main(["--config", str(self.config_path()), "once",
                           "--allow-empty-cursor"])
        self.assertNotEqual(rc, 4, err.getvalue())
        self.assertNotIn("cursor refused", err.getvalue())


ORDER = "owner/demo#2"


def _index_two_issues() -> dict:
    index = _index_with_one_issue()
    index["issues"][ORDER] = {"state": "tracked", "comments_seen": 2,
                              "last_comment_id": 9101, "needs_person": []}
    return index


class PartialReplayStillRefuses(DoctorAndCli):
    """Issue #87 P0-2: the recommended repair must not disable the guard.

    ``replay --issue 1`` rebuilds one thread and writes one cursor entry. With
    only the file-level checks, that entry made the file look healthy while
    ``#2`` still had none, and the next round re-read ``#2``'s whole history -
    the re-scan the guard exists to stop, reached through the guard's own
    printed advice.
    """

    def two_issues(self) -> None:
        self.store.save_index(_index_two_issues())

    def test_one_entry_beside_two_read_issues_is_refused(self):
        self.two_issues()
        self.write_cursor({"version": 1, "issues": {THREAD: 9003}})
        problem = self.store.cursor_problem()
        self.assertIn("no entry for 1 issue(s)", problem)
        self.assertIn(ORDER, problem)

    def test_both_entries_is_fine(self):
        self.two_issues()
        self.write_cursor({"version": 1,
                           "issues": {THREAD: 9003, ORDER: 9101}})
        self.assertEqual(self.store.cursor_problem(), "")

    def test_a_legacy_numeric_entry_counts(self):
        # A pre-migration file is healthy and must not be refused.
        self.two_issues()
        self.write_cursor({"version": 1, "issues": {THREAD: 9003, "2": 9101}})
        self.assertEqual(self.store.cursor_problem(), "")

    def test_a_round_after_a_partial_replay_is_refused(self):
        self.two_issues()
        self.write_cursor({"version": 1, "issues": {THREAD: 9003}})
        engine = Engine(config.load(None, {"github": {"source": "none"}}),
                        self.store, NullSource(), FakeBus(),
                        clock=lambda: 1_000_000.0)
        with self.assertRaises(CursorRefused):
            engine.run_once()

    def test_replay_reports_the_remaining_gap(self):
        self.two_issues()
        self.write_cursor({"version": 1, "issues": {}})
        engine = Engine(config.load(None, {"github": {"source": "none"}}),
                        self.store, NullSource(), FakeBus(),
                        clock=lambda: 1_000_000.0)
        out = engine.replay(1)
        self.assertEqual(out["cursor_gap"], [ORDER],
                         "one rebuilt thread must not hide the other")
        self.assertEqual(out["comments"], 0)

    def test_cli_replay_names_the_issues_left(self):
        self.two_issues()
        self.write_cursor({"version": 1, "issues": {}})
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["--config", str(self.config_path()), "replay",
                           "--issue", "1"])
        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn("cursor still missing for", err.getvalue())
        self.assertIn(ORDER, err.getvalue())
        self.assertNotEqual(self.store.cursor_problem(), "",
                            "a one-thread replay must not open the guard")

    def test_the_gap_closes_when_every_issue_has_an_entry(self):
        self.two_issues()
        self.write_cursor({"version": 1, "issues": {}})
        engine = Engine(config.load(None, {"github": {"source": "none"}}),
                        self.store, NullSource(), FakeBus(),
                        clock=lambda: 1_000_000.0)
        engine.replay(1)
        self.assertNotEqual(self.store.cursor_problem(), "")
        cursor = self.store.load_cursor()
        cursor.setdefault("issues", {})[ORDER] = 9101
        self.store.save_cursor(cursor)
        self.assertEqual(self.store.cursor_problem(), "")


if __name__ == "__main__":
    unittest.main()
