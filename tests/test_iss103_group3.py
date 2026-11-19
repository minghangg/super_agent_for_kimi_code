"""L1 tests for ISS-103 group 3: the KAP dispatch loop.

Offline: HTTP is patched everywhere.

What must be true:

* the idempotency key is `{issue}-{round}-{hash8}` and a re-submit that hits
  `40927 conflict` counts as "already submitted" instead of failing or running a
  second round;
* `queued` is a legal receipt but **not** a start -- the fence waits for the
  ordinal to grow (P0-5 again, from the caller's side);
* `blocked` / `failed` fail immediately without entering the fence;
* `needs_person` is interaction-driven: only a `question` (or a `failed`
  outcome) stops the round, a timeout is reported as a timeout;
* a cold session is `unknown`, not `idle`, when its last turn has no outcome.

Run: ``python -m pytest tests/test_iss103_group3.py -q``
"""
from __future__ import annotations

import unittest
from unittest import mock

from super_agent import direct, dispatch_cli as D

SID = "session_00000000-0000-4000-8000-000000000000"


class PromptRef(unittest.TestCase):
    def test_shape(self) -> None:
        ref = D.prompt_ref("103", 2, "hello")
        self.assertTrue(ref.startswith("103-2-"))
        self.assertEqual(len(ref.split("-")[2]), 8)

    def test_same_text_same_key_and_different_text_differs(self) -> None:
        self.assertEqual(D.prompt_ref("103", 1, "a"), D.prompt_ref("103", 1, "a"))
        self.assertNotEqual(D.prompt_ref("103", 1, "a"), D.prompt_ref("103", 1, "b"))
        self.assertNotEqual(D.prompt_ref("103", 1, "a"), D.prompt_ref("103", 2, "a"))

    def test_no_issue_falls_back(self) -> None:
        self.assertTrue(D.prompt_ref("", 1, "a").startswith("adhoc-1-"))


class Submit(unittest.TestCase):
    def test_a_conflict_counts_as_already_submitted(self) -> None:
        payload = {"code": 40927, "msg": "conflict"}
        with mock.patch.object(D, "http_post", lambda *a, **k: payload):
            pid, status = D.submit_prompt("http://x", "t", SID, "hi", "103-1-deadbeef")
        self.assertEqual(pid, "103-1-deadbeef")
        self.assertEqual(status, "running")     # go straight to :wait, do not re-send

    def test_a_normal_receipt_carries_the_status(self) -> None:
        payload = {"code": 0, "data": {"prompt_id": "msg_1", "status": "queued"}}
        with mock.patch.object(D, "http_post", lambda *a, **k: payload):
            pid, status = D.submit_prompt("http://x", "t", SID, "hi", "103-1-x")
        self.assertEqual((pid, status), ("msg_1", "queued"))

    def test_the_prompt_id_goes_into_the_body(self) -> None:
        seen = {}

        def fake_post(url, token, body, timeout=60):
            seen["body"] = body
            return {"code": 0, "data": {"prompt_id": "msg_1", "status": "running"}}

        with mock.patch.object(D, "http_post", fake_post):
            D.submit_prompt("http://x", "t", SID, "hi", "103-1-deadbeef")
        self.assertEqual(seen["body"]["prompt_id"], "103-1-deadbeef")


class QueuedIsNotAStart(unittest.TestCase):
    def test_queued_waits_for_the_ordinal(self) -> None:
        """`queued` must not short-circuit: the fence still has to see a new round."""
        seen = {"n": 0}

        def fake_post(url, token, body, timeout=60):
            seen["n"] += 1
            return {"code": 0, "data": {"prompt_id": "msg_1", "status": "queued"}}

        turns = {"turns": [{"ordinal": 6, "status": "running"}]}
        with mock.patch.object(D, "http_post", fake_post), \
                mock.patch.object(D, "current_ordinal", lambda *a: 6), \
                mock.patch.object(D, "http_get", lambda *a, **k: {"code": 0, "data": turns}):
            with self.assertRaises(D.DispatchError) as ctx:
                D.run_turn("http://x", "t", SID, "hi", timeout=1, quiet=True)
        self.assertIn("round did not start", str(ctx.exception))
        self.assertEqual(seen["n"], 1)          # submitted once, never re-sent

    def test_blocked_fails_before_the_fence(self) -> None:
        def fake_post(url, token, body, timeout=60):
            return {"code": 0, "data": {"prompt_id": "msg_1", "status": "blocked"}}

        with mock.patch.object(D, "http_post", fake_post), \
                mock.patch.object(D, "current_ordinal", lambda *a: 6):
            with self.assertRaises(D.DispatchError) as ctx:
                D.run_turn("http://x", "t", SID, "hi", timeout=1, quiet=True)
        self.assertIn("status='blocked'", str(ctx.exception))

    def test_failed_fails_before_the_fence(self) -> None:
        def fake_post(url, token, body, timeout=60):
            return {"code": 0, "data": {"prompt_id": "msg_1", "status": "failed"}}

        with mock.patch.object(D, "http_post", fake_post), \
                mock.patch.object(D, "current_ordinal", lambda *a: 6):
            with self.assertRaises(D.DispatchError) as ctx:
                D.run_turn("http://x", "t", SID, "hi", timeout=1, quiet=True)
        self.assertIn("status='failed'", str(ctx.exception))


class NeedsPersonIsInteractionDriven(unittest.TestCase):
    def test_a_timeout_is_reported_as_a_timeout_not_as_needs_person(self) -> None:
        data = {"timed_out": True, "session": {"activity": "turn"}}
        with mock.patch.object(D, "http_post", lambda *a, **k: {"code": 0, "data": data}):
            with self.assertRaises(D.DispatchError) as ctx:
                D.wait_rounds("http://x", "t", SID, timeout=0.02)
        self.assertNotIsInstance(ctx.exception, D.NeedsPerson)
        self.assertIn("非交互阻塞", str(ctx.exception))

    def test_a_question_is_needs_person(self) -> None:
        data = {"timed_out": False,
                "pending_interactions": [{"interaction_id": "q_9", "kind": "question"}],
                "session": {"activity": "turn"}}
        with mock.patch.object(D, "http_post", lambda *a, **k: {"code": 0, "data": data}):
            with self.assertRaises(D.NeedsPerson) as ctx:
                D.wait_rounds("http://x", "t", SID, timeout=5)
        self.assertEqual(ctx.exception.interaction_id, "q_9")
        self.assertNotIsInstance(ctx.exception, D.DispatchError)

    def test_an_approval_is_not_needs_person(self) -> None:
        """An approval is answerable without a human under auto/yolo."""
        calls = {"n": 0}
        approve = {"timed_out": False,
                   "pending_interactions": [{"interaction_id": "a_1", "kind": "approval"}],
                   "session": {"activity": "turn"}}
        done = {"timed_out": False, "pending_interactions": [],
                "session": {"activity": "idle", "last_turn_outcome": "completed"}}

        def fake_post(url, token, body, timeout=60):
            calls["n"] += 1
            if "/interactions/" in url:
                return {"code": 0, "data": {"outcome": body["decision"]}}
            return {"code": 0, "data": approve if calls["n"] <= 2 else done}

        with mock.patch.object(D, "http_post", fake_post):
            res = D.wait_rounds("http://x", "t", SID, timeout=5, permission_mode="auto")
        self.assertEqual(res["outcome"], "completed")
        self.assertEqual(len(res["interactions_seen"]), 1)


class ColdSessionIsUnknown(unittest.TestCase):
    """§5.4: `activity=idle` alone is not "not running"."""

    def _liveness(self, ses, turns):
        def fake_get(path, timeout=20.0):
            if "/turns" in path:
                return 200, {"code": 0, "data": {"turns": turns}}
            return 200, {"code": 0, "data": ses}

        with mock.patch.object(direct, "get", fake_get):
            return direct.liveness(SID)

    def test_turn_is_turn(self) -> None:
        self.assertEqual(self._liveness({"activity": "turn"}, []), "turn")

    def test_background_is_background(self) -> None:
        self.assertEqual(self._liveness({"activity": "background"}, []), "background")

    def test_idle_with_a_terminal_turn_is_idle(self) -> None:
        turns = [{"ordinal": 6, "status": "ended", "outcome": "completed"}]
        self.assertEqual(self._liveness({"activity": "idle"}, turns), "idle")

    def test_interrupted_is_also_terminal(self) -> None:
        turns = [{"ordinal": 6, "status": "ended", "outcome": "interrupted"}]
        self.assertEqual(self._liveness({"activity": "idle"}, turns), "idle")

    def test_idle_without_a_terminal_turn_is_unknown(self) -> None:
        """The whole point: not-loaded sessions report "not busy" unconditionally."""
        self.assertEqual(self._liveness({"activity": "idle"}, []), "unknown")
        self.assertEqual(self._liveness(
            {"activity": "idle"}, [{"ordinal": 6, "status": "running"}]), "unknown")

    def test_an_unreadable_session_is_unknown(self) -> None:
        self.assertEqual(self._liveness({"_err": "http=40401"}, []), "unknown")


class TurnsOrder(unittest.TestCase):
    """`turns` is newest-first and `items` is oldest-first: both need reversing."""

    def test_the_latest_assistant_message_wins(self) -> None:
        data = {"turns": [
            {"ordinal": 6, "items": [{"type": "user_message", "text": "q2"},
                                     {"type": "assistant_message", "text": "newest"}]},
            {"ordinal": 5, "items": [{"type": "assistant_message", "text": "older"}]},
        ]}
        with mock.patch.object(D, "http_get", lambda *a, **k: {"code": 0, "data": data}):
            self.assertEqual(D.fetch_final_answer("http://x", "t", SID), "newest")

    def test_no_assistant_message_returns_empty(self) -> None:
        data = {"turns": [{"ordinal": 6,
                           "items": [{"type": "user_message", "text": "q"}]}]}
        with mock.patch.object(D, "http_get", lambda *a, **k: {"code": 0, "data": data}):
            self.assertEqual(D.fetch_final_answer("http://x", "t", SID), "")


if __name__ == "__main__":
    unittest.main()
