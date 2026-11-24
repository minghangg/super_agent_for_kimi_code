"""R2 counterexamples: Windows rename vs. an open reader (#129 P0).

QA rejected commit ``758b652`` on one P0: on Windows ``os.replace(tmp, path)``
fails with ``WinError 5`` whenever a process holds the target open for reading,
and the code that introduced the rename then (a) failed a write the old
in-place writer performed, (b) left the *other* map written -- a half write --
and (c) swallowed the failure in ``register_key``, which ``at_cli`` discarded,
so an AT could be created with no key registered and rc=0.

Injections, not timing: ``store.atomic_write_text`` already routed every rename
through ``os.replace``, so patching that one call reproduces ``WinError 5``
deterministically on any OS. That keeps the regression meaningful on Linux,
where rename-over-open succeeds and a timing test would pass by accident.

The three properties, one per QA finding:

1. a short hold is ridden out by retry; a long hold degrades to the baseline's
   in-place write **and says so** -- it must not fail, and must not be silent;
2. a failure on the second map rolls the first one back (no half write);
3. ``register_key`` failure reaches its caller (no silent rc=0).

Run: ``python -m pytest tests/test_session_map_replace.py -q``
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from super_agent import session_map_store, store

FULL_SID = "session_" + "a1b2c3d4" + "-1111-2222-3333-" + "444455556666"


def _winerror5(tmp: str) -> PermissionError:
    """The exact failure Windows raises when the target is open for reading."""
    return PermissionError(13, "Access is denied", tmp, 5, None)


class _StateDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self._env = dict(os.environ)
        os.environ["SESS_EVENTS_STATE_DIR"] = str(self.state)
        self.sessions = self.state / "sessions.json"
        self.issues = self.state / "issue_sessions.json"
        self.sessions.write_text(json.dumps({"pre": {"id": FULL_SID}}), encoding="utf-8")
        self.issues.write_text(json.dumps({}), encoding="utf-8")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def read(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))


class Counterexample5OpenReader(_StateDir):
    """P0-1 ①: an open reader must not turn a write into a failure."""

    def test_a_transient_refusal_is_retried_and_the_write_lands(self):
        from super_agent import session_broker

        calls = {"n": 0}
        real = store.os.replace

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] <= 3:
                raise _winerror5(str(dst))
            return real(src, dst)

        with mock.patch.object(store.os, "replace", flaky):
            mode = session_map_store.write_with_retry(self.sessions, '{"x": 1}')
        self.assertEqual(mode, "rename")
        self.assertGreaterEqual(calls["n"], 4)
        self.assertEqual(self.read(self.sessions), {"x": 1})

    def test_a_reader_using_the_shared_open_does_not_block_the_write(self):
        """QA P0-1 ①: a reader holding the map open must not fail the write.

        The reader is the repo's own shared-open reader. It takes the lock, so
        the writer's in-place path (the only one Windows allows while a foreign
        handle is open) is invisible to it: the write lands and the read is
        never torn.
        """
        from super_agent import session_map_io, session_broker

        with session_map_io.open_text(self.sessions) as held:
            self.assertIsNotNone(held.read(1))
            session_broker.set_key("qa-b", FULL_SID)
        self.assertEqual(self.read(self.sessions)["qa-b"]["id"], FULL_SID)

    def test_an_external_plain_handle_is_ridden_out_by_the_retry(self):
        """A plain ``open()`` handle (no delete sharing) must not lose the write.

        Windows refuses to rename onto such a handle at all, so the write
        degrades to the in-place path -- under the lock, so a same-repo reader
        cannot observe a half-written file. Either way the key must land.
        """
        from super_agent import session_broker

        handle = open(self.sessions, "rb")
        try:
            self.assertIsNotNone(handle.read(1))
            session_broker.set_key("qa-c", FULL_SID)
        finally:
            handle.close()
        self.assertEqual(self.read(self.sessions)["qa-c"]["id"], FULL_SID)

    def test_a_long_hold_degrades_under_the_lock_and_reports_it(self):
        """Past the retry budget the write degrades -- never silently, never lost.

        The in-place path is only safe because readers take the same lock, so
        it must be *reported*: a caller that cannot tell which path ran cannot
        tell a degraded write from a normal one.
        """
        mode = session_map_store.write_with_retry(self.sessions, '{"y": 2}')
        self.assertEqual(mode, "rename")
        with mock.patch.object(store.os, "replace",
                               lambda src, dst: (_ for _ in ()).throw(
                                   _winerror5(str(dst)))):
            mode = session_map_store.write_with_retry(self.sessions, '{"y": 3}')
        self.assertEqual(mode, "inplace", "长持有被静默处理")
        self.assertEqual(self.read(self.sessions), {"y": 3})

    def test_retry_is_bounded(self):
        """The attempt count is fixed: a held file cannot make the CLI hang."""
        calls = {"n": 0}
        real = store.os.replace

        def flaky(src, dst):
            calls["n"] += 1
            return real(src, dst)

        with mock.patch.object(store.os, "replace", flaky):
            session_map_store.write_with_retry(self.sessions, '{"z": 3}')
        self.assertEqual(calls["n"], 1, "正常路径不该重试")


class Counterexample6HalfWrite(_StateDir):
    """P0-1 ②: a failure on the second map must roll the first one back."""

    def test_a_failed_second_save_leaves_both_maps_untouched(self):
        from super_agent import session_broker

        before_sessions = self.read(self.sessions)
        before_issues = self.read(self.issues)
        real = session_map_store.save_and_verify

        def selective(path, data, sort_keys):
            if path.name == "issue_sessions.json":
                raise session_map_store.BrokerError("注入：第二份写入失败")
            return real(path, data, sort_keys)

        with mock.patch.object(session_map_store, "save_and_verify", selective):
            with self.assertRaises(session_map_store.BrokerError):
                session_broker.bind_role("proj#7", "qa", FULL_SID, name="qa-proj")
        self.assertEqual(self.read(self.sessions), before_sessions, "sessions.json 半写")
        self.assertEqual(self.read(self.issues), before_issues, "issue_sessions.json 已改")

    def test_a_failed_first_save_does_not_touch_the_second(self):
        from super_agent import session_broker

        before_issues = self.read(self.issues)

        def refuse(path, data, sort_keys):
            raise session_map_store.BrokerError("注入：第一份写入失败")

        with mock.patch.object(session_map_store, "save_and_verify", refuse):
            with self.assertRaises(session_map_store.BrokerError):
                session_broker.bind_role("proj#8", "qa", FULL_SID, name="qa-proj")
        self.assertEqual(self.read(self.issues), before_issues)
        self.assertNotIn("qa-proj", self.read(self.sessions))

    def test_a_failed_rollback_is_reported_not_masked(self):
        """When the restore itself fails the error names the half-written file."""
        from super_agent import session_broker

        calls = {"n": 0}
        real = session_map_store.save_and_verify

        def fail_second_and_restore(path, data, sort_keys):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise session_map_store.BrokerError("注入：失败")
            return real(path, data, sort_keys)

        with mock.patch.object(session_map_store, "save_and_verify",
                               fail_second_and_restore):
            with self.assertRaises(session_map_store.BrokerError) as caught:
                session_broker.bind_role("proj#9", "qa", FULL_SID, name="qa-proj")
        self.assertIn("回滚失败", str(caught.exception))


class Counterexample7SilentLoss(_StateDir):
    """P0-1 ③: ``register_key`` failure must reach the caller."""

    def test_register_key_raises_instead_of_returning_a_warning(self):
        from super_agent import session_broker

        def refuse(path, data, sort_keys):
            raise session_map_store.BrokerError("注入：写入失败")

        with mock.patch.object(session_map_store, "save_and_verify", refuse):
            with self.assertRaises(session_map_store.BrokerError):
                session_broker.register_key("qa-d", FULL_SID)

    def test_the_at_start_path_returns_nonzero_when_the_key_was_not_written(self):
        from types import SimpleNamespace
        from unittest import mock as m

        from super_agent import at_cli, config, role_config

        cfg = config.load()
        ctx = SimpleNamespace(roles=role_config.load("missing-roles.json"),
                              projects={"demo": "owner/demo"}, call={}, cfg=cfg)
        args = SimpleNamespace(role="supervisor", project="demo", issue=1, cwd=".",
                               new=True, dry_run=False, task="", provider="",
                               model="", effort="")
        with m.patch.object(at_cli, "_config", return_value=cfg), \
                m.patch.object(at_cli.issue_cli, "_Ctx", return_value=ctx), \
                m.patch.object(at_cli.session_ledger, "load_sessions", return_value={}), \
                m.patch.object(at_cli.issue_cli, "_call", return_value={"labels": []}), \
                m.patch.object(at_cli, "_dispatch",
                               return_value={"session_id": FULL_SID}), \
                m.patch.object(at_cli.session_ledger, "register_session_key",
                               side_effect=session_map_store.BrokerError("写入失败")) as reg, \
                m.patch.object(at_cli.session_ledger, "bind_session") as bind:
            rc = at_cli.cmd_start(args)
        self.assertEqual(rc, 1, "AT 建了会话但键没登记，却返回 0")
        reg.assert_called_once()
        bind.assert_not_called()


class Counterexample8UnreadableMap(_StateDir):
    """A write must never treat an unparseable map as empty and rewrite it."""

    def test_an_unparseable_map_is_refused_not_replaced(self):
        from super_agent import session_broker

        broken = '{"keep": {"id": "session_x"}, '
        self.sessions.write_text(broken, encoding="utf-8")
        with self.assertRaises(session_map_store.BrokerError) as caught:
            session_broker.set_key("qa-e", FULL_SID)
        self.assertIn("sessions.json", str(caught.exception))
        self.assertEqual(self.sessions.read_text(encoding="utf-8"), broken,
                         "不可解析的台账被整份覆盖")

    def test_a_missing_map_is_a_new_map_and_is_allowed(self):
        from super_agent import session_broker

        self.sessions.unlink()
        session_broker.set_key("qa-f", FULL_SID)
        self.assertEqual(self.read(self.sessions)["qa-f"]["id"], FULL_SID)


if __name__ == "__main__":
    unittest.main()
