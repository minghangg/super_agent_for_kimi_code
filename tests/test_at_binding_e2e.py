"""End-to-end: ``gh_issue.bind --bind-role at`` -> engine -> questionnaire -> AT.

The one property this file exists for: **what the binding tool writes, the
engine reads, and an answer typed into the browser reaches the AT session.**

Every other test in ``test_engine_at_verdict.py`` builds its fixture by hand -
``issue_sessions.json`` with an ``{"at": {...}}`` entry written straight into a
temp directory. That is the right way to pin the engine's own rules, and it is
exactly the shape that hid the QA finding on #60: a producer that never runs in
production, with tests that stay green because they write the data themselves.

So the tests here **run the real producer**: ``tools/gh_issue.py bind`` from the
workbench, with ``SESS_EVENTS_STATE_DIR`` pointed at the test's temp root. Two
facts fall out of that, and neither is visible in a hand-written fixture:

The two sides of the key, in one place:

* ``gh_issue.bind`` writes ``<project>#<n>`` - the workbench's short project
  name, taken from its ``config/gh_projects.json``;
* this engine keys a thread by ``issuekey.thread_key`` ->
  ``<owner>/<repo>#<n>``.

The names used here are **read from that config at run time**, never written
into this file: the mapping is private workbench data, and a test that embeds it
turns a public repository into a copy of it.
* the same spelling must also reach the watchdog, or a registered AT has no
  watchdog watching it.

The workbench is located through ``LWA_ROOT`` (default ``../<private-workbench>``
beside this repository); when it is absent the producer tests **skip** loudly
rather than rewrite the binding by hand, because a silent rewrite is the failure
being tested for.

Run: ``python -m pytest tests/test_at_binding_e2e.py -q``
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from super_agent import at_serve, config, markers
from super_agent.allowlist import ProjectAllowlist
from super_agent.engine import Engine
from super_agent.store import Store

def _lwa_root() -> Path | None:
    raw = os.environ.get("LWA_ROOT") or ""
    candidates = [Path(raw)] if raw else []
    candidates.append(Path(__file__).resolve().parent.parent.parent
                      / "<private-workbench>")
    for path in candidates:
        if (path / "tools" / "gh_issue.py").is_file():
            return path
    return None


LWA = _lwa_root()


def _producer_target() -> tuple[str, str]:
    """``(project, repo)`` of a project the workbench's CLI accepts.

    Read from the workbench's own ``config/gh_projects.json`` instead of being
    written here: that mapping is private data, and embedding it would put a
    copy of it in a public repository. The choice is deterministic (first key in
    sort order) so the tests are stable without naming any particular project.
    """
    if LWA is None:
        return "demo", "owner/demo"
    try:
        data = json.loads((LWA / "config" / "gh_projects.json")
                          .read_text(encoding="utf-8"))
        projects = data.get("projects") or {}
    except (OSError, ValueError, AttributeError):
        return "demo", "owner/demo"
    for project in sorted(projects):
        repo = str(projects[project] or "")
        if "/" in repo:
            return project, repo
    return "demo", "owner/demo"


PROJECT, _REPO_RAW = _producer_target()
# ``issuekey.thread_key`` lower-cases the repo; the constant must agree with it
# or the test compares two spellings of the same thread.
REPO = _REPO_RAW.strip().lower()
THREAD = f"{REPO}#64"                 # what the engine keys by
CLI_KEY = f"{PROJECT}#64"             # what gh_issue.bind writes
AT_SID = "session_at_e2e_64"
OTHER_REPO = "owner/other"


class FakeBus:
    def __init__(self):
        self.injected = []

    def healthz(self):
        return {"ok": True, "ready": True, "connected": True, "seq": 1}

    def inject(self, event_type, payload):
        self.injected.append((event_type, payload))
        return {"ok": True, "seq": len(self.injected)}

    def payloads(self, event_type):
        return [p for e, p in self.injected if e == event_type]

    def kinds(self, event_type):
        return [p.get("kind") for p in self.payloads(event_type)]


class FakeSessions:
    def archive_session(self, sid):
        return True, {"ids": [sid]}

    def session_info(self, sid):
        return {"archived": False}


class Comments:
    def __init__(self):
        self.rows = []

    def add(self, cid, body, author="at"):
        self.rows.append({"id": cid, "issue": 64, "body": body,
                          "author": author, "created_at": ""})

    def issues(self, limit=20):
        return [64]

    def title(self, issue):
        return "probe"

    def comments(self, issue):
        return sorted(self.rows, key=lambda c: c["id"])

    def paged(self, issue, *, since_id=0, max_pages=10):
        return self.comments(issue)

    def issue_state(self, issue):
        return "open"


ALLOWLIST = {
    "schema_version": 1,
    "defaults": {"mode": "observe", "creation_mode": "pm_created"},
    "projects": {
        PROJECT: {"repo": REPO, "cwd": ".", "mode": "assist",
                  "creation_mode": "pm_created", "issues": [64],
                  "actions": ["bus_inject", "collect", "reconcile", "archive"],
                  "max_active_writers": 1,
                  "roles": ["worker", "qa", "pm", "at"]},
    },
}

QUESTIONS = "[AT] 有疑问 #64 <!-- verdict: questions -->"


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class Base(unittest.TestCase):
    """One temp root holds the producer's output **and** the engine's state.

    ``SESS_EVENTS_STATE_DIR`` and ``session_events_path`` point at the same
    directory, so ``gh_issue.bind`` writes where the engine reads - the wiring
    a fixture would otherwise paper over.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "store"          # the engine's index/cursor
        self.shared = self.root / "lwa_state"     # sessions.json + issue_sessions.json
        self.shared.mkdir(parents=True, exist_ok=True)
        self.events = self.shared / "events.jsonl"
        (self.shared / "sessions.json").write_text(json.dumps({
            "supervisor": {"id": "session_sup"},
        }), encoding="utf-8")
        self.allow = self.root / "allowlist.json"
        self.allow.write_text(json.dumps(ALLOWLIST), encoding="utf-8")
        self.source = Comments()
        self.bus = FakeBus()
        self._env = os.environ.get("SESS_EVENTS_STATE_DIR")
        os.environ["SESS_EVENTS_STATE_DIR"] = str(self.shared)

    def tearDown(self):
        if self._env is None:
            os.environ.pop("SESS_EVENTS_STATE_DIR", None)
        else:
            os.environ["SESS_EVENTS_STATE_DIR"] = self._env
        self.tmp.cleanup()

    # ------------------------------------------------------------ the producer
    def bind_at(self, session=AT_SID, name="at-e2e", issue=64):
        """Run the real ``gh_issue.py bind --bind-role at``.

        Not a helper that writes JSON - it shells out to the workbench CLI, so
        the key spelling, the ``Denied`` guard and the argparse ``choices`` are
        all exercised. A test that writes the file itself cannot fail when the
        producer rejects ``at``.
        """
        if LWA is None:
            self.skipTest("workbench not found; set LWA_ROOT to run E1-E3")
        proc = subprocess.run(
            [sys.executable, str(LWA / "tools" / "gh_issue.py"), "bind",
             "--role", "pm", "--project", PROJECT, "--issue", str(issue),
             "--bind-role", "at", "--session", session, "--name", name],
            cwd=str(LWA), capture_output=True, text=True,
            env=dict(os.environ, SESS_EVENTS_STATE_DIR=str(self.shared)))
        return proc

    # ------------------------------------------------------------------ engine
    def engine(self, *, engine_cfg=None):
        extra = {"session_events_path": str(self.events),
                 "allowlist": ["issue_comment.created"]}
        extra.update(engine_cfg or {})
        cfg = config.load(None, {
            "mode": "assist", "role": "supervisor",
            "github": {"source": "none", "repo": REPO},
            "engine": extra,
        })
        engine = Engine(cfg, Store(self.state), self.source, self.bus,
                        project_allowlist=ProjectAllowlist(self.allow),
                        session_api=FakeSessions())
        engine._round_closed_threads = lambda index: []
        return engine

    def write_bind_stream(self):
        """The lifecycle stream: one AT session bound to issue 64.

        Written by hand because it is a *different* producer (the workbench's
        ``sess_events``); it is not the record these tests are about.
        """
        from super_agent import session_events
        rows = [
            {"seq": 1, "at": "2026-11-13T10:00:00+00:00",
             "type": session_events.CREATED, "sid": AT_SID, "key": "at-e2e"},
            {"seq": 2, "at": "2026-11-13T10:00:01+00:00",
             "type": session_events.BOUND, "sid": AT_SID, "key": THREAD,
             "role": "at", "issue": 64},
        ]
        self.events.write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


class E1BindingLands(Base):
    """E1: the producer writes ``at`` where the engine can find it."""

    def test_e1_bind_at_writes_the_session(self):
        proc = self.bind_at()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        db = json.loads((self.shared / "issue_sessions.json")
                        .read_text(encoding="utf-8"))
        self.assertEqual(db[CLI_KEY]["at"]["session"], AT_SID)

    def test_e1b_the_key_the_producer_writes_is_the_short_form(self):
        # The difference that made the binding invisible, stated as a fact: the
        # CLI writes ``<project>#<n>`` and it is **not** a qualified repo key.
        proc = self.bind_at()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        db = json.loads((self.shared / "issue_sessions.json")
                        .read_text(encoding="utf-8"))
        self.assertIn(CLI_KEY, db)
        self.assertNotIn(THREAD, db,
                         "the producer does not write the qualified key")

    def test_e1c_binding_twice_is_idempotent(self):
        self.assertEqual(self.bind_at().returncode, 0)
        self.assertEqual(self.bind_at().returncode, 0)
        db = json.loads((self.shared / "issue_sessions.json")
                        .read_text(encoding="utf-8"))
        self.assertEqual(list(db[CLI_KEY]), ["at"])


class E2EngineReads(Base):
    """E2: the engine resolves that record by its own thread key."""

    def test_e2_at_sid_reads_what_the_cli_wrote(self):
        self.assertEqual(self.bind_at().returncode, 0)
        engine = self.engine()
        self.assertEqual(engine._at_sid(THREAD), AT_SID,
                         "a binding the engine cannot read is not a binding")

    def test_e2b_the_qualified_key_wins_when_both_exist(self):
        self.assertEqual(self.bind_at().returncode, 0)
        path = self.shared / "issue_sessions.json"
        db = json.loads(path.read_text(encoding="utf-8"))
        db[THREAD] = {"at": {"session": "session_qualified", "name": "at-q"}}
        path.write_text(json.dumps(db), encoding="utf-8")
        engine = self.engine()
        self.assertEqual(engine._at_sid(THREAD), "session_qualified")

    def test_e2c_a_bare_number_thread_still_finds_its_own(self):
        self.assertEqual(self.bind_at().returncode, 0)
        engine = self.engine()
        self.assertEqual(engine._at_sid("64"), AT_SID)


class E3AnswerComesBack(Base):
    """E3: real link, real HTTP submit, and the AT session receives the answer."""

    def test_e3_a_submitted_answer_reaches_the_at_session(self):
        self.assertEqual(self.bind_at().returncode, 0)
        self.write_bind_stream()
        received = []

        def transport(sid, text):
            received.append((sid, text))

        port = _free_port()
        server = at_serve.serve(root=self.state, port=port, transport=transport)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            engine = self.engine(engine_cfg={"at_question_port": port,
                                             "session_events_path": str(self.events)})
            self.source.add(9100, QUESTIONS)
            engine.run_once()
            payloads = self.bus.payloads("event.agent.supervisor")
            self.assertEqual(len(payloads), 1)
            link = payloads[0]["question_url"]
            self.assertTrue(link.startswith(f"http://127.0.0.1:{port}/q/"))

            page = urllib.request.urlopen(link, timeout=5).read().decode("utf-8")
            self.assertIn("AT 有疑问", page)

            data = urllib.parse.urlencode({"answer": "先补 E3 证据"}).encode()
            body = urllib.request.urlopen(link, data=data, timeout=5).read() \
                .decode("utf-8")
            self.assertIn("已提交", body)
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0][0], AT_SID,
                             "the answer must reach the session the CLI bound")
            self.assertIn("先补 E3 证据", received[0][1])
        finally:
            server.shutdown()
            server.server_close()

    def test_e3b_the_question_records_the_bound_sid(self):
        self.assertEqual(self.bind_at().returncode, 0)
        self.write_bind_stream()
        engine = self.engine(engine_cfg={"at_question_port": 59999})
        self.source.add(9100, QUESTIONS)
        engine.run_once()
        files = list((self.state / "atserve" / "questions").glob("*.json"))
        self.assertEqual(len(files), 1)
        record = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(record["sid"], AT_SID)


class E4WatchdogSeesTheBinding(Base):
    """E4: the same spelling reaches the watchdog, not just the question path."""

    def test_e4_the_at_watchdog_fires_for_a_cli_bound_thread(self):
        self.assertEqual(self.bind_at().returncode, 0)
        self.write_bind_stream()
        engine = self.engine(engine_cfg={"at_stall_rounds": 3})
        for _ in range(2):
            engine.run_once()
        self.assertNotIn("at_stall",
                         self.bus.kinds("event.agent.supervisor"))
        engine.run_once()
        kinds = self.bus.kinds("event.agent.supervisor")
        self.assertEqual(kinds.count("at_stall"), 1,
                         "a registered AT must have a watchdog watching it")
        payload = self.bus.payloads("event.agent.supervisor")[0]
        self.assertTrue(payload["exception"])
        self.assertIn("AT 停摆", payload["text"])

    def test_e4b_the_stall_counter_uses_the_engine_key(self):
        self.assertEqual(self.bind_at().returncode, 0)
        self.write_bind_stream()
        engine = self.engine(engine_cfg={"at_stall_rounds": 2})
        engine.run_once()
        index = engine.store.load_index()
        self.assertIn(THREAD, index.get("at_stall") or {},
                      "the counter is keyed by the thread, not by the CLI key")
        self.assertNotIn(CLI_KEY, index.get("at_stall") or {})


class E5UnboundFailsClosed(Base):
    """E5: no binding still means no recipient (the regression from #64 修订1)."""

    def test_e5_an_unbound_thread_has_no_at_session(self):
        engine = self.engine()
        self.assertEqual(engine._at_sid(THREAD), "")
        self.assertEqual(engine._at_sid(f"{OTHER_REPO}#64"), "")

    def test_e5b_a_question_on_an_unbound_thread_names_no_recipient(self):
        # An AT key exists (so the verdict is verified and the question is
        # filed), but no thread is bound - the sid must stay empty.
        (self.shared / "sessions.json").write_text(json.dumps({
            "supervisor": {"id": "session_sup"},
            "at-e2e": {"id": AT_SID},
        }), encoding="utf-8")
        engine = self.engine(engine_cfg={"at_question_port": 59999})
        self.source.add(9100, QUESTIONS)
        engine.run_once()
        files = list((self.state / "atserve" / "questions").glob("*.json"))
        self.assertEqual(len(files), 1)
        record = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(record["sid"], "")

    def test_e5c_another_repo_of_the_same_number_is_not_borrowed(self):
        # A different repository shares the number with the bound thread. The
        # qualified lookup must not collapse them.
        self.assertEqual(self.bind_at().returncode, 0)
        engine = self.engine()
        self.assertEqual(engine._at_sid(THREAD), AT_SID)
        self.assertEqual(engine._at_sid(f"{OTHER_REPO}#64"), "")


class KeyLookup(unittest.TestCase):
    """The lookup order itself, without a producer or a store."""

    def _engine(self, ledger):
        engine = Engine.__new__(Engine)
        engine.cfg = {"github": {"repo": REPO}}
        engine.projects = None
        engine._load_issue_sessions = lambda: ledger
        return engine

    def _ledger(self):
        return {
            CLI_KEY: {"at": {"session": "short"}},
            THREAD: {"at": {"session": "full"}},
        }

    def test_qualified_first_then_short(self):
        engine = self._engine(self._ledger())
        self.assertEqual(engine._issue_session_keys(THREAD),
                         [THREAD, CLI_KEY])
        self.assertEqual(engine._issue_session_entry(THREAD).get("at"),
                         {"session": "full"})

    def test_a_qualified_thread_never_falls_back_to_a_bare_number(self):
        # Two repositories share issue numbers; a numeric fallback would hand
        # one of them the other's answer.
        engine = self._engine(self._ledger())
        self.assertNotIn("64", engine._issue_session_keys(THREAD))

    def test_a_bare_number_thread_may_use_the_numeric_key(self):
        engine = self._engine(self._ledger())
        self.assertEqual(engine._issue_session_keys("64")[0], "64")


if __name__ == "__main__":
    unittest.main()
