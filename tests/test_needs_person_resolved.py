"""``needs_person`` can be dismissed: side table, CLI, read-side suppression.

The properties pinned here:

* a dismissed entry stops showing in ``status`` and ``brief`` while it stays in
  ``index.json`` - nothing is rewritten and nothing is deleted;
* an entry that was not dismissed still shows (the suppression is not a
  blanket "hide everything");
* dismissing twice is idempotent: the first audit row (``at``/``by``/``entries``)
  is kept verbatim and only one event is written;
* the same cid coming back from a later triage does not revive it;
* one issue that exists under both the legacy and the qualified key is
  dismissed under both, and a second command may still add the key it missed;
* a save that fails exits 2, names the failed key and writes no success event;
* the union of two side tables is commutative.

Every test uses its own temporary state root and never touches the production
``state/``. The lifecycle stream is injected explicitly because ``--state-dir``
does not isolate it.

Run: ``python -m pytest tests/test_needs_person_resolved.py -q``
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from super_agent import cli, config, needs
from super_agent import store as store_mod
from super_agent.store import Store

THREAD = "owner/demo#1"

FIXTURE = {"issues": {"1": {"title": "probe", "comments": [
    {"id": 9001, "body": "ordinary discussion, no markers at all", "author": "u"},
    {"id": 9100, "body": "[decision] X", "author": "u"},
]}}}

FIXTURE_TWO = {"issues": {"1": {"title": "probe", "comments": [
    {"id": 9001, "body": "ordinary discussion, no markers at all", "author": "u"},
    {"id": 9100, "body": "[decision] X", "author": "u"},
    {"id": 9101, "body": "[decision] Y", "author": "u"},
]}}}


class Base(unittest.TestCase):
    repo = "owner/demo"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.events = self.root / "lifecycle" / "events.jsonl"
        self.events.parent.mkdir(parents=True, exist_ok=True)
        self.events.write_text("", encoding="utf-8")
        self.store = Store(self.state)

    def tearDown(self):
        self.tmp.cleanup()

    # ------------------------------------------------------------- plumbing
    def cfg_path(self, fixture: dict, *, repo: str | None = None,
                 name: str = "cfg.json", allowed: bool = True) -> Path:
        path = self.root / "fixtures" / f"issue-{abs(hash(name))}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(fixture), encoding="utf-8")
        allow = self.root / "config" / f"allowlist-{name}"
        entry = {"repo": self.repo, "cwd": ".",
                 "mode": "observe", "creation_mode": "pm_created",
                 "issues": [1] if allowed else [],
                 "actions": ["bus_inject", "collect", "bind", "archive",
                             "reconcile"],
                 "max_active_writers": 1, "roles": ["worker", "qa"]}
        allow.parent.mkdir(parents=True, exist_ok=True)
        allow.write_text(json.dumps({
            "schema_version": 1,
            "defaults": {"mode": "observe", "creation_mode": "pm_created"},
            "projects": {"demo": entry}}), encoding="utf-8")
        cfg = {
            "role": "supervisor", "mode": "observe",
            "github": {"source": "fixture", "fixture": str(path),
                       "repo": self.repo if repo is None else repo},
            "engine": {"session_events_path": str(self.events),
                       "allowlist": ["issue_comment.created"],
                       "project_allowlist": str(allow),
                       "archived_crosscheck": False},
        }
        out = self.root / "cfg" / name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(cfg), encoding="utf-8")
        return out

    def run_cli(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = cli.main(argv + ["--state-dir", str(self.state),
                                  "--session-api", "none"])
        return rc, out.getvalue()

    def seed(self, fixture: dict = FIXTURE, *, repo: str | None = None,
             name: str = "cfg.json"):
        cfg = self.cfg_path(fixture, repo=repo, name=name)
        rc, text = self.run_cli(["once", "--config", str(cfg)])
        self.assertIn(rc, (0,), text)
        return cfg

    def resolve(self, *extra, issue="1", cid=9100):
        return self.run_cli(["resolve", "--issue", issue, "--comment", str(cid),
                             "--by", "supervisor", *extra])

    def index(self) -> dict:
        return json.loads((self.state / "index.json").read_text("utf-8"))

    def rows(self) -> list:
        path = self.state / "events.jsonl"
        if not path.is_file():
            return []
        return [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]

    def of_type(self, kind: str) -> list:
        return [r for r in self.rows() if r.get("type") == kind]

    def status(self, *extra) -> tuple:
        return self.run_cli(["status", *extra])

    def entries(self, key: str = THREAD) -> list:
        return (self.index()["issues"].get(key) or {}).get("needs_person") or []


class Seeded(Base):
    def test_seed_really_writes_one_entry(self):
        self.seed()
        rows = self.entries()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["comment_id"], 9100)
        rc, text = self.status()
        self.assertEqual(rc, 0)
        self.assertIn("9100", text)
        self.assertNotIn("无", text)


class DismissHidesIt(Base):
    """Counter-example 1: after dismissing, the row is suppressed - not deleted."""

    def test_resolve_hides_it_and_replay_rebuilds_it(self):
        cfg = self.seed()
        rc, text = self.resolve()
        self.assertEqual(rc, 0, text)

        rc, text = self.status()
        self.assertEqual(rc, 0, text)
        self.assertIn("无", text)

        # replay rebuilds the row from the comment stream: suppressed, not gone.
        rc, text = self.run_cli(["replay", "--config", str(cfg), "--issue", "1"])
        self.assertEqual(rc, 0, text)
        self.assertIn("comments=2", text)
        self.assertEqual([r["comment_id"] for r in self.entries()], [9100])

        rc, text = self.status()
        self.assertIn("无", text)

    def test_brief_hides_it_too(self):
        from super_agent import brief as brief_mod
        self.seed()
        cfg = config.load(None, {
            "role": "supervisor", "github": {"source": "fixture"},
            "engine": {"session_events_path": str(self.events)}})
        # Section 5 lists *any* recent comment id, so the item is identified by
        # the needs_person reason, which only sections 3/4 print.
        before = brief_mod.render(brief_mod.build_brief(
            cfg, self.store, lwa_root=str(self.root)))
        self.assertIn("marker '[decision]'", before)
        self.resolve()
        after = brief_mod.render(brief_mod.build_brief(
            cfg, self.store, lwa_root=str(self.root)))
        self.assertNotIn("marker '[decision]'", after)

    def test_a_second_comment_with_the_same_marker_is_a_new_entry(self):
        cfg = self.cfg_path(FIXTURE_TWO)
        self.run_cli(["once", "--config", str(cfg)])
        self.resolve()
        rc, text = self.run_cli(["once", "--config", str(cfg)])
        self.assertEqual(rc, 0, text)
        rc, text = self.status()
        self.assertIn("9101", text)
        self.assertNotIn("9100", text)


class UndismissedStillShows(Base):
    """Counter-example 2: mutation ``is_open() -> True`` must turn this red."""

    def test_only_the_undismissed_entry_is_listed(self):
        cfg = self.cfg_path(FIXTURE_TWO)
        self.run_cli(["once", "--config", str(cfg)])
        rc, text = self.resolve()
        self.assertEqual(rc, 0, text)
        rc, text = self.status()
        self.assertEqual(rc, 0)
        self.assertIn("9101", text)
        self.assertNotIn("9100", text)


class Idempotent(Base):
    """Counter-example 3: mutation ``mark() -> always overwrite`` must turn this red."""

    def test_second_resolve_changes_nothing(self):
        self.seed()
        rc, text = self.resolve()
        self.assertEqual(rc, 0, text)
        first = self.index()["needs_person_resolved"]["owner/demo#1|9100"]

        rc, text = self.resolve()
        self.assertEqual(rc, 1, text)
        self.assertIn("already resolved", text)

        second = self.index()["needs_person_resolved"]["owner/demo#1|9100"]
        self.assertEqual(first, second)
        self.assertEqual(len(self.of_type("needs_person.resolved")), 1)


class NotRevived(Base):
    """Counter-example 4: the same cid triaged again stays dismissed."""

    def test_same_cid_coming_back_stays_hidden(self):
        cfg = self.seed()
        self.resolve()
        rc, text = self.run_cli(["replay", "--config", str(cfg), "--issue", "1"])
        self.assertEqual(rc, 0, text)
        rc, text = self.run_cli(["once", "--config", str(cfg)])
        self.assertEqual(rc, 0, text)
        rc, text = self.status()
        self.assertIn("无", text)
        self.assertEqual(self.index()["needs_person_resolved"]["owner/demo#1|9100"]
                         ["state"], "resolved")


class DuplicateRows(Base):
    """One cid, N rows: one dismissal folds them all, and says how many."""

    def test_two_rows_one_key_one_event(self):
        self.seed()
        index = self.store.load_index()
        index["issues"][THREAD]["needs_person"].append(
            {"comment_id": 9100, "reason": "route.undeliverable",
             "at": "2026-11-10T10:00:01+00:00"})
        self.store.save_index(index)
        rc, text = self.status()
        self.assertEqual(text.count("9100"), 2, text)

        rc, text = self.resolve()
        self.assertEqual(rc, 0, text)
        # W/A count KEYS, M counts ROWS: one key, two folded rows.
        self.assertIn("1 keys: 0 already, 1 newly resolved (2 entries folded)", text)
        self.assertEqual(
            self.index()["needs_person_resolved"]["owner/demo#1|9100"]["entries"], 2)
        self.assertEqual(len(self.of_type("needs_person.resolved")), 1)
        rc, text = self.status()
        self.assertIn("无", text)


class TwoKeyForms(Base):
    """Counter-example 5: one issue under a bare and a qualified key."""

    def make_two_keys(self) -> None:
        bare = self.cfg_path(FIXTURE, repo="", name="cfg_a.json", allowed=False)
        self.run_cli(["once", "--config", str(bare)])
        self.assertIn("1", self.index()["issues"])
        self.assertNotIn(THREAD, self.index()["issues"])
        qualified = self.cfg_path(FIXTURE, name="cfg_b.json")
        self.run_cli(["once", "--config", str(qualified)])
        self.assertEqual(sorted(self.index()["issues"]), ["1", THREAD])
        self.assertEqual(len(self.entries("1")), 1)
        self.assertEqual(len(self.entries(THREAD)), 1)

    def test_one_command_dismisses_both_keys(self):
        self.make_two_keys()
        rc, text = self.resolve()
        self.assertEqual(rc, 0, text)
        self.assertIn("2 keys: 0 already, 2 newly resolved", text)
        marks = self.index()["needs_person_resolved"]
        self.assertEqual(sorted(marks), ["1|9100", "owner/demo#1|9100"])
        rc, text = self.status()
        self.assertIn("无", text)
        self.assertEqual(len(self.of_type("needs_person.resolved")), 1)

    def test_legacy_first_then_the_whole_issue(self):
        # Only the bare key exists: dismiss it, then let the round copy the key
        # forward. The qualified row is a *new* visible row and must stay so
        # until a second command dismisses it.
        bare = self.cfg_path(FIXTURE, repo="", name="cfg_a.json", allowed=False)
        self.run_cli(["once", "--config", str(bare)])
        rc, text = self.resolve()
        self.assertEqual(rc, 0, text)

        qualified = self.cfg_path(FIXTURE, name="cfg_b.json")
        self.run_cli(["once", "--config", str(qualified)])
        rc, text = self.status()
        self.assertEqual(text.count("9100"), 1, text)     # the qualified row

        rc, text = self.resolve()
        self.assertEqual(rc, 0, text)
        self.assertIn("2 keys: 1 already, 1 newly resolved", text)
        rc, text = self.status()
        self.assertIn("无", text)
        self.assertEqual(len(self.of_type("needs_person.resolved")), 2)

    def test_no_matching_key_exits_1_with_the_key_list(self):
        self.seed()
        rc, text = self.resolve(cid=9199)
        self.assertEqual(rc, 1, text)
        self.assertIn("9100", text)

    def test_unknown_issue_lists_the_keys_present(self):
        self.seed()
        rc, text = self.resolve(issue="owner/demo#7")
        self.assertEqual(rc, 1, text)
        self.assertIn(THREAD, text)


class SaveFails(Base):
    """Counter-example 5c: a failed save must exit 2, name the key, write no success."""

    def test_fault_hook_exits_2_and_keeps_the_index(self):
        self.seed()
        before = self.index()

        def boom(_tmp):
            raise OSError("disk full")

        store_mod.FAULT_HOOK = boom
        try:
            rc, text = self.resolve()
        finally:
            store_mod.FAULT_HOOK = None

        self.assertEqual(rc, 2, text)
        self.assertIn("failed", text)
        self.assertIn(THREAD, text)
        # The in-memory copy was dirtied by merge_into_side before the save, so
        # the assertion must re-read the file, not the variable.
        after = store_mod.Store(self.state).load_index()
        self.assertNotIn("needs_person_resolved", after)
        self.assertEqual(before["issues"], after["issues"])
        self.assertEqual(len(self.of_type("needs_person.resolve_failed")), 1)
        self.assertEqual(self.of_type("needs_person.resolved"), [])

    def test_a_silent_drop_is_caught_by_the_read_back(self):
        self.seed()

        def drop(_tmp):
            return None

        real = store_mod.atomic_write_text

        def write_without_marks(path, text):
            if path.name == "index.json":
                data = json.loads(text)
                data.pop("needs_person_resolved", None)
                text = json.dumps(data, ensure_ascii=False, indent=1,
                                  sort_keys=True) + "\n"
            return real(path, text)

        store_mod.atomic_write_text = write_without_marks
        try:
            rc, text = self.resolve()
        finally:
            store_mod.atomic_write_text = real
        self.assertEqual(rc, 2, text)


class EntriesZero(Base):
    """``entries=0`` means: rows at the first read, gone from disk by the save."""

    def test_rows_cleared_between_the_two_reads(self):
        self.seed()
        real = Store.load_index
        calls = {"n": 0}

        def load(self_):
            calls["n"] += 1
            data = real(self_)
            if calls["n"] == 2:      # the re-read just before the save
                for rec in (data.get("issues") or {}).values():
                    rec.pop("needs_person", None)
            return data

        Store.load_index = load
        try:
            rc, text = self.resolve()
        finally:
            Store.load_index = real

        self.assertEqual(rc, 0, text)
        self.assertEqual(
            self.index()["needs_person_resolved"]["owner/demo#1|9100"]["entries"], 0)
        self.assertEqual(len(self.of_type("needs_person.resolved")), 1)
        self.assertEqual(len(self.of_type("needs_person.resolve_raced")), 1)


class Audit(Base):
    def test_all_shows_both_timestamps_and_the_dismissal(self):
        self.seed()
        entry_at = self.index()["issues"][THREAD]["needs_person"][0]["at"]
        self.resolve("--comment-ref", "9200")
        rc, text = self.status("--all")
        self.assertEqual(rc, 0, text)
        self.assertIn("resolved_at=", text)
        self.assertIn("entry_at=", text)
        self.assertIn(f"entry_at={entry_at}", text)
        self.assertIn("by supervisor", text)
        self.assertIn("via comment 9200", text)
        self.assertIn("entries=1", text)

    def test_replay_does_not_touch_the_dismissal_time(self):
        cfg = self.seed()
        self.resolve()
        before = self.index()["needs_person_resolved"]["owner/demo#1|9100"]["at"]
        rc, text = self.run_cli(["replay", "--config", str(cfg), "--issue", "1"])
        self.assertEqual(rc, 0, text)
        after = self.index()["needs_person_resolved"]["owner/demo#1|9100"]["at"]
        self.assertEqual(before, after)

    def test_event_counts_keys_and_skipped_entries(self):
        bare = self.cfg_path(FIXTURE, repo="", name="cfg_a.json", allowed=False)
        self.run_cli(["once", "--config", str(bare)])
        self.resolve()
        qualified = self.cfg_path(FIXTURE, name="cfg_b.json")
        self.run_cli(["once", "--config", str(qualified)])
        self.resolve()
        events = self.of_type("needs_person.resolved")
        self.assertEqual(len(events), 2)
        self.assertEqual(events[-1]["keys"], [THREAD])
        self.assertEqual(events[-1]["already"], ["1"])
        self.assertEqual(events[-1]["entries_skipped"], 1)


class StatusSnapshot(Base):
    """The default ``status`` shape must not change: this is the byte-level guard."""

    def test_default_output_is_byte_identical(self):
        self.store.save_index({
            "version": 1, "meta": {},
            "issues": {THREAD: {"state": "needs_person", "needs_person": [
                {"comment_id": 9100, "reason": "marker '[decision]'",
                 "at": "2026-11-10T10:00:00+00:00"},
                {"comment_id": 9101, "reason": "marker '[blocked]'",
                 "at": "2026-11-10T10:00:05+00:00"}]}},
            "assignments": {},
            "needs_person_resolved": {"owner/demo#1|9100": {
                "state": "resolved", "reason": "marker '[decision]'",
                "at": "2026-11-10T12:00:00+00:00", "by": "supervisor",
                "by_comment": 9200, "superseded_by": None, "entries": 1}},
        })
        rc, text = self.status()
        self.assertEqual(rc, 0)
        self.assertEqual(text, (
            "status  role=supervisor mode=observe external_actions=0\n"
            "- issue #owner/demo#1 comment 9101 (marker '[blocked]') "
            "at 2026-11-10T10:00:05+00:00\n"))

    def test_all_adds_the_audit_to_every_dismissed_row(self):
        self.store.save_index({
            "version": 1, "meta": {},
            "issues": {THREAD: {"state": "needs_person", "needs_person": [
                {"comment_id": 9100, "reason": "marker '[decision]'",
                 "at": "2026-11-10T10:00:00+00:00"}]}},
            "assignments": {},
            "needs_person_resolved": {"owner/demo#1|9100": {
                "state": "resolved", "reason": "marker '[decision]'",
                "at": "2026-11-10T12:00:00+00:00", "by": "supervisor",
                "by_comment": 9200, "superseded_by": None, "entries": 1}},
        })
        rc, text = self.status("--all")
        self.assertEqual(rc, 0, text)
        self.assertIn("- issue #owner/demo#1 comment 9100 (marker '[decision]') "
                      "at 2026-11-10T10:00:00+00:00", text)
        self.assertIn("resolved_at=2026-11-10T12:00:00+00:00", text)


class Arbitration(Base):
    """§2.4: the union rule, both directions."""

    def row(self, state, at, by, entries=1):
        return {"state": state, "reason": "r", "at": at, "by": by,
                "by_comment": None, "superseded_by": None, "entries": entries}

    def merge(self, left, right):
        import copy
        out = {"needs_person_resolved": {"k": copy.deepcopy(left)}}
        needs.merge_into(out, {"needs_person_resolved": {"k": copy.deepcopy(right)}})
        return out["needs_person_resolved"]["k"]

    def test_a_persons_ruling_beats_an_earlier_automatic_one(self):
        auto = self.row("superseded", "2026-11-10T09:00:00+00:00", "engine")
        human = self.row("resolved", "2026-11-10T12:00:00+00:00", "supervisor")
        self.assertEqual(self.merge(auto, human)["state"], "resolved")
        self.assertEqual(self.merge(human, auto)["state"], "resolved")

    def test_same_state_earlier_at_wins(self):
        early = self.row("resolved", "2026-11-10T09:00:00+00:00", "zoe")
        late = self.row("resolved", "2026-11-10T12:00:00+00:00", "amy")
        self.assertEqual(self.merge(early, late)["by"], "zoe")
        self.assertEqual(self.merge(late, early)["by"], "zoe")

    def test_same_state_same_at_is_broken_by_by(self):
        a = self.row("superseded", "2026-11-10T09:00:00+00:00", "amy")
        b = self.row("superseded", "2026-11-10T09:00:00+00:00", "zoe")
        self.assertEqual(self.merge(a, b)["by"], "amy")
        self.assertEqual(self.merge(b, a)["by"], "amy")

    def test_entries_is_the_maximum(self):
        a = self.row("resolved", "2026-11-10T09:00:00+00:00", "amy", entries=1)
        b = self.row("resolved", "2026-11-10T09:00:00+00:00", "amy", entries=5)
        self.assertEqual(self.merge(a, b)["entries"], 5)
        self.assertEqual(self.merge(b, a)["entries"], 5)

    def test_union_is_commutative(self):
        cases = [
            (self.row("superseded", "2026-11-10T09:00:00+00:00", "engine"),
             self.row("resolved", "2026-11-10T12:00:00+00:00", "supervisor", 3)),
            (self.row("resolved", "2026-11-10T09:00:00+00:00", "zoe"),
             self.row("resolved", "2026-11-10T09:00:00+00:00", "amy", 2)),
            (self.row("superseded", "2026-11-10T09:00:00+00:00", "engine"),
             self.row("superseded", "2026-11-10T11:00:00+00:00", "engine")),
        ]
        for left, right in cases:
            self.assertEqual(self.merge(left, right), self.merge(right, left))

    def test_merge_never_drops_a_key(self):
        import copy
        out = {"needs_person_resolved": {
            "a|1": self.row("resolved", "2026-11-10T09:00:00+00:00", "x"),
            "b|2": self.row("resolved", "2026-11-10T09:00:00+00:00", "x")}}
        needs.merge_into(out, {"needs_person_resolved": {
            "c|3": self.row("resolved", "2026-11-10T09:00:00+00:00", "y")}})
        self.assertEqual(sorted(out["needs_person_resolved"]), ["a|1", "b|2", "c|3"])
        self.assertEqual(copy.deepcopy(out)["needs_person_resolved"]["c|3"]["by"], "y")

    def test_merge_into_side_writes_only_the_marks_key(self):
        index = {"version": 1, "meta": {"x": 1}, "issues": {"o/r#1": {"a": 1}},
                 "assignments": {"asg": {"b": 2}}}
        needs.merge_into_side(index, {"o/r#1|9100": self.row(
            "resolved", "2026-11-10T09:00:00+00:00", "x")})
        self.assertEqual(index["meta"], {"x": 1})
        self.assertEqual(index["issues"], {"o/r#1": {"a": 1}})
        self.assertEqual(index["assignments"], {"asg": {"b": 2}})


class Unit(Base):
    def test_is_open_without_a_comment_id_stays_true(self):
        self.assertTrue(needs.is_open({"needs_person_resolved": {}}, THREAD,
                                      {"reason": "no id"}))

    def test_marks_never_creates_the_key(self):
        index = {}
        self.assertEqual(needs.marks(index), {})
        self.assertNotIn(needs.MARKS, index)

    def test_mark_rejects_an_unknown_state(self):
        with self.assertRaises(ValueError):
            needs.mark({}, THREAD, 9100, state="gone", by="x")


class EngineSupersedes(Base):
    """A route that finally lands supersedes the rows it left behind."""

    def test_supersede_marks_only_this_cids_undeliverable_rows(self):
        from super_agent.engine import Engine
        engine = object.__new__(Engine)
        engine.store = self.store
        index = {"issues": {THREAD: {"needs_person": [
            {"comment_id": 9100, "reason": "route.undeliverable", "at": "t1"},
            {"comment_id": 9100, "reason": "marker '[decision]'", "at": "t2"},
            {"comment_id": 9101, "reason": "route.undeliverable", "at": "t3"},
        ]}}}
        engine._supersede_route(index, THREAD, 9100, "route:1")
        marks = index["needs_person_resolved"]
        self.assertEqual(list(marks), ["owner/demo#1|9100"])
        self.assertEqual(marks["owner/demo#1|9100"]["state"], "superseded")
        self.assertEqual(marks["owner/demo#1|9100"]["entries"], 1)
        self.assertEqual(marks["owner/demo#1|9100"]["superseded_by"], "route:1")
        self.assertEqual(len(self.of_type("needs_person.superseded")), 1)

    def test_supersede_is_a_no_op_without_such_rows(self):
        from super_agent.engine import Engine
        engine = object.__new__(Engine)
        engine.store = self.store
        index = {"issues": {THREAD: {"needs_person": [
            {"comment_id": 9100, "reason": "marker '[decision]'", "at": "t"}]}}}
        engine._supersede_route(index, THREAD, 9100, "route:1")
        self.assertNotIn("needs_person_resolved", index)
        self.assertEqual(self.of_type("needs_person.superseded"), [])


if __name__ == "__main__":
    unittest.main()
