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
    """Direction B: the writer alone guarantees atomicity, readers hold no lock."""

    def test_a_reader_uses_a_window_that_does_not_outlive_the_read(self):
        """The reader must not hold the file open while doing anything else.

        On this host ``os.replace`` is refused while **any** handle is open,
        shared flags included (measured: WinError 5 with our own reader). So the
        reader's contract is that it closes immediately after the bytes are in
        memory -- which is what makes a bind succeed while reads are happening.
        """
        from super_agent import session_map_io, session_broker

        text = session_map_io.read_json(self.sessions)      # opens and closes
        self.assertEqual(text, {"pre": {"id": FULL_SID}})
        session_broker.set_key("qa-b", FULL_SID)
        self.assertEqual(self.read(self.sessions)["qa-b"]["id"], FULL_SID)

    def test_a_reader_holding_the_file_open_is_refused_not_degraded(self):
        """An open reader at write time is refused -- the honest answer here."""
        from super_agent import session_map_io, session_broker

        before = self.sessions.read_bytes()
        with session_map_io.open_text(self.sessions) as held:
            held.read(1)
            with self.assertRaises(session_map_store.BrokerError):
                session_broker.set_key("qa-b2", FULL_SID)
        self.assertEqual(self.sessions.read_bytes(), before)

    def test_a_foreign_plain_handle_is_refused_loudly_not_degraded(self):
        """A plain ``open()`` holder defeats every atomic swap.

        Measured here: with such a handle held, ``os.replace`` fails with
        WinError 5 and ``ReplaceFileW`` fails too -- and while it retries it
        leaves the name absent, so it is not used at all. Direction B requires
        a loud refusal instead of a degraded write.
        """
        from super_agent import session_broker

        before = self.sessions.read_bytes()
        handle = open(self.sessions, "rb")
        try:
            self.assertIsNotNone(handle.read(1))
            with self.assertRaises(session_map_store.BrokerError) as caught:
                session_broker.set_key("qa-c", FULL_SID)
        finally:
            handle.close()
        message = str(caught.exception)
        self.assertIn("未写入任何内容", message)
        self.assertIn("关闭该进程后重试", message)
        self.assertEqual(self.sessions.read_bytes(), before)
        self.assertFalse(self.sessions.with_name("sessions.json.tmp").exists())

    def test_the_write_lands_as_soon_as_the_foreign_handle_closes(self):
        from super_agent import session_broker

        open(self.sessions, "rb").close()
        session_broker.set_key("qa-c2", FULL_SID)
        self.assertEqual(self.read(self.sessions)["qa-c2"]["id"], FULL_SID)

    def test_no_degraded_write_path_exists_in_code(self):
        """Direction B: no in-place write, and no second swap mechanism."""
        import ast

        source = Path(session_map_store.__file__).read_text(encoding="utf-8")
        self.assertFalse(hasattr(session_map_store, "_inplace_write"))
        self.assertFalse(hasattr(session_map_store, "write_with_retry"))
        self.assertFalse(hasattr(session_map_store, "_replace_file_w"))
        self.assertNotIn("ReplaceFileW(", source)

        tree = ast.parse(source)
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Name) and func.id == "open"):
                continue
            mode = ""
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                mode = str(node.args[1].value)
            for kw in node.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    mode = str(kw.value.value)
            if not any(ch in mode for ch in "wax+"):
                continue
            target = node.args[0] if node.args else None
            name = target.id if isinstance(target, ast.Name) else ""
            if "tmp" in name.lower() or "+" in mode:
                continue
            offenders.append((node.lineno, name, mode))
        self.assertEqual(offenders, [], f"出现指向目标文件的写 open：{offenders}")

    def test_a_permanent_refusal_changes_nothing(self):
        before = self.sessions.read_bytes()
        boom = mock.Mock(side_effect=OSError(5, "Access is denied"))
        with mock.patch.object(store.os, "replace", boom):
            with self.assertRaises(session_map_store.BrokerError) as caught:
                session_map_store.write_atomic(self.sessions, '{"z": 3}')
        self.assertIn("未写入任何内容", str(caught.exception))
        self.assertEqual(self.sessions.read_bytes(), before)
        self.assertFalse(self.sessions.with_name("sessions.json.tmp").exists())
        self.assertGreaterEqual(boom.call_count, 1)

    def test_the_normal_path_swaps_once(self):
        calls = {"n": 0}
        real = store.os.replace

        def counted(src, dst):
            calls["n"] += 1
            return real(src, dst)

        with mock.patch.object(store.os, "replace", counted):
            session_map_store.write_atomic(self.sessions, '{"z": 4}')
        self.assertEqual(calls["n"], 1, "正常路径只换一次")
        self.assertEqual(self.read(self.sessions), {"z": 4})


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



class Counterexample9BareReaders(_StateDir):
    """Supervisor cid 7142911506: bare readers must not see an empty ledger.

    A lock-free reader is the normal case -- ``reconcile``, ``sessions_ledger
    load``, the context gate. Under direction B the writer guarantees the
    format, so these readers need no lock; what they must never do is turn a
    swap window into "no keys", which ``reconcile`` reports as every key being
    missing.
    """

    def test_a_lock_free_reader_never_sees_an_empty_or_torn_map(self):
        """Every successful read is one whole version: never empty, never torn.

        The reader opens, reads and closes (that is the production shape -- see
        ``session_map_io.read_json``). If a swap is in flight the open fails,
        which is reported as "unreadable" and is *not* the same answer as "no
        keys". A write refused because a reader is mid-open is also allowed here:
        direction B says that must be loud, and a bind that fails loudly is not
        a silent loss.
        """
        import threading

        stats = {"ok": 0, "unreadable": 0, "empty": 0, "torn": 0, "refused": 0}
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                try:
                    raw = self.sessions.read_text(encoding="utf-8")
                except OSError:
                    stats["unreadable"] += 1
                    continue
                if not raw:
                    stats["empty"] += 1
                    continue
                try:
                    json.loads(raw)
                except ValueError:
                    stats["torn"] += 1
                    continue
                stats["ok"] += 1

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            for n in range(60):
                try:
                    session_map_store.write_atomic(
                        self.sessions,
                        json.dumps({"pre": {"id": FULL_SID}, "n": n}))
                except session_map_store.BrokerError:
                    stats["refused"] += 1
        finally:
            stop.set()
            thread.join(timeout=5)
        self.assertGreater(stats["ok"], 0)
        self.assertEqual(stats["empty"], 0, "裸读读到了空台账")
        self.assertEqual(stats["torn"], 0, "裸读读到了半写")
        # Whatever was refused must have left the file parseable.
        self.assertEqual(json.loads(self.sessions.read_text(encoding="utf-8"))["pre"]["id"],
                         FULL_SID)

    def test_read_json_separates_unreadable_from_empty(self):
        from super_agent import session_map_io

        self.sessions.write_text("{}", encoding="utf-8")
        self.assertEqual(session_map_io.read_json(self.sessions), {},
                         "空文件必须报 {} 而不是 None")
        self.sessions.write_text("{ broken", encoding="utf-8")
        self.assertIsNone(session_map_io.read_json(self.sessions))
        self.sessions.unlink()
        self.assertIsNone(session_map_io.read_json(self.sessions))

    def test_reconcile_does_not_invent_missing_keys_under_concurrent_writes(self):
        """The reported failure: an unreadable ledger made every key 'missing'."""
        import threading

        from super_agent import session_map_io

        self.sessions.write_text(
            json.dumps({"a": {"id": FULL_SID}, "b": {"id": FULL_SID}}),
            encoding="utf-8")
        events = self.state / "events.jsonl"
        events.write_text("", encoding="utf-8")

        stop = threading.Event()
        seen_empty = []

        def writer():
            n = 0
            while not stop.is_set():
                n += 1
                session_map_store.write_atomic(
                    self.sessions,
                    json.dumps({"a": {"id": FULL_SID}, "b": {"id": FULL_SID},
                                "n": n}))

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        from super_agent import session_events

        try:
            for _ in range(40):
                text = session_map_io.read_sessions_text(self.sessions)
                if text is None:
                    seen_empty.append("unreadable")
                elif not session_events.load_sessions_ledger(self.sessions):
                    seen_empty.append("empty")
        finally:
            stop.set()
            thread.join(timeout=5)
        self.assertEqual(seen_empty, [], "reconcile 的读点出现空/不可读台账")
        self.assertEqual(session_map_io.read_json(self.sessions)["a"]["id"], FULL_SID)

    def test_a_new_lock_free_read_point_still_cannot_see_a_half_write(self):
        """Counterexample 3: a fresh ``read_text`` point, no lock, no torn read."""
        import threading

        stop = threading.Event()
        torn = []
        big = {"pad": "x" * 4000}

        def writer():
            n = 0
            while not stop.is_set():
                n += 1
                payload = dict(big, n=n)
                session_map_store.write_atomic(self.sessions, json.dumps(payload))

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        try:
            for _ in range(200):
                try:
                    raw = self.sessions.read_text(encoding="utf-8")
                except OSError:
                    continue
                if not raw:
                    torn.append("empty")
                    continue
                try:
                    json.loads(raw)
                except ValueError:
                    torn.append("torn")
        finally:
            stop.set()
            thread.join(timeout=5)
        self.assertEqual(torn, [], f"新增裸读点读到半写：{torn[:5]}")




class Counterexample10UnreadableLedger(_StateDir):
    """QA 7145879381: an unreadable ledger must not be reported as agreement."""

    def _reconcile(self, sessions_text: str, live: set[str]):
        from super_agent import session_events

        events = self.state / "events.jsonl"
        events.write_text("", encoding="utf-8")
        self.sessions.write_text(sessions_text, encoding="utf-8")
        return session_events.reconcile(events, self.sessions, server_sids=live)

    def test_an_unreadable_ledger_is_not_ok(self):
        report = self._reconcile("{ broken", {"session_x"})
        self.assertTrue(report["ledger_error"], "不可读台账没有留下原因")
        self.assertFalse(report["ok"], "不可读台账被报成 ok")

    def test_an_absent_ledger_reports_the_problem(self):
        """A missing map is reported, not silently treated as "no keys"."""
        from super_agent import session_events

        events = self.state / "events.jsonl"
        events.write_text("", encoding="utf-8")
        self.sessions.unlink()          # setUp created it; this case needs it gone
        report = session_events.reconcile(events, self.sessions, server_sids={"session_x"})
        self.assertTrue(report["ledger_error"])

    def test_a_readable_ledger_has_no_error(self):
        """Readable: the readable flag is clean; drift is decided by the diff."""
        report = self._reconcile(json.dumps({"a": {"id": FULL_SID}}), {FULL_SID})
        self.assertEqual(report["ledger_error"], "")
        self.assertNotIn("ledger_error", report["diff"])

    def test_an_absent_map_is_not_retried(self):
        """QA P0-1: the hot path must not sleep on a file that never existed."""
        import time as _time

        from super_agent import session_map_io

        missing = self.state / "never-created.json"
        start = _time.perf_counter()
        for _ in range(50):
            self.assertIsNone(session_map_io.read_text_retry(missing))
        elapsed = _time.perf_counter() - start
        self.assertLess(elapsed, 0.5, f"缺失文件重试过慢：50 次 {elapsed:.2f}s")



if __name__ == "__main__":
    unittest.main()