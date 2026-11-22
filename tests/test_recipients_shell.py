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
    """With no ``to_role`` the plain chain answers (supervisor only since #72)."""

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
        # #72: the delivery target is the supervisor. Before #72 this fixture had
        # only a bare ``pm-demo`` key - it passed because the bare key was leg
        # ①, which is the defect #72 removes. Keeping it would now mean "nobody
        # to send to" (empty resolver ⇒ undelivered, sent=0).
        (tmp / "sessions.json").write_text(json.dumps(
            {"pm-demo": {"id": "session_00000000-0000-4000-8000-000000000001"},
             "supervisor": {"id": "session_00000000-0000-4000-8000-000000000002"}}),
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


class TheChainIsTheProjectPmOnly(unittest.TestCase):
    """Delivery goes to the scoped PM, never to a supervisor or another project."""

    def _configure(self, tmp, sessions):
        import json
        from dataclasses import replace
        from pathlib import Path

        tmp = Path(tmp)
        saved = dw._cfg()
        self.addCleanup(lambda: dw.configure(saved))
        dw.configure(replace(saved, state_root=tmp, workspace_root=tmp,
                             sessions_path=tmp / "sessions.json"))
        (tmp / "sessions.json").write_text(json.dumps(sessions), encoding="utf-8")

    def test_scoped_pm_wins_and_supervisor_is_not_a_fallback(self):
        import tempfile

        self._configure(tempfile.mkdtemp(), {
            "pm-demo": {"id": "session_pm_demo"},
            "supervisor": {"id": "session_sup"},
        })
        self.assertEqual(dw.resolve_recipients("acme/demo"),
                         [("pm-demo", "session_pm_demo")])

    def test_no_scoped_pm_is_empty_not_supervisor_or_broadcast(self):
        import tempfile

        self._configure(tempfile.mkdtemp(), {
            "pm-other-1a2b3c4d": {"id": "session_pm_other"},
            "supervisor": {"id": "session_sup"},
        })
        self.assertEqual(dw.resolve_recipients("acme/demo"), [])

    def test_the_resolver_carries_no_level_docs_and_no_alarm_leg(self):
        import inspect

        src = inspect.getsource(dw.resolve_recipients)
        self.assertNotIn('pm_key = f"pm-{proj}"', src)
        self.assertNotIn("live_pm_sids", src)

    def test_the_module_no_longer_mentions_the_broadcast_leg(self):
        import inspect

        src = inspect.getsource(dw)
        self.assertNotIn("live_pm_sids", src)
        # ...but the payload sites that name ``pm_key`` for the event body stay:
        # they are data, not recipient resolution.
        self.assertIn("pm_key", inspect.getsource(dw._send_delivery))

    def test_brief_scopes_by_the_shared_predicate(self):
        # brief.py:230 was a dumb filter (``k == f"pm-<project>"``) that matched
        # nothing live; #72 part 3 routes it through the same ``scoped_by`` the
        # engine uses, so a rotated key is listed instead of "无".
        import inspect

        from super_agent import brief

        src = inspect.getsource(brief._projects_items)
        self.assertIn('sessionkeys.scoped_by(k, "pm", project)', src)
        # The old dumb filter, as an assignment: a comment may still quote it.
        self.assertNotIn('keys = [k for k in keys if k == f"pm-{project}"]', src)


if __name__ == "__main__":
    unittest.main()
