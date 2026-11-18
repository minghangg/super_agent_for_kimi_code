"""The recipients shell: unchanged fallback, a real seam for #72 (#92 §P0-1b).

Run: ``python -m pytest tests/test_recipients_shell.py -q``

This issue does **not** implement the ``to_role`` path - #72 owns it, and its
precondition (``pm`` is bindable) does not hold yet. What is proved here is that
the shell neither changes today's behaviour nor is decoration.
"""
from __future__ import annotations

import unittest
from unittest import mock

from super_agent import decide_watch as dw


class Fallback(unittest.TestCase):
    """With no ``to_role`` the four-level chain answers exactly as before."""

    def test_none_and_no_role_fall_back_to_the_chain(self):
        calls = []

        def fake(repo):
            calls.append(repo)
            return [("pm-x", "sid-1")]

        with mock.patch.object(dw, "resolve_recipients", fake):
            self.assertEqual(dw.recipients(None), [("pm-x", "sid-1")])
            self.assertEqual(dw.recipients({"repo": "o/n", "to_role": []}),
                             [("pm-x", "sid-1")])
        # No envelope names no repo, so the chain is asked for the empty one -
        # exactly what the literal fallback says.
        self.assertEqual(calls, ["", "o/n"])

    def test_an_empty_to_role_is_a_broadcast_not_a_target(self):
        with mock.patch.object(dw, "resolve_recipients", lambda repo: [("s", "i")]):
            self.assertEqual(dw.recipients({"repo": "o/n", "to_role": []}),
                             [("s", "i")])

    def test_a_missing_repo_asks_the_chain_for_nothing(self):
        calls = []
        with mock.patch.object(dw, "resolve_recipients",
                               lambda repo: calls.append(repo) or []):
            self.assertEqual(dw.recipients({"to_role": []}), [])
        self.assertEqual(calls, [""])


class TheSeamIsReal(unittest.TestCase):
    """The signature is not decoration: ``to_role`` really routes to ``resolve``."""

    def test_a_to_role_calls_the_resolver(self):
        seen = []

        def resolve(env):
            seen.append(env)
            return [("pm", "sid-pm")]

        out = dw.recipients({"to_role": ["pm"]}, resolve=resolve)
        self.assertEqual(out, [("pm", "sid-pm")])
        self.assertEqual(seen, [{"to_role": ["pm"]}])

    def test_a_to_role_without_a_resolver_answers_empty(self):
        # Fail-closed: a target nobody can resolve must not degrade into a
        # broadcast to the fallback chain.
        with mock.patch.object(dw, "resolve_recipients",
                               lambda repo: [("pm-x", "sid-1")]):
            self.assertEqual(dw.recipients({"repo": "o/n", "to_role": ["pm"]}), [])


class OnTheDeliveryPath(unittest.TestCase):
    """P0-2: ``_send_delivery`` really goes through ``recipients()``.

    The other assertions in this file prove the function is correct. This one
    proves it is **reached** - the difference #92's first round got wrong.
    """

    def test_every_send_passes_through_the_shell(self):
        import json
        import tempfile
        from dataclasses import replace
        from pathlib import Path

        tmp = Path(tempfile.mkdtemp())
        saved = dw._cfg()
        self.addCleanup(lambda: dw.configure(saved))
        dw.configure(replace(saved, state_root=tmp, workspace_root=tmp,
                             event_source=lambda: [],
                             heartbeat=lambda *a, **k: None,
                             send_transport=lambda *a, **k: True))
        (tmp / "sessions.json").write_text(json.dumps(
            {"pm-demo": {"id": "session_00000000-0000-4000-8000-000000000001"}}),
            encoding="utf-8")
        rec = {"v": 1, "project": "demo", "repo": "acme/demo", "issue": 1,
               "worker_name": "wk", "worker_session": "", "pm_key": "pm-demo",
               "pm_sid": "", "kind": "comment", "evidence": "cid:1",
               "ledger_line": 1, "detected_ts": 1e6, "dl_id": "dl-1"}
        seen = []
        real = dw.recipients

        def spy(env, resolve=None):
            seen.append(dict(env or {}))
            return real(env, resolve)

        with mock.patch.object(dw, "recipients", spy):
            sent = dw.enqueue_delivery(dw.load_delivery_state(), rec, dry=True,
                                       now=1e6, no_cooldown=True, no_merge=True)
        self.assertEqual(sent, 1)
        self.assertEqual(len(seen), 1, "the send must go through recipients()")
        self.assertEqual(seen[0]["repo"], "acme/demo")
        self.assertEqual(seen[0]["issue"], 1)

    def test_the_send_line_no_longer_calls_the_chain_directly(self):
        import inspect

        src = inspect.getsource(dw._send_delivery)
        self.assertIn("recipients(", src)
        self.assertNotIn("resolve_recipients(rec[", src)


class TheFourSitesAreMarked(unittest.TestCase):
    """§P0-1b: the four ``f"pm-{proj}"`` sites carry the #72 marker."""

    def test_decide_watch_marks_three_sites(self):
        import inspect

        src = inspect.getsource(dw)
        self.assertEqual(src.count("#72 落地点"), 2)
        # ...and the payload site is the one that injects pm_key/pm_sid.
        self.assertIn("pm_key", inspect.getsource(dw._send_delivery))

    def test_brief_marks_its_site(self):
        import inspect

        from super_agent import brief

        self.assertIn("#72 落地点", inspect.getsource(brief))

    def test_behaviour_is_unchanged(self):
        # A comment must not rename the key: the chain still looks for the bare
        # ``pm-<project>`` until #72 changes it.
        import inspect

        self.assertIn('pm_key = f"pm-{proj}" if proj else ""',
                      inspect.getsource(dw.resolve_recipients))


if __name__ == "__main__":
    unittest.main()
