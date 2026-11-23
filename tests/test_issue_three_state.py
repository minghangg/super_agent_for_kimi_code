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

from super_agent import config, github_source, needs
from super_agent.allowlist import ProjectAllowlist
from super_agent.engine import Engine
from super_agent.store import Store, action_id

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
        self.events.write_text(json.dumps({
            "seq": 1, "at": "2026-11-10T10:00:00+00:00",
            "type": "session.created", "sid": "session_pm",
            "key": "pm-demo-3f7c1a90"}) + "\n", encoding="utf-8")
        (self.events.parent / "sessions.json").write_text(json.dumps({
            "pm-demo-3f7c1a90": {"id": "session_pm"}}), encoding="utf-8")

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
        # Only the creation mode is permanent. The refusal *text* is never a
        # criterion: the wording belongs to ``allowlist.check``, and the
        # "issue #N" form is unreachable through ``_dispatch`` anyway
        # (``_project_for`` answers None before ``check`` is asked), so the old
        # prefix matched dead code and pinned a reader to a false belief.
        self.assertEqual(Engine.PERMANENT_REFUSAL_PREFIXES,
                         ("creation_mode_forbidden",))
        self.assertTrue(Engine.is_permanent_refusal(
            "creation_mode_forbidden: pm_created does not create sessions"))
        # A project/action refusal is *not* permanent: the allowlist is a file a
        # person edits, and the next round must be able to succeed once fixed
        # (tests/test_qa_route.py::test_refused_allowlist_is_retried_next_round).
        for reason in ("issue #9 not allowlisted (observe only; ...)",
                       "project None not allowlisted (repository unlisted)",
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

        # ``_dispatch`` is stubbed, not ``_route_review``: the recording point
        # under test lives in ``_route_review``, so replacing it would answer
        # the question by not asking it. A permanent refusal still counts one
        # round (``refusal_streak.rounds == 1``) - what stops the climb is
        # ``permanent`` being set and the loop skipping it from then on, not an
        # absent streak. The old ``assertNotIn("refusal_streak")`` was true
        # only because the stub had removed the code that writes it.
        def dispatch(*args, **kwargs):
            calls["n"] += 1
            return {"dispatched": False, "refused": True,
                    "reason": "creation_mode_forbidden: pm_created does not "
                              "create sessions"}

        engine._dispatch = dispatch
        engine._issue_state = lambda issue: "open"
        index = {"routes": {"route:owner/demo#7:3": {
                     "thread": "owner/demo#7", "comment_id": 3,
                     "verdict": "pass", "action_id": "z", "ok": False}},
                 "assignments": {"asg_7_worker_1": {
                     "thread": "owner/demo#7", "issue": 7,
                     "role": "worker", "status": "working"}},
                 "issues": {}}
        engine._retry_routes(index)
        route_rec = index["routes"]["route:owner/demo#7:3"]
        self.assertTrue(route_rec["permanent"])
        self.assertEqual(calls["n"], 1)
        rows_after = (index["issues"]["owner/demo#7"]["needs_person"])
        # Two rows, both true and both about this comment: ``_route_review``
        # records the failed send as ``route.undeliverable`` when it returns,
        # and the retry pass adds ``route.refused_permanent`` for the reason
        # that will not change. Collapsing them would lose the difference
        # between "could not send" and "will never send".
        permanent_rows = [r for r in rows_after
                          if str(r.get("reason") or "").startswith(
                              "route.refused_permanent")]
        self.assertEqual(len(permanent_rows), 1)
        self.assertIn("creation_mode_forbidden", permanent_rows[0]["reason"])
        # Second round: the permanent flag keeps it out of the loop entirely.
        engine._retry_routes(index)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(len(index["issues"]["owner/demo#7"]["needs_person"]), 2)
        # Three fail-closed assertions: the permanent path is a refusal, so it
        # leaves the route failed, writes no attempt, and injects no frame.
        self.assertFalse(route_rec["ok"])
        self.assertEqual(engine.store.action_attempted(
            action_id("route:owner/demo#7:3", "bus_inject", "3")), 0)
        self.assertIsNone(engine.bus, "the refusal must not become a frame")
        # A permanent refusal counts one round and stops there: the loop never
        # reaches it again, so the alert threshold is out of reach.
        self.assertLess(route_rec["refusal_streak"]["rounds"], 3,
                        "a permanent refusal never climbs to the alert")


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


class Source:
    """Enough of a source for ``run_round``; ``page`` is the live list."""

    def __init__(self, page):
        self.page = list(page)

    def issues(self, limit=20):
        return list(self.page)

    def title(self, issue):
        return "probe"

    def comments(self, issue):
        return []

    def paged(self, issue, *, since_id=0, max_pages=10):
        return []

    def issue_state(self, issue):
        return "open"


class RefusedRetryAlert(unittest.TestCase):
    """必修 A/B: a refusal that will not clear becomes visible, once."""

    THREAD = "owner/demo#7"
    KEY = "route:owner/demo#7:3"

    class Bus:
        def __init__(self):
            self.injected = []

        def healthz(self):
            return {"ok": True, "ready": True, "connected": True, "seq": 1}

        def inject(self, event_type, payload):
            self.injected.append((event_type, payload))
            return {"ok": True, "seq": len(self.injected)}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.allow = self.root / "allowlist.json"
        self.allow.write_text(json.dumps(GOOD), encoding="utf-8")
        self.events = self.root / "sessions" / "events.jsonl"
        self.events.parent.mkdir(parents=True, exist_ok=True)
        self.events.write_text(json.dumps({
            "seq": 1, "at": "2026-11-10T10:00:00+00:00",
            "type": "session.created", "sid": "session_pm",
            "key": "pm-demo-3f7c1a90"}) + "\n", encoding="utf-8")
        self.bus = self.Bus()
        (self.events.parent / "sessions.json").write_text(json.dumps({
            "pm-demo-3f7c1a90": {"id": "session_pm"}}), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def engine(self):
        cfg = config.load(None, {"mode": "assist",
                                 "github": {"source": "none", "repo": "owner/demo"},
                                 "engine": {"archived_crosscheck": False,
                                            "session_events_path": str(self.events)}})
        engine = Engine(cfg, Store(self.state), github_source.NullSource(),
                        self.bus, project_allowlist=ProjectAllowlist(self.allow))
        engine._issue_state = lambda issue: "open"
        return engine

    def seed(self, engine):
        index = engine.store.load_index()
        index.setdefault("routes", {})[self.KEY] = {
            "thread": self.THREAD, "comment_id": 3, "verdict": "pass",
            "action_id": "seed", "ok": False}
        index.setdefault("assignments", {})["asg_7_worker_1"] = {
            "thread": self.THREAD, "issue": 7, "role": "worker",
            "status": "working", "sid": "session_w1"}
        engine.store.save_index(index)

    def alerts(self, engine):
        rec = (engine.store.load_index().get("issues") or {}).get(self.THREAD) or {}
        return [r for r in rec.get("needs_person") or []
                if str(r.get("reason") or "").startswith("route.refused_retryable")]

    def warned(self, engine):
        path = self.state / "events.jsonl"
        if not path.is_file():
            return []
        rows = [json.loads(x) for x in
                path.read_text("utf-8").splitlines() if x.strip()]
        return [r for r in rows if r["type"] == "route.refused_retryable"]

    def test_the_real_refusal_text_counts_three_rounds_and_alerts_once(self):
        engine = self.engine()
        engine.source = Source([])
        self.seed(engine)
        seen = []
        original = engine._route_review

        def spy(*args, **kwargs):
            out = original(*args, **kwargs)
            seen.append(str(out.get("reason") or ""))
            return out

        engine._route_review = spy
        for _ in range(3):
            engine.run_once()
        # Real refusal: no stub, no hand-written text. Issue 7 is in no
        # project's list, so ``_project_for`` answers None and the wording is
        # the production one.
        self.assertEqual(len(seen), 3, seen)
        self.assertTrue(all("not allowlisted" in r for r in seen), seen)
        self.assertEqual(self.bus.injected, [], "the alert is not a frame")
        alerts = self.alerts(engine)
        self.assertEqual(len(alerts), 1, alerts)
        self.assertTrue(alerts[0]["reason"].startswith(
            "route.refused_retryable[user]:"))
        # The row names what was observed, not a guessed cause: three of the
        # four refusal shapes have nothing to do with the allowlist.
        self.assertNotIn("allowlist", alerts[0]["reason"])
        self.assertIn("cid 3", alerts[0]["reason"])
        self.assertIn("route.decided", alerts[0]["reason"])
        warned = self.warned(engine)
        self.assertEqual(len(warned), 1)
        # The refusal's own text rides on the event, so a reader can tell the
        # four shapes apart without opening ``route.decided``.
        self.assertIn("not allowlisted", warned[0]["reason"])
        route = engine.store.load_index()["routes"][self.KEY]
        self.assertEqual(route["refusal_streak"]["rounds"], 3)
        # A fourth refused round adds one count, not a second alert.
        engine.run_once()
        self.assertEqual(len(self.alerts(engine)), 1)
        self.assertEqual(len(self.warned(engine)), 1)
        self.assertEqual(
            engine.store.load_index()["routes"][self.KEY]
            ["refusal_streak"]["rounds"], 4)

    def test_the_issue_going_live_in_github_delivers_and_quiets_the_streak(self):
        # The healthy path is not "edit the overlay": ``_sync_allowlist_issues``
        # rewrites ``issues`` from the source every round, so a hand edit is
        # undone before the retry reads it. What heals a refusal is the issue
        # becoming live again (the on-hold label removed), which the sync then
        # writes - the shape #75 hit.
        engine = self.engine()
        engine.source = Source([])
        self.seed(engine)
        for _ in range(3):
            engine.run_once()
        self.assertEqual(self.bus.injected, [])
        self.assertEqual(len(self.alerts(engine)), 1)
        engine.source.page = [7]
        calls = {"n": 0}
        original = engine._route_review

        def spy(*args, **kwargs):
            calls["n"] += 1
            return original(*args, **kwargs)

        engine._route_review = spy
        engine.run_once()
        self.assertEqual([e for e, _ in self.bus.injected],
                         ["event.agent.project_pm"])
        self.assertEqual(calls["n"], 1)
        route = engine.store.load_index()["routes"][self.KEY]
        self.assertTrue(route["ok"])
        self.assertEqual(route["refusal_streak"]["rounds"], 0)
        stamp = route["refusal_streak"]["alerted_at"]
        self.assertEqual(len(self.alerts(engine)), 1, "one alert, ever")
        # Ten more rounds: ok=True keeps the retry pass out, so no call and no
        # second alert, and the alert moment does not move.
        for _ in range(10):
            engine.run_once()
        self.assertEqual(calls["n"], 1, "a delivered route is never retried")
        self.assertEqual(len(self.bus.injected), 1)
        self.assertEqual(len(self.alerts(engine)), 1)
        self.assertEqual(
            engine.store.load_index()["routes"][self.KEY]
            ["refusal_streak"]["alerted_at"], stamp)

    def test_a_restart_does_not_re_alert(self):
        engine = self.engine()
        engine.source = Source([])
        self.seed(engine)
        for _ in range(10):
            engine.run_once()
        self.assertEqual(len(self.alerts(engine)), 1)
        self.assertEqual(len(self.warned(engine)), 1)
        engine2 = self.engine()
        engine2.source = Source([])
        for _ in range(3):
            engine2.run_once()
        self.assertEqual(len(self.alerts(engine2)), 1, "the streak survives")
        self.assertEqual(len(self.warned(engine2)), 1, "and so does the alert")
        rows = engine2.store.load_index()["issues"][self.THREAD]["needs_person"]
        mine = [r for r in rows if needs.cid_of(r) == 3]
        self.assertEqual(
            len([r for r in mine
                 if str(r.get("reason") or "").startswith("route.refused_retryable")]),
            1, "two alert rows for one verdict is the shape this forbids")
        self.assertEqual(
            [r for r in mine
             if str(r.get("reason") or "").startswith("dispatch.refused")], [])

    def test_a_suppressed_dispatch_is_not_a_refusal(self):
        engine = self.engine()
        engine.source = Source([])
        self.seed(engine)
        engine.dispatch_enabled = False
        for _ in range(5):
            engine.run_once()
        self.assertEqual(self.alerts(engine), [])
        self.assertEqual(self.bus.injected, [])
        route = engine.store.load_index()["routes"][self.KEY]
        self.assertNotIn("refusal_streak", route,
                         "dispatch disabled is not a refusal")
        # The other shape without ``refused``: the record never reaches
        # ``_route_review``'s dispatch at all, because an attempt already
        # exists. Neither counts nor clears - the second half matters, since a
        # suppress-and-reset pair would let a stuck verdict alert forever.
        engine.dispatch_enabled = True
        engine._dispatch = lambda *a, **k: {
            "dispatched": False, "refused": True,
            "reason": "project None not allowlisted (repository unlisted)"}
        index = engine.store.load_index()
        seed_route = index["routes"][self.KEY]
        index["meta"]["run_count"] = 50
        engine._route_review(index, self.THREAD, 7, {"id": 3, "body": "",
                                                     "author": ""},
                             {"verdict": "pass"}, log_unverified=False)
        self.assertEqual(seed_route["refusal_streak"]["rounds"], 1)
        index["meta"]["run_count"] = 51
        engine.store.begin_action(
            action_id(self.KEY, "bus_inject", "3"), verb="bus_inject",
            kind="bus_inject", assignment_id=self.KEY, input_ref="3")
        engine._route_review(index, self.THREAD, 7, {"id": 3, "body": "",
                                                     "author": ""},
                             {"verdict": "pass"}, log_unverified=False)
        self.assertEqual(seed_route["refusal_streak"]["rounds"], 1,
                         "an already-attempted record neither counts nor clears")

    def test_a_closed_issue_stays_silent_and_keeps_the_streak(self):
        engine = self.engine()
        engine.source = Source([])
        self.seed(engine)
        engine._issue_state = lambda issue: "closed"
        for _ in range(5):
            engine.run_once()
        route = engine.store.load_index()["routes"][self.KEY]
        self.assertIn("skipped_closed_at", route)
        self.assertNotIn("refusal_streak", route)
        self.assertEqual(self.bus.injected, [])
        self.assertEqual(self.alerts(engine), [])

    def test_two_entries_in_one_round_count_once(self):
        # ``_route_review`` is reachable twice per round: inline from
        # ``_on_review`` and again from the retry pass at the round's tail. The
        # round token makes the second entry a no-op.
        engine = self.engine()
        index = engine.store.load_index()
        route = {"thread": self.THREAD, "comment_id": 3, "verdict": "pass",
                 "action_id": "seed", "ok": False}
        index["routes"] = {self.KEY: route}
        index["meta"]["run_count"] = 41
        engine._dispatch = lambda *a, **k: {
            "dispatched": False, "refused": True,
            "reason": "project None not allowlisted (repository unlisted)"}
        comment = {"id": 3, "body": "", "author": ""}
        for _ in range(2):
            engine._route_review(index, self.THREAD, 7, comment,
                                 {"verdict": "pass"}, log_unverified=False)
        self.assertEqual(route["refusal_streak"]["rounds"], 1,
                         "one round is one count, not one per call")
        index["meta"]["run_count"] = 42
        engine._route_review(index, self.THREAD, 7, comment,
                             {"verdict": "pass"}, log_unverified=False)
        self.assertEqual(route["refusal_streak"]["rounds"], 2,
                         "the next round counts again")


class PermanentStillPermanent(unittest.TestCase):
    """反例 3: the creation mode stays permanent and never climbs to an alert."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.allow = self.root / "allowlist.json"
        self.allow.write_text(json.dumps(GOOD), encoding="utf-8")
        (self.root / "sessions.json").write_text(json.dumps({
            "pm-demo-3f7c1a90": {"id": "session_pm"}}), encoding="utf-8")
        (self.root / "events.jsonl").write_text(json.dumps({
            "seq": 1, "at": "2026-11-10T10:00:00+00:00",
            "type": "session.created", "sid": "session_pm",
            "key": "pm-demo-3f7c1a90"}) + "\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def engine(self):
        cfg = config.load(None, {"mode": "assist",
                                 "github": {"source": "none", "repo": "owner/demo"},
                                 "engine": {"archived_crosscheck": False,
                                            "session_state_dir": str(self.root)}})
        engine = Engine(cfg, Store(self.state), github_source.NullSource(), None,
                        project_allowlist=ProjectAllowlist(self.allow))
        engine._issue_state = lambda issue: "open"
        return engine

    def test_permanent_refusal_is_recorded_once_and_not_retried(self):
        engine = self.engine()
        calls = {"n": 0}

        def dispatch(*args, **kwargs):
            calls["n"] += 1
            return {"dispatched": False, "refused": True,
                    "reason": "creation_mode_forbidden: pm_created does not "
                              "create sessions"}

        engine._dispatch = dispatch
        index = {"routes": {"route:owner/demo#7:3": {
                     "thread": "owner/demo#7", "comment_id": 3,
                     "verdict": "pass", "action_id": "z", "ok": False}},
                 "assignments": {"asg_7_worker_1": {
                     "thread": "owner/demo#7", "issue": 7,
                     "role": "worker", "status": "working"}},
                 "issues": {}}
        engine._retry_routes(index)
        route_rec = index["routes"]["route:owner/demo#7:3"]
        self.assertTrue(route_rec["permanent"])
        self.assertEqual(calls["n"], 1)
        rows_after = (index["issues"]["owner/demo#7"]["needs_person"])
        permanent_rows = [r for r in rows_after
                          if str(r.get("reason") or "").startswith(
                              "route.refused_permanent")]
        self.assertEqual(len(permanent_rows), 1)
        engine._retry_routes(index)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(len(index["issues"]["owner/demo#7"]["needs_person"]), 2)
        self.assertFalse(route_rec["ok"])
        self.assertEqual(engine.store.action_attempted(
            action_id("route:owner/demo#7:3", "bus_inject", "3")), 0)
        self.assertIsNone(engine.bus, "the refusal must not become a frame")
        self.assertLess(route_rec["refusal_streak"]["rounds"], 3,
                        "a permanent refusal never climbs to the alert")

    def test_permanent_refusal_is_classified(self):
        self.assertEqual(Engine.PERMANENT_REFUSAL_PREFIXES,
                         ("creation_mode_forbidden",))
        self.assertTrue(Engine.is_permanent_refusal(
            "creation_mode_forbidden: pm_created does not create sessions"))
        for reason in ("issue #9 not allowlisted (observe only; ...)",
                       "project None not allowlisted (repository unlisted)",
                       "action 'bind' not allowlisted",
                       "target session is archived",
                       "no supervisor sid: engine.supervisor_sid is empty",
                       ""):
            self.assertFalse(Engine.is_permanent_refusal(reason), reason)
