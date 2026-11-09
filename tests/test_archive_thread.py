"""Archive, read-back, bind reconcile and the archived-sid dispatch guard.

The properties pinned here:

* ``archive_thread`` is idempotent (one attempt per delivery, ever);
* ``completed`` is only written after the read-back says ``archived is True`` -
  a failed read-back leaves ``blocked``, never a fake success;
* ``_dispatch`` refuses a payload aimed at an archived sid, without counting an
  external action and without writing an attempt;
* ``reconcile_close`` writes ``thread.reconciled`` and reports
  ``server_supplied=false`` as not-ok (UNKNOWN, not OK).

Run: ``python -m pytest tests/test_archive_thread.py -q``
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from super_agent import config, session_events
from super_agent.allowlist import ProjectAllowlist
from super_agent.engine import Engine
from super_agent.store import Store, action_id

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
    def __init__(self, *, fail=False):
        self.injected = []
        self.fail = fail

    def healthz(self):
        return {"ok": True, "ready": True, "connected": True, "seq": 1}

    def inject(self, event_type, payload):
        if self.fail:
            raise RuntimeError("bus down")
        self.injected.append((event_type, payload))
        return {"ok": True, "seq": len(self.injected)}


class FakeSessions:
    """Stands in for ``super_agent.api``: archive + read-back."""

    def __init__(self, *, archive_ok=True, archived_after=True):
        self.archive_ok = archive_ok
        self.archived_after = archived_after
        self.archived = set()
        self.archive_calls = []

    def archive_session(self, sid):
        self.archive_calls.append(sid)
        if self.archive_ok and self.archived_after:
            self.archived.add(sid)
        return self.archive_ok, {"ids": [sid]}

    def session_info(self, sid):
        return {"archived": sid in self.archived}


class Comments:
    def __init__(self, rows=None):
        self.rows = list(rows or [])

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
        self.allow = self.root / "allowlist.json"
        self.allow.write_text(json.dumps(GOOD), encoding="utf-8")
        self.source = Comments()
        self.bus = FakeBus()
        self.sessions = FakeSessions()

    def tearDown(self):
        self.tmp.cleanup()

    def bind(self, sid="session_w1", *, name="wk-probe-1", archive=False):
        rows = [
            {"seq": 1, "at": "2026-11-09T10:00:00+00:00",
             "type": session_events.CREATED, "sid": sid, "key": name},
            {"seq": 2, "at": "2026-11-09T10:00:01+00:00",
             "type": session_events.BOUND, "sid": sid, "key": THREAD,
             "role": "worker", "issue": 1},
        ]
        if archive:
            rows.append({"seq": 3, "at": "2026-11-09T10:00:02+00:00",
                         "type": session_events.ARCHIVED, "sid": sid})
        self.events.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                               encoding="utf-8")

    def engine(self) -> Engine:
        cfg = config.load(None, {
            "mode": "assist",
            "github": {"source": "none", "repo": "owner/demo"},
            "engine": {"session_events_path": str(self.events),
                       "allowlist": ["issue_comment.created"]},
        })
        return Engine(cfg, Store(self.state), self.source, self.bus,
                      project_allowlist=ProjectAllowlist(self.allow),
                      session_api=self.sessions)

    def review(self, engine, cid=9001):
        self.bind()
        self.source.add(cid, "[WK:wk-probe-1] done")
        engine.run_once()

    def events_rows(self):
        path = self.state / "events.jsonl"
        if not path.is_file():
            return []
        return [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]


class Archive(Base):
    def test_archive_moves_to_completed_after_read_back(self):
        engine = self.engine()
        self.review(engine)
        rec = engine.store.load_index()["assignments"]["asg_1_worker_1"]
        self.assertEqual(rec["status"], "completed")
        self.assertEqual(rec["tombstone"]["archived"], True)
        self.assertEqual(rec["tombstone"]["read_back"], True)
        self.assertEqual(self.sessions.archive_calls, ["session_w1"])

    def test_archive_is_idempotent(self):
        engine = self.engine()
        self.review(engine)
        engine.archive_thread(THREAD)
        engine.archive_thread(THREAD)
        self.assertEqual(self.sessions.archive_calls, ["session_w1"])

    def test_failed_read_back_blocks_and_never_pretends_done(self):
        self.sessions = FakeSessions(archive_ok=True, archived_after=False)
        engine = self.engine()
        self.review(engine)
        rec = engine.store.load_index()["assignments"]["asg_1_worker_1"]
        self.assertEqual(rec["status"], "blocked")
        self.assertIn("read-back", rec["tombstone"]["reason"])
        base = action_id("asg_1_worker_1", "archive", "9001")
        self.assertEqual(engine.store.load_outbox_state()["actions"][base]["status"],
                         "uncertain")

    def test_archive_failure_is_blocked(self):
        self.sessions = FakeSessions(archive_ok=False)
        engine = self.engine()
        self.review(engine)
        rec = engine.store.load_index()["assignments"]["asg_1_worker_1"]
        self.assertEqual(rec["status"], "blocked")

    def test_archive_attempt_is_pruned_but_the_notification_is_kept(self):
        engine = self.engine()
        self.review(engine)
        state = engine.store.load_outbox_state()
        leftovers = sorted(k for k in state["attempts"]
                           if k.startswith("asg_1_worker_1:"))
        # The archive counter is terminal and pruned; the notification counter
        # must survive (a late retry would otherwise re-send it).
        self.assertEqual(leftovers, ["asg_1_worker_1:bus_inject:9001"])


class ArchivedSidGuard(Base):
    def test_dispatch_refuses_an_archived_sid(self):
        self.bind(archive=True)
        engine = self.engine()
        out = engine._dispatch({"kind": "bus_inject",
                                "event": "event.agent.waiting_review",
                                "issue": 1, "input_ref": "1",
                                "payload": {"sid": "session_w1"}})
        self.assertFalse(out["dispatched"])
        self.assertTrue(out["refused"])
        self.assertEqual(self.bus.injected, [])
        self.assertEqual(engine.store.load_counters()["external_actions"], 0)
        self.assertEqual([e["type"] for e in self.events_rows()].count(
            "dispatch.archived_sid"), 1)

    def test_unreadable_stream_fails_closed(self):
        self.events.unlink(missing_ok=True)
        engine = self.engine()
        out = engine._dispatch({"kind": "bus_inject", "event": "x",
                                "issue": 1, "input_ref": "1",
                                "payload": {"sid": "session_w1"}})
        self.assertTrue(out["refused"])

    def test_live_sid_is_not_refused(self):
        self.bind()
        engine = self.engine()
        out = engine._dispatch({"kind": "bus_inject", "event": "x",
                                "issue": 1, "input_ref": "2",
                                "payload": {"sid": "session_w1"}})
        self.assertTrue(out["dispatched"])
        self.assertEqual(len(self.bus.injected), 1)


class ReconcileClose(Base):
    def test_close_writes_thread_reconciled(self):
        engine = self.engine()
        self.review(engine)
        out = engine.reconcile_close(THREAD)
        self.assertFalse(out["server_supplied"], "no live side was consulted")
        self.assertFalse(out["ok"], "UNKNOWN is never OK")
        rows = [e for e in self.events_rows() if e["type"] == "thread.reconciled"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["server_supplied"], False)

    def test_close_refuses_while_an_assignment_is_open(self):
        self.bind()
        engine = self.engine()
        engine.run_once()
        out = engine.reconcile_close(THREAD)
        self.assertFalse(out["ok"])
        self.assertEqual(out["unfinished"], ["asg_1_worker_1"])


if __name__ == "__main__":
    unittest.main()
