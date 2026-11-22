"""L1 tests for ISS-103 group 1: the five P0 stop-the-bleeding fixes.

Every test here is offline: `http_get`/`http_post` are patched, nothing talks to a
server and no session is created.

What must be true after the fix (2.7 dropped `busy` / `main_turn_active` /
`last_turn_reason` from the session object, so every old judge read `None`):

* 1.1 the end judge is `:wait`'s `timed_out is False` **and** `activity != "turn"`,
  never a `busy` read; an interrupted turn is a normal finish, not an error;
* P0-5 the round fence accepts **only** `ordinal > prev_ordinal` — a turn list
  that pins `ordinal=6, status=running` must raise instead of returning the
  previous round's answer;
* 1.3 `status()` derives `busy`/`main_turn_active` from `activity` and leaves both
  `None` when `activity` is missing (no `None` masquerading as `False`);
* 1.4 a pending interaction is answered **inside** the wait loop, and
  `permission-mode=manual` answers `rejected` (fail closed, no `NameError`);
* 1.5 cancellation goes through `:cancel` and judges `data.cancelled`.

Run: ``python -m pytest tests/test_iss103_p0.py -q``
"""
from __future__ import annotations

import unittest
from unittest import mock

from super_agent import decide_watch, direct, dispatch_cli as D

SID = "session_00000000-0000-4000-8000-000000000000"


class RoundFence(unittest.TestCase):
    """P0-5: the fence must not have a `status == "running"` bypass."""

    def test_a_pinned_turn_list_must_raise(self) -> None:
        """Mock `turns` returns ordinal=6 forever ⇒ `await_turn_start(prev=6)` raises.

        The old judge (which also accepted `status == "running"`) returns here and
        the caller then reports the **previous** round's answer as if it were new.
        """
        turns = {"turns": [{"ordinal": 6, "status": "running"}]}
        with mock.patch.object(D, "http_get", lambda *a, **k: {"code": 0, "data": turns}):
            with self.assertRaises(D.DispatchError) as ctx:
                D.await_turn_start("http://x", "t", SID, prev_ordinal=6, deadline_s=0.05)
        self.assertIn("round did not start", str(ctx.exception))

    def test_a_new_ordinal_is_accepted(self) -> None:
        turns = {"turns": [{"ordinal": 7, "status": "running"}]}
        with mock.patch.object(D, "http_get", lambda *a, **k: {"code": 0, "data": turns}):
            self.assertEqual(D.await_turn_start("http://x", "t", SID, 6, deadline_s=1), 7)

    def test_first_turn_zero_is_accepted(self) -> None:
        turns = {"turns": [{"ordinal": 0, "status": "ended"}]}
        with mock.patch.object(D, "http_get", lambda *a, **k: {"code": 0, "data": turns}):
            self.assertEqual(D.await_turn_start("http://x", "t", SID, -1, deadline_s=1), 0)

    def test_current_ordinal_preserves_zero(self) -> None:
        turns = {"turns": [{"ordinal": 0, "status": "ended"}]}
        with mock.patch.object(D, "http_get", lambda *a, **k: {"code": 0, "data": turns}):
            self.assertEqual(D.current_ordinal("http://x", "t", SID), 0)

    def test_missing_ordinal_still_means_no_turn(self) -> None:
        turns = {"turns": [{"status": "running"}]}
        with mock.patch.object(D, "http_get", lambda *a, **k: {"code": 0, "data": turns}):
            self.assertEqual(D.current_ordinal("http://x", "t", SID), -1)
            with self.assertRaises(D.DispatchError):
                D.await_turn_start("http://x", "t", SID, -1, deadline_s=0.01)

    def test_source_has_no_running_status_bypass(self) -> None:
        """Freeze the rule, not just today's behaviour: no `status == "running"` judge."""
        import inspect
        src = inspect.getsource(D.await_turn_start)
        self.assertNotIn('status") == "running"', src)
        self.assertIn("ordinal", src)


class EndJudge(unittest.TestCase):
    """1.1: `:wait` decides, `busy` is never read, interrupted is a normal finish."""

    def _wait(self, data):
        return mock.patch.object(D, "http_post", lambda *a, **k: {"code": 0, "data": data})

    def test_idle_turn_timeout_false_finishes(self) -> None:
        data = {"timed_out": False, "activity": "idle",
                "session": {"activity": "idle", "last_turn_outcome": "completed"}}
        with self._wait(data):
            res = D.wait_rounds("http://x", "t", SID, timeout=5)
        self.assertFalse(res["timed_out"])
        self.assertEqual(res["outcome"], "completed")

    def test_background_does_not_block_the_finish(self) -> None:
        """`activity != "idle"` would spin here forever; the judge is `!= "turn"`."""
        data = {"timed_out": False,
                "session": {"activity": "background", "last_turn_outcome": "completed"}}
        with self._wait(data):
            res = D.wait_rounds("http://x", "t", SID, timeout=5)
        self.assertEqual(res["activity"], "background")

    def test_still_in_turn_times_out(self) -> None:
        data = {"timed_out": True, "session": {"activity": "turn"}}
        with self._wait(data):
            with self.assertRaises(D.DispatchError) as ctx:
                D.wait_rounds("http://x", "t", SID, timeout=0.02)
        self.assertIn("timed out", str(ctx.exception))

    def test_interrupted_is_not_an_error(self) -> None:
        data = {"timed_out": False,
                "session": {"activity": "idle", "last_turn_outcome": "interrupted"}}
        turns = {"turns": [{"ordinal": 7, "status": "ended",
                            "items": [{"type": "assistant_message", "text": "ok"}]}]}
        with self._wait(data), \
                mock.patch.object(D, "current_ordinal", lambda *a: 6), \
                mock.patch.object(D, "http_get",
                                  lambda *a, **k: {"code": 0, "data": turns}):
            answer = D.run_turn("http://x", "t", SID, "hi", timeout=5, quiet=True)
        self.assertEqual(answer, "ok")

    def test_failed_outcome_raises(self) -> None:
        data = {"timed_out": False,
                "session": {"activity": "idle", "last_turn_outcome": "failed"}}
        turns = {"turns": [{"ordinal": 7, "status": "ended", "items": []}]}
        with self._wait(data), \
                mock.patch.object(D, "current_ordinal", lambda *a: 6), \
                mock.patch.object(D, "http_get",
                                  lambda *a, **k: {"code": 0, "data": turns}):
            with self.assertRaises(D.DispatchError) as ctx:
                D.run_turn("http://x", "t", SID, "hi", timeout=5, quiet=True)
        self.assertIn("outcome=failed", str(ctx.exception))

    def test_no_busy_read_in_the_wait_path(self) -> None:
        import inspect
        for fn in (D.wait_rounds, D.run_turn, D.await_turn_start):
            self.assertNotIn('get("busy")', inspect.getsource(fn), fn.__name__)


class StatusDerivation(unittest.TestCase):
    """1.3: `busy`/`main_turn_active` derive from `activity`; missing stays None."""

    def _status(self, info):
        with mock.patch.object(direct, "session_info", lambda sid: info), \
                mock.patch.object(direct, "is_new", lambda: True), \
                mock.patch.object(direct, "_ws_status", lambda sid: {}), \
                mock.patch.object(direct, "wire_ctx", lambda sid: {"ctx_state": "absent"}):
            return direct.status(SID)

    def test_idle(self) -> None:
        out = self._status({"activity": "idle", "archived": False})
        self.assertEqual(out["activity"], "idle")
        self.assertIs(out["busy"], False)
        self.assertIs(out["main_turn_active"], False)

    def test_turn(self) -> None:
        out = self._status({"activity": "turn"})
        self.assertIs(out["busy"], True)
        self.assertIs(out["main_turn_active"], True)

    def test_background_is_busy_but_no_main_turn(self) -> None:
        out = self._status({"activity": "background"})
        self.assertIs(out["busy"], True)
        self.assertIs(out["main_turn_active"], False)

    def test_missing_activity_keeps_none(self) -> None:
        """The whole point: `None` must not masquerade as `False`."""
        out = self._status({"archived": False})
        self.assertIsNone(out["busy"])
        self.assertIsNone(out["main_turn_active"])

    def test_keys_are_in_the_contract(self) -> None:
        self.assertIn("activity", direct.STATUS_KEYS)
        self.assertIn("last_turn_outcome", direct.STATUS_KEYS)
        self.assertIn("busy", direct.STATUS_KEYS)      # old keys kept, not deleted


class InteractionAnswer(unittest.TestCase):
    """1.4: answered inside the loop, `manual` → rejected, no global PERMISSION_MODE."""

    def test_manual_rejects(self) -> None:
        self.assertEqual(D._decision_for("manual"), "rejected")
        self.assertEqual(D._decision_for("auto"), "approved")
        self.assertEqual(D._decision_for("yolo"), "approved")
        self.assertEqual(D._decision_for("nonsense"), "rejected")   # fail closed
        self.assertEqual(D._decision_for(""), "rejected")

    def test_no_permission_mode_global(self) -> None:
        self.assertFalse(hasattr(D, "PERMISSION_MODE"))

    def test_a_pending_approval_is_answered_then_the_round_finishes(self) -> None:
        """The interaction is answered within the wait loop, not after a timeout."""
        calls: list = []
        pending_answer = {
            "timed_out": False,
            "pending_interactions": [{"interaction_id": "it_1", "kind": "approval"}],
            "session": {"activity": "turn"},
        }
        done = {"timed_out": False, "pending_interactions": [],
                "session": {"activity": "idle", "last_turn_outcome": "completed"}}

        def fake_post(url, token, body, timeout=60):
            calls.append((url, body))
            if url.endswith("/interactions/it_1"):
                return {"code": 0, "data": {"outcome": body["decision"]}}
            return {"code": 0, "data": pending_answer if len(calls) == 1 else done}

        with mock.patch.object(D, "http_post", fake_post):
            res = D.wait_rounds("http://x", "t", SID, timeout=5, permission_mode="manual")
        self.assertEqual(res["outcome"], "completed")
        answered = [b for u, b in calls if "/interactions/" in u]
        self.assertEqual(len(answered), 1)
        self.assertEqual(answered[0]["decision"], "rejected")   # manual ⇒ fail closed

    def test_question_stops_for_a_person(self) -> None:
        data = {"timed_out": False,
                "pending_interactions": [{"interaction_id": "q_1", "kind": "question",
                                          "request": {"questions": ["pick one"]}}],
                "session": {"activity": "turn"}}
        with mock.patch.object(D, "http_post", lambda *a, **k: {"code": 0, "data": data}):
            with self.assertRaises(D.NeedsPerson) as ctx:
                D.wait_rounds("http://x", "t", SID, timeout=5, permission_mode="auto")
        self.assertEqual(ctx.exception.interaction_id, "q_1")

    def test_a_gone_interaction_is_not_a_dispatch_failure(self) -> None:
        for code in (40404, 41001):
            with mock.patch.object(D, "http_post",
                                   lambda *a, _c=code, **k: {"code": _c, "data": {}}):
                res = D._auto_answer("http://x", "t", SID,
                                     {"interaction_id": "it_2", "kind": "approval"}, "auto")
            self.assertEqual(res["code"], code)
            self.assertIsNone(res["outcome"])


class Cancellation(unittest.TestCase):
    """1.5: `:cancel` replaces `:abort`; the judge is `data.cancelled`."""

    def test_cancel_session_uses_the_cancel_endpoint(self) -> None:
        seen = {}

        def fake_post(path, body=None, timeout=30.0):
            seen["path"] = path
            seen["body"] = body
            return 200, {"code": 0, "data": {"cancelled": True}}

        with mock.patch.object(direct, "post", fake_post):
            ok, code = direct.cancel_session(SID, include_background=False)
        self.assertTrue(ok)
        self.assertEqual(code, 0)
        self.assertTrue(seen["path"].endswith(":cancel"), seen["path"])
        self.assertEqual(seen["body"], {"include_background": False})

    def test_cancel_session_is_not_a_wrapper_with_no_caller(self) -> None:
        """P1-b: it must have a caller outside `abort_session`, or be merged back."""
        import inspect
        from super_agent import console
        self.assertIn("cancel_session", inspect.getsource(console.cmd_stop))
        self.assertIn("cancel_session", inspect.getsource(direct.abort_session))

    def test_abort_session_falls_back_when_cancel_is_refused(self) -> None:
        with mock.patch.object(direct, "cancel_session", lambda *a, **k: (False, 40001)), \
                mock.patch.object(direct, "active_prompt", lambda sid: ""):
            ok, code = direct.abort_session(SID)
        self.assertFalse(ok)
        self.assertEqual(code, 40001)

    def test_no_abort_endpoint_left_in_the_abort_path(self) -> None:
        import inspect
        self.assertNotIn(":abort\"", inspect.getsource(direct.cancel_session))
        self.assertNotIn(":abort\"", inspect.getsource(direct.abort_session))


class LivenessReadsActivity(unittest.TestCase):
    """§1.4③: `probe_liveness` stops falling through to "no busy field"."""

    def _probe(self, detail):
        with mock.patch.object(decide_watch, "_probe_session", lambda sid: (True, detail)):
            return decide_watch.probe_liveness(SID)

    def test_turn_is_busy(self) -> None:
        self.assertEqual(self._probe({"activity": "turn"})[0], decide_watch.LIVENESS_BUSY)

    def test_background_is_busy(self) -> None:
        self.assertEqual(self._probe({"activity": "background"})[0], decide_watch.LIVENESS_BUSY)

    def test_idle_is_idle(self) -> None:
        self.assertEqual(self._probe({"activity": "idle"})[0], decide_watch.LIVENESS_IDLE)

    def test_pending_interaction_is_unknown_not_dead(self) -> None:
        state, why = self._probe({"activity": "idle", "pending_interaction": "approval"})
        self.assertEqual(state, decide_watch.LIVENESS_UNKNOWN)
        self.assertIn("pending_interaction", why)

    def test_neither_key_is_unknown(self) -> None:
        self.assertEqual(self._probe({})[0], decide_watch.LIVENESS_UNKNOWN)


if __name__ == "__main__":
    unittest.main()
