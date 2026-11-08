"""L1 tests for ``super_agent/session_health.py`` (ISS-25 item 2).

The port's whole point is that it no longer knows how to reach a session
client: the probe is injected. So the tests pin exactly that contract:

* an uninjected probe is red (``unknown`` + a reason naming the missing probe),
  never a silent ``ok`` and never a self-imported client;
* the three probe shapes map to ``ok`` / ``dead`` / ``unknown``, and a
  ``(None, ...)`` answer must stay ``unknown`` -- "cannot tell" is not "dead";
* an unreadable registration table is ``unknown``, a missing key is
  ``missing`` and a non-string value is ``invalid`` (config errors, not deaths);
* ``exit_of`` maps only ``ok``/``dead`` to 0/1, everything else to 2;
* an empty ``--key`` on the CLI is a parameter error (exit 3).

Every table goes under ``C:/tmp/iss25_2``; the live ``state/`` is never read
or written. Run: ``python -m pytest tests/test_session_health.py -q``
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from super_agent import session_health

TMP_ROOT = Path("C:/tmp/iss25_2")


def probe_true_unarchived(sid):
    return True, {"id": sid, "archived": False}


def probe_true_archived(sid):
    return True, {"id": sid, "archived": True}


def probe_gone(sid):
    return False, "none"


def probe_cannot_tell(sid):
    return None, "err"


def probe_raises(sid):
    raise RuntimeError("boom")


class SessionHealthCase(unittest.TestCase):
    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=str(TMP_ROOT))
        self.tmp = Path(self._tmp.name)
        self.table = self.tmp / "sessions.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_table(self, payload) -> str:
        self.table.write_text(json.dumps(payload), encoding="utf-8")
        return str(self.table)

    def check(self, key="k", payload=None, **kwargs):
        sessions = self.write_table({"k": {"id": "session_abc"}}
                                    if payload is None else payload)
        return session_health.check_key(key, sessions, **kwargs)


class NoProbe(SessionHealthCase):
    def test_uninjected_probe_is_unknown_and_says_so(self):
        res = self.check(probe=None)
        self.assertEqual(res["state"], "unknown")
        self.assertIn("未注入", res["reason"])

    def test_module_default_probe_is_none(self):
        self.assertIsNone(session_health.DEFAULT_PROBE)
        self.assertEqual(session_health.DEFAULT_KEY, "")

    def test_uninjected_probe_wins_over_module_default(self):
        with mock.patch.object(session_health, "DEFAULT_PROBE", None):
            res = self.check()
        self.assertEqual(res["state"], "unknown")

    def test_module_default_probe_is_used_when_not_passed(self):
        with mock.patch.object(session_health, "DEFAULT_PROBE", probe_true_unarchived):
            res = self.check()
        self.assertEqual(res["state"], "ok")

    def test_module_never_imports_a_session_client(self):
        src = Path(session_health.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import sess", src)


class ThreeStates(SessionHealthCase):
    def test_live_unarchived_is_ok(self):
        res = self.check(probe=probe_true_unarchived)
        self.assertEqual(res["state"], "ok")
        self.assertIs(res["archived"], False)
        self.assertEqual(res["sid"], "session_abc")

    def test_archived_is_dead(self):
        res = self.check(probe=probe_true_archived)
        self.assertEqual(res["state"], "dead")
        self.assertIs(res["archived"], True)

    def test_server_40401_is_dead(self):
        res = self.check(probe=probe_gone)
        self.assertEqual(res["state"], "dead")
        self.assertEqual(res["detail"], "none")

    def test_cannot_tell_is_unknown_not_dead(self):
        res = self.check(probe=probe_cannot_tell)
        self.assertEqual(res["state"], "unknown")
        self.assertNotEqual(res["state"], "dead")

    def test_probe_exception_is_unknown_not_dead(self):
        res = self.check(probe=probe_raises)
        self.assertEqual(res["state"], "unknown")
        self.assertIn("探测异常", res["reason"])

    def test_true_with_non_dict_detail_is_unknown(self):
        res = self.check(probe=lambda sid: (True, "weird"))
        self.assertEqual(res["state"], "unknown")


class TableProblems(SessionHealthCase):
    def test_missing_file_is_unknown(self):
        res = session_health.check_key("k", str(self.tmp / "nope.json"),
                                       probe=probe_true_unarchived)
        self.assertEqual(res["state"], "unknown")
        self.assertNotEqual(res["state"], "dead")

    def test_broken_json_is_unknown(self):
        self.table.write_text("{not json", encoding="utf-8")
        res = session_health.check_key("k", str(self.table), probe=probe_true_unarchived)
        self.assertEqual(res["state"], "unknown")

    def test_top_level_not_object_is_unknown(self):
        res = self.check(payload=["k"], probe=probe_true_unarchived)
        self.assertEqual(res["state"], "unknown")

    def test_missing_key_is_missing(self):
        res = self.check(payload={"other": {"id": "x"}}, probe=probe_true_unarchived)
        self.assertEqual(res["state"], "missing")
        self.assertEqual(res["sid"], "")

    def test_non_string_value_is_invalid(self):
        res = self.check(payload={"k": {"x": 1}}, probe=probe_true_unarchived)
        self.assertEqual(res["state"], "invalid")

    def test_blank_id_is_invalid(self):
        res = self.check(payload={"k": "   "}, probe=probe_true_unarchived)
        self.assertEqual(res["state"], "invalid")

    def test_bare_string_value_is_accepted(self):
        res = self.check(payload={"k": " session_xyz "}, probe=probe_true_unarchived)
        self.assertEqual(res["state"], "ok")
        self.assertEqual(res["sid"], "session_xyz")


class ExitCodes(unittest.TestCase):
    def test_exit_of_mapping(self):
        self.assertEqual(session_health.exit_of("ok"), 0)
        self.assertEqual(session_health.exit_of("dead"), 1)
        for state in ("unknown", "missing", "invalid", "whatever"):
            self.assertEqual(session_health.exit_of(state), 2)

    def test_constants(self):
        self.assertEqual((session_health.EXIT_OK, session_health.EXIT_DEAD,
                          session_health.EXIT_UNKNOWN, session_health.EXIT_PARAM),
                         (0, 1, 2, 3))


class Cli(SessionHealthCase):
    def run_cli(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = session_health.main(argv)
        return code, out.getvalue()

    def test_empty_key_is_param_error(self):
        code, _ = self.run_cli(["--key", ""])
        self.assertEqual(code, session_health.EXIT_PARAM)

    def test_empty_keys_list_is_param_error(self):
        code, _ = self.run_cli(["--keys", " , "])
        self.assertEqual(code, session_health.EXIT_PARAM)

    def test_uninjected_probe_exits_unknown(self):
        sessions = self.write_table({"k": {"id": "session_abc"}})
        with mock.patch.object(session_health, "DEFAULT_PROBE", None):
            code, _ = self.run_cli(["--key", "k", "--sessions", sessions])
        self.assertEqual(code, session_health.EXIT_UNKNOWN)

    def test_dead_exits_one(self):
        sessions = self.write_table({"k": {"id": "session_abc"}})
        with mock.patch.object(session_health, "DEFAULT_PROBE", probe_gone):
            code, _ = self.run_cli(["--key", "k", "--sessions", sessions])
        self.assertEqual(code, session_health.EXIT_DEAD)

    def test_ok_exits_zero(self):
        sessions = self.write_table({"k": {"id": "session_abc"}})
        with mock.patch.object(session_health, "DEFAULT_PROBE", probe_true_unarchived):
            code, out = self.run_cli(["--key", "k", "--sessions", sessions])
        self.assertEqual(code, session_health.EXIT_OK)
        self.assertIn("OK", out)

    def test_json_output_is_single_object(self):
        sessions = self.write_table({"k": {"id": "session_abc"}})
        with mock.patch.object(session_health, "DEFAULT_PROBE", probe_true_unarchived):
            _, out = self.run_cli(["--key", "k", "--sessions", sessions, "--json"])
        self.assertEqual(json.loads(out)["state"], "ok")


class EnvOverride(SessionHealthCase):
    def test_new_env_name_wins(self):
        other = self.tmp / "other.json"
        other.write_text(json.dumps({"k": {"id": "session_env"}}), encoding="utf-8")
        with mock.patch.dict(os.environ,
                             {session_health.ENV_SESSIONS: str(other)}, clear=False):
            res = session_health.check_key("k", probe=probe_true_unarchived)
        self.assertEqual(res["sid"], "session_env")

    def test_legacy_env_name_still_works(self):
        other = self.tmp / "legacy.json"
        other.write_text(json.dumps({"k": {"id": "session_legacy"}}), encoding="utf-8")
        env = {session_health.ENV_SESSIONS: "",
               session_health.ENV_SESSIONS_LEGACY: str(other)}
        with mock.patch.dict(os.environ, env, clear=False):
            res = session_health.check_key("k", probe=probe_true_unarchived)
        self.assertEqual(res["sid"], "session_legacy")

    def test_explicit_argument_beats_env(self):
        other = self.tmp / "other.json"
        other.write_text(json.dumps({"k": {"id": "session_env"}}), encoding="utf-8")
        explicit = self.write_table({"k": {"id": "session_explicit"}})
        with mock.patch.dict(os.environ,
                             {session_health.ENV_SESSIONS: str(other)}, clear=False):
            res = session_health.check_key("k", explicit, probe=probe_true_unarchived)
        self.assertEqual(res["sid"], "session_explicit")


if __name__ == "__main__":
    unittest.main()
