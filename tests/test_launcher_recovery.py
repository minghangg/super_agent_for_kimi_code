"""Counter-example 4: after SIGKILL, only ``prepared`` is replayed.

The failure this guards against is the tempting one: on restart, take every
unfinished record and send it again. ``in_flight`` records may already have
landed, so a blind resend creates the duplicate the whole outbox exists to
prevent.

The assertions are about **which store calls happened**, not only about the
final state: a resend that fails immediately would leave the state looking
identical while having already POSTed. So the transport is counted.

Run: ``python -m pytest tests/test_launcher_recovery.py -q``
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from super_agent import launcher, store as store_mod
from super_agent.config import load as load_config
from super_agent.engine import Engine
from super_agent.github_source import FixtureSource
from super_agent.store import Store

FIXTURE = {"issues": {"42": {"title": "t", "comments": [
    {"id": 9001, "body": "hello", "author": "teammate"}]}}}


class CountingTransport:
    """Stands in for the GitHub writer: records every POST it is asked to make."""

    def __init__(self) -> None:
        self.posts: list[dict] = []

    def __call__(self, *args, **kwargs):
        self.posts.append({"args": args, "kwargs": kwargs})
        return {"id": 1}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fixture = self.root / "fixture.json"
        self.fixture.write_text(json.dumps(FIXTURE), encoding="utf-8")
        self.state = self.root / "data"
        self.state.mkdir()
        self.cfg = load_config(None, {
            "github": {"source": "fixture", "fixture": str(self.fixture)},
            "store": {"max_event_lines": 10_000, "max_outbox_lines": 1_000},
        })
        self.store = Store(self.state)

    def tearDown(self):
        self.tmp.cleanup()

    def engine(self) -> Engine:
        eng = Engine(self.cfg, self.store, FixtureSource(self.fixture), bus=None,
                     clock=lambda: 1_000_000.0)
        return eng


class TestBlindResend(Base):
    """The three statuses must be treated differently, and only one replays."""

    def test_in_flight_and_uncertain_are_reconciled_not_resent(self):
        prepared = store_mod.action_id("A1", "comment", "c-1")
        in_flight = store_mod.action_id("A2", "comment", "c-2")
        uncertain = store_mod.action_id("A3", "comment", "c-3")
        self.store.begin_action(prepared, verb="comment", kind="comment",
                                assignment_id="A1", input_ref="c-1", issue=42)
        self.store.begin_action(in_flight, verb="comment", kind="comment",
                                assignment_id="A2", input_ref="c-2", issue=42)
        self.store.mark_in_flight(in_flight)
        self.store.begin_action(uncertain, verb="comment", kind="comment",
                                assignment_id="A3", input_ref="c-3", issue=42)
        self.store.mark_in_flight(uncertain)
        self.store.mark_uncertain(uncertain, "timeout")

        recovered = self.store.recover_outbox()
        self.assertEqual([r["action_id"] for r in recovered["replayable"]],
                         [prepared], "only prepared is certain not to have run")
        self.assertEqual(sorted(recovered["moved_to_uncertain"]), [in_flight])
        self.assertEqual(sorted(r["action_id"] for r in recovered["reconcile"]),
                         sorted([in_flight, uncertain]))

        # Reconciling without a probe must not send anything: it parks the
        # record for a person. This is the assertion that a blind resend fails.
        transport = CountingTransport()
        with mock.patch.object(store_mod, "atomic_write_text"), \
                mock.patch("super_agent.github.write_client.request",
                           transport, create=True):
            outcomes = self.engine().reconcile_uncertain(None)
        self.assertEqual(transport.posts, [],
                         f"reconcile must not POST: {transport.posts}")
        self.assertEqual({r["outcome"] for r in outcomes}, {"waiting_person"})

    def test_startup_replays_prepared_with_its_original_action_id(self):
        prepared = store_mod.action_id("A1", "comment", "c-1")
        rec = self.store.begin_action(prepared, verb="comment", kind="comment",
                                      assignment_id="A1", input_ref="c-1",
                                      issue=42)
        seen = {}

        def fake_dispatch(action, reuse_action_id=None):
            seen["action_id"] = reuse_action_id
            seen["kind"] = action.get("kind")
            return {"dispatched": True, "action_id": reuse_action_id}

        eng = self.engine()
        with mock.patch.object(eng, "_dispatch", fake_dispatch):
            result = eng.startup()
        self.assertTrue(result["ok"], result)
        self.assertEqual(seen["action_id"], rec["action_id"],
                         "a replay reuses the original id, never a #2 attempt")


class TestLauncherStartsRecovery(Base):
    """The host must run recovery before the first round, not after.

    A round that starts on an unresolved outbox would act on state the outbox
    has not settled yet.
    """

    def test_build_engine_component_runs_startup(self):
        eng = self.engine()
        calls: list[str] = []

        def fake_startup(probe=None):
            calls.append("startup")
            return {"ok": True, "error": ""}

        def fake_run_once():
            calls.append("round")
            return {"mode": "observe", "issues": 0, "comments_new": 0,
                    "processed": 0, "needs_person": 0, "external_actions": 0,
                    "errors": []}

        eng.startup = fake_startup
        eng.run_once = fake_run_once
        with mock.patch("super_agent.cli._engine", return_value=eng):
            built = launcher.build_engine_component(self.cfg, self.store)
        self.assertIs(built, eng)
        self.assertEqual(calls, ["startup"],
                         "startup must run once, and no round before it")

    def test_recovery_failure_refuses_to_start(self):
        eng = self.engine()
        eng.startup = lambda probe=None: {"ok": False, "error": "outbox stuck"}
        with mock.patch("super_agent.cli._engine", return_value=eng):
            with self.assertRaises(RuntimeError) as ctx:
                launcher.build_engine_component(self.cfg, self.store)
        self.assertIn("outbox stuck", str(ctx.exception))


class TestEntryPointParity(Base):
    def test_same_issue_has_the_same_result_through_cli_and_launcher(self):
        from super_agent import cli
        from super_agent.store import Store

        old_store = Store(self.root / "old")
        new_store = Store(self.root / "new")
        old = cli._engine(self.cfg, old_store, None)
        new = launcher.build_engine_component(self.cfg, new_store)
        old.startup()
        old.run_once()
        new.run_once()
        self.assertEqual(old_store.load_index().get("issues"),
                         new_store.load_index().get("issues"))
        self.assertEqual(old_store.load_cursor(), new_store.load_cursor())
        def event_types(store):
            return [json.loads(line)["type"] for line in
                    store.events_path.read_text(encoding="utf-8").splitlines() if line]
        self.assertEqual(event_types(old_store), event_types(new_store))


if __name__ == "__main__":
    unittest.main()
