"""L1 tests for the read-only ``reporter`` (ISS-49).

Every test builds its own state root under a temporary directory and injects
it explicitly; the production ``state/`` is never read or written.

What is pinned here, in the order the acceptance list states it:

* a source that cannot be read is shown as ``未知``, never as ``无需行动`` -
  including a present-but-broken ``outbox_state.json`` (which the store's
  reader would swallow into an empty skeleton);
* the ``无需行动`` predicate is exactly ``index_ok ∧ outbox_ok ∧ counts == 0``,
  and ``health`` is reported on its own line instead of joining the conjunction;
* a dict-shaped but useless dismissal row still shows the item (the gap
  ``needs.is_open`` leaves, closed additively here);
* dedupe normalises through ``issuekey.issue_number`` so a bare key and a
  qualified key, a str and an int, are one row - and the raw key is kept for
  display;
* ``brief._classify`` is not drifted from: the table is written out literally,
  not ``assert f(x) == f(x)``;
* a real dismissal removes the row and ``needs.is_open`` agrees;
* no socket is needed, and running the collector writes nothing except the
  allowlist last-good cache the engine already maintains;
* the page is a self-contained HTML document: escaped text, no credential
  string, no plaintext ``http://`` link, no script and no inline handler.

Test rule for ``outbox_state.json``
-----------------------------------

Any test that involves the outbox **must produce the file through the real
store** (``Store.begin_action`` / ``mark_*``), never by hand-writing an
``actions`` dict. Hand-written records invent field combinations production
cannot create - ``comment_id`` on an ``uncertain`` action is the one that bit
us - so a green test built that way proves nothing about the real page. That
is exactly how the "one event reported twice" defect survived a passing
suite.

Run: ``python -m pytest tests/test_reporter.py -q``
"""
from __future__ import annotations

import json
import re
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from super_agent import brief as brief_mod
from super_agent import config
from super_agent import needs as needs_mod
from super_agent import reporter as rep
from super_agent import assignments as asg_mod
from super_agent.assignments import ACTIVE_STATES
from super_agent.engine import Engine
from super_agent.github_source import NullSource
from super_agent.store import Store

THREAD = "owner/demo#7"
ROLE = "supervisor"


def _cfg(**over) -> dict:
    base = {"role": ROLE, "github": {"source": "none"},
            "store": {"max_event_lines": 1000, "max_outbox_lines": 1000}}
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = {**base[key], **value}
        else:
            base[key] = value
    return config.load(None, base)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.state = self.root / "state"
        self.store = Store(self.state)
        self.cfg = _cfg()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ------------------------------------------------------------- fixtures
    def engine(self, *, index=None, cfg=None) -> Engine:
        if index is not None:
            self.write_index(index)
        return Engine(cfg or self.cfg, self.store, NullSource())

    def write_index(self, data, *, path: Path | None = None) -> Path:
        target = path or self.store.index_path
        target.write_text(json.dumps(data), encoding="utf-8")
        return target

    def begin_uncertain(self, aid: str, *, verb: str, input_ref: str,
                        issue: int = 7, assignment_id: str | None = None) -> dict:
        """A genuinely ``uncertain`` action, written by the store itself.

        This is the shape production produces: ``begin_action`` then
        ``mark_uncertain``. Note what the record is *missing* - ``comment_id``
        never reaches the state file on this path - which is the whole reason
        ``input_ref`` is parsed for a delivery id.
        """
        rec = self.store.begin_action(aid, verb=verb, kind=verb,
                                      assignment_id=assignment_id,
                                      input_ref=input_ref, issue=issue)
        self.store.mark_uncertain(rec["action_id"], "unconfirmed")
        return self.store.outbox_action(rec["action_id"]) or {}

    def index_with(self, rows, *, key=THREAD, assignments=None) -> dict:
        return {"version": 1, "meta": {},
                "issues": {key: {"state": "needs_person", "needs_person": rows}},
                "assignments": assignments or {}}

    def report(self, engine=None, **kw):
        engine = engine or self.engine(index=self.index_with([]))
        return rep.collect(engine, **kw)


class SourceHealth(Base):
    """P1-1: ``is_file`` decides existence; a reader's empty dict does not."""

    def test_missing_index_is_unknown_not_empty(self):
        engine = self.engine()
        health = rep.probe_source_health(self.store)
        self.assertFalse(health["index"]["exists"])
        self.assertFalse(health["index"]["valid"])
        self.assertIn("不存在", health["index"]["error"])
        report = rep.collect(engine)
        self.assertEqual(report["status"], "unknown")
        self.assertNotIn("无需行动", rep.render(report))

    def test_broken_json_index_is_unknown(self):
        self.store.index_path.write_text("{not json", encoding="utf-8")
        health = rep.probe_source_health(self.store)
        self.assertTrue(health["index"]["exists"])
        self.assertFalse(health["index"]["valid"])
        report = self.report(engine=self.engine())
        self.assertEqual(report["status"], "unknown")
        self.assertIn("不是合法 JSON", " ".join(report["unknown_reasons"]))

    def test_empty_object_index_is_an_empty_index(self):
        """``{}`` is a JSON object with no keys: defaults make it readable.

        This is the one case where "present but empty" is *not* unknown - an
        index with no ``issues`` key genuinely lists no issue. A file that is
        absent, truncated or structurally wrong is the unknown case (below).
        """
        self.store.index_path.write_text("{}", encoding="utf-8")
        report = self.report(engine=self.engine())
        self.assertTrue(rep.probe_source_health(self.store)["index"]["valid"])
        self.assertEqual(report["status"], "none")
        self.assertIn("无需行动", rep.render(report))

    def test_broken_outbox_state_is_unknown(self):
        self.engine(index=self.index_with([]))
        self.store.outbox_state_path.write_text("{truncated", encoding="utf-8")
        report = rep.collect(Engine(self.cfg, self.store, NullSource()))
        health = rep.probe_source_health(self.store)
        self.assertFalse(health["outbox"]["valid"])
        self.assertFalse(report["outbox_ok"])
        self.assertEqual(report["status"], "unknown")
        self.assertNotIn("无需行动", rep.render(report))

    def test_health_missing_does_not_change_status(self):
        engine = self.engine(index=self.index_with([]))
        report = rep.collect(engine)
        self.assertFalse(report["sources"]["health"]["exists"])
        self.assertEqual(report["status"], "none")
        page = rep.render(report)
        self.assertIn("health", page)
        self.assertIn("缺失", page)
        self.assertIn("无需行动", page)

    def test_health_criterion_is_the_file_not_health_read(self):
        """Behaviour, not a mock: ``health.read`` cannot produce either answer.

        ``health.read`` returns the same ``{}`` for a corrupt file and for an
        absent one, so it can never distinguish these two cases. Patching it
        proves nothing (a mutant that called it would stay green); asserting the
        two different verdicts does.
        """
        self.engine(index=self.index_with([]))
        self.store.health_path.write_text(json.dumps({"pid": 1}), encoding="utf-8")
        partial = rep.collect(Engine(self.cfg, self.store, NullSource()))
        self.assertEqual(partial["engine"]["health"]["state"], "ok")
        self.assertTrue(rep.probe_source_health(self.store)["health"]["valid"])

        self.store.health_path.write_text("{}", encoding="utf-8")
        empty = rep.collect(Engine(self.cfg, self.store, NullSource()))
        self.assertEqual(empty["engine"]["health"]["state"], "bad")
        self.assertFalse(rep.probe_source_health(self.store)["health"]["valid"])
        self.assertIn("空对象", empty["sources"]["health"]["error"])
        self.assertNotEqual(partial["engine"]["health"], empty["engine"]["health"])

    def test_missing_outbox_is_an_empty_outbox(self):
        engine = self.engine(index=self.index_with([]))
        report = rep.collect(engine)
        self.assertTrue(report["outbox_ok"])
        self.assertTrue(any("不存在" in w for w in report["warnings"]))
        self.assertEqual(report["status"], "none")


class NoActionPredicate(Base):
    """P1-2: ``index_ok ∧ outbox_ok ∧ counts == 0``, health excluded."""

    def test_exactly_the_conjunction(self):
        cases = [
            (True, True, 0, "none"),
            (True, True, 1, "action"),
            (False, True, 0, "unknown"),
            (True, False, 0, "unknown"),
            (False, False, 0, "unknown"),
        ]
        for index_ok, outbox_ok, rows, expected in cases:
            with self.subTest(index_ok=index_ok, outbox_ok=outbox_ok, rows=rows):
                with tempfile.TemporaryDirectory() as tmp:
                    store = Store(Path(tmp) / "s")
                    self.store = store
                    engine = self.engine(
                        index=self.index_with([{"comment_id": 11, "reason": "x"}]
                                              if rows else []))
                    if not index_ok:
                        store.index_path.write_text("{bad", encoding="utf-8")
                    if not outbox_ok:
                        store.outbox_state_path.write_text("{bad", encoding="utf-8")
                    report = rep.collect(engine)
                    self.assertEqual(report["status"], expected)

    def test_health_is_a_separate_line(self):
        self.store.health_path.write_text("{bad", encoding="utf-8")
        report = self.report()
        self.assertEqual(report["status"], "none")
        page = rep.render(report)
        self.assertIn("无需行动", page)
        self.assertIn("health", page)
        self.assertIn("坏", page)


class DamagedDismissalRow(Base):
    """P1-3: a dict-shaped non-ruling must not hide the item.

    The real fix belongs in ``needs.is_open`` (it should accept only a row whose
    ``state`` is a real state); this ticket may not edit ``needs.py``, so the
    reporter re-adds exactly those rows. Backlog: needs.py validation.
    """

    def test_bad_side_table_values_still_show(self):
        for bad in ({}, {"state": "bogus"}, {"state": None}):
            with self.subTest(bad=bad):
                with tempfile.TemporaryDirectory() as tmp:
                    store = Store(Path(tmp) / "s")
                    self.store = store
                    key = needs_mod.entry_key(THREAD, 11)
                    index = self.index_with([{"comment_id": 11,
                                              "reason": "needs a person"}])
                    index[needs_mod.MARKS] = {key: bad}
                    engine = self.engine(index=index)
                    report = rep.collect(engine)
                    self.assertEqual(report["counts"][rep.CATEGORY_ROLE], 1)
                    page = rep.render(report)
                    self.assertIn("comment 11", page)

    def test_real_ruling_stays_suppressed(self):
        for state in needs_mod.STATES:
            with self.subTest(state=state):
                with tempfile.TemporaryDirectory() as tmp:
                    store = Store(Path(tmp) / "s")
                    self.store = store
                    index = self.index_with([{"comment_id": 11, "reason": "x"}])
                    index[needs_mod.MARKS] = {
                        needs_mod.entry_key(THREAD, 11): {"state": state,
                                                          "at": "2026-01-01T00:00:00+00:00"}}
                    engine = self.engine(index=index)
                    report = rep.collect(engine)
                    self.assertEqual(report["counts"][rep.CATEGORY_ROLE], 0)


class Dedupe(Base):
    """P1 (repair round): identity must never collapse two different items.

    The old key was ``(issue number, cid)``, so every ``cid=None`` row shared one
    key and all but the first vanished from the page. These tests pin the three
    rules of ``_signature`` instead: no cid means its own row, a cid pairs with
    the *thread* (repo + number), and a bare number matches at most one repo.
    """

    def _seed(self, index):
        self.write_index(index)
        return Engine(self.cfg, self.store, NullSource())

    def test_two_outbox_actions_are_two_rows(self):
        """Counterexample 2: two cid-less actions must not become one row."""
        self.write_index(self.index_with([]))
        self.begin_uncertain("a-archive", verb="archive", input_ref="inject:9")
        self.begin_uncertain("a-comment", verb="comment", input_ref="not-a-number")
        report = rep.collect(Engine(self.cfg, self.store, NullSource()))
        self.assertEqual(len(report["items"]), 2)
        page = rep.render(report)
        for locator in ("a-archive", "a-comment"):
            self.assertIn(locator, page)
        for reason in ("unconfirmed",):
            self.assertIn(reason, page)

    def test_same_number_in_two_repos_are_two_rows(self):
        """Counterexample 3: same issue number, different repos: not one thread."""
        index = {"version": 1, "meta": {}, "assignments": {},
                 "issues": {
                     "a/x#7": {"needs_person": [{"comment_id": 11,
                                                 "reason": "repo a needs a person"}]},
                     "b/y#7": {"needs_person": [{"comment_id": 11,
                                                 "reason": "repo b needs a person"}]},
                 }}
        engine = self._seed(index)
        report = rep.collect(engine)
        self.assertEqual(len(report["items"]), 2)
        page = rep.render(report)
        for reason in ("repo a needs a person", "repo b needs a person"):
            self.assertIn(reason, page)

    def test_bare_key_is_one_row_only_when_one_repo_holds_it(self):
        single = {"version": 1, "meta": {}, "assignments": {},
                  "issues": {"7": {"needs_person": [{"comment_id": 11,
                                                     "reason": "bare"}]},
                             THREAD: {"needs_person": [{"comment_id": 11,
                                                        "reason": "qualified"}]}}}
        engine = self._seed(single)
        self.assertEqual(len(rep.collect(engine)["items"]), 1)
        ambiguous = {"version": 1, "meta": {}, "assignments": {},
                     "issues": {"7": {"needs_person": [{"comment_id": 11,
                                                        "reason": "bare"}]},
                                "a/x#7": {"needs_person": [{"comment_id": 11,
                                                            "reason": "repo a"}]},
                                "b/y#7": {"needs_person": [{"comment_id": 11,
                                                            "reason": "repo b"}]}}}
        engine = self._seed(ambiguous)
        self.assertEqual(len(rep.collect(engine)["items"]), 3)

    def test_failed_archive_is_one_row_not_two(self):
        """Counterexample 1 (the P1): the engine's failure path writes both layers.

        ``_mark_blocked(cid=55)`` goes into the assignment's ``tombstone``, and
        ``store.mark_uncertain`` writes an outbox record that carries the
        delivery id *only* in ``input_ref`` (``Store._transition`` never copies
        ``comment_id`` on this path). Without reading ``input_ref`` the two rows
        can never share a signature and the same failure is shown twice.

        The record is produced by the real store, so if the store ever starts
        writing ``comment_id`` this test keeps working for the right reason.
        """
        self.write_index(self.index_with([], assignments={
            "asg_7_worker_1": {"id": "asg_7_worker_1", "thread": THREAD,
                               "issue": 7, "role": "worker", "status": "blocked",
                               "updated_at": "2026-01-01T00:00:00+00:00",
                               "tombstone": {"reason": "archive unconfirmed",
                                             "cid": 55,
                                             "at": "2026-01-01T00:00:00+00:00",
                                             "attempt": 1}},
        }))
        rec = self.begin_uncertain("asg_7_worker_1:archive:55", verb="archive",
                                   input_ref="55", assignment_id="asg_7_worker_1")
        self.assertNotIn("comment_id", rec)      # the store does not write it
        self.assertEqual(rec["input_ref"], "55")

        report = rep.collect(Engine(self.cfg, self.store, NullSource()))
        self.assertEqual(len(report["items"]), 1)
        counts = report["counts"]
        self.assertEqual(counts[rep.CATEGORY_ROLE] + counts[rep.CATEGORY_BLOCKED], 1)
        self.assertEqual(counts[rep.CATEGORY_BLOCKED], 1)
        self.assertEqual(sorted(report["items"][0]["also_from"]), ["outbox"])
        page = rep.render(report)
        self.assertIn("asg_7_worker_1", page)
        self.assertIn("archive unconfirmed", page)

    def test_non_numeric_input_ref_does_not_merge(self):
        """A block with no delivery id and an unrelated action stay two rows."""
        self.write_index(self.index_with([], assignments={
            "asg_7_worker_1": {"id": "asg_7_worker_1", "thread": THREAD,
                               "issue": 7, "role": "worker", "status": "blocked",
                               "updated_at": "2026-01-01T00:00:00+00:00",
                               "tombstone": {"reason": "no sid recorded", "cid": 55,
                                             "at": "2026-01-01T00:00:00+00:00"}},
        }))
        self.begin_uncertain("a-comment", verb="comment", input_ref="inject:3")
        report = rep.collect(Engine(self.cfg, self.store, NullSource()))
        self.assertEqual(len(report["items"]), 2)

    def test_missing_comment_id_on_index_rows_keeps_both(self):
        """Counterexample 4 (P2-1): two cid-less rows under one key, both shown."""
        self.write_index(self.index_with([
            {"reason": "row-A-no-cid"},
            {"reason": "row-B-no-cid"},
        ]))
        report = rep.collect(Engine(self.cfg, self.store, NullSource()))
        self.assertEqual(len(report["items"]), 2)
        page = rep.render(report)
        for reason in ("row-A-no-cid", "row-B-no-cid"):
            self.assertIn(reason, page)

    def test_display_keeps_the_raw_key(self):
        index = self.index_with([{"comment_id": 11, "reason": "x"}], key="7")
        engine = self._seed(index)
        report = rep.collect(engine)
        self.assertEqual(report["items"][0]["key"], "7")
        self.assertIn("7", rep.render(report))

    def test_bare_and_qualified_keys_are_one_row(self):
        index = {"version": 1, "meta": {}, "assignments": {},
                 "issues": {
                     "7": {"needs_person": [{"comment_id": 11, "reason": "bare"}]},
                     THREAD: {"needs_person": [{"comment_id": 11,
                                                "reason": "qualified"}]},
                 }}
        engine = self._seed(index)
        report = rep.collect(engine)
        self.assertEqual(len(report["items"]), 1)
        self.assertIn(report["items"][0]["key"], ("7", THREAD))

    def test_int_and_str_keys_are_one_row(self):
        """Called on ``_dedupe`` directly: ``json.dumps`` would collapse the keys.

        A dict literal with ``7`` and ``"7"`` cannot reach the store as two keys -
        JSON has only string keys, so writing then reading back leaves one entry
        and the assertion would pass no matter what ``_signature`` does.
        """
        items = [
            self._item_for_key(7, "int"),
            self._item_for_key("7", "str"),
        ]
        self.assertEqual(len(rep._dedupe(items)), 1)

    def _item_for_key(self, key, reason):
        locator = f"{key} comment 11"
        return rep._item(rep.CATEGORY_ROLE, "index", key=key, cid=11, at=None,
                         reason=reason, locator=locator)

class ClassifyDrift(Base):
    """P1-5: the expected table is written down, not re-derived.

    .. note::

       ``EXPECTED`` below is a **contract, not a snapshot of the implementation**.
       Editing an entry changes product semantics (which bucket a real person
       sees an item in), so it needs a design review - not a quick green test.
       The table is deliberately duplicated here instead of being imported from
       the module: an expectation derived from the code cannot drift-detect.
    """

    EXPECTED = {
        "[user] 需要用户拍板": rep.CATEGORY_USER,
        "等用户确认": rep.CATEGORY_USER,
        "用户拍板": rep.CATEGORY_USER,
        "[release] 发版前阻塞": rep.CATEGORY_ROLE,
        "[urgent] 紧急": rep.CATEGORY_ROLE,
        "marker '[decision]'": rep.CATEGORY_ROLE,
        "review needs a person": rep.CATEGORY_ROLE,
        "USER asks": rep.CATEGORY_ROLE,
        "": rep.CATEGORY_ROLE,
        None: rep.CATEGORY_ROLE,
    }

    def test_classify_does_not_drift(self):
        for reason, expected in self.EXPECTED.items():
            with self.subTest(reason=reason):
                self.assertEqual(rep._bucket_for_reason(reason), expected)

    def test_brief_classify_agrees(self):
        """The reporter's buckets must be ``brief``'s, not a copy of its tables."""
        user, urgent = brief_mod._markers({})
        for reason, expected in self.EXPECTED.items():
            with self.subTest(reason=reason):
                bucket = brief_mod._classify(reason, (user, urgent))
                want = (rep.CATEGORY_USER if bucket == brief_mod.PRIORITY_USER
                        else rep.CATEGORY_ROLE)
                self.assertEqual(want, expected)

    def test_marker_tables_are_not_duplicated(self):
        source = Path(rep.__file__).read_text(encoding="utf-8")
        self.assertNotIn('"用户拍板"', source)
        self.assertNotIn('"[release]"', source)


class ConfiguredMarkers(Base):
    """P2-2: markers added in the config must reach the page, not just ``brief``."""

    CUSTOM = "请老板拍板"

    def _cfg(self) -> dict:
        return _cfg(engine={"user_markers": list(brief_mod.USER_MARKERS)
                            + [self.CUSTOM]})

    def test_custom_marker_buckets_to_user(self):
        cfg = self._cfg()
        self.assertEqual(rep._bucket_for_reason(f"x {self.CUSTOM} y", cfg),
                         rep.CATEGORY_USER)
        self.assertEqual(rep._bucket_for_reason(f"x {self.CUSTOM} y"),
                         rep.CATEGORY_ROLE)   # defaults alone do not match it

    def test_custom_marker_applies_to_collected_items(self):
        engine = self.engine(
            index=self.index_with([{"comment_id": 11,
                                    "reason": f"[decision] {self.CUSTOM}"}]),
            cfg=self._cfg())
        report = rep.collect(engine)
        self.assertEqual(report["counts"][rep.CATEGORY_USER], 1)
        self.assertEqual(report["counts"][rep.CATEGORY_ROLE], 0)
        self.assertIn(self.CUSTOM, rep.render(report))

    def test_collect_passes_the_engine_cfg(self):
        """A cfg-blind reporter would show this under 裁决 instead of 拍板."""
        engine = self.engine(
            index=self.index_with([{"comment_id": 11, "reason": self.CUSTOM}]),
            cfg=self._cfg())
        self.assertEqual(rep.collect(engine)["items"][0]["category"],
                         rep.CATEGORY_USER)


class Dismissal(Base):
    """Counterexample A: a real dismissal removes the row."""

    def test_mark_removes_the_item(self):
        index = self.index_with([{"comment_id": 11, "reason": "x"}])
        engine = self.engine(index=index)
        before = rep.collect(engine)
        self.assertEqual(before["counts"][rep.CATEGORY_ROLE], 1)
        needs_mod.mark(index, THREAD, 11, state=needs_mod.RESOLVED, by="pm")
        self.store.save_index(index)
        reopened = self.store.load_index()
        self.assertFalse(needs_mod.is_open(reopened, THREAD, {"comment_id": 11}))
        after = rep.collect(engine)
        self.assertEqual(after["status"], "none")
        self.assertIn("无需行动", rep.render(after))


class Counterexamples(Base):
    """Counterexamples B and C."""

    def test_b_three_healthy_sources_with_zero_counts(self):
        engine = self.engine(index=self.index_with([]))
        # An empty outbox written by the store, not a hand-made dict: this is
        # what "nothing was ever dispatched" looks like on disk.
        self.store.save_outbox_state({"version": 1, "actions": {}, "attempts": {}})
        self.store.health_path.write_text(json.dumps(
            {"pid": 1, "mode": "observe", "running": True, "phase": "stopped"}),
            encoding="utf-8")
        report = rep.collect(engine)
        self.assertEqual(report["status"], "none")
        page = rep.render(report)
        self.assertIn("无需行动", page)
        self.assertNotIn("issue#", page)

    def test_c_broken_structures_do_not_raise(self):
        broken = {
            "issues is a list": {"version": 1, "meta": {}, "assignments": {},
                                 "issues": []},
            "record is not a dict": {"version": 1, "meta": {}, "assignments": {},
                                     "issues": {"7": "nope"}},
            "needs_person is a string": {
                "version": 1, "meta": {}, "assignments": {},
                "issues": {"7": {"needs_person": "nope"}}},
            "assignments record is a string": {
                "version": 1, "meta": {}, "issues": {},
                "assignments": {"asg_1": "nope"}},
        }
        for label, index in broken.items():
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory() as tmp:
                    store = Store(Path(tmp) / "s")
                    self.store = store
                    store.index_path.write_text(json.dumps(index), encoding="utf-8")
                    engine = Engine(self.cfg, store, NullSource())
                    report = rep.collect(engine)        # must not raise
                    self.assertEqual(report["status"], "unknown")
                    page = rep.render(report)
                    self.assertIn("未知", page)
                    self.assertNotIn("无需行动", page)

    def test_c_missing_index_renders_whole_page(self):
        report = self.report(engine=self.engine())
        page = rep.render(report)
        self.assertTrue(page.startswith("<!DOCTYPE html>"))
        self.assertIn("</html>", page)
        self.assertIn("未知", page)

    def test_status_raising_is_caught(self):
        engine = self.engine(index=self.index_with([]))
        with mock.patch.object(type(engine), "status",
                               side_effect=AttributeError("boom")):
            report = rep.collect(engine)
        self.assertEqual(report["status"], "unknown")
        self.assertIn("engine.status()", " ".join(report["unknown_reasons"]))
        self.assertIn("未知", rep.render(report))


class NoSideEffects(Base):
    """Counterexample D: no socket, and the state dir gains nothing new."""

    def test_no_socket_needed(self):
        engine = self.engine(index=self.index_with([]))
        with mock.patch.object(socket, "socket", side_effect=AssertionError("socket")):
            report, page = rep.collect_and_render(engine)
        self.assertEqual(report["status"], "none")
        self.assertIn("无需行动", page)

    def _snapshot(self) -> dict:
        return {p.name: (p.stat().st_size, p.stat().st_mtime_ns)
                for p in sorted(self.state.iterdir()) if p.is_file()}

    def test_collect_does_not_write_state(self):
        index = self.index_with([{"comment_id": 11, "reason": "x"}])
        allow = self.root / "allowlist.json"
        allow.write_text(json.dumps({
            "schema_version": 1,
            "defaults": {"mode": "observe", "creation_mode": "pm_created"},
            "projects": {"demo": {"repo": "owner/demo", "cwd": ".",
                                  "mode": "observe", "creation_mode": "pm_created",
                                  "issues": [7], "actions": ["bus_inject"],
                                  "max_active_writers": 1,
                                  "roles": ["worker", "qa"]}}}), encoding="utf-8")
        cfg = _cfg(engine={"project_allowlist": str(allow)})
        before = self._snapshot()
        engine = Engine(cfg, self.store, NullSource())
        engine.status(all=False)
        rep.collect(engine, source=NullSource())
        after = self._snapshot()
        changed = {name for name in after
                   if before.get(name) != after[name]}
        added = set(after) - set(before)
        self.assertLessEqual(changed | added, {"allowlist.last-good"})


class Rendering(Base):
    def _page_with(self, rows=None, *, reason="x") -> str:
        engine = self.engine(index=self.index_with(rows or []))
        return rep.render(rep.collect(engine))

    def test_escaping_and_redaction(self):
        index = self.index_with([{"comment_id": 11,
                                  "reason": "<script>alert(1)</script>"}])
        engine = self.engine(index=index)
        report = rep.collect(engine, source=_FakeComments({
            7: [{"id": 11, "body": "<img src=x onerror=alert(1)> sk-abcdefgh12345678"}]}))
        page = rep.render(report)
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)
        # no *attribute* survives; the escaped text is inert, so look for a real
        # tag carrying the handler instead of the bare substring
        self.assertIsNone(re.search(
            r"<[a-zA-Z][^<>]*\son(error|load|click|mouseover|focus)\s*=", page))
        self.assertNotIn("sk-abcdefgh12345678", page)
        self.assertNotIn("sk-", page)
        self.assertIn("redacted", page)
        self.assertNotIn('href="http://', page)
        self.assertNotIn("http://", page)

    def test_no_script_and_no_inline_handlers(self):
        page = self._page_with([{"comment_id": 11, "reason": "x"}])
        self.assertNotIn("<script", page.lower())
        for attr in ("onclick=", "onload=", "onmouseover=", "onerror="):
            self.assertEqual(page.count(attr), 0)

    def test_machine_checkable_html_skeleton(self):
        page = self._page_with([{"comment_id": 11, "reason": "x"}])
        self.assertTrue(page.startswith("<!DOCTYPE html>"))
        self.assertIn('<html lang="zh-CN">', page)
        self.assertIn('name="viewport"', page)
        self.assertEqual(len(re.findall(r"<h1[ >]", page)), 1)
        for landmark in ("<header", "<nav", "<main", "<footer"):
            self.assertIn(landmark, page)
        self.assertIn("<details", page)
        self.assertIn("<summary", page)
        # heading levels never skip: collect the sequence and check it
        levels = [int(m) for m in re.findall(r"<h([1-6])[ >]", page)]
        self.assertEqual(levels[0], 1)
        for prev, cur in zip(levels, levels[1:]):
            self.assertLessEqual(cur, prev + 1)
        # action items are list items, not bare divs
        self.assertIn('<ul class="items">', page)
        self.assertIn('<li class="item">', page)

    def test_machine_checkable_css(self):
        css = rep.STYLE
        order = re.search(r"@layer reset,\s*base,\s*theme,\s*components,\s*utilities;", css)
        self.assertIsNotNone(order)
        self.assertIn("@layer reset {", css)
        self.assertIn("@layer base {", css)
        self.assertIn("@layer theme {", css)
        self.assertIn("@layer components {", css)
        self.assertIn("@layer utilities {", css)
        self.assertNotRegex(css, r"(?m)^\s*\*\s*\{")
        self.assertNotIn("* {", css)
        for logical in ("margin-inline", "padding-inline", "padding-block",
                        "min-inline-size", "max-inline-size"):
            self.assertIn(logical, css)
        for physical in ("margin-left", "margin-right", "padding-left",
                         "padding-right", "width:", "left:", "right:"):
            self.assertNotIn(physical, css)
        # dynamic viewport unit with a plain fallback for older engines
        self.assertIn("min-block-size: 100dvh", css)
        # three token tiers, each building on the one above
        self.assertIn("tier 1", css)
        self.assertIn("tier 2", css)
        self.assertIn("tier 3", css)

    def test_status_never_relies_on_colour(self):
        page = self._page_with([{"comment_id": 11, "reason": "x"}])
        self.assertIn("需要行动", page)
        stale = self._page_with([{"comment_id": 11, "reason": "x",
                                  "at": "2020-01-01T00:00:00+00:00"}])
        self.assertIn("过期", stale)
        unknown = rep.render(rep.collect(self.engine(index={
            "version": 1, "meta": {}, "issues": [], "assignments": {}})))
        self.assertIn("未知", unknown)
        # every state carries a word next to its colour class
        for state in rep._EXPIRY_TEXT:
            self.assertIn(f"expiry--{state}", rep.STYLE)
        self.assertIn(rep._EXPIRY_TEXT["stale"], stale)
        self.assertIn(rep._EXPIRY_TEXT["unknown"], page)   # a row with no ``at``

    def test_expiry_three_states(self):
        now = "2026-01-10T00:00:00+00:00"
        index = self.index_with([{"comment_id": 11, "reason": "x"}])
        engine = self.engine(index=index)
        report = rep.collect(engine, now=now)
        self.assertEqual(report["items"][0]["expiry"]["state"], "unknown")
        index["issues"][THREAD]["needs_person"][0]["at"] = "2026-01-09T23:00:00+00:00"
        self.store.save_index(index)
        fresh = rep.collect(engine, now=now)
        self.assertEqual(fresh["items"][0]["expiry"]["state"], "fresh")
        index["issues"][THREAD]["needs_person"][0]["at"] = "2026-01-01T00:00:00+00:00"
        self.store.save_index(index)
        stale = rep.collect(engine, now=now)
        self.assertEqual(stale["items"][0]["expiry"]["state"], "stale")
        self.assertIn("过期", rep.render(stale))

    def test_second_screen_never_names_an_issue(self):
        engine = self.engine(index=self.index_with(
            [{"comment_id": 11, "reason": "x"}], key=THREAD))
        page = rep.render(rep.collect(engine))
        details = page.split("<details", 1)[1]
        self.assertNotIn("issue#", details)
        self.assertNotIn("#7", details)
        self.assertNotIn("comment 11", details)
        self.assertIn("项目概览", details)

    def test_quote_missing_is_explicit(self):
        index = self.index_with([{"comment_id": 11, "reason": "x"}])
        report = rep.collect(self.engine(index=index), source=None)
        page = rep.render(report)
        self.assertIn("引用不可用", page)
        self.assertEqual(report["quotes_missing"], 1)

    def test_quote_present_when_source_has_it(self):
        index = self.index_with([{"comment_id": 11, "reason": "x"}])
        report = rep.collect(self.engine(index=index),
                             source=_FakeComments({7: [{"id": 11,
                                                        "body": "\n  first line\nsecond"}]}))
        self.assertEqual(report["items"][0]["quote"], "first line")
        self.assertIn("first line", rep.render(report))

    def test_render_is_pure(self):
        engine = self.engine(index=self.index_with([{"comment_id": 11,
                                                     "reason": "x"}]))
        report = rep.collect(engine)
        with mock.patch.object(Path, "read_text", side_effect=AssertionError("io")):
            first = rep.render(report)
            second = rep.render(report)
        self.assertEqual(first, second)


class Blocked(Base):
    def test_blocked_and_waiting_person_are_active_and_shown(self):
        for state in rep.BLOCKED_STATES:
            with self.subTest(state=state):
                self.assertIn(state, ACTIVE_STATES)
                with tempfile.TemporaryDirectory() as tmp:
                    store = Store(Path(tmp) / "s")
                    self.store = store
                    engine = self.engine(index=self.index_with(
                        [], assignments={
                            "asg_7_worker_1": {
                                "id": "asg_7_worker_1", "thread": THREAD,
                                "issue": 7, "role": "worker", "status": state,
                                "updated_at": "2026-01-01T00:00:00+00:00",
                                "tombstone": {
                                    "reason": "no sid recorded", "cid": 5,
                                    "at": "2026-01-01T00:00:00+00:00",
                                    "attempt": 2}}}))
                    report = rep.collect(engine)
                    blocked = [it for it in report["items"]
                               if it["category"] == rep.CATEGORY_BLOCKED]
                    self.assertEqual(len(blocked), 1)
                    self.assertEqual(blocked[0]["extra"]["attempt"], 2)
                    self.assertIn("asg_7_worker_1", rep.render(report))

    def test_mark_blocked_shape_is_understood(self):
        rec = {"status": "working", "sid": "s1", "updated_at": ""}
        asg_mod.mark_blocked(rec, "no sid recorded", cid=5, at="2026-01-01T00:00:00+00:00")
        self.assertEqual(rec["status"], "blocked")
        engine = self.engine(index=self.index_with([], assignments={"asg_7_worker_1": rec}))
        report = rep.collect(engine)
        self.assertEqual(report["counts"][rep.CATEGORY_BLOCKED], 1)


class Cli(Base):
    def test_no_quotes_never_builds_a_source(self):
        """``--no-quotes`` short-circuits before any backend is constructed."""
        from super_agent import cli
        from super_agent import github_source
        cfg_path = self.root / "cfg.json"
        cfg_path.write_text(json.dumps({"github": {"source": "live",
                                                  "repo": "owner/demo"}}),
                            encoding="utf-8")
        out = self.root / "page.html"
        with mock.patch.object(github_source, "LiveSource",
                               side_effect=AssertionError("live source built")) as live, \
             mock.patch.object(github_source, "FixtureSource",
                               side_effect=AssertionError("fixture built")), \
             mock.patch.object(github_source, "build",
                               side_effect=AssertionError("build called")) as build:
            code = cli.main(["reporter", "--config", str(cfg_path),
                             "--state-dir", str(self.state), "--no-quotes",
                             "--out", str(out)])
        live.assert_not_called()
        build.assert_not_called()
        self.assertEqual(code, 0)
        self.assertIn("</html>", out.read_text(encoding="utf-8"))

    def test_github_source_build_is_not_called(self):
        from super_agent import cli
        from super_agent import github_source
        with mock.patch.object(github_source, "build",
                               side_effect=AssertionError("build called")):
            code = cli.main(["reporter", "--state-dir", str(self.state),
                             "--no-quotes"])
        self.assertEqual(code, 0)

    def test_out_writes_a_file_and_stdout_stays_clean(self):
        from super_agent import cli
        out = self.root / "sub" / "page.html"
        code = cli.main(["reporter", "--state-dir", str(self.state),
                         "--no-quotes", "--out", str(out)])
        self.assertEqual(code, 0)
        text = out.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("<!DOCTYPE html>"))

    def test_fixture_supplies_quotes(self):
        from super_agent import cli
        index = self.index_with([{"comment_id": 11, "reason": "x"}])
        self.store.save_index(index)
        fixture = self.root / "fixture.json"
        fixture.write_text(json.dumps({"issues": {"7": {
            "title": "t", "comments": [{"id": 11, "body": "[user] 请拍板"}]}}}),
            encoding="utf-8")
        out = self.root / "page.html"
        code = cli.main(["reporter", "--state-dir", str(self.state),
                         "--fixture", str(fixture), "--out", str(out)])
        self.assertEqual(code, 0)
        self.assertIn("请拍板", out.read_text(encoding="utf-8"))

    def test_bad_fixture_exits_one(self):
        from super_agent import cli
        bad = self.root / "bad.json"
        bad.write_text("{", encoding="utf-8")
        code = cli.main(["reporter", "--state-dir", str(self.state),
                         "--fixture", str(bad)])
        self.assertEqual(code, 1)


    def test_broken_fixture_reports_the_real_reason(self):
        """Counterexample 5 (P2-2): a bad fixture path must not blame ``none``.

        ``github_source.build`` turns an unreadable fixture into a
        ``BrokenSource``; the page used to fall back to ``NullSource`` and then
        print "github.source=none", a setting the user never chose. The reason
        now travels into the report.
        """
        from super_agent import cli
        missing = self.root / "no-such-fixture.json"
        cfg_path = self.root / "cfg.json"
        cfg_path.write_text(json.dumps({"github": {"source": "fixture",
                                                   "fixture": str(missing)}}),
                            encoding="utf-8")
        self.store.save_index(self.index_with([{"comment_id": 11, "reason": "x"}]))
        out = self.root / "page.html"
        code = cli.main(["reporter", "--config", str(cfg_path),
                         "--state-dir", str(self.state), "--out", str(out)])
        self.assertEqual(code, 0)
        page = out.read_text(encoding="utf-8")
        self.assertNotIn("github.source=none", page)
        self.assertTrue("fixture" in page or "Fixture" in page or
                        "FileNotFoundError" in page,
                        "the page must name the real cause")
        report_error = next((ln for ln in page.splitlines()
                             if "数据源构建失败" in ln), "")
        self.assertTrue(report_error, "no source-error line on the page")

    def test_source_none_with_repo_does_not_build_a_live_source(self):
        """``github.source=none`` wins over a configured repo (P2-3)."""
        from super_agent import cli
        from super_agent import github_source
        cfg_path = self.root / "cfg.json"
        cfg_path.write_text(json.dumps({"github": {"source": "none",
                                                   "repo": "owner/demo"}}),
                            encoding="utf-8")
        out = self.root / "page.html"
        self.store.save_index(self.index_with([{"comment_id": 11, "reason": "x"}]))
        with mock.patch.object(github_source, "LiveSource",
                               side_effect=AssertionError("live source built")) as live:
            code = cli.main(["reporter", "--config", str(cfg_path),
                             "--state-dir", str(self.state), "--out", str(out)])
        live.assert_not_called()
        self.assertEqual(code, 0)
        page = out.read_text(encoding="utf-8")
        self.assertIn("数据源不可用（github.source=none）", page)

    def test_source_fixture_in_cfg_is_honoured(self):
        """The config's ``github.source=fixture`` is the backend, not live."""
        from super_agent import cli
        from super_agent import github_source
        self.store.save_index(self.index_with([{"comment_id": 11, "reason": "x"}]))
        fixture = self.root / "fx.json"
        fixture.write_text(json.dumps({"issues": {"7": {
            "title": "t", "comments": [{"id": 11, "body": "from the fixture"}]}}}),
            encoding="utf-8")
        cfg_path = self.root / "cfg.json"
        cfg_path.write_text(json.dumps({"github": {"source": "fixture",
                                                   "fixture": str(fixture)}}),
                            encoding="utf-8")
        out = self.root / "page.html"
        with mock.patch.object(github_source, "LiveSource",
                               side_effect=AssertionError("live source built")):
            code = cli.main(["reporter", "--config", str(cfg_path),
                             "--state-dir", str(self.state), "--out", str(out)])
        self.assertEqual(code, 0)
        self.assertIn("from the fixture", out.read_text(encoding="utf-8"))

    def test_fixture_flag_overrides_cfg_source(self):
        from super_agent import cli
        from super_agent import github_source
        self.store.save_index(self.index_with([{"comment_id": 11, "reason": "x"}]))
        flag_fixture = self.root / "flag.json"
        flag_fixture.write_text(json.dumps({"issues": {"7": {
            "title": "t", "comments": [{"id": 11, "body": "from --fixture"}]}}}),
            encoding="utf-8")
        cfg_path = self.root / "cfg.json"
        cfg_path.write_text(json.dumps({"github": {"source": "live",
                                                   "repo": "owner/demo"}}),
                            encoding="utf-8")
        out = self.root / "page.html"
        with mock.patch.object(github_source, "LiveSource",
                               side_effect=AssertionError("live source built")):
            code = cli.main(["reporter", "--config", str(cfg_path),
                             "--state-dir", str(self.state),
                             "--fixture", str(flag_fixture), "--out", str(out)])
        self.assertEqual(code, 0)
        self.assertIn("from --fixture", out.read_text(encoding="utf-8"))


class _FakeComments:
    """A source double that only implements what the reporter reads."""

    def __init__(self, rows: dict):
        self.rows = rows
        self.calls = []

    def comments(self, issue: int) -> list[dict]:
        self.calls.append(int(issue))
        return list(self.rows.get(int(issue), []))

    def issues(self, limit: int = 20) -> list[int]:
        return sorted(self.rows)[:limit]

    def title(self, issue: int) -> str:
        return ""

    def issue_state(self, issue: int) -> str:
        return "unknown"


if __name__ == "__main__":
    unittest.main()
