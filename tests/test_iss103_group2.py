"""L1 tests for ISS-103 group 2: the v2→v1 migration and the two hard gates.

Offline: every HTTP entry point is patched, no session is created.

What must be true:

* **P0-3** `direct.restore_session` no longer returns `(st == 200)`. A v1 bulk
  response for a missing sid is HTTP 200 / code 0 / `results[].ok=false`; the old
  judge called that success and the caller (`wss_bus.ctl`) journalled a restore
  that never happened.
* **P0-4** `console._request_verdict` fails closed when the body carries neither
  `results` nor a boolean verdict key. `data:{}` and `data:None` used to be
  `(True, '顶层 code=0')`; `{archived:true}` stays True because that **is** the
  documented single-action shape (`openapi`: `required`).
* paging goes through `data.next_page_token`; `after_id` silently returned the
  first page on 2.7, so `list_all_sessions()` saw 50 of 944 sessions.
* no `api/v2` path survives in the package.

Run: ``python -m pytest tests/test_iss103_group2.py -q``
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from super_agent import console, direct

SID = "session_00000000-0000-4000-8000-000000000000"
ROOT = Path(__file__).resolve().parent.parent


class VerdictGate(unittest.TestCase):
    """P0-4: the three shapes that used to be green."""

    def test_single_action_booleans_are_accepted(self) -> None:
        for key in ("archived", "restored", "cancelled", "taken_over"):
            ok, why = console._request_verdict(200, {"code": 0, "data": {key: True}})
            self.assertTrue(ok, key)
            self.assertIn("单动作", why)

    def test_a_false_boolean_is_a_refusal(self) -> None:
        for key in ("archived", "restored", "cancelled", "taken_over"):
            ok, _why = console._request_verdict(200, {"code": 0, "data": {key: False}})
            self.assertFalse(ok, key)

    def test_a_non_boolean_is_not_a_verdict(self) -> None:
        """`{"archived": "yes"}` is a struct drift, not a yes."""
        ok, why = console._request_verdict(200, {"code": 0, "data": {"archived": "yes"}})
        self.assertFalse(ok)
        self.assertIn("不可判", why)

    def test_an_empty_body_fails_closed(self) -> None:
        for body in ({}, None, [], "ok"):
            ok, why = console._request_verdict(200, {"code": 0, "data": body})
            self.assertFalse(ok, body)
            self.assertIn("不可判", why)

    def test_the_old_false_green_is_gone(self) -> None:
        """Freeze the regression: none of these may return the old reason string."""
        for body in ({}, None):
            _ok, why = console._request_verdict(200, {"code": 0, "data": body})
            self.assertNotEqual(why, "顶层 code=0")

    def test_bulk_shape_is_unchanged(self) -> None:
        ok, why = console._request_verdict(
            200, {"code": 0, "data": {"results": [{"ok": True}], "succeeded": 1, "failed": 0}})
        self.assertTrue(ok)
        self.assertIn("批量", why)

    def test_a_missing_sid_in_bulk_is_a_refusal(self) -> None:
        ok, why = console._request_verdict(
            200, {"code": 0, "data": {"results": [{"ok": False}], "succeeded": 0, "failed": 1}})
        self.assertFalse(ok)
        self.assertIn("succeeded=0", why)


class RestoreVerdict(unittest.TestCase):
    """P0-3: restore must not be judged on the HTTP status alone."""

    def test_a_missing_sid_restore_fails(self) -> None:
        payload = {"code": 0, "data": {"results": [{"ok": False}],
                                       "succeeded": 0, "failed": 1}}
        with mock.patch.object(direct, "post", lambda *a, **k: (200, payload)):
            ok, _d = direct.restore_session(SID)
        self.assertFalse(ok)          # the old `st == 200` said True

    def test_a_real_restore_succeeds(self) -> None:
        payload = {"code": 0, "data": {"results": [{"ok": True}],
                                       "succeeded": 1, "failed": 0}}
        with mock.patch.object(direct, "post", lambda *a, **k: (200, payload)):
            ok, _d = direct.restore_session(SID)
        self.assertTrue(ok)

    def test_it_hits_v1(self) -> None:
        seen = {}

        def fake_post(path, body=None, timeout=30.0):
            seen["path"] = path
            return 200, {"code": 0, "data": {"results": [{"ok": True}], "succeeded": 1}}

        with mock.patch.object(direct, "post", fake_post):
            direct.restore_session(SID)
        self.assertEqual(seen["path"], "/api/v1/sessions:restore")

    def test_source_has_no_http_status_judge(self) -> None:
        """Walk the AST: no `return` whose value is `st == 200`, and the verdict
        function must actually be called."""
        import ast
        import inspect
        for fn in (direct.restore_session, direct.takeover_session,
                   direct.archive_session):
            tree = ast.parse(inspect.getsource(fn))
            returns = [n for n in ast.walk(tree) if isinstance(n, ast.Return)]
            self.assertTrue(returns, fn.__name__)
            for node in returns:
                dumped = ast.dump(node.value) if node.value is not None else ""
                self.assertNotIn("st == 200", dumped, fn.__name__)
            names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
            self.assertIn("_request_verdict", names, fn.__name__)


class Paging(unittest.TestCase):
    """`after_id` silently returned page one; the cursor is `next_page_token`."""

    def test_the_cursor_is_sent_as_page_token(self) -> None:
        seen = {}

        def fake_get(path, timeout=20.0):
            seen["path"] = path
            return 200, {"code": 0, "data": {"items": [{"id": "session_b"}],
                                             "next_page_token": "TOK2"}}

        with mock.patch.object(direct, "get", fake_get):
            items, nxt, err = direct.list_sessions(page_token="TOK1")
        self.assertIn("page_token=TOK1", seen["path"])
        self.assertNotIn("after_id", seen["path"])
        self.assertEqual(nxt, "TOK2")
        self.assertEqual(len(items), 1)
        self.assertEqual(err, "")

    def test_archived_uses_the_new_parameter_name(self) -> None:
        seen = {}

        def fake_get(path, timeout=20.0):
            seen["path"] = path
            return 200, {"code": 0, "data": {"items": [], "next_page_token": ""}}

        with mock.patch.object(direct, "get", fake_get):
            direct.list_sessions(archived_only=True)
        self.assertIn("archived=true", seen["path"])
        self.assertNotIn("archived_only", seen["path"])

    def test_all_pages_are_walked(self) -> None:
        pages = {
            "": ([{"id": "a"}], "T2"),
            "T2": ([{"id": "b"}], "T3"),
            "T3": ([{"id": "c"}], ""),
        }
        seen_tokens = []

        def fake_get(path, timeout=20.0):
            token = ""
            if "page_token=" in path:
                token = path.split("page_token=")[1].split("&")[0]
            seen_tokens.append(token)
            items, nxt = pages[token]
            return 200, {"code": 0, "data": {"items": items, "next_page_token": nxt}}

        with mock.patch.object(direct, "get", fake_get):
            items, err = direct.list_all_sessions(page_size=5)
        self.assertEqual(err, "")
        self.assertEqual(sorted(i["id"] for i in items), ["a", "b", "c"])
        self.assertEqual(seen_tokens, ["", "T2", "T3"])

    def test_a_stuck_cursor_does_not_loop_forever(self) -> None:
        def fake_get(path, timeout=20.0):
            return 200, {"code": 0, "data": {"items": [{"id": "a"}], "next_page_token": "SAME"}}

        with mock.patch.object(direct, "get", fake_get):
            items, err = direct.list_all_sessions(page_size=1)
        self.assertEqual(err, "")
        self.assertEqual(len(items), 1)


class NoV2Paths(unittest.TestCase):
    def test_no_api_v2_string_in_the_package(self) -> None:
        hits = []
        for path in (ROOT / "super_agent").rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            if "api/v2" in text:
                hits.append(str(path.relative_to(ROOT)))
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
