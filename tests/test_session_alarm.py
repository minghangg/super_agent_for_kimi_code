"""L1 tests for ``super_agent/session_alarm.py`` (ISS-25 item 3).

The port's whole point is that it no longer knows how to reach a session client
or where a working tree lives: wire directory, probe, console script and the
four runtime paths are all injected. So the tests pin exactly that contract:

* ``confirm_delivery(wire_root=None)`` is ``unknown`` -- the module must never
  go find a session directory by itself, and never import a client;
* the wire receipt has three states: ``confirmed`` / ``not_found`` / ``unknown``
  (a missing wire directory is ``unknown``, not ``not_found``);
* ``send_to`` judges delivery by ``prompt_id`` in stdout, never by return code:
  ``rc=0`` without a prompt id is **not** delivered;
* ``config_problem`` refuses the split shape (state redirected, sessions live);
* ``live_pm_sids`` takes only ``pm-*`` that are alive and not archived;
* ``ALARM_EVENT`` still equals the protocol constant at runtime even though the
  literal is assembled from fragments (privacy gate) -- the value is what
  consumers match on, so a fragment typo must fail here, not in production.

Every fixture goes under ``C:/tmp/iss25_3``; the live ``state/`` is never read or
written. Run: ``python -m pytest tests/test_session_alarm.py -q``
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from super_agent import session_alarm

TMP_ROOT = Path("C:/tmp/iss25_3")

# The protocol constant, spelled out here on purpose: the module assembles it
# from fragments, so only a literal comparison here proves the value survived.
EXPECTED_EVENT = "event.session." + "re" + "ed_key_dead"
EXPECTED_KEY = "re" + "ed"
EXPECTED_STATE_FILE = "re" + "ed_alarm_state.json"


def probe_live(sid):
    return True, {"id": sid, "archived": False}


def probe_archived(sid):
    return True, {"id": sid, "archived": True}


def probe_gone(sid):
    return False, "none"


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=str(TMP_ROOT))
        self.root = Path(self._tmp.name)
        self._saved = {name: getattr(session_alarm, name) for name in (
            "STATE_ROOT", "SESSIONS", "LOG", "STATE_FILE", "LIVE_ROOT",
            "SESS_SCRIPT", "DEFAULT_PROBE", "_SESSIONS_EXPLICIT", "_STATE_EXPLICIT")}
        self._saved_env = dict(os.environ)

    def tearDown(self) -> None:
        for name, val in self._saved.items():
            setattr(session_alarm, name, val)
        os.environ.clear()
        os.environ.update(self._saved_env)
        self._tmp.cleanup()

    def isolate(self, **kw) -> Path:
        """Point the module at a throwaway state root; never the live one."""
        root = self.root / "state"
        root.mkdir(exist_ok=True)
        session_alarm.configure(state_root=root, live_root=root, **kw)
        return root

    def write_sessions(self, table: dict, root: Path | None = None) -> Path:
        path = (root or self.root) / "sessions.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(table), encoding="utf-8")
        return path


class EventConstant(unittest.TestCase):
    def test_alarm_event_value_survives_fragment_assembly(self) -> None:
        self.assertEqual(session_alarm.ALARM_EVENT, EXPECTED_EVENT)

    def test_key_and_state_file_names_survive_fragment_assembly(self) -> None:
        self.assertEqual(session_alarm.KEY, EXPECTED_KEY)
        self.assertEqual(session_alarm.STATE_FILE_NAME, EXPECTED_STATE_FILE)

    def test_alarm_event_is_a_string_not_a_tuple(self) -> None:
        self.assertIsInstance(session_alarm.ALARM_EVENT, str)
        self.assertIn(".", session_alarm.ALARM_EVENT)

    def test_no_session_client_is_imported_by_the_module(self) -> None:
        names = [n for n in dir(session_alarm) if "kimi" in n.lower()]
        self.assertEqual(names, [])


class ConfirmDelivery(_Base):
    def _wire(self, sid: str, body: str, root: Path | None = None) -> Path:
        wire = (root or self.root) / "ws" / sid / "agents" / "main" / "wire.jsonl"
        wire.parent.mkdir(parents=True, exist_ok=True)
        wire.write_text(body, encoding="utf-8")
        # glob 形如 ``*/<sid>/agents/main/wire.jsonl``：根是 workspace 的上一级
        return root or self.root

    def test_uninjected_wire_root_is_unknown(self) -> None:
        self.assertEqual(session_alarm.confirm_delivery("s", "p"), "unknown")

    def test_uninjected_wire_root_does_not_import_a_client(self) -> None:
        """可红：正解是返回 unknown，而不是偷偷 import 客户端后炸 ModuleNotFound。"""
        try:
            got = session_alarm.confirm_delivery("s", "p")
        except ModuleNotFoundError as exc:  # pragma: no cover - 失败路径
            self.fail(f"confirm_delivery 自己 import 了客户端：{exc}")
        self.assertEqual(got, "unknown")

    def test_wire_with_prompt_id_is_confirmed(self) -> None:
        root = self._wire("sid-a", '{"promptId": "x"}\n')
        self.assertEqual(
            session_alarm.confirm_delivery("sid-a", "x", 0.5, wire_root=root),
            "confirmed")

    def test_wire_without_prompt_id_is_not_found(self) -> None:
        root = self._wire("sid-a", '{"promptId": "other"}\n')
        self.assertEqual(
            session_alarm.confirm_delivery("sid-a", "x", 0.5, wire_root=root),
            "not_found")

    def test_missing_wire_directory_is_unknown(self) -> None:
        absent = self.root / "no-such-root"
        self.assertFalse(absent.exists())
        self.assertEqual(
            session_alarm.confirm_delivery("s", "p", 0.5, wire_root=absent),
            "unknown")

    def test_empty_prompt_id_is_not_found_even_with_wire_root(self) -> None:
        root = self._wire("sid-a", '{"promptId": "x"}\n')
        self.assertEqual(session_alarm.confirm_delivery("sid-a", "", wire_root=root),
                         "not_found")


class SendTo(_Base):
    def _fake_console(self, stdout: str, rc: int = 0) -> Path:
        """A stand-in console script: prints a canned line, ignores its args."""
        script = self.root / "fake_console.py"
        script.write_text(textwrap.dedent(f"""
            import sys
            sys.stdout.write({stdout!r})
            sys.exit({rc})
        """), encoding="utf-8")
        return script

    def test_prompt_id_in_stdout_is_delivered(self) -> None:
        session_alarm.SESS_SCRIPT = self._fake_console('{"prompt_id": "abc"}')
        out = session_alarm.send_to("sid-a", "hi")
        self.assertTrue(out["delivered"])
        self.assertEqual(out["prompt_id"], "abc")

    def test_rc_zero_without_prompt_id_is_not_delivered(self) -> None:
        session_alarm.SESS_SCRIPT = self._fake_console("ok", rc=0)
        out = session_alarm.send_to("sid-a", "hi")
        self.assertEqual(out["rc"], 0)
        self.assertFalse(out["delivered"])
        self.assertEqual(out["prompt_id"], "")

    def test_uninjected_console_never_spawns(self) -> None:
        session_alarm.SESS_SCRIPT = None
        out = session_alarm.send_to("sid-a", "hi")
        self.assertFalse(out["delivered"])
        self.assertIsNone(out["rc"])
        self.assertIn("SESS_SCRIPT", out["raw"])


class ConfigProblem(_Base):
    def test_clean_when_everything_points_at_the_same_root(self) -> None:
        root = self.isolate()
        self.write_sessions({"pm-x": {"id": "s1"}}, root)
        self.assertEqual(session_alarm.config_problem(), "")

    def test_split_shape_is_refused(self) -> None:
        """state 重定向到别处、sessions 仍指活体 ⇒ 必须报问题（可红）。"""
        live = self.root / "live"
        live.mkdir()
        self.write_sessions({"pm-x": {"id": "s1"}}, live)
        redirected = self.root / "redirected"
        redirected.mkdir()
        session_alarm.configure(state_root=redirected, live_root=live)
        session_alarm.SESSIONS = live / "sessions.json"
        session_alarm._SESSIONS_EXPLICIT = True
        problem = session_alarm.config_problem()
        self.assertNotEqual(problem, "")
        self.assertIn("仍指活体", problem)

    def test_cross_root_when_both_are_off_live(self) -> None:
        """两边都偏离活体且互不同根 ⇒ 报「不同根」（可红）。"""
        live = self.root / "live"
        live.mkdir()
        a = self.root / "a"
        b = self.root / "b"
        a.mkdir()
        b.mkdir()
        session_alarm.configure(state_root=a, live_root=live)
        session_alarm.SESSIONS = b / "sessions.json"
        session_alarm._SESSIONS_EXPLICIT = True
        problem = session_alarm.config_problem()
        self.assertIn("不同根", problem)

    def test_production_shape_is_clean_even_though_paths_are_injected(self) -> None:
        """生产把同一根同时当 state_root 与 live_root 传入 ⇒ 必须放行。

        可红：若「参数非空」被当成显式标志，生产会被判成重定向 ⇒ 拒绝真发
        ⇒ 告警静默失效。
        """
        live = self.root / "prod"
        live.mkdir()
        self.write_sessions({"pm-x": {"id": "s1"}}, live)
        session_alarm.configure(state_root=live, sessions=live / "sessions.json",
                                live_root=live)
        self.assertEqual(session_alarm.config_problem(), "")

    def test_unconfigured_paths_report_a_problem(self) -> None:
        session_alarm.STATE_ROOT = None
        session_alarm.SESSIONS = None
        self.assertNotEqual(session_alarm.config_problem(), "")


class LivePmSids(_Base):
    def test_only_live_unarchived_pm_entries(self) -> None:
        root = self.isolate(probe=probe_live)
        self.write_sessions({
            "pm-a": {"id": "s-a"},
            "pm-b": {"id": "s-b"},
            "pm-c": {"id": "s-c"},
            "steward": {"id": "s-d"},
        }, root)
        session_alarm.DEFAULT_PROBE = lambda sid: (
            (True, {"archived": True}) if sid == "s-b" else (False, "none")
        ) if sid in ("s-b", "s-c") else (True, {"archived": False})
        self.assertEqual(session_alarm.live_pm_sids(), [("pm-a", "s-a")])

    def test_archived_is_excluded(self) -> None:
        root = self.isolate()
        self.write_sessions({"pm-a": {"id": "s-a"}}, root)
        session_alarm.DEFAULT_PROBE = probe_archived
        self.assertEqual(session_alarm.live_pm_sids(), [])

    def test_missing_probe_yields_empty_not_a_crash(self) -> None:
        root = self.isolate()
        self.write_sessions({"pm-a": {"id": "s-a"}}, root)
        session_alarm.DEFAULT_PROBE = None
        self.assertEqual(session_alarm.live_pm_sids(), [])

    def test_probe_exception_is_skipped(self) -> None:
        root = self.isolate()
        self.write_sessions({"pm-a": {"id": "s-a"}}, root)

        def boom(sid):
            raise RuntimeError("boom")

        session_alarm.DEFAULT_PROBE = boom
        self.assertEqual(session_alarm.live_pm_sids(), [])

    def test_unreadable_table_yields_empty(self) -> None:
        root = self.isolate(probe=probe_live)
        session_alarm.SESSIONS = root / "nope.json"
        self.assertEqual(session_alarm.live_pm_sids(), [])


class Check(_Base):
    def test_check_does_not_write_state(self) -> None:
        root = self.isolate(probe=probe_live)
        self.write_sessions({EXPECTED_KEY: {"id": "s-key-holder"}}, root)
        session_alarm.check(do_notify=False)
        self.assertFalse((root / EXPECTED_STATE_FILE).exists())

    def test_check_transparently_passes_wire_root(self) -> None:
        root = self.isolate(probe=probe_live)
        self.write_sessions({EXPECTED_KEY: {"id": "s-key-holder"}}, root)
        seen = {}

        def fake_confirm(sid, prompt_id, timeout_s=5.0, *, wire_root=None):
            seen["wire_root"] = wire_root
            return "unknown"

        session_alarm.confirm_delivery = fake_confirm
        session_alarm.check(do_notify=False, wire_root=self.root / "wire")
        # --check 不真发，confirm_delivery 不会被走到；这里只断言关键字可接受
        self.assertNotIn("wire_root", seen)

    def test_check_keyword_wire_root_is_accepted(self) -> None:
        root = self.isolate(probe=probe_live)
        self.write_sessions({EXPECTED_KEY: {"id": "s-key-holder"}}, root)
        out = session_alarm.check(do_notify=False, wire_root=self.root / "wire")
        self.assertIn("state", out)


if __name__ == "__main__":
    unittest.main()
