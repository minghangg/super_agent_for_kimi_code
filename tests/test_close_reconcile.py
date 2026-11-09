"""Close reconciliation: four conditions, two rounds, never a fake completion.

The properties pinned here:

* ``completed`` needs the four conditions in two consecutive rounds, and the
  second round has to be a later ``run_round`` - a loop inside one round cannot
  confirm itself;
* a failed observation moves a release into ``close_reconcile`` and resets the
  streak; it never completes;
* an already-archived worker is not archived again and produces no error;
* a late event on a completed assignment is audited, not restarted;
* the scan only visits ``waiting_release``/``close_reconcile`` and honours
  ``engine.max_closed_thread_checks``.

Run: ``python -m pytest tests/test_close_reconcile.py -q``
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from super_agent import config, session_events
from super_agent.allowlist import ProjectAllowlist
from super_agent.engine import Engine
from super_agent.store import Store

THREAD = "owner/demo#1"

GOOD = {
    "schema_version": 1,
    "defaults": {"mode": "observe", "creation_mode": "pm_created"},
    "projects": {
        "demo": {"repo": "owner/demo", "cwd": ".", "mode": "assist",
                 "creation_mode": "pm_created", "issues": [1],
                 "actions": ["bus_inject", "collect", "reconcile", "archive"],
                 "max_active_writers": 1, "roles": ["worker", "qa"]},
    },
}


class FakeBus:
    def __init__(self):
        self.injected = []

    def healthz(self):
        return {"ok": True, "ready": True, "connected": True, "seq": 1}

    def inject(self, event_type, payload):
        self.injected.append((event_type, payload))
        return {"ok": True, "seq": len(self.injected)}


class FakeSessions:
    def __init__(self):
        self.archived = set()
        self.archive_calls = []

    def archive_session(self, sid):
        self.archive_calls.append(sid)
        self.archived.add(sid)
        return True, {"ids": [sid]}

    def session_info(self, sid):
        return {"archived": sid in self.archived}


class Comments:
    """Every comment at once, with a switch for the closed state."""

    def __init__(self):
        self.rows = []

    def add(self, cid, body, author="worker"):
        self.rows.append({"id": cid, "issue": 1, "body": body,
                          "author": author, "created_at": ""})

    def issues(self, limit=20):
        return [1]

    def title(self, issue):
        return "probe"

    def comments(self, issue):
        return sorted(self.rows, key=lambda c: c["id"])

    def paged(self, issue, *, since_id=0, max_pages=10):
        return self.comments(issue)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.events = self.root / "lifecycle" / "events.jsonl"
        self.events.parent.mkdir(parents=True, exist_ok=True)
        self.ledger = self.events.parent / "issue_sessions.json"
        self.allow = self.root / "allowlist.json"
        self.allow.write_text(json.dumps(GOOD), encoding="utf-8")
        self.source = Comments()
        self.bus = FakeBus()
        self.sessions = FakeSessions()
        self.issue_state = "closed"
        self.tasks = {}
        self.bind()

    def tearDown(self):
        self.tmp.cleanup()

    def bind(self):
        rows = [
            {"seq": 1, "at": "2026-11-10T10:00:00+00:00",
             "type": session_events.CREATED, "sid": "session_w1", "key": "wk-probe-1"},
            {"seq": 2, "at": "2026-11-10T10:00:01+00:00",
             "type": session_events.BOUND, "sid": "session_w1", "key": THREAD,
             "role": "worker", "issue": 1},
        ]
        self.events.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                               encoding="utf-8")

    def ledger_write(self, data):
        self.ledger.write_text(json.dumps(data), encoding="utf-8")

    def engine(self) -> Engine:
        cfg = config.load(None, {
            "mode": "assist",
            "github": {"source": "none", "repo": "owner/demo"},
            "engine": {"session_events_path": str(self.events),
                       "allowlist": ["issue_comment.created"]},
        })
        engine = Engine(cfg, Store(self.state), self.source, self.bus,
                        project_allowlist=ProjectAllowlist(self.allow),
                        session_api=self.sessions)
        # Injected so the test never touches the workbench consoles.
        object.__setattr__(engine, "_issue_state", lambda issue: self.issue_state)
        engine._test_tasks = self.tasks
        engine._running_tasks = lambda sid: (self.tasks.get(sid) or [], "")
        return engine

    def deliver_and_pass(self, engine, *, pass_now=True):
        self.source.add(9001, "[WK:wk-probe-1] done")
        if pass_now:
            self.source.add(9100, "[QA] **通过**", author="qa")
        engine.run_once()

    def index(self, engine):
        return engine.store.load_index()

    def rows(self):
        path = self.state / "events.jsonl"
        if not path.is_file():
            return []
        return [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]


class TwoRounds(Base):
    def test_completes_only_on_the_second_round(self):
        engine = self.engine()
        self.deliver_and_pass(engine)
        rec = self.index(engine)["assignments"]["asg_1_worker_1"]
        self.assertEqual(rec["status"], "waiting_release",
                         "one observation is not proof")
        state = self.index(engine)["close_reconcile"][THREAD]
        self.assertEqual(state["consecutive_ok"], 1)

        engine.run_once()
        rec = self.index(engine)["assignments"]["asg_1_worker_1"]
        self.assertEqual(rec["status"], "completed")
        state = self.index(engine)["close_reconcile"][THREAD]
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["consecutive_ok"], 2)
        kinds = [r["type"] for r in self.rows()]
        self.assertEqual(kinds.count("thread.completed"), 1)

    def test_one_round_alone_cannot_reach_two(self):
        # Proof that the streak is round-scoped: calling the reconciler twice
        # from the same round must not confirm it.
        engine = self.engine()
        self.deliver_and_pass(engine)
        index = self.index(engine)
        first = engine._reconcile_thread(index, THREAD)
        second = engine._reconcile_thread(index, THREAD)
        self.assertEqual(first["consecutive_ok"], 1)
        self.assertEqual(second["consecutive_ok"], 1,
                         "the same run_count must not advance the streak")
        self.assertEqual(index["assignments"]["asg_1_worker_1"]["status"],
                         "waiting_release")


class Counterfact1(Base):
    """worker archived early: no second archive, no error."""

    def test_an_already_archived_worker_is_not_archived_again(self):
        self.sessions.archived.add("session_w1")
        engine = self.engine()
        self.deliver_and_pass(engine)
        self.assertEqual(self.sessions.archive_calls, [],
                         "the read-back already says archived")
        out = engine.archive_thread(THREAD, self.index(engine))
        self.assertEqual(out["archived"], [])
        self.assertEqual(out["errors"], [])
        # The archived assignment is not a live binding, so the thread has no
        # worker to sync: the verdict is escalated, not dispatched. What matters
        # here is that nothing was archived twice and nothing raised.
        self.assertEqual(self.sessions.archive_calls, [])


class Counterfact2(Base):
    """a QA close while the sessions are still live: two rounds, then completed."""

    def test_two_rounds_are_required_and_the_check_is_repeatable(self):
        engine = self.engine()
        self.deliver_and_pass(engine)
        ledger = {THREAD: {"worker": {"session": "session_w1", "name": "wk-probe-1"}}}
        self.ledger_write(ledger)
        engine.run_once()
        rec = self.index(engine)["assignments"]["asg_1_worker_1"]
        self.assertEqual(rec["status"], "close_reconcile")
        state = self.index(engine)["close_reconcile"][THREAD]
        self.assertEqual(state["consecutive_ok"], 0)
        self.assertIn("binding", state["blocked_reason"])

        # The supervisor clears the binding; the next two rounds close it.
        self.ledger_write({})
        engine.run_once()
        self.assertEqual(self.index(engine)["assignments"]["asg_1_worker_1"]
                         ["status"], "close_reconcile")
        engine.run_once()
        self.assertEqual(self.index(engine)["assignments"]["asg_1_worker_1"]
                         ["status"], "completed")


class Counterfact3(Base):
    """binding cleanup failed: close_reconcile, never completed."""

    def test_a_registered_binding_holds_the_thread(self):
        self.ledger_write({THREAD: {"qa": {"session": "session_q1", "name": "qa-1"}}})
        engine = self.engine()
        self.deliver_and_pass(engine)
        for _ in range(3):
            engine.run_once()
        rec = self.index(engine)["assignments"]["asg_1_worker_1"]
        self.assertEqual(rec["status"], "close_reconcile")
        self.assertNotEqual(rec["status"], "completed")
        state = self.index(engine)["close_reconcile"][THREAD]
        self.assertFalse(state["conditions"]["binding_cleared"])
        self.assertGreaterEqual(state["attempts"], 2)

    def test_an_unarchived_sid_blocks_completion(self):
        class NoArchive(FakeSessions):
            def archive_session(self, sid):
                self.archive_calls.append(sid)
                return False, {"err": "refused"}

        engine = self.engine()
        object.__setattr__(engine, "sessions", NoArchive())
        engine.session_api = engine.sessions
        self.source.add(9001, "[WK:wk-probe-1] done")
        self.source.add(9100, "[QA] **通过**", author="qa")
        engine.run_once()
        engine.run_once()
        rec = self.index(engine)["assignments"]["asg_1_worker_1"]
        self.assertEqual(rec["status"], "blocked")
        self.assertNotEqual(rec["status"], "completed")

    def test_an_open_issue_blocks_completion(self):
        self.issue_state = "open"
        engine = self.engine()
        self.deliver_and_pass(engine)
        engine.run_once()
        engine.run_once()
        rec = self.index(engine)["assignments"]["asg_1_worker_1"]
        self.assertEqual(rec["status"], "close_reconcile")
        self.assertEqual(self.index(engine)["close_reconcile"][THREAD]
                         ["detail"]["issue_state"], "open")

    def test_running_tasks_block_completion(self):
        self.tasks = {"session_w1": ["task-1"]}
        engine = self.engine()
        self.deliver_and_pass(engine)
        engine.run_once()
        engine.run_once()
        rec = self.index(engine)["assignments"]["asg_1_worker_1"]
        self.assertEqual(rec["status"], "close_reconcile")
        self.assertEqual(self.index(engine)["close_reconcile"][THREAD]
                         ["detail"]["running_tasks"], ["task-1"])


class Counterfact5(Base):
    """a late event on a closed thread: audit only, no restart."""

    def test_a_late_delivery_after_completion_changes_nothing(self):
        engine = self.engine()
        self.deliver_and_pass(engine)
        engine.run_once()
        self.assertEqual(self.index(engine)["assignments"]["asg_1_worker_1"]
                         ["status"], "completed")
        self.source.add(9500, "[WK:wk-probe-1] reworked it anyway")
        engine.run_once()
        rec = self.index(engine)["assignments"]["asg_1_worker_1"]
        self.assertEqual(rec["status"], "completed")
        self.assertEqual(rec["delivery_comment_id"], 9001,
                         "a late delivery never replaces the recorded one")
        kinds = [r["type"] for r in self.rows()]
        self.assertNotIn("assignment.waiting_review", kinds[-3:])


class ScanBounds(Base):
    def test_only_held_states_are_checked_and_the_cap_applies(self):
        engine = self.engine()
        self.deliver_and_pass(engine)
        index = self.index(engine)
        # Nothing to check while the worker is still working.
        index["assignments"]["asg_1_worker_1"]["status"] = "working"
        engine.store.save_index(index)
        out = engine._round_closed_threads(engine.store.load_index())
        self.assertEqual(out, [])

        index = self.index(engine)
        for n in range(2, 6):
            thread = f"owner/demo#{n}"
            index["assignments"][f"asg_{n}_worker_1"] = {
                "id": f"asg_{n}_worker_1", "thread": thread, "issue": n,
                "role": "worker", "status": "waiting_release",
                "sid": f"session_w{n}", "qa_verdict": "pass"}
            self.sessions.archived.add(f"session_w{n}")
        index["assignments"]["asg_1_worker_1"]["status"] = "waiting_release"
        engine.cfg["engine"]["max_closed_thread_checks"] = 2
        out = engine._round_closed_threads(index)
        self.assertEqual(len(out), 2)
        self.assertEqual([r["thread"] for r in out], sorted(
            [r["thread"] for r in out]))

    def test_the_cap_is_config_validated(self):
        with self.assertRaises(config.ConfigError):
            config.load(None, {"engine": {"max_closed_thread_checks": 0}})
        with self.assertRaises(config.ConfigError):
            config.load(None, {"engine": {"max_closed_thread_checks": "ten"}})
        cfg = config.load(None, {"engine": {"max_closed_thread_checks": 3}})
        self.assertEqual(cfg["engine"]["max_closed_thread_checks"], 3)
        # Registered in DEFAULTS as well, so the key is never silently absent.
        self.assertEqual(config.DEFAULTS["engine"]["max_closed_thread_checks"], 10)


if __name__ == "__main__":
    unittest.main()
