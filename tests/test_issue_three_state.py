"""Issue three-state: open / suspend (``stage:on-hold``) / closed.

GitHub only has ``open`` and ``closed``. The missing third state ("suspend")
is carried by a label, and the engine reads it in three places:

* ``github_source.issues`` drops rows whose labels intersect
  ``github.on_hold_labels`` - matched as a **set**, never by position and never
  by the ``stage:`` prefix, because ``stage:dev`` is progress, not a switch;
* the allowlist's ``issues`` list is refreshed from that same answer, so
  "which issues are live" stops being a hand-kept file;
* ``_retry_routes`` stops retrying a route whose refusal is permanent
  (an allowlist answer that will never change) or whose issue is **closed** -
  ``unknown`` is never treated as closed, or one broken probe would stop every
  delivery in the engine.

Run: ``python -m pytest tests/test_issue_three_state.py -q``
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from super_agent import config, github_source
from super_agent.allowlist import ProjectAllowlist
from super_agent.engine import Engine
from super_agent.store import Store

HOLD = "stage:on-hold"

GOOD = {
    "schema_version": 1,
    "defaults": {"mode": "observe", "creation_mode": "pm_created"},
    "projects": {
        "demo": {"repo": "owner/demo", "cwd": ".", "mode": "assist",
                 "creation_mode": "pm_created", "issues": [1],
                 "actions": ["bus_inject", "collect"],
                 "roles": ["worker", "qa", "pm"]},
    },
}


def rows(*specs):
    """``(number, [label names])`` -> REST-shaped rows."""
    out = []
    for number, labels in specs:
        out.append({"number": number,
                    "labels": [{"name": n} for n in labels]})
    return out


class ListSource(github_source.LiveSource):
    """``LiveSource`` whose list endpoint is an in-memory page."""

    def __init__(self, page, *, on_hold_labels=None):
        super().__init__("owner/demo", on_hold_labels=on_hold_labels)
        self.page = list(page)
        self.requests = []

    def _get(self, path, query=None):
        self.requests.append({"path": path, "query": dict(query or {})})
        return self.page


class HoldFiltering(unittest.TestCase):
    """必修 A: ``issues()`` drops held rows, keeps the live one."""

    def test_held_issue_is_not_returned(self):
        src = ListSource(rows((1, []), (2, [HOLD]), (3, [])),
                         on_hold_labels=[HOLD])
        self.assertEqual(src.issues(limit=20), [1, 3])

    def test_label_match_is_set_membership_not_position(self):
        """反例 1: a held issue that also carries other labels is still held."""
        src = ListSource(rows((7, ["kind:feat", "stage:dev", HOLD])),
                         on_hold_labels=[HOLD])
        self.assertEqual(src.issues(limit=20), [])

    def test_stage_prefix_is_not_filtered(self):
        """Only the configured names are held - ``stage:dev`` must survive."""
        src = ListSource(rows((4, ["stage:dev"]), (5, [HOLD])),
                         on_hold_labels=[HOLD])
        self.assertEqual(src.issues(limit=20), [4])

    def test_empty_config_means_no_filter(self):
        """反例 3: an empty list is fail-open, never fail-closed."""
        src = ListSource(rows((1, [HOLD]), (2, [HOLD])), on_hold_labels=[])
        self.assertEqual(src.issues(limit=20), [1, 2])

    def test_limit_does_not_swallow_the_live_issue(self):
        """反例 2: 30 held rows in front must not hide the live one."""
        page = rows(*[(n, [HOLD]) for n in range(100, 130)])
        page.append({"number": 200, "labels": []})
        src = ListSource(page, on_hold_labels=[HOLD])
        self.assertEqual(src.issues(limit=20), [200])

    def test_page_is_requested_at_the_api_maximum(self):
        src = ListSource(rows((1, [])), on_hold_labels=[HOLD])
        src.issues(limit=3)
        self.assertEqual(src.requests[0]["query"]["per_page"], 100)

    def test_pull_requests_are_still_skipped(self):
        src = ListSource([{"number": 9, "labels": [],
                           "pull_request": {"url": "https://api.github.com/x"}}],
                         on_hold_labels=[HOLD])
        self.assertEqual(src.issues(limit=20), [])

    def test_fixture_source_honours_the_same_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.json"
            path.write_text(json.dumps({"issues": {
                "1": {"labels": [HOLD]},
                "2": {"labels": [{"name": "stage:dev"}]},
                "3": {},
            }}), encoding="utf-8")
            src = github_source.FixtureSource(path, on_hold_labels=[HOLD])
            self.assertEqual(src.issues(limit=20), [2, 3])


class AllowlistSync(unittest.TestCase):
    """必修 B: the overlay carries the live list, idempotently."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.allow = self.root / "allowlist.json"
        self.allow.write_text(json.dumps(GOOD), encoding="utf-8")
        self.projects = ProjectAllowlist(self.allow)

    def tearDown(self):
        self.tmp.cleanup()

    def overlay(self) -> dict:
        path = self.root / "allowlist.local.json"
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def test_sync_writes_the_overlay_not_the_tracked_file(self):
        before = self.allow.read_text(encoding="utf-8")
        out = self.projects.sync_issues("demo", [4, 2])
        self.assertTrue(out["changed"])
        self.assertEqual(self.overlay()["projects"]["demo"]["issues"], [2, 4])
        self.assertEqual(self.allow.read_text(encoding="utf-8"), before)
        self.assertEqual(self.projects.data["projects"]["demo"]["issues"], [2, 4])

    def test_sync_is_idempotent(self):
        self.projects.sync_issues("demo", [2, 4])
        stamp = (self.root / "allowlist.local.json").stat().st_mtime_ns
        out = self.projects.sync_issues("demo", [4, 2])
        self.assertFalse(out["changed"])
        self.assertFalse(out["written"])
        self.assertEqual((self.root / "allowlist.local.json").stat().st_mtime_ns,
                         stamp)

    def test_unknown_project_is_refused_without_raising(self):
        out = self.projects.sync_issues("nope", [1])
        self.assertEqual(out["written"], False)
        self.assertIn("not in the allowlist", out["error"])

    def test_read_only_directory_only_warns(self):
        """A failed write must report, never raise: a checkout can be read-only."""
        target = self.root / "locked"
        target.mkdir()
        allow = target / "allowlist.json"
        allow.write_text(json.dumps(GOOD), encoding="utf-8")
        projects = ProjectAllowlist(allow)
        # Point the overlay at a directory so the open() cannot succeed.
        projects.overlay_path = target / "as-a-dir"
        projects.overlay_path.mkdir()
        out = projects.sync_issues("demo", [5])
        self.assertFalse(out["written"])
        self.assertTrue(out["error"])


class RouteRetry(unittest.TestCase):
    """必修 C: permanent refusals and closed issues stop the retry loop."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.allow = self.root / "allowlist.json"
        self.allow.write_text(json.dumps(GOOD), encoding="utf-8")
        self.events = self.root / "sessions" / "events.jsonl"
        self.events.parent.mkdir(parents=True, exist_ok=True)
        self.events.write_text("", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def engine(self, source) -> Engine:
        cfg = config.load(None, {"mode": "assist",
                                 "github": {"source": "none", "repo": "owner/demo"},
                                 "engine": {"archived_crosscheck": False,
                                            "session_events_path": str(self.events)}})
        return Engine(cfg, Store(self.state), source, None,
                      project_allowlist=ProjectAllowlist(self.allow))

    def test_permanent_refusal_is_classified(self):
        for reason in ("issue #9 not allowlisted (observe only; ...)",
                       "creation_mode_forbidden: pm_created does not create sessions"):
            self.assertTrue(Engine.is_permanent_refusal(reason), reason)
        # A project/action refusal is *not* permanent: the allowlist is a file a
        # person edits, and the next round must be able to succeed once fixed
        # (tests/test_qa_route.py::test_refused_allowlist_is_retried_next_round).
        for reason in ("project None not allowlisted (repository unlisted)",
                       "action 'bind' not allowlisted",
                       "target session is archived",
                       "no supervisor sid: engine.supervisor_sid is empty",
                       ""):
            self.assertFalse(Engine.is_permanent_refusal(reason), reason)

    def test_closed_issue_is_skipped_and_unknown_is_not(self):
        """反例 4: only ``closed`` early-returns; ``unknown`` keeps retrying."""
        src = github_source.NullSource()
        engine = self.engine(src)
        index = {"routes": {"route:owner/demo#7:1": {
                     "thread": "owner/demo#7", "comment_id": 1,
                     "verdict": "pass", "action_id": "x", "ok": False}},
                 "assignments": {"asg_7_worker_1": {
                     "thread": "owner/demo#7", "issue": 7,
                     "role": "worker", "status": "working"}}}
        engine._issue_state = lambda issue: "closed"
        self.assertEqual(engine._retry_routes(index), 0)
        self.assertIn("skipped_closed_at", index["routes"]["route:owner/demo#7:1"])

        index2 = {"routes": {"route:owner/demo#7:2": {
                      "thread": "owner/demo#7", "comment_id": 2,
                      "verdict": "pass", "action_id": "y", "ok": False}},
                  "assignments": {"asg_7_worker_1": {
                      "thread": "owner/demo#7", "issue": 7,
                      "role": "worker", "status": "working"}}}
        engine._issue_state = lambda issue: "unknown"
        engine._route_review = lambda *a, **k: {"dispatched": False,
                                                "reason": "target session is archived"}
        engine._retry_routes(index2)
        self.assertNotIn("skipped_closed_at", index2["routes"]["route:owner/demo#7:2"])

    def test_permanent_refusal_is_recorded_once_and_not_retried(self):
        src = github_source.NullSource()
        engine = self.engine(src)
        calls = {"n": 0}

        def route(*args, **kwargs):
            calls["n"] += 1
            return {"dispatched": False,
                    "reason": "issue #7 not allowlisted (observe only; ...)"}

        engine._route_review = route
        engine._issue_state = lambda issue: "open"
        index = {"routes": {"route:owner/demo#7:3": {
                     "thread": "owner/demo#7", "comment_id": 3,
                     "verdict": "pass", "action_id": "z", "ok": False}},
                 "assignments": {"asg_7_worker_1": {
                     "thread": "owner/demo#7", "issue": 7,
                     "role": "worker", "status": "working"}},
                 "issues": {}}
        engine._retry_routes(index)
        self.assertTrue(index["routes"]["route:owner/demo#7:3"]["permanent"])
        self.assertEqual(calls["n"], 1)
        rows_after = (index["issues"]["owner/demo#7"]["needs_person"])
        self.assertEqual(len(rows_after), 1)
        self.assertIn("route.refused_permanent", rows_after[0]["reason"])
        # Second round: the permanent flag keeps it out of the loop entirely.
        engine._retry_routes(index)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(len(index["issues"]["owner/demo#7"]["needs_person"]), 1)


class ConfigKey(unittest.TestCase):
    def test_default_is_the_single_hold_label(self):
        cfg = config.load(None)
        self.assertEqual(cfg["github"]["on_hold_labels"], [HOLD])

    def test_must_be_a_list_of_strings(self):
        with self.assertRaises(config.ConfigError):
            config.load(None, {"github": {"on_hold_labels": "stage:on-hold"}})
        with self.assertRaises(config.ConfigError):
            config.load(None, {"github": {"on_hold_labels": [1]}})

    def test_sample_matches_defaults(self):
        """RULES §12: a new field lands in DEFAULTS and the sample together."""
        sample = Path(__file__).resolve().parent.parent / "config" / "sample.engine.json"
        data = json.loads(sample.read_text(encoding="utf-8"))
        self.assertEqual(data["github"]["on_hold_labels"],
                         config.DEFAULTS["github"]["on_hold_labels"])
