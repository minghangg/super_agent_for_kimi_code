"""#140: an error stops the engine; a network blip is the one exception.

The ruling this file encodes (user 2026-11-28): *do not* keep running while
failing. So there are three properties here, and the counter-examples matter as
much as the happy paths:

1. **a failed listing reads nothing** - no old page is promoted to "the live
   set", and no comment is consumed, so nothing can be lost by the failure;
2. **a transient symptom buys a few rounds, a real fault buys none** - a
   timeout is tolerated by the loop's failure counter, a 401 ends the run at
   once (`runloop` reason ``fatal``);
3. **the local answers that never needed GitHub still work** - a delivery that
   arrived before its assignment is re-read once the assignment exists
   (``delivery_recovery``), and a suspended issue still resolves its project
   from the repository (``project_lookup``).

Run: ``python -m pytest tests/test_iss140_faults_and_recovery.py -q``
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from super_agent import config, github_source, runloop
from super_agent.allowlist import ProjectAllowlist
from super_agent.engine import Engine
from super_agent.engine_parts import (collect, fault_report,
                                      source_faults, stop_notice)
from super_agent.store import Store

ALLOW = {
    "schema_version": 1,
    "defaults": {"mode": "observe", "creation_mode": "pm_created"},
    "projects": {
        "demo": {"repo": "owner/demo", "cwd": ".", "mode": "assist",
                 "creation_mode": "pm_created", "issues": [1],
                 "actions": ["bus_inject", "collect"],
                 "roles": ["worker", "qa", "pm"]},
        "other": {"repo": "owner/other", "cwd": ".", "mode": "observe",
                  "creation_mode": "pm_created", "issues": [2],
                  "actions": ["collect"], "roles": ["worker"]},
    },
}


class _ListSource:
    """``comments()`` is the whole listing; ``issues()`` can be made to fail."""

    def __init__(self, rows, fail=None, count=None):
        self.rows = rows
        self.fail = fail
        self.count = count

    def comments(self, issue):
        return list(self.rows)

    def issues(self, limit=100):
        if self.fail:
            raise github_source.SourceError(self.fail)
        if self.count is None:
            return [1]
        return list(range(1, self.count + 1))[:int(limit)]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.allow_path = self.root / "allowlist.json"
        self.allow_path.write_text(json.dumps(ALLOW), encoding="utf-8")
        self.events = self.root / "sessions" / "events.jsonl"
        self.events.parent.mkdir(parents=True, exist_ok=True)
        self.events.write_text("", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def engine(self, source, repo="owner/demo") -> Engine:
        cfg = config.load(None, {"mode": "assist",
                                 "github": {"source": "none", "repo": repo},
                                 "engine": {"archived_crosscheck": False,
                                            "session_events_path": str(self.events)}})
        return Engine(cfg, Store(self.state), source, None,
                      project_allowlist=ProjectAllowlist(self.allow_path))

    @staticmethod
    def events_of(engine):
        raw = engine.store.events_path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in raw if line.strip()]


class SourceFaults(unittest.TestCase):
    def test_transport_symptoms_are_transient(self):
        for message in ("SourceError: TimeoutError: ",
                        "ClientConnectorError: Cannot connect to host api.github.com",
                        "ServerDisconnectedError: Server disconnected",
                        "ssl: default [None]",
                        "ConnectionResetError: reset by peer"):
            self.assertEqual(source_faults.classify(message), "transient", message)

    def test_everything_else_is_fatal(self):
        for message in ("GitHub 401: Bad credentials",
                        "GitHub 404: Not Found",
                        "unexpected issues payload",
                        "fixture /tmp/x.json is not valid JSON",
                        "KeyError: 'projects'",
                        ""):
            self.assertEqual(source_faults.classify(message), "fatal", message)


class ProjectOwnership(Base):
    def test_suspended_issue_still_resolves_its_project(self):
        engine = self.engine(_ListSource([]))
        self.assertEqual(engine._project_for(7), "demo")
        self.assertEqual(engine._project_for(1), "demo")

    def test_unknown_repository_stays_unknown(self):
        engine = self.engine(_ListSource([]), repo="owner/nowhere")
        self.assertIsNone(engine._project_for(7))

    def test_refusal_text_separates_the_two_faults(self):
        allow = ProjectAllowlist(self.allow_path)
        ok, reason = allow.check(None)
        self.assertFalse(ok)
        self.assertIn("no project claims this repository", reason)
        ok, reason = allow.check("ghost")
        self.assertFalse(ok)
        self.assertIn("project 'ghost' not allowlisted", reason)


class ProjectPrecedence(Base):
    """Counter-example: the live list wins over the repository fallback."""

    def test_an_explicit_listing_beats_the_repository(self):
        engine = self.engine(_ListSource([]), repo="owner/demo")
        self.assertEqual(engine._project_for(2), "other")

    def test_fallback_never_invents_a_project(self):
        engine = self.engine(_ListSource([]), repo="owner/nowhere")
        self.assertIsNone(engine._project_for(999))


class DeliveryRecovery(Base):
    def test_delivery_behind_cursor_is_read_again(self):
        rows = [{"id": 10, "body": "older chatter"},
                {"id": 20, "body": "[WK:wk-ISS-140-x] 交付完成"}]
        read = collect.read_issue(
            _ListSource(rows), 140, "owner/demo#140", {}, 25,
            at_mode=False, qa_recovery=False, delivery_recovery=True,
            max_comments=50, max_pages=10)
        self.assertEqual([row["id"] for row in read.fresh], [20])

    def test_without_recovery_the_cursor_still_wins(self):
        rows = [{"id": 20, "body": "[WK:wk-ISS-140-x] 交付完成"}]
        read = collect.read_issue(
            _ListSource(rows), 140, "owner/demo#140", {}, 25,
            at_mode=False, qa_recovery=False, delivery_recovery=False,
            max_comments=50, max_pages=10)
        self.assertEqual(read.fresh, [])

    def test_recovery_predicate_mirrors_qa(self):
        terminal = {"completed", "archived", "cancelled"}
        live_worker = [{"role": "worker", "status": "working", "delivery_cids": []}]
        delivered = [{"role": "worker", "status": "working",
                      "delivery_cids": [20]}]
        gone = [{"role": "worker", "status": "completed", "delivery_cids": []}]
        cids = lambda rec: list(rec.get("delivery_cids") or [])
        self.assertTrue(collect.delivery_recovery(
            live_worker, terminal=terminal, output_cids=cids))
        self.assertFalse(collect.delivery_recovery(
            delivered, terminal=terminal, output_cids=cids))
        self.assertFalse(collect.delivery_recovery(
            gone, terminal=terminal, output_cids=cids))


class CrossTalk(Base):
    """Counter-example: each recovery reads only its own kind of row."""

    def test_delivery_recovery_does_not_take_review_rows(self):
        rows = [{"id": 20, "body": "[qa-lwa-ISS-99]-[完工]-[pm-x]-[lwa] QA 通过"}]
        read = collect.read_issue(
            _ListSource(rows), 140, "owner/demo#140", {}, 25,
            at_mode=False, qa_recovery=False, delivery_recovery=True,
            max_comments=50, max_pages=10)
        self.assertEqual(read.fresh, [], "a review is not a delivery")

    def test_qa_recovery_does_not_take_delivery_rows(self):
        rows = [{"id": 20, "body": "[WK:wk-ISS-140-x] 交付完成"}]
        read = collect.read_issue(
            _ListSource(rows), 140, "owner/demo#140", {}, 25,
            at_mode=False, qa_recovery=True, delivery_recovery=False,
            max_comments=50, max_pages=10)
        self.assertEqual(read.fresh, [], "a delivery is not a review")


class RoundFaults(Base):
    def test_a_transient_failure_reads_nothing_and_keeps_no_page(self):
        from super_agent.github import cache as github_cache

        github_cache.write_issues([7], root=self.root)
        engine = self.engine(_ListSource([], fail="TimeoutError: "))
        summary = engine.run_once()
        self.assertTrue(summary.get("errors"))
        self.assertFalse(summary.get("source_fatal"),
                         "a timeout is not fatal on its own")
        self.assertEqual(engine._live_issues, set(),
                         "a cached page must never stand in for the live set")
        events = self.events_of(engine)
        failed = [e for e in events if e.get("type") == "source.failed"]
        self.assertEqual(failed[-1].get("kind"), "transient")
        self.assertNotIn("stale_issues", failed[-1])
        self.assertEqual([e for e in events if e.get("type") == "allowlist.synced"],
                         [])
        self.assertEqual([e for e in events if e.get("type") == "comment.processed"],
                         [], "a failed round consumes no comment")

    def test_a_fatal_failure_is_flagged_for_the_loop(self):
        engine = self.engine(_ListSource([], fail="GitHub 401: Bad credentials"))
        summary = engine.run_once()
        self.assertTrue(summary.get("source_fatal"))
        failed = [e for e in self.events_of(engine) if e.get("type") == "source.failed"]
        self.assertEqual(failed[-1].get("kind"), "fatal")

    def test_truncation_is_visible_and_not_invented(self):
        engine = self.engine(_ListSource([], count=3))
        engine.cfg["github"]["max_issues"] = 3
        self.assertEqual(engine.run_once().get("issues_truncated"), 3)

        quiet = self.engine(_ListSource([], count=2))
        quiet.cfg["github"]["max_issues"] = 3
        self.assertNotIn("issues_truncated", quiet.run_once())


class LoopStops(Base):
    """The loop, not just the round: fatal ends it now, blips get three."""

    def _store(self):
        return Store(self.state)

    def test_a_fatal_round_ends_the_run_immediately(self):
        class Fatal:
            calls = 0

            def run_once(self):
                Fatal.calls += 1
                return {"errors": ["issues: GitHub 401: Bad credentials"],
                        "source_fatal": True}

        sleeps = []
        out = runloop.run_loop(Fatal(), self._store(), interval=1,
                               max_failures=3, sleep=sleeps.append)
        self.assertEqual(out["reason"], "fatal")
        self.assertEqual(out["rounds"], 1, "no retry, no waiting")
        self.assertEqual(out["exit_code"], 1)
        self.assertEqual(sleeps, [])

    def test_a_blip_is_tolerated_then_the_run_fails(self):
        class Blip:
            def run_once(self):
                return {"errors": ["issues: TimeoutError: "]}

        out = runloop.run_loop(Blip(), self._store(), interval=1,
                               max_failures=3, sleep=lambda _s: None)
        self.assertEqual(out["reason"], "max_failures")
        self.assertEqual(out["rounds"], 3)
        self.assertEqual(out["exit_code"], 1)

    def test_the_default_budget_is_three(self):
        self.assertEqual(runloop.DEFAULT_MAX_FAILURES, 3)

    def test_the_clock_budget_stops_a_streak_that_rounds_allow(self):
        """``min(rounds, 10 min)``: a raised round budget cannot buy an hour."""

        from datetime import datetime, timedelta, timezone

        class Blip:
            rounds = 0

            def run_once(self):
                Blip.rounds += 1
                return {"errors": ["issues: TimeoutError: "]}

        base = datetime(2026, 11, 28, tzinfo=timezone.utc)

        def fake_now():
            # 4 minutes per round: three rounds fit in the 10-minute budget,
            # the fourth does not - even though 99 rounds are allowed.
            return (base + timedelta(minutes=4 * Blip.rounds)).isoformat(
                timespec="seconds")

        out = runloop.run_loop(Blip(), self._store(), interval=1,
                               max_failures=99, sleep=lambda _s: None, now=fake_now)
        self.assertEqual(out["reason"], "max_failures")
        self.assertEqual(out["rounds"], 4, "10 minutes at 4 min/round is the ceiling")

    def test_a_stop_names_its_cause_in_health(self):
        """``running=false`` alone is not a report; the next reader needs why."""

        class Fatal:
            def run_once(self):
                return {"errors": ["issues: GitHub 401: Bad credentials"],
                        "source_fatal": True}

            def write_health(self, **kwargs):
                self.kwargs = kwargs
                return kwargs

        engine = Fatal()
        runloop.run_loop(engine, self._store(), interval=1, max_failures=3,
                         sleep=lambda _s: None)
        self.assertIn("GitHub 401", engine.kwargs.get("error", ""))
        self.assertTrue(engine.kwargs.get("degraded"))


class RefusalRealert(Base):
    """Counter-example: the reminder must be slow, and a success must reset it."""

    KEY = "owner/demo#1"

    def _drive(self, engine, rounds, *, start=1, index=None, route=None):
        index = index or {"meta": {"run_count": 0},
                          "issues": {self.KEY: {"needs_person": []}}}
        route = {} if route is None else route
        for i in range(start, start + rounds):
            index["meta"]["run_count"] = i
            engine._count_route_refusal(index, route, self.KEY, 42,
                                        {"reason": "no project claims this repository"})
        return index, route

    def test_first_alert_at_three_then_every_thirty(self):
        engine = self.engine(_ListSource([]))
        index, route = self._drive(engine, 3)
        self.assertEqual(len(index["issues"][self.KEY]["needs_person"]), 1)
        index, route = self._drive(engine, 27, start=4, index=index, route=route)
        rows = index["issues"][self.KEY]["needs_person"]
        self.assertEqual(len(rows), 2, "rounds 4..29 stay quiet")
        self.assertIn("realerted_at", route["refusal_streak"])
        index, _ = self._drive(engine, 1, start=31, index=index, route=route)
        self.assertEqual(len(index["issues"][self.KEY]["needs_person"]), 2)

    def test_a_second_round_does_not_alert(self):
        engine = self.engine(_ListSource([]))
        index, _ = self._drive(engine, 2)
        self.assertEqual(index["issues"][self.KEY]["needs_person"], [])

    def test_success_clears_the_clock_for_a_new_cause(self):
        engine = self.engine(_ListSource([]))
        index, route = self._drive(engine, 3)
        self.assertIn("alerted_at", route["refusal_streak"])
        engine._clear_route_refusal(route)
        self.assertNotIn("alerted_at", route["refusal_streak"])
        self.assertNotIn("realerted_at", route["refusal_streak"])
        index, route = self._drive(engine, 2, start=10, index=index, route=route)
        self.assertNotIn("alerted_at", route["refusal_streak"])
        index, route = self._drive(engine, 1, start=12, index=index, route=route)
        self.assertIn("alerted_at", route["refusal_streak"])


class FaultsCarryTheirCause(Base):
    """The rule behind the rule: a failure states *why*, not just *what*.

    "KeyError" alone is a symptom with no address - the reader cannot tell a
    missing config key from a corrupt index from a bug three frames up. These
    tests pin the type, the message and at least one frame of the traceback in
    every record that reports a fault.
    """

    def test_describe_states_type_message_and_where(self):
        try:
            raise ValueError("index.json has no 'assignments' key")
        except ValueError as exc:
            text = fault_report.describe(exc)
        self.assertIn("ValueError: index.json has no 'assignments' key", text)
        self.assertIn("line", text, "the raise site must be visible")
        self.assertIn(__file__.split("\\")[-1], text,
                      "the frame names the file the fault happened in")

    def test_describe_never_raises_on_an_odd_exception(self):
        class Weird(Exception):
            def __str__(self):
                raise RuntimeError("no text for you")

        text = fault_report.describe(Weird())
        self.assertIn("Weird", text)

    def test_a_source_fault_records_the_trace_not_just_the_type(self):
        engine = self.engine(_ListSource([], fail="TimeoutError: "))
        engine.run_once()
        failed = [e for e in self.events_of(engine)
                  if e.get("type") == "source.failed"][-1]
        self.assertIn("TimeoutError", failed["trace"])
        self.assertIn("line", failed["trace"], "the raise site must be recorded")

    def test_a_fatal_fault_records_the_trace_too(self):
        engine = self.engine(_ListSource([], fail="GitHub 401: Bad credentials"))
        engine.run_once()
        fatal = [e for e in self.events_of(engine)
                 if e.get("type") == "source.failed"][-1]
        self.assertEqual(fatal["kind"], "fatal")
        self.assertIn("GitHub 401", fatal["trace"])

    def test_a_crashing_round_prints_its_frames(self):
        import io
        from contextlib import redirect_stderr

        class Crash:
            def run_once(self):
                raise KeyError("assignments")

        err = io.StringIO()
        with redirect_stderr(err):
            out = runloop.run_loop(Crash(), Store(self.state), interval=1,
                                   max_failures=1, sleep=lambda _s: None)
        self.assertEqual(out["reason"], "max_failures")
        self.assertIn("KeyError", err.getvalue())
        self.assertIn("line", err.getvalue(), "stderr must show the frames")
        failed = [e for e in self.events_of(type("E", (), {
            "store": Store(self.state)})())
            if e.get("type") == "run.failed"][-1]
        self.assertIn("KeyError", failed["trace"])


class StopNotice(Base):
    """A stop that nobody hears is not fail-loud, it is fail-quiet."""

    class _Bus:
        def __init__(self, reply=None, boom=None):
            self.sent = []
            self.reply = reply if reply is not None else {"ok": True}
            self.boom = boom

        def inject(self, event, payload):
            self.sent.append((event, payload))
            if self.boom:
                raise self.boom
            return self.reply

    def _engine(self, *, sid="session_sup", bus=None):
        engine = self.engine(_ListSource([]))
        engine._supervisor_sid = lambda index=None: sid
        engine.bus = bus
        return engine

    def test_a_fatal_stop_is_handed_to_the_supervisor_leg(self):
        bus = self._Bus()
        engine = self._engine(bus=bus)
        out = stop_notice.notice(engine, engine.store, reason="fatal",
                                 error="GitHub 401: Bad credentials",
                                 now=lambda: "2026-11-28T01:00:00+00:00")
        self.assertTrue(out["sent"])
        event, payload = bus.sent[-1]
        self.assertEqual(event, "event.agent.supervisor")
        self.assertEqual(payload["sid"], "session_sup")
        self.assertIn("【引擎停机】", payload["text"])
        self.assertIn("GitHub 401", payload["text"])
        labels = [e for e in self.events_of(engine)
                  if e.get("type") == "run.stop_notice"][-1]
        self.assertTrue(labels["ok"])

    def test_no_supervisor_sid_is_reported_not_raised(self):
        engine = self._engine(sid="", bus=self._Bus())
        out = stop_notice.notice(engine, engine.store, reason="max_failures",
                                 error="TimeoutError", now=lambda: "t")
        self.assertFalse(out["sent"])
        self.assertEqual(out["error"], "no supervisor sid")

    def test_a_bus_that_raises_does_not_raise(self):
        engine = self._engine(bus=self._Bus(boom=RuntimeError("daemon down")))
        out = stop_notice.notice(engine, engine.store, reason="fatal",
                                 error="x", now=lambda: "t")
        self.assertFalse(out["sent"])
        self.assertIn("daemon down", out["error"])

    def test_a_clean_stop_sends_nothing(self):
        bus = self._Bus()
        engine = self._engine(bus=bus)

        class Clean:
            rounds = 0

            def run_once(self):
                Clean.rounds += 1
                return {"mode": "assist", "errors": []}

            def write_health(self, **_kw):
                return {}

        engine_obj = Clean()
        engine_obj.bus = bus
        engine_obj._supervisor_sid = lambda index=None: "session_sup"
        runloop.run_loop(engine_obj, Store(self.state), interval=1,
                         max_failures=3, max_rounds=2, sleep=lambda _s: None)
        self.assertEqual(bus.sent, [], "a clean stop is not an incident")


class RecoveryThrottle(unittest.TestCase):
    def test_the_delivery_walk_is_gated_but_never_the_qa_one(self):
        terminal = {"completed"}
        cids = lambda rec: list(rec.get("delivery_cids") or [])
        rows = {"asg_1": {"role": "worker", "status": "working",
                           "delivery_cids": [], "thread": "t"},
                "asg_2": {"role": "qa", "status": "working",
                          "review_comment_id": None, "thread": "t"}}
        qa_off, delivery_off = collect.recovery_flags(
            rows, "t", terminal=terminal, output_cids=cids, due=False)
        qa_on, delivery_on = collect.recovery_flags(
            rows, "t", terminal=terminal, output_cids=cids, due=True)
        self.assertTrue(qa_off, "the QA half is not what the throttle is for")
        self.assertEqual(qa_off, qa_on, "due must not change the QA answer")
        self.assertFalse(delivery_off, "the whole-listing walk is throttled")
        self.assertTrue(delivery_on)

    def test_the_default_cadence_is_three_rounds(self):
        from super_agent import config_defaults
        self.assertEqual(
            config_defaults.DEFAULTS["github"]["delivery_recovery_every"], 3)


if __name__ == "__main__":
    unittest.main()
