"""ISS-90: a rotation record derives what the stream knows and asks for the rest.

The properties pinned here, and why each is a counter-example rather than a
happy path:

* **nine fields are derived, not typed.** A hand-typed `old_ctx` that disagrees
  with the ledger makes the record a second, worse source of truth. The test
  builds a stream and asserts the record matches it;
* **four fields are refused when absent.** `trigger_reason=""` would read as
  "unknown cause", which is a different statement from "nobody said" -- and the
  record exists to answer exactly that;
* **`trigger_reason` and `ordered_by` cannot be merged.** "Automatic because ctx
  hit the line" and "the user ordered it" are different facts. They are separate
  parameters, so merging them is a type error, not a discipline problem;
* **`archived=true` is not proof of "no more feeding".** A session can be
  archived and still receive prompts; the check asks the daemon.

Run: ``python -m pytest tests/test_rotation.py -q``
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from super_agent import rotation, session_events


def _event(seq, etype, **kw):
    return {"seq": seq, "type": etype, "at": kw.pop("at", "2026-11-20T10:00:00+08:00"), **kw}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, events, ledger=None, sessions=None):
        (self.root / "events.jsonl").write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
            encoding="utf-8")
        if ledger is not None:
            (self.root / "ledger.jsonl").write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in ledger) + "\n",
                encoding="utf-8")
        if sessions is not None:
            (self.root / "sessions.json").write_text(
                json.dumps(sessions, ensure_ascii=False), encoding="utf-8")

    def standard(self, *, archived=True, key="scout-3f7c1a90"):
        old, new = "session_old-0000", "session_new-1111"
        events = [
            _event(1, session_events.CREATED, sid=new, key=key, at="2026-11-20T10:00:00+08:00"),
            _event(2, session_events.KEY_CHANGED, key=key, sid=new, from_sid=old,
                   role="scout", at="2026-11-20T10:00:01+08:00"),
            _event(3, session_events.ROTATED, key=key, role="scout", sid=new,
                   from_sid=old, to_sid=new, at="2026-11-20T10:00:02+08:00"),
        ]
        if archived:
            events.append(_event(4, session_events.ARCHIVED, sid=old,
                                 at="2026-11-20T10:00:03+08:00"))
        ledger = [
            {"t": "2026-11-20T09:55:00", "act": "ctx-gate", "note": f"ctx=62.5% verdict=allow key={key}"},
            {"t": "2026-11-20T10:05:00", "act": "ctx-gate", "note": f"ctx=2.1% verdict=allow key={key}"},
        ]
        self.write(events, ledger, {key: {"id": new}})
        return key, old, new


class Derive(Fixture):
    def test_the_nine_fields_come_from_the_stream_and_ledger(self):
        key, old, new = self.standard()
        rec = rotation.collect(key, state_root=self.root)
        self.assertEqual(rec["role"], "scout")
        self.assertEqual(rec["old_session"], old)
        self.assertEqual(rec["new_session"], new)
        self.assertTrue(rec["old_session_archived"])
        self.assertEqual(rec["old_ctx"], "62.5%")
        self.assertEqual(rec["new_ctx"], "2.1%")
        self.assertEqual(rec["new_session_created_by"], "dispatch")
        self.assertTrue(rec["rotation_id"].startswith(key))

    def test_an_unarchived_old_session_is_reported_as_false(self):
        key, _, _ = self.standard(archived=False)
        rec = rotation.collect(key, state_root=self.root)
        self.assertFalse(rec["old_session_archived"])

    def test_no_rotation_in_the_stream_raises_instead_of_returning_empty(self):
        self.write([_event(1, session_events.CREATED, sid="s1", key="scout-x")])
        with self.assertRaises(rotation.RotationIncomplete):
            rotation.collect("scout-x", state_root=self.root)

    def test_a_missing_ctx_reading_stays_empty_not_zero(self):
        # "unknown" and "0%" are different claims; a fabricated 0% would read as
        # a rotation that gained nothing.
        key, _, _ = self.standard()
        (self.root / "ledger.jsonl").unlink()
        rec = rotation.collect(key, state_root=self.root)
        self.assertEqual(rec["old_ctx"], "")
        self.assertEqual(rec["new_ctx"], "")


class Person(unittest.TestCase):
    """The four fields only a person can supply."""

    def setUp(self):
        self.fx = Fixture("run")
        self.fx.setUp()
        self.key, _, _ = self.fx.standard()
        self.addCleanup(self.fx.doCleanups)

    def test_a_complete_record_has_all_four(self):
        rec = rotation.compose(self.key, state_root=self.fx.root,
                               trigger_reason="ctx_threshold",
                               ordered_by="supervisor",
                               handoff_doc="state/asst_handoff/x.md")
        self.assertEqual(rec["trigger_reason"], "ctx_threshold")
        self.assertEqual(rec["ordered_by"], "supervisor")
        self.assertEqual(rec["handoff_doc"], "state/asst_handoff/x.md")
        self.assertIsNone(rec["model_change"])

    def test_a_missing_field_is_refused_not_defaulted(self):
        for kwargs, missing in (
                (dict(trigger_reason="", ordered_by="supervisor", handoff_doc="x"),
                 "trigger_reason"),
                (dict(trigger_reason="ctx_threshold", ordered_by="", handoff_doc="x"),
                 "ordered_by"),
                (dict(trigger_reason="ctx_threshold", ordered_by="supervisor",
                      handoff_doc="  "), "handoff_doc")):
            with self.subTest(missing=missing):
                with self.assertRaises(rotation.RotationIncomplete) as ctx:
                    rotation.compose(self.key, state_root=self.fx.root, **kwargs)
                self.assertIn(missing, ctx.exception.missing)

    def test_an_unknown_trigger_reason_is_refused(self):
        with self.assertRaises(rotation.RotationIncomplete):
            rotation.compose(self.key, state_root=self.fx.root,
                             trigger_reason="随便", ordered_by="supervisor",
                             handoff_doc="x")

    def test_trigger_reason_and_ordered_by_are_separate(self):
        # The whole point: one column cannot hold both facts.
        rec = rotation.compose(self.key, state_root=self.fx.root,
                               trigger_reason="user_order", ordered_by="user",
                               handoff_doc="x")
        self.assertEqual(rec["trigger_reason"], "user_order")
        self.assertEqual(rec["ordered_by"], "user")

    def test_a_model_change_is_recorded_when_given(self):
        rec = rotation.compose(self.key, state_root=self.fx.root,
                               trigger_reason="model_change", ordered_by="user",
                               handoff_doc="x",
                               model_change={"from": "a", "to": "b"})
        self.assertEqual(rec["model_change"], {"from": "a", "to": "b"})


class SelfCheck(Fixture):
    def test_a_clean_rotation_passes(self):
        key, _, _ = self.standard(key="scout-3f7c1a90")
        self.assertEqual(rotation.self_check(key, state_root=self.root), [])

    def test_a_bare_key_has_no_random_tail(self):
        key, _, _ = self.standard(key="scout")
        problems = rotation.self_check(key, state_root=self.root)
        self.assertTrue(any("随机尾" in p for p in problems), problems)

    def test_a_date_tail_is_not_a_random_tail(self):
        key, _, _ = self.standard(key="scout-20261116")
        problems = rotation.self_check(key, state_root=self.root)
        self.assertTrue(any("随机尾" in p for p in problems), problems)

    def test_an_unarchived_old_session_fails_the_check(self):
        key, _, _ = self.standard(archived=False)
        problems = rotation.self_check(key, state_root=self.root)
        self.assertTrue(any("未归档" in p for p in problems), problems)

    def test_the_old_key_still_pointing_at_the_old_session_fails(self):
        key, old, _ = self.standard()
        (self.root / "sessions.json").write_text(
            json.dumps({key: {"id": old}}), encoding="utf-8")
        problems = rotation.self_check(key, state_root=self.root)
        self.assertTrue(any("旧键仍在" in p for p in problems), problems)


class Render(unittest.TestCase):
    def test_the_body_carries_both_a_summary_and_the_json(self):
        rec = {
            "rotation_id": "scout-20261120-1000", "role": "scout",
            "old_session": "session_old", "old_session_archived": True,
            "new_session": "session_new", "new_session_created_by": "dispatch",
            "old_ctx": "62.5%", "new_ctx": "2.1%",
            "trigger_reason": "ctx_threshold", "ordered_by": "supervisor",
            "ordered_at": "2026-11-20T10:00:00+08:00",
            "handoff_doc": "x.md", "model_change": None, "schema_version": 1,
        }
        body = rotation.render_markdown(rec)
        self.assertIn("换血记录 scout-20261120-1000", body)
        self.assertIn("62.5% → 2.1%", body)
        self.assertIn("```json", body)
        self.assertIn('"trigger_reason": "ctx_threshold"', body)


class JournalLookup(unittest.TestCase):
    """The zero-feed check asks the daemon, never a workbench file."""

    def test_an_unreachable_daemon_yields_no_records(self):
        # "could not ask" and "nothing found" collapse to [] here on purpose:
        # self_check only calls this with an explicit `since`, and a caller that
        # needs the distinction passes a port it knows is live.
        self.assertEqual(
            rotation.journal_sends("session_x", since=1.0, port=1), [])

    def test_the_query_targets_the_journal_endpoint(self):
        # A path change here would silently point the check at a file that does
        # not exist on another machine.
        import inspect
        src = inspect.getsource(rotation.journal_sends)
        self.assertIn("/journal", src)
        self.assertIn("sid", src)
        self.assertIn("verb", src)


class NoWorkbenchDependency(unittest.TestCase):
    def test_rotation_never_names_a_private_path(self):
        # The module must be usable on another machine: a check that reads a
        # workbench file cannot answer "was this session fed" there.
        src = Path(rotation.__file__).read_text(encoding="utf-8")
        for needle in ("Laike_work_with_agent", "tools/sess.py", "tools/ctx.py"):
            self.assertNotIn(needle, src)


if __name__ == "__main__":
    unittest.main()
