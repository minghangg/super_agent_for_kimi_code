"""#ISS-85: a ``[PM:TASK]`` first line is a dispatch, and it stays quiet.

The failure these pin: a task book carrying ``[PM:TASK]`` reached the verdict
branch as ``state == ""`` - "the PM answered and the answer was unreadable" -
and filed an exception for four task books in one day (#82 cid 6894664944,
#84 cid 6896920416). The content was right; only the prefix was never read.

Two halves, and they are opposed on purpose:

* the prefix declares "this is a dispatch, not a state", so the answer is
  ``ignored`` - no frame, no row, no unreadable verdict;
* a task book whose own line *answers* (``[PM:TASK] 可关单 #42``) is still
  ignored, but it leaves exactly one ``warn`` event. A real answer written
  with the wrong prefix must be visible to somebody, and the doctor is the
  somebody - not the exception bell that #75 removed.

A ``[PM]`` line without the prefix keeps answering exactly as before: the fix
for #75 must not be loosened by this one.

Run: ``python -m pytest tests/test_iss85_pm_task.py -q``
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from super_agent import config, markers, session_events
from super_agent.allowlist import ProjectAllowlist
from super_agent.engine import Engine, TASK_PREFIX_RE
from super_agent.store import Store

THREAD = "owner/demo#1"

# The first line of the #84 task book, verbatim - the shape that filed the last
# unreadable exception. Its tail contains ``已关单的 route``, so the guard reads
# it as a conclusion and leaves the one warn: that is the designed answer, not
# an accident. Silence about it would hide a real answer written under the
# wrong prefix.
TASK_BOOK = "[PM:TASK] ISS-84 实现单：已关单的 route 必须终止重试"

# A task book whose own line answers nothing at all - what most dispatch lines
# look like - so the guard has nothing to say about it.
QUIET_BOOK = "[PM:TASK] ISS-85 实现单：加前缀与防呆（方案豁免）"

GOOD = {
    "schema_version": 1,
    "defaults": {"mode": "observe", "creation_mode": "pm_created"},
    "projects": {
        "demo": {"repo": "owner/demo", "cwd": ".", "mode": "assist",
                 "creation_mode": "pm_created", "issues": [1],
                 "actions": ["bus_inject", "collect", "reconcile", "archive"],
                 "max_active_writers": 1, "roles": ["worker", "qa", "pm"]},
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

    def payloads(self, event_type):
        return [p for e, p in self.injected if e == event_type]


class FakeSessions:
    def archive_session(self, sid):
        return True, {"ids": [sid]}

    def session_info(self, sid):
        return {"archived": False}


class Comments:
    def __init__(self):
        self.rows = []

    def add(self, cid, body, author="pm"):
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

    def issue_state(self, issue):
        return "open"


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
        self.events.write_text("\n".join(json.dumps(r) for r in [
            {"seq": 1, "at": "2026-11-10T10:00:00+00:00",
             "type": session_events.CREATED, "sid": "session_pm",
             "key": "pm-demo"},
            {"seq": 2, "at": "2026-11-10T10:00:01+00:00",
             "type": session_events.BOUND, "sid": "session_pm", "key": THREAD,
             "role": "pm", "issue": 1},
        ]) + "\n", encoding="utf-8")
        (self.events.parent / "sessions.json").write_text(
            json.dumps({"supervisor": {"id": "session_sup"},
                        "pm-demo": {"id": "session_pm"}}), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def engine(self):
        cfg = config.load(None, {
            "mode": "assist", "role": "supervisor",
            "github": {"source": "none", "repo": "owner/demo"},
            "engine": {"session_events_path": str(self.events),
                       "allowlist": ["issue_comment.created"]},
        })
        engine = Engine(cfg, Store(self.state), self.source, self.bus,
                        project_allowlist=ProjectAllowlist(self.allow),
                        session_api=FakeSessions())
        engine._round_closed_threads = lambda index: []
        return engine

    def rows(self, engine):
        rec = (engine.store.load_index().get("issues") or {}).get(THREAD) or {}
        return rec.get("needs_person") or []

    def logged(self, type_name):
        path = self.state / "events.jsonl"
        if not path.is_file():
            return []
        return [e for e in (json.loads(x) for x in
                            path.read_text("utf-8").splitlines() if x.strip())
                if e.get("type") == type_name]

    def frames(self):
        return (self.bus.payloads("event.agent.supervisor")
                + self.bus.payloads("event.agent.needs_person")
                + self.bus.payloads("event.agent.project_pm"))


class Prefix(unittest.TestCase):
    """The matcher itself: every spelling a hand-written prefix takes."""

    def test_the_ascii_forms(self):
        for line in ("[PM:TASK] x", "[PM:task] x", "[PM:Task] x",
                     "[supervisor:TASK] x", "[SUPERVISOR:task] x"):
            self.assertTrue(TASK_PREFIX_RE.match(line), line)

    def test_the_full_width_forms(self):
        for line in ("［PM：TASK］ x", "［PM:TASK］ x", "[PM：TASK] x",
                     "［PM：task］ x"):
            self.assertTrue(TASK_PREFIX_RE.match(line), line)

    def test_whitespace_around_every_part(self):
        for line in ("  [PM:TASK] x", "[ PM : TASK ] x", "[PM:  TASK] x"):
            self.assertTrue(TASK_PREFIX_RE.match(line), line)

    def test_a_verdict_prefix_is_not_a_task_book(self):
        for line in ("[PM] x", "[PM] 可关单 #42", "[supervisor] x"):
            self.assertIsNone(TASK_PREFIX_RE.match(line), line)

    def test_the_verdict_parser_still_ignores_the_task_prefix(self):
        # The reason the engine had to learn this at all.
        self.assertIsNone(markers.parse_pm_verdict(TASK_BOOK))
        self.assertIsNone(markers.PM_PREFIX_RE.match("[PM:TASK] xxx"))


class TaskBookIsIgnored(Base):
    """Counter-examples 1 and 5: the four task books, and their spellings."""

    def test_a_task_book_is_ignored_with_no_frame(self):
        # Counter-example 1: the #84 first line. Its tail reads as a conclusion
        # (``已关单的 route``), so the guard leaves one warn - and still no
        # frame and no row. Being told is not being woken.
        engine = self.engine()
        self.source.add(6896920416, TASK_BOOK)
        engine.run_once()
        self.assertEqual(self.frames(), [])
        self.assertEqual(self.rows(engine), [])
        self.assertEqual(len(self.logged("pm.task_book_conclusion")), 1)

    def test_a_quiet_task_book_leaves_no_warn(self):
        engine = self.engine()
        self.source.add(9100, QUIET_BOOK)
        engine.run_once()
        self.assertEqual(self.frames(), [])
        self.assertEqual(self.rows(engine), [])
        self.assertEqual(self.logged("pm.task_book_conclusion"), [])

    def test_the_state_and_the_reason(self):
        engine = self.engine()
        state, reason = engine._triage({"id": 1, "author": "pm",
                                        "body": TASK_BOOK})
        self.assertEqual(state, "ignored")
        self.assertEqual(reason, "pm task book")

    def test_lowercase_and_full_width_are_recognised(self):
        for body in ("[PM:task] ISS-85 实现单：加前缀与防呆",
                     "［PM：TASK］ ISS-85 实现单：加前缀与防呆"):
            with self.subTest(body=body):
                self.setUp()
                try:
                    engine = self.engine()
                    self.source.add(9100, body)
                    engine.run_once()
                    self.assertEqual(self.frames(), [], body)
                    self.assertEqual(self.rows(engine), [], body)
                    # No conclusion word here, so the guard stays silent too.
                    self.assertEqual(self.logged("pm.task_book_conclusion"), [],
                                     body)
                finally:
                    self.tearDown()

    def test_the_supervisor_spelling_is_recognised(self):
        engine = self.engine()
        self.source.add(9100, "[supervisor:TASK] ISS-85 实现单：加前缀与防呆")
        engine.run_once()
        self.assertEqual(self.frames(), [])
        self.assertEqual(self.rows(engine), [])
        self.assertEqual(self.logged("pm.task_book_conclusion"), [])

    def test_a_body_mentioning_a_conclusion_is_still_silent(self):
        # The first-line rule every other parser follows: a body that explains
        # ``可关单`` is documentation, not an answer, so the guard says nothing.
        engine = self.engine()
        self.source.add(9100, QUIET_BOOK + "\n\n完工条件：本单可关单时按 verdict 契约报。")
        engine.run_once()
        self.assertEqual(self.frames(), [])
        self.assertEqual(self.logged("pm.task_book_conclusion"), [])


class ConclusionGuard(Base):
    """Counter-example 3: the one warn, and no frame beside it."""

    def test_a_conclusion_after_the_prefix_logs_exactly_one_warn(self):
        engine = self.engine()
        self.source.add(9100, "[PM:TASK] 可关单 #42")
        for _ in range(3):
            engine.run_once()
        events = self.logged("pm.task_book_conclusion")
        self.assertEqual(len(events), 1, events)
        self.assertEqual(events[0]["comment_id"], 9100)
        self.assertEqual(events[0]["level"], "warn")
        self.assertEqual(events[0]["consumers"], ["doctor"])
        self.assertIn("use [PM] + verdict marker", events[0]["reason"])
        self.assertEqual(self.frames(), [])
        self.assertEqual(self.rows(engine), [])

    def test_the_warn_does_not_become_a_verdict(self):
        # The refusal that matters: a conclusion word inside a task book must
        # never change state. Only the prefix decides, and the prefix says
        # "not a state".
        engine = self.engine()
        state, reason = engine._triage({"id": 1, "author": "pm",
                                        "body": "[PM:TASK] 可关单 #42"})
        self.assertEqual(state, "ignored")
        self.assertEqual(reason, "pm task book")


class VerdictStillWorks(Base):
    """Counter-examples 2 and 4: the two regressions this must not cause."""

    def test_a_real_verdict_is_read_as_before(self):
        # Counter-example 2. The parse is checked at both levels: the coordinate
        # named in the marker, and the release frame the coordinate produces.
        out = markers.parse_pm_verdict(
            "[PM] 可关单 #42 <!-- verdict: ready delivery: 123 -->")
        self.assertEqual(out["state"], "ready")
        self.assertEqual(out["delivery"], 123)
        engine = self.engine()
        self.source.add(9100, "[PM] 可关单 #1 <!-- verdict: ready -->")
        engine.run_once()
        payloads = self.bus.payloads("event.agent.supervisor")
        self.assertEqual(len(payloads), 1)
        self.assertFalse(payloads[0]["exception"])

    def test_a_task_book_with_the_old_prefix_is_still_not_a_verdict(self):
        # #75's fix stays exactly as it was: an unmarked ``[PM]`` note is
        # ignored with its own reason, not read as a task book.
        engine = self.engine()
        state, reason = engine._triage({"id": 1, "author": "pm",
                                        "body": "[PM] 任务书：修两条 P0"})
        self.assertEqual(state, "ignored")
        self.assertEqual(reason, "pm comment is not a verdict")

    def test_a_task_book_does_not_silence_a_later_verdict(self):
        engine = self.engine()
        self.source.add(9100, QUIET_BOOK)
        self.source.add(9200, "[PM] 可关单 #1 <!-- verdict: ready -->")
        engine.run_once()
        payloads = self.bus.payloads("event.agent.supervisor")
        self.assertEqual(len(payloads), 1)
        self.assertFalse(payloads[0]["exception"])
        self.assertEqual(self.logged("pm.task_book_conclusion"), [])


if __name__ == "__main__":
    unittest.main()
