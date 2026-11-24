"""#129: one writer for ``sessions.json`` and ``issue_sessions.json``.

The two maps are read by the engine and written by several CLIs. Every writer
used to do the same thing by hand -- read the whole file, mutate one key, write
the whole file back -- so two processes writing different keys could each drop
the other's row. Nothing reports that: the key is simply gone, and the symptom
is a session that is never told about its issue (#92, #124 C).

What this file pins:

* **counterexample 1** -- two processes writing different keys lose neither;
  the two maps read back identically from both.
* **counterexample 2** -- ``replace_all_for_migration`` refuses while the
  launcher or any legacy writer is alive, and refuses when the operator has not
  attested that the writers were stopped (absence is not proof).
* **counterexample 3** -- no production path still calls the old whole-file
  ``save`` / ``save_issue_sessions`` aliases (AST check).
* **counterexample 4** -- no production writer in the session-map set calls
  ``write_text`` (AST check); the broker is the only writer.

Run: ``python -m pytest tests/test_session_broker.py -q``
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "super_agent"

FULL_SID = "session_" + "a1b2c3d4" + "-1111-2222-3333-" + "444455556666"

# Every module that used to write one of the two maps by hand. All of them now
# go through ``session_broker``; a ``write_text`` here means one went back.
PRODUCTION_WRITERS = (
    PKG / "session_ledger.py",
    PKG / "sessions_ledger.py",
    PKG / "issue_cli.py",
    PKG / "at_cli.py",
)

OLD_ALIASES = {"save", "save_issue_sessions"}


def audit_source(source: str, name: str = "<memory>") -> list[str]:
    """AST check: old whole-file aliases and direct ``write_text`` in a writer.

    Returns one message per violation. ``write_text`` is refused outright in
    these modules: an atomic save belongs to the broker, and a direct write is
    how the read-modify-write race comes back.
    """
    tree = ast.parse(source)
    problems: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        receiver = getattr(func.value, "id", getattr(func.value, "attr", ""))
        if func.attr in OLD_ALIASES and "ledger" in str(receiver):
            problems.append(f"{name}:{node.lineno} 旧整覆盖别名 {receiver}.{func.attr}()")
        if func.attr == "write_text":
            problems.append(f"{name}:{node.lineno} 直接 write_text（应走 broker）")
    return problems


class _StateDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self._env = dict(os.environ)
        os.environ["SESS_EVENTS_STATE_DIR"] = str(self.state)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def read(self, name: str) -> dict:
        return json.loads((self.state / name).read_text(encoding="utf-8"))


class Counterexample1ConcurrentKeys(_StateDir):
    """Two processes, different keys: neither key disappears."""

    def test_two_processes_writing_different_keys_lose_nothing(self):
        from super_agent import session_broker

        gate = self.state / "go"
        script = (
            "import os, sys, time\n"
            "sys.path.insert(0, %r)\n"
            "from super_agent import session_broker\n"
            "key = sys.argv[1]\n"
            "gate = %r\n"
            "while not os.path.exists(gate):\n"
            "    time.sleep(0.005)\n"
            "for i in range(25):\n"
            "    session_broker.register_key('%%s-%%02d' %% (key, i), %r)\n"
            % (str(ROOT), str(gate), FULL_SID))
        procs = [subprocess.Popen([sys.executable, "-c", script, name],
                                  env=dict(os.environ),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for name in ("alpha", "beta")]
        gate.write_text("go", encoding="utf-8")
        for proc in procs:
            _out, err = proc.communicate(timeout=180)
            self.assertEqual(proc.returncode, 0, err.decode("utf-8", "replace"))
        sessions = self.read("sessions.json")
        expected = {f"{name}-{i:02d}" for name in ("alpha", "beta") for i in range(25)}
        self.assertEqual(expected - set(sessions), set(), "并发写丢了键")
        self.assertEqual(session_broker.read_sessions(), sessions)

    def test_both_maps_are_written_under_one_lock(self):
        """A bind writes ``issue_sessions`` and ``sessions`` in one critical section."""
        from super_agent import session_broker

        session_broker.bind_role("proj#7", "qa", FULL_SID, name="qa-proj-20261125")
        self.assertEqual(self.read("issue_sessions.json")["proj#7"]["qa"]["session"],
                         FULL_SID)
        self.assertEqual(self.read("sessions.json")["qa-proj-20261125"]["id"], FULL_SID)
        self.assertEqual(session_broker.read_issue_sessions()["proj#7"]["qa"]["session"],
                         self.read("issue_sessions.json")["proj#7"]["qa"]["session"])


class Counterexample2MigrationMutex(_StateDir):
    """Whole-map migration is refused while anything can still write."""

    def setUp(self):
        super().setUp()
        self.sessions = self.state / "sessions.json"
        self.issue = self.state / "issue_sessions.json"
        self.sessions.write_text(json.dumps({"a": {"id": FULL_SID}}), encoding="utf-8")
        self.issue.write_text(json.dumps({}), encoding="utf-8")

    def test_a_live_launcher_refuses_even_with_full_attestation(self):
        from super_agent import session_broker

        (self.state / "launcher.pid").write_text(str(os.getpid()), encoding="utf-8")
        with self.assertRaises(session_broker.MigrationRefused) as caught:
            session_broker.replace_all_for_migration(
                {"b": {"id": FULL_SID}}, {}, attest=session_broker.LEGACY_WRITERS)
        self.assertIn("launcher", str(caught.exception))
        self.assertEqual(self.read("sessions.json"), {"a": {"id": FULL_SID}})

    def test_a_live_legacy_writer_lock_refuses(self):
        from super_agent import session_broker

        (self.state / "wss_bus.lock").write_text(
            json.dumps({"pid": os.getpid()}), encoding="utf-8")
        with self.assertRaises(session_broker.MigrationRefused):
            session_broker.replace_all_for_migration(
                {"b": {"id": FULL_SID}}, {}, attest=session_broker.LEGACY_WRITERS)

    def test_no_marker_at_all_still_needs_attestation(self):
        """Absence is not proof: a missing launcher.pid is not "no other writer"."""
        from super_agent import session_broker

        with self.assertRaises(session_broker.MigrationRefused) as caught:
            session_broker.replace_all_for_migration({"b": {"id": FULL_SID}}, {})
        for name in session_broker.LEGACY_WRITERS:
            self.assertIn(name, str(caught.exception))
        self.assertEqual(self.read("sessions.json"), {"a": {"id": FULL_SID}})

    def test_all_stopped_and_attested_is_allowed(self):
        from super_agent import session_broker

        result = session_broker.replace_all_for_migration(
            {"b": {"id": FULL_SID}}, {"proj#9": {}},
            attest=session_broker.LEGACY_WRITERS)
        self.assertTrue(result["replaced"])
        self.assertEqual(self.read("sessions.json"), {"b": {"id": FULL_SID}})
        self.assertEqual(self.read("issue_sessions.json"), {"proj#9": {}})


class Counterexample3OldAlias(_StateDir):
    """The old whole-file aliases are gone from the production surface."""

    def test_no_old_alias_is_left_on_either_module(self):
        from super_agent import session_ledger, sessions_ledger

        self.assertFalse(hasattr(session_ledger, "save_issue_sessions"))
        self.assertFalse(hasattr(sessions_ledger, "save"))

    def test_the_ast_check_finds_the_alias_it_should_red(self):
        planted = "def f(db):\n    sessions_ledger.save(db)\n"
        self.assertTrue(audit_source(planted, "planted.py"))

    def test_production_writers_do_not_call_the_alias(self):
        for path in PRODUCTION_WRITERS:
            source = path.read_text(encoding="utf-8")
            self.assertEqual(audit_source(source, path.name), [])


class Counterexample4DirectWrite(_StateDir):
    """No session-map writer touches the file directly."""

    def test_the_ast_check_finds_write_text(self):
        planted = "def f(p, data):\n    p.write_text(data)\n"
        self.assertTrue(audit_source(planted, "planted.py"))

    def test_production_writers_have_no_write_text(self):
        for path in PRODUCTION_WRITERS:
            self.assertEqual(audit_source(path.read_text(encoding="utf-8"), path.name), [])

    def test_a_save_reads_back_what_it_wrote(self):
        """Read-back is part of the write: a mismatch is an error, not silence."""
        from super_agent import session_broker

        session_broker.set_key("pm-proj", FULL_SID)
        self.assertEqual(session_broker.read_sessions()["pm-proj"]["id"], FULL_SID)


if __name__ == "__main__":
    unittest.main()
