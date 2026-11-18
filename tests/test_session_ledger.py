"""Write side of the session state: one root, one writer, monotonic ``seq``.

The property that matters is agreement with the engine: ``bind`` writes
``issue_sessions.json`` **beside** ``sessions.json``, in the root
``session_events.resolve_session_root`` returns. A second resolution chain would
write into a directory nobody reads, and the symptom is silence - the delivery
is never matched to an assignment and nobody is told (#92).

Two writers run concurrently here: the point of the file lock is that two
processes cannot both read the last ``seq`` and then both append ``seq + 1``.

Run: ``python -m pytest tests/test_session_ledger.py -q``
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from super_agent import session_events, session_ledger

ROOT = Path(__file__).resolve().parents[1]

FULL_SID = "session_" + "a1b2c3d4" + "-1111-2222-3333-" + "444455556666"


class Paths(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self._env = dict(os.environ)
        os.environ["SESS_EVENTS_STATE_DIR"] = str(self.state)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def test_the_root_is_the_env_dir(self):
        self.assertEqual(session_ledger.root(), self.state.resolve())

    def test_issue_sessions_sits_beside_sessions_json(self):
        self.assertEqual(session_ledger.issue_sessions_path().parent,
                         session_ledger.sessions_path().parent)
        self.assertEqual(session_ledger.issue_sessions_path().parent,
                         session_ledger.root())

    def test_an_unconfigured_root_names_the_four_entries(self):
        """No level applies: the refusal lists what to configure, not a guess."""
        os.environ.pop("SESS_EVENTS_STATE_DIR", None)
        os.environ.pop(session_events.ENV_LWA_STATE, None)
        with self.assertRaises(session_ledger.SessionLedgerError) as caught:
            session_ledger.root({})
        message = str(caught.exception)
        for entry in ("SESS_EVENTS_STATE_DIR", "LWA_STATE_DIR",
                      "session_state_dir", "session_events_path"):
            self.assertIn(entry, message)

    def test_a_declared_config_root_wins_over_the_env(self):
        declared = self.state / "declared"
        self.assertEqual(session_ledger.root({"engine": {"session_state_dir": str(declared)}}),
                         declared.resolve())


class Emit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self._env = dict(os.environ)
        os.environ["SESS_EVENTS_STATE_DIR"] = str(self.state)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def rows(self):
        path = self.state / "events.jsonl"
        return [json.loads(line) for line in path.read_text("utf-8").splitlines()]

    def test_emit_appends_and_seq_increases(self):
        first = session_ledger.emit("session.bound", issue=7, role="qa", sid=FULL_SID)
        second = session_ledger.emit("session.bound", issue=8, role="worker", sid=FULL_SID)
        self.assertEqual(second["seq"], first["seq"] + 1)
        self.assertEqual([r["seq"] for r in self.rows()], [1, 2])
        self.assertEqual(self.rows()[0]["type"], "session.bound")

    def test_a_field_outside_the_whitelist_is_refused(self):
        with self.assertRaises(session_ledger.SessionLedgerError):
            session_ledger.emit("session.bound", body="a comment body")
        self.assertFalse((self.state / "events.jsonl").is_file())

    def test_two_processes_do_not_share_a_seq(self):
        script = (
            "import sys; sys.path.insert(0, %r)\n"
            "from super_agent import session_ledger\n"
            "for _ in range(5):\n"
            "    session_ledger.emit('session.created', sid=%r)\n" % (str(ROOT), FULL_SID)
        )
        env = dict(os.environ)
        procs = [subprocess.Popen([sys.executable, "-c", script], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for _ in range(3)]
        for proc in procs:
            proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0)
        seqs = [row["seq"] for row in self.rows()]
        self.assertEqual(len(seqs), 15)
        self.assertEqual(len(set(seqs)), 15, "duplicate seq from two writers")
        self.assertEqual(seqs, sorted(seqs))


class Bind(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self._env = dict(os.environ)
        os.environ["SESS_EVENTS_STATE_DIR"] = str(self.state)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def test_registering_a_key_is_idempotent(self):
        first = session_ledger.register_session_key("qa-demo-20261119", FULL_SID)
        second = session_ledger.register_session_key("qa-demo-20261119", FULL_SID)
        self.assertIn("已登记", first)
        self.assertIn("已登记", second)
        data = json.loads((self.state / "sessions.json").read_text("utf-8"))
        self.assertEqual(list(data), ["qa-demo-20261119"])

    def test_a_fixed_session_is_recognised_by_name_only(self):
        path = self.state / "sessions.json"
        path.write_text(json.dumps({
            "qa": {"id": "session_" + "1" * 8 + "-1111-2222-3333-" + "1" * 12},
            "pm-demo-abc12345": {"id": "session_" + "2" * 8 + "-1111-2222-3333-" + "2" * 12},
            "wk-demo-20261119": {"id": "session_" + "3" * 8 + "-1111-2222-3333-" + "3" * 12},
        }), encoding="utf-8")
        fixed = session_ledger.fixed_session_ids(
            lambda name: name == "qa" or name.startswith("pm-"))
        self.assertEqual(set(fixed.values()), {"qa", "pm-demo-abc12345"})

    def test_sid_problem_names_the_shape(self):
        self.assertEqual(session_ledger.sid_problem(FULL_SID), "")
        problem = session_ledger.sid_problem("session_a8fd")
        self.assertIn("session_a8fd", problem)
        self.assertIn("期望形态", problem)


if __name__ == "__main__":
    unittest.main()
