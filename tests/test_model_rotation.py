"""L1 tests for ``super_agent.pick_worker_model`` (ISS-32 rule: workers rotate; #106).

The module has two jobs and the tests pin both, because each one is a rule that
cannot be checked by reading the file once:

* **the worker rotation** -- ``wk-*`` workers draw their model from the
  ``rotation`` table, never from a named profile (user ruling 2026-10-31). A
  planted ``profiles.worker`` key must not hijack it.
* **the named profile** -- ``qa``/``pm``/``steward``/... come from ``profiles``.
  There is no ``worker`` key, so ``--profile worker`` falls back on purpose.

Every read falls back to the built-in default when the table is missing or
malformed: a broken config must change no behaviour, only print a WARN. These
tests cover that too -- it is the property that keeps a bad table from taking
dispatch down.

Isolation: the cursor, the cooldown file and the profile table are all
redirected at a temp directory through the module's own hooks, and the profile
table path through ``$SAFKC_MODEL_PROFILES`` so the real
``config/model_profiles.json`` is never touched.

Run: ``python -m pytest tests/test_model_rotation.py -q``
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from super_agent import agent_config, pick_worker_model as picker


class _Isolated(unittest.TestCase):
    """Temp config + temp state root, and the real ones restored afterwards."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cfg = self.root / "model_profiles.json"
        self.state = self.root / "state"
        self.state.mkdir()

        self._saved_env = os.environ.get(agent_config.ENV_FILE)
        self._orig_root = picker._state_root
        self._orig_profiles = picker._profiles_path
        picker._state_root = lambda: str(self.state)
        picker._profiles_path = lambda: str(self.cfg)
        os.environ[agent_config.ENV_FILE] = str(self.root / "no-agent.json")
        agent_config._reset_cache()

    def tearDown(self) -> None:
        picker._state_root = self._orig_root
        picker._profiles_path = self._orig_profiles
        if self._saved_env is None:
            os.environ.pop(agent_config.ENV_FILE, None)
        else:
            os.environ[agent_config.ENV_FILE] = self._saved_env
        agent_config._reset_cache()
        self._tmp.cleanup()

    def table(self, profiles=None, rotation=None) -> dict:
        return {"schema_version": 1,
                "rotation": rotation or [{"model": "cb-deepseek-v4.1-flash",
                                          "effort": "high"}],
                "profiles": profiles or {}}

    def write(self, data) -> None:
        if isinstance(data, str):
            self.cfg.write_text(data, encoding="utf-8")
        else:
            self.cfg.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def run_main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = picker.main(list(argv))
        return rc, out.getvalue().strip(), err.getvalue().strip()

    def cursor(self) -> dict:
        return json.loads((self.state / "model_rotation.json").read_text(encoding="utf-8"))


class Fallback(_Isolated):
    """A missing or broken table changes no behaviour -- only a WARN."""

    def test_a_missing_table_falls_back_with_a_warning(self):
        rc, out, err = self.run_main()
        self.assertEqual(rc, 0)
        self.assertIn("--model cb-deepseek-v4.1-flash", out)
        self.assertIn("--effort high", out)
        self.assertIn("[WARN]", err)

    def test_a_broken_table_falls_back_without_raising(self):
        for bad in ("{not json", "[]",
                    json.dumps({"rotation": [], "profiles": {}}),
                    json.dumps({"schema_version": 1, "rotation": [{"model": "x"}],
                                "profiles": {}})):
            self.write(bad)
            rc, out, err = self.run_main("--peek")
            self.assertEqual(rc, 0, bad)
            self.assertIn("--model cb-deepseek-v4.1-flash", out, bad)
            self.assertIn("[WARN]", err, bad)

    def test_a_configured_table_is_actually_read(self):
        """Counter-example to the fallback: a real table must win."""
        self.write(self.table({"qa": {"model": "qa-model", "effort": "medium"}}))
        rc, out, err = self.run_main("--profile", "qa")
        self.assertEqual(rc, 0)
        self.assertIn("--model qa-model", out)
        self.assertEqual(err, "", "a good table must not warn")


class Rotation(_Isolated):
    """The cursor advances one slot per call, and ``--peek`` does not."""

    def two_slots(self) -> dict:
        return self.table(rotation=[{"model": "first-model", "effort": "low"},
                                    {"model": "second-model", "effort": "high"}])

    def test_consecutive_calls_advance(self):
        self.write(self.two_slots())
        _, first, _ = self.run_main()
        _, second, _ = self.run_main()
        self.assertIn("--model first-model", first)
        self.assertIn("--model second-model", second)

    def test_the_cursor_wraps(self):
        self.write(self.two_slots())
        for _ in range(2):
            self.run_main()
        self.assertEqual(self.cursor()["index"], 0)

    def test_peek_does_not_advance(self):
        self.write(self.two_slots())
        _, first, _ = self.run_main("--peek")
        _, second, _ = self.run_main("--peek")
        self.assertEqual(first, second)
        self.assertFalse((self.state / "model_rotation.json").exists(),
                         "--peek must not write a cursor at all")
        _, after, _ = self.run_main()
        self.assertEqual(first, after, "the normal call must start where peek did")

    def test_a_planted_worker_profile_does_not_hijack_the_rotation(self):
        self.write(self.table({"worker": {"model": "PROBE-HIJACK", "effort": "max"}}))
        _, out, _ = self.run_main("--peek")
        self.assertIn("--model cb-deepseek-v4.1-flash", out)
        self.assertNotIn("PROBE-HIJACK", out)

    def test_a_cooling_model_is_skipped(self):
        self.write(self.two_slots())
        (self.state / "model_cooldown.json").write_text(
            json.dumps({"first-model": "2999-01-01T00:00:00"}), encoding="utf-8")
        _, out, err = self.run_main("--peek")
        self.assertIn("--model second-model", out)
        self.assertIn("first-model", err)

    def test_the_log_records_the_pick(self):
        self.write(self.two_slots())
        self.run_main("--log", "wk-probe-20261120")
        line = (self.state / "model_rotation.log").read_text(encoding="utf-8").strip()
        self.assertIn("wk-probe-20261120", line)
        self.assertIn("first-model", line)


class Profile(_Isolated):
    """``--profile`` reads the table; ``worker`` has no entry on purpose."""

    def test_a_named_profile_reads_the_table(self):
        self.write(self.table({"qa": {"model": "qa-model", "effort": "medium"}}))
        rc, out, err = self.run_main("--profile", "qa")
        self.assertEqual(rc, 0)
        self.assertIn("--model qa-model", out)
        self.assertIn("--effort medium", out)
        self.assertEqual(err, "")

    def test_worker_falls_back_because_the_table_has_no_worker_key(self):
        self.write(self.table({"qa": {"model": "qa-model", "effort": "medium"}}))
        rc, out, err = self.run_main("--profile", "worker")
        self.assertEqual(rc, 0)
        self.assertIn(f"--model {picker.FALLBACK_PROFILE['model']}", out)
        self.assertIn("worker", err)

    def test_a_profile_does_not_touch_the_rotation_cursor(self):
        self.write(self.table({"qa": {"model": "qa-model", "effort": "medium"}}))
        self.run_main("--profile", "qa")
        self.assertFalse((self.state / "model_rotation.json").exists())


class Json(unittest.TestCase):
    """The output shape ``pm.md`` parses: model, effort, index."""

    def test_the_json_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            orig_root, orig_profiles = picker._state_root, picker._profiles_path
            picker._state_root = lambda: str(state)
            picker._profiles_path = lambda: str(state / "missing.json")
            try:
                out = io.StringIO()
                with redirect_stdout(out), redirect_stderr(io.StringIO()):
                    picker.main(["--json"])
            finally:
                picker._state_root, picker._profiles_path = orig_root, orig_profiles
        got = json.loads(out.getvalue().strip())
        self.assertEqual(set(got), {"model", "effort", "index"})
        self.assertIsInstance(got["index"], int)

    def test_the_profile_json_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            cfg = state / "model_profiles.json"
            cfg.write_text(json.dumps({"schema_version": 1,
                                       "rotation": [{"model": "m", "effort": "low"}],
                                       "profiles": {"qa": {"model": "qa-model",
                                                           "effort": "medium"}}}),
                           encoding="utf-8")
            orig_root, orig_profiles = picker._state_root, picker._profiles_path
            picker._state_root = lambda: str(state)
            picker._profiles_path = lambda: str(cfg)
            try:
                out = io.StringIO()
                with redirect_stdout(out), redirect_stderr(io.StringIO()):
                    picker.main(["--profile", "qa", "--json"])
            finally:
                picker._state_root, picker._profiles_path = orig_root, orig_profiles
        got = json.loads(out.getvalue().strip())
        self.assertEqual(got["profile"], "qa")
        self.assertEqual(got["source"], "config")
        self.assertEqual(got["model"], "qa-model")


class StateRoot(unittest.TestCase):
    """The cursor root is the session root, not a path this module invents."""

    def test_it_follows_the_session_root(self):
        from super_agent import sess_events
        self.assertEqual(Path(picker._state_root()), sess_events.resolve_root())

    def test_an_unconfigured_root_still_answers(self):
        """Counter-example: a fresh clone must not refuse to dispatch."""
        from super_agent import session_events, sess_events
        saved = os.environ.get("SESS_EVENTS_STATE_DIR")
        os.environ["SESS_EVENTS_STATE_DIR"] = ""
        agent_config._reset_cache()
        orig = sess_events.resolve_root

        def boom(*a, **k):
            raise session_events.SessionStreamNotConfigured("no root")

        sess_events.resolve_root = boom
        try:
            got = picker._state_root()
        finally:
            sess_events.resolve_root = orig
            if saved is None:
                os.environ.pop("SESS_EVENTS_STATE_DIR", None)
            else:
                os.environ["SESS_EVENTS_STATE_DIR"] = saved
            agent_config._reset_cache()
        self.assertTrue(got.endswith("state"), got)


if __name__ == "__main__":
    unittest.main()
