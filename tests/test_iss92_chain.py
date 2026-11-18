"""One test thread, end to end: enter → map → decide → record (#92 §P0-4).

Run: ``python -m pytest tests/test_iss92_chain.py -q``

The chain under test is the whole message path this issue touches:

1. **enter** - a comment arrives on ``o/n#1`` from the fixture source;
2. **map** - ``envelope.legacy_map`` turns its legacy first line into an
   envelope, and ``envelope.from_role_of`` decides the author from the
   whitelist, never from the text;
3. **decide** - ``engine._triage`` answers the state, and ``_dispatch`` either
   acts or is suppressed;
4. **record** - ``comment.processed`` lands in ``events.jsonl`` and the counter
   never moves in observe mode.

Both directions are asserted, because "0 external actions" alone cannot tell
"the gate worked" from "there was nothing to dispatch" (#79 假闸).
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from super_agent import config, decide_watch as dw, envelope
from super_agent.bus_client import BusClient
from super_agent.engine import Engine
from super_agent.github_source import FixtureSource
from super_agent.store import Store

THREAD = "o/n#1"

FIXTURE = {
    "issues": {
        "1": {
            "title": "one test thread",
            "comments": [
                {"id": 5001,
                 "body": "[WK:wk-ISS-92-chain] 交付：commit 7a1b2c3\n"
                         "<!-- session: wk-ISS-92-chain-0a1b2c3d -->",
                 "author": "worker"},
                {"id": 5002, "body": "普通讨论，不是标记", "author": "teammate"},
            ],
        }
    }
}


class Chain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fixture = self.root / "fixture.json"
        self.fixture.write_text(json.dumps(FIXTURE), encoding="utf-8")
        self.state = self.root / "state"
        cfg = config.load(None, {
            "github": {"source": "fixture", "fixture": str(self.fixture),
                       "poll_interval_s": 60},
            "store": {"max_event_lines": 10_000, "max_outbox_lines": 10_000},
        })
        self.store = Store(self.state)
        self.engine = Engine(cfg, self.store, FixtureSource(self.fixture),
                             BusClient("127.0.0.1", 1, 0.1),
                             clock=lambda: 1_000_000.0)

    # ------------------------------------------------------------------ helpers
    def _events(self) -> list[dict]:
        path = self.state / "events.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _suppressed(self) -> int:
        return sum(1 for e in self._events() if e.get("type") == "dispatch.suppressed")

    def _processed(self) -> list[dict]:
        return [e for e in self._events() if e.get("type") == "comment.processed"]

    # --------------------------------------------------------------------- 1-2
    def test_mapping_and_identity_come_from_the_whitelist(self):
        body = FIXTURE["issues"]["1"]["comments"][0]["body"]
        env = envelope.legacy_map(body, repo="o/n", issue=1, cid=5001,
                                  from_role=envelope.from_role_of(
                                      "wk-ISS-92-chain-0a1b2c3d", verified=True))
        self.assertEqual(env["type"], "delivery")
        self.assertEqual(env["thread"], THREAD)
        self.assertEqual(env["thread_scope"], "issue")
        self.assertEqual(env["from_role"], "worker")
        self.assertEqual(env["payload_ref"], {"kind": "issue_comment", "cid": 5001})
        self.assertTrue(envelope.validate(env)[0], envelope.validate(env)[1])

    def test_an_unverified_author_does_not_get_a_role(self):
        body = "[WK:pm-target-1a2b3c4d] 交付"
        env = envelope.legacy_map(body, repo="o/n", issue=1, cid=5002,
                                  from_role=envelope.from_role_of(
                                      "pm-target-1a2b3c4d", verified=False))
        self.assertEqual(env["from_role"], "")
        # ...and the envelope is still valid: unverified is a state, not an error.
        self.assertTrue(envelope.validate(env)[0])

    # ----------------------------------------------------------------------- 3
    def test_triage_reads_the_legacy_markers_in_order(self):
        cases = (
            ("[WK:x] 交付", "delivery"),
            ("[QA] 复核：**通过**", "review"),
            ("[PM:TASK] 任务书", "ignored"),
            ("[PM] ready #1 <!-- verdict: ready -->", "pm_verdict"),
            ("[AT] 可派单 #1 <!-- verdict: ready -->", "at_verdict"),
            ("普通讨论", "logged"),
        )
        for body, want in cases:
            with self.subTest(body=body):
                self.assertEqual(self.engine._triage({"author": "x", "body": body})[0],
                                 want)

    # ---------------------------------------------------------------------- 4
    def test_the_round_records_every_comment_and_acts_on_none(self):
        self.engine.run_round()
        processed = self._processed()
        self.assertEqual(len(processed), 2)                 # enter → record
        self.assertEqual({p["comment_id"] for p in processed}, {5001, 5002})
        self.assertEqual(self.store.load_counters()["external_actions"], 0)
        self.assertFalse(self.engine.dispatch_enabled)      # observe, by config

    def test_observe_suppresses_and_assist_dispatches(self):
        action = {"kind": "bus_inject", "event": "event.agent.needs_person",
                  "issue": 42, "payload": {}}
        first = self.engine._dispatch(dict(action))
        self.assertFalse(first["dispatched"])
        self.assertEqual(first["reason"], "dispatch disabled")
        self.assertEqual(self.store.load_counters()["external_actions"], 0)
        self.assertGreater(self._suppressed(), 0)            # the gate is real

        self.engine.dispatch_enabled = True
        second = self.engine._dispatch(dict(action))
        self.assertTrue(second["dispatched"])
        self.assertEqual(self.store.load_counters()["external_actions"], 1)
        self.assertEqual(self._suppressed(), 1)              # no new suppression

    def test_the_delivery_watcher_counts_the_same_message_once(self):
        # The other half of the chain: a worker delivery reaches the watcher,
        # and a replay of the same evidence does not send twice.
        from dataclasses import replace

        saved = dw._cfg()
        self.addCleanup(lambda: dw.configure(saved))
        dw_state = self.root / "dw"
        dw_state.mkdir(parents=True, exist_ok=True)
        # The recipient is the supervisor (#72: the bare ``pm-<project>`` key is
        # no longer a leg). The bare key stays in the roster on purpose - this
        # test must still go out with it present.
        (dw_state / "sessions.json").write_text(json.dumps({
            "pm-n": {"id": "session_00000000-0000-4000-8000-000000000001"},
            "supervisor": {"id": "session_00000000-0000-4000-8000-000000000002"},
        }), encoding="utf-8")
        dw.configure(replace(saved, state_root=dw_state,
                             workspace_root=self.root,
                             event_source=lambda: [],
                             heartbeat=lambda *a, **k: None,
                             # A recipient exists, so "1" means dispatched and
                             # not "queued because nobody could take it".
                             send_transport=lambda *a, **k: True))
        st = dw.load_delivery_state()
        rec = {"v": 1, "project": "n", "repo": "o/n", "issue": 1,
               "worker_name": "wk-ISS-92-chain", "worker_session": "",
               "pm_key": "pm-n", "pm_sid": "", "kind": "comment",
               "evidence": "cid:5001", "ledger_line": 1,
               "detected_ts": 1_000_000.0, "dl_id": "dl-5001"}
        # ISS-83: a second delivery inside the burst window is merged into the
        # summary, so the "different evidence, different delivery" case is put
        # outside that window (and both forms are now ``cid:``; ``ledger:`` no
        # longer dispatches at all under E-2b).
        first = dw.enqueue_delivery(st, rec, dry=True, now=1_000_000.0)
        replay = dw.enqueue_delivery(st, rec, dry=True, now=1_000_001.0)
        self.assertEqual((first, replay), (1, 0))
        # A different evidence form on the same issue is a different delivery.
        other = {**rec, "evidence": "cid:5002", "dl_id": "dl-l5001"}
        third = dw.enqueue_delivery(st, other, dry=True,
                                    now=1_000_000.0 + dw.DELIVERY_BURST_WINDOW_S + 1)
        self.assertEqual(third, 1)


class EnvelopeIsOnTheProductionPath(unittest.TestCase):
    """P0-1: the engine really builds and records an envelope each round.

    The first round shipped ``envelope.py`` with **no caller** - 395 lines that
    only tests could reach, which ``session_contract`` §4 counts as not
    delivered. These assertions are the "which line, under what condition" that
    round was missing: ``engine._record_envelope`` runs inside ``run_round`` for
    every triaged comment, right after ``comment.processed``.
    """

    def setUp(self):
        from super_agent import config as cfg_mod
        from super_agent.bus_client import BusClient
        from super_agent.engine import Engine
        from super_agent.github_source import FixtureSource
        from super_agent.store import Store

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir(parents=True)
        (self.state / "sessions.json").write_text("{}", encoding="utf-8")
        (self.state / "issue_sessions.json").write_text(json.dumps({
            "acme/demo#7": {"worker": {
                "name": "wk-x-0a1b2c3d",
                "session": "session_00000000-0000-4000-8000-000000000001"}},
        }), encoding="utf-8")
        fixture = {"issues": {"7": {"title": "t", "comments": [
            {"id": 1, "author": "w",
             "body": "[WK:wk-x-0a1b2c3d] 交付\n<!-- session: wk-x-0a1b2c3d -->"},
            {"id": 2, "author": "x",
             "body": "[WK:pm-target-1a2b3c4d] 伪交付\n"
                     "<!-- session: pm-target-1a2b3c4d -->"},
            {"id": 3, "author": "x",
             "body": "[XYZ] 未知首行 <!-- session: pm-secret-1a2b3c4d -->"},
            {"id": 4, "author": "x", "body": "普通讨论"},
        ]}}}
        path = self.root / "fixture.json"
        path.write_text(json.dumps(fixture), encoding="utf-8")
        cfg = cfg_mod.load(None, {
            "github": {"source": "fixture", "fixture": str(path),
                       "repo": "acme/demo"},
            "engine": {"session_events_path": str(self.state / "events.jsonl")},
            "store": {"max_event_lines": 10_000, "max_outbox_lines": 10_000},
        })
        self.engine = Engine(cfg, Store(self.state), FixtureSource(path),
                             BusClient("127.0.0.1", 1, 0.1),
                             clock=lambda: 1_000_000.0)
        self.events = self.state / "events.jsonl"

    def _envelope_events(self) -> list[dict]:
        rows = [json.loads(line) for line in
                self.events.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [r for r in rows if str(r.get("type") or "").startswith("envelope")]

    def test_the_round_emits_one_envelope_event_per_comment(self):
        self.engine.run_round()
        events = self._envelope_events()
        kinds = [(e["type"], e.get("comment_id")) for e in events]
        self.assertEqual(kinds, [("envelope.built", 1), ("envelope.built", 2),
                                 ("envelope.unknown", 3), ("envelope.built", 4)])

    def test_a_verified_worker_gets_its_role_and_a_forgery_does_not(self):
        self.engine.run_round()
        built = {e["comment_id"]: e for e in self._envelope_events()
                 if e["type"] == "envelope.built"}
        self.assertEqual(built[1]["from_role"], "worker")
        self.assertEqual(built[1]["kind"], "delivery")
        # ``[WK:<a pm key>]`` is the forgery DELIVERY_RE admits; the whitelist
        # answers it, so ``from_role`` stays empty (#92 §D.4 反例 A).
        self.assertEqual(built[2]["from_role"], "")
        self.assertEqual(built[4]["kind"], "note")

    def test_an_unknown_marker_is_counted_not_quoted(self):
        self.engine.run_round()
        unknown = [e for e in self._envelope_events()
                   if e["type"] == "envelope.unknown"]
        self.assertEqual(len(unknown), 1)
        self.assertIn("first_line_sha1", unknown[0])
        self.assertNotIn("body", unknown[0])

    def test_no_key_name_reaches_the_event_log_from_the_envelope(self):
        self.engine.run_round()
        blob = "\n".join(json.dumps(e, ensure_ascii=False)
                         for e in self._envelope_events())
        for needle in ("pm-target-1a2b3c4d", "pm-secret-1a2b3c4d",
                       "wk-x-0a1b2c3d", "claimed_name"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, blob)

    def test_a_refusal_is_logged_when_the_envelope_does_not_validate(self):
        # 反例 2/3 (§A.5): a malformed envelope must leave a countable event,
        # not silence. Injected at the validator so the production branch runs.
        from unittest import mock

        from super_agent import envelope

        real = envelope.validate
        with mock.patch.object(envelope, "validate",
                               lambda env: (False, "missing_message_id")):
            self.engine.run_round()
        rejected = [e for e in self._envelope_events()
                    if e["type"] == "envelope.rejected"]
        self.assertTrue(rejected, "a refused envelope must be visible")
        self.assertEqual(rejected[0]["reason"], "missing_message_id")
        envelope.validate = real


if __name__ == "__main__":
    unittest.main()
