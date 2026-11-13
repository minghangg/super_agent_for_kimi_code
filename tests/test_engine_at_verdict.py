"""``[AT]`` verdict routing: a second book, its own watchdog, its own link.

The properties pinned here, and why each one is a counter-example rather than a
happy path:

* **two ledgers, two silences.** An AT answer must not land in ``pm_verdicts``
  and a PM answer must not land in ``at_verdicts``. One merged table would make
  a PM that stopped answering and an AT that stopped answering look like the
  same fact, and EITHER answer would silence the other role's watchdog (J1/J2);
* **``questions`` is a state of its own.** It rings with a ``question_url`` and
  **without** ``exception: true``. Folding it into ``blocked`` turns every
  normal question into a failure notice, which is how readers learn to skip
  the failure notices (J3);
* **the PM watchdog does not read the AT book.** A QA pass, a silent PM and a
  chatty AT is exactly the shape where the bug hides: if the AT verdict resets
  ``pm_stall``, the PM that never answered is silently forgiven (J4);
* **the AT watchdog does not read the PM book**, and a watchdog with nobody to
  watch fires on nothing - so it only counts threads an AT was bound to (J5);
* **at most once per comment.** A re-read comment is silent; a *different*
  comment on the same thread still rings (J6);
* **``at-*`` and not ``at``.** The session-key prefix decides whether a link is
  put in front of a person, and ``at`` would claim unrelated keys (J7);
* **the answer comes back.** A submitted answer reaches the AT session, and a
  service that could not send says so instead of claiming success (J8).

Run: ``python -m pytest tests/test_engine_at_verdict.py -q``
"""
from __future__ import annotations

import json
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

from super_agent import at_serve, config, markers, session_events
from super_agent.allowlist import ProjectAllowlist
from super_agent.engine import Engine
from super_agent.store import Store

THREAD = "owner/demo#1"
AT_READY = "[AT] 可派单 #1 <!-- verdict: ready -->"
AT_QUESTIONS = "[AT] 有疑问 #1 <!-- verdict: questions -->"
AT_BLOCKED = "[AT] 受阻 #1 <!-- verdict: blocked -->"
AT_UNMARKED = "[AT] 有疑问 #1"
PM_READY = "[PM] 可关单 #1 <!-- verdict: ready -->"
PASS = "[QA] 通过（P0=0） <!-- fix_needed: false -->"


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
        self.rows.append({"id": cid, "issue": 1, "body": body,
                          "author": author, "created_at": ""})

    def issues(self, limit=20):
        return [1]

    def title(self, issue):
        return "probe"

    def comments(self, issue):
        return sorted(self.rows, key=lambda c: c["id"])

    def paged(self, issue, *, since_id=0, max_pages=10):
        return self.comments(issue)

    def issue_state(self, issue):
        return "open"


GOOD = {
    "schema_version": 1,
    "defaults": {"mode": "observe", "creation_mode": "pm_created"},
    "projects": {
        "demo": {"repo": "owner/demo", "cwd": ".", "mode": "assist",
                 "creation_mode": "pm_created", "issues": [1],
                 "actions": ["bus_inject", "collect", "reconcile", "archive"],
                 "max_active_writers": 1,
                 "roles": ["worker", "qa", "pm", "at"]},
    },
}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.events = self.root / "lifecycle" / "events.jsonl"
        self.events.parent.mkdir(parents=True, exist_ok=True)
        self.allow = self.root / "allowlist.json"
        self.allow.write_text(json.dumps(GOOD), encoding="utf-8")
        self.source = Comments()
        self.bus = FakeBus()
        self.sessions = FakeSessions()
        self.sessions_path = self.events.parent / "sessions.json"
        self.sessions_path.write_text(json.dumps({
            "supervisor": {"id": "session_sup"},
            "pm-demo": {"id": "session_pm"},
            "at-demo": {"id": "session_at"},
        }), encoding="utf-8")
        self.issue_sessions = self.events.parent / "issue_sessions.json"
        self.issue_sessions.write_text(json.dumps({
            THREAD: {"at": {"name": "at-demo", "session": "session_at",
                            "issue": 1}}}), encoding="utf-8")
        self.bind()

    def tearDown(self):
        self.tmp.cleanup()

    def bind(self):
        rows = [
            {"seq": 1, "at": "2026-11-13T10:00:00+00:00",
             "type": session_events.CREATED, "sid": "session_w1",
             "key": "wk-probe-1"},
            {"seq": 2, "at": "2026-11-13T10:00:01+00:00",
             "type": session_events.BOUND, "sid": "session_w1", "key": THREAD,
             "role": "worker", "issue": 1},
            {"seq": 3, "at": "2026-11-13T10:00:02+00:00",
             "type": session_events.CREATED, "sid": "session_q1",
             "key": "qa-demo"},
            {"seq": 4, "at": "2026-11-13T10:00:03+00:00",
             "type": session_events.BOUND, "sid": "session_q1", "key": THREAD,
             "role": "qa", "issue": 1},
            {"seq": 5, "at": "2026-11-13T10:00:04+00:00",
             "type": session_events.CREATED, "sid": "session_pm",
             "key": "pm-demo"},
            {"seq": 6, "at": "2026-11-13T10:00:05+00:00",
             "type": session_events.BOUND, "sid": "session_pm", "key": THREAD,
             "role": "pm", "issue": 1},
            {"seq": 7, "at": "2026-11-13T10:00:06+00:00",
             "type": session_events.CREATED, "sid": "session_at",
             "key": "at-demo"},
            {"seq": 8, "at": "2026-11-13T10:00:07+00:00",
             "type": session_events.BOUND, "sid": "session_at", "key": THREAD,
             "role": "at", "issue": 1},
        ]
        self.events.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                               encoding="utf-8")

    def engine(self, *, mode="assist", engine_cfg=None, role="supervisor"):
        extra = {"session_events_path": str(self.events),
                 "allowlist": ["issue_comment.created"]}
        extra.update(engine_cfg or {})
        cfg = config.load(None, {
            "mode": mode, "role": role,
            "github": {"source": "none", "repo": "owner/demo"},
            "engine": extra,
        })
        engine = Engine(cfg, Store(self.state), self.source, self.bus,
                        project_allowlist=ProjectAllowlist(self.allow),
                        session_api=self.sessions)
        engine._round_closed_threads = lambda index: []
        return engine

    def deliver(self):
        self.source.add(9001, "[WK:wk-probe-1] done", author="worker")

    def hold(self, engine, *, qa_pass=True):
        """A thread held in ``waiting_release`` - the shape both watchdogs watch."""
        index = engine.store.load_index()
        index.setdefault("assignments", {})["asg_1_worker_1"] = {
            "id": "asg_1_worker_1", "thread": THREAD, "issue": 1,
            "role": "worker", "status": "waiting_release", "sid": "session_w1",
            "delivery_comment_id": 9001,
            "qa_verdict": "pass" if qa_pass else ""}
        engine.store.save_index(index)


class TwoBooks(Base):
    """J1/J2: the two ledgers never see each other."""

    def test_j1_an_at_verdict_does_not_touch_the_pm_book(self):
        engine = self.engine()
        self.deliver()
        self.source.add(9100, AT_READY)
        engine.run_once()
        index = engine.store.load_index()
        self.assertNotIn(THREAD, index.get("pm_verdicts") or {},
                         "an AT verdict must not be filed as a PM verdict")
        self.assertEqual(index["at_verdicts"][THREAD]["state"], "ready")

    def test_j2_a_pm_verdict_does_not_touch_the_at_book(self):
        engine = self.engine()
        self.deliver()
        self.source.add(9100, PM_READY, author="pm")
        engine.run_once()
        index = engine.store.load_index()
        self.assertNotIn(THREAD, index.get("at_verdicts") or {},
                         "a PM verdict must not be filed as an AT verdict")
        self.assertEqual(index["pm_verdicts"][THREAD]["state"], "ready")

    def test_both_books_can_hold_the_same_thread(self):
        engine = self.engine()
        self.deliver()
        self.source.add(9100, PM_READY, author="pm")
        self.source.add(9200, AT_QUESTIONS)
        engine.run_once()
        index = engine.store.load_index()
        self.assertEqual(index["pm_verdicts"][THREAD]["state"], "ready")
        self.assertEqual(index["at_verdicts"][THREAD]["state"], "questions")


class QuestionsAreTheirOwnState(Base):
    """J3: ``questions`` rings, carries a link, and is not a failure."""

    def test_j3_questions_carries_a_url_and_is_not_an_exception(self):
        engine = self.engine(engine_cfg={"at_question_port": 59999})
        self.deliver()
        self.source.add(9100, AT_QUESTIONS)
        engine.run_once()
        payloads = self.bus.payloads("event.agent.supervisor")
        self.assertEqual(len(payloads), 1)
        self.assertFalse(payloads[0]["exception"],
                         "a question is a normal stop, not a failure")
        self.assertEqual(payloads[0]["kind"], "at_question")
        self.assertTrue(payloads[0]["question_url"].startswith(
            "http://127.0.0.1:59999/q/"))
        state = engine.store.load_index()["at_verdicts"][THREAD]["state"]
        self.assertEqual(state, "questions")

    def test_a_question_is_filed_even_without_a_service(self):
        engine = self.engine(engine_cfg={"at_question_port": 59999})
        self.deliver()
        self.source.add(9100, AT_QUESTIONS)
        engine.run_once()
        files = list((self.state / "atserve" / "questions").glob("*.json"))
        self.assertEqual(len(files), 1)
        record = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(record["thread"], THREAD)
        self.assertEqual(record["sid"], "session_at")

    def test_ready_and_blocked_have_their_own_kinds(self):
        engine = self.engine()
        self.deliver()
        self.source.add(9100, AT_READY)
        engine.run_once()
        engine2 = self.engine()
        self.source.add(9200, AT_BLOCKED)
        engine2.run_once()
        kinds = self.bus.kinds("event.agent.supervisor")
        self.assertIn("at_ready", kinds)
        self.assertIn("at_blocked", kinds)

    def test_an_unmarked_question_is_treated_as_blocked(self):
        # The word is reported, but it does not decide a state: silence about
        # an answer that arrived is the failure nobody can see.
        engine = self.engine()
        self.deliver()
        self.source.add(9100, AT_UNMARKED)
        engine.run_once()
        payloads = self.bus.payloads("event.agent.supervisor")
        self.assertEqual(len(payloads), 1)
        self.assertTrue(payloads[0]["exception"])
        self.assertEqual(payloads[0]["state"], "")


class WatchdogsDoNotSeeEachOther(Base):
    """J4/J5: two counters, two silences, neither forgiven by the other role."""

    def test_j4_a_chatty_at_does_not_silence_the_pm_watchdog(self):
        # An AT verdict **each round**, and the PM never speaks. If the AT
        # book reset the PM counter, the count would restart every round and
        # the PM watchdog would never fire - a silent PM forgiven forever by
        # another role's answer.
        engine = self.engine(engine_cfg={"pm_stall_rounds": 3})
        self.hold(engine)
        for round_no in range(3):
            self.source.add(9100 + round_no, AT_READY)
            engine.run_once()
        kinds = self.bus.kinds("event.agent.supervisor")
        self.assertIn("pm_stall", kinds,
                      "an AT answer must not reset the PM watchdog")

    def test_j4b_the_at_verdict_leaves_pm_stall_alone(self):
        engine = self.engine(engine_cfg={"pm_stall_rounds": 3})
        self.hold(engine)
        for _ in range(2):
            engine.run_once()
        index = engine.store.load_index()
        self.assertIn(THREAD, index.get("pm_stall") or {})
        engine._record_at_verdict(index, THREAD, {"state": "ready"}, 9100)
        self.assertIn(THREAD, index.get("pm_stall") or {},
                      "_record_at_verdict must not touch pm_stall")

    def test_a_pm_verdict_does_not_reset_the_at_watchdog(self):
        engine = self.engine(engine_cfg={"at_stall_rounds": 3})
        index = engine.store.load_index()
        index.setdefault("assignments", {})["asg_1_worker_1"] = {
            "id": "asg_1_worker_1", "thread": THREAD, "issue": 1,
            "role": "worker", "status": "working", "sid": "session_w1",
            "delivery_comment_id": 9001, "qa_verdict": ""}
        engine.store.save_index(index)
        for _ in range(2):
            engine.run_once()
        self.source.add(9100, PM_READY, author="pm")
        engine.run_once()
        engine.run_once()
        kinds = self.bus.kinds("event.agent.supervisor")
        self.assertIn("at_stall", kinds,
                      "a PM answer must not reset the AT watchdog")

    def test_j5_the_at_watchdog_fires_at_fifteen_rounds(self):
        engine = self.engine()
        cfg = engine.cfg.get("engine") or {}
        self.assertNotIn("at_stall_rounds", cfg, "15 is the default, not a copy")
        for _ in range(14):
            engine.run_once()
        self.assertEqual(self.bus.kinds("event.agent.supervisor"), [],
                         "nothing before the threshold")
        engine.run_once()
        kinds = self.bus.kinds("event.agent.supervisor")
        self.assertEqual(kinds.count("at_stall"), 1)
        payloads = self.bus.payloads("event.agent.supervisor")
        self.assertTrue(payloads[0]["exception"])
        self.assertIn("AT 停摆", payloads[0]["text"])

    def test_j5b_the_at_watchdog_fires_once_per_hold(self):
        engine = self.engine(engine_cfg={"at_stall_rounds": 2})
        for _ in range(6):
            engine.run_once()
        kinds = self.bus.kinds("event.agent.supervisor")
        self.assertEqual(kinds.count("at_stall"), 1)

    def test_j5c_a_thread_with_no_at_bound_is_not_watched(self):
        # Nobody to watch is not a stall; a watchdog that fires on every thread
        # is a watchdog nobody reads.
        self.issue_sessions.write_text("{}", encoding="utf-8")
        engine = self.engine(engine_cfg={"at_stall_rounds": 1})
        engine.run_once()
        self.assertEqual(self.bus.payloads("event.agent.supervisor"), [])

    def test_an_at_verdict_resets_the_at_watchdog_only(self):
        engine = self.engine(engine_cfg={"at_stall_rounds": 3})
        for _ in range(2):
            engine.run_once()
        self.source.add(9100, AT_READY)
        engine.run_once()
        index = engine.store.load_index()
        self.assertNotIn(THREAD, index.get("at_stall") or {})
        engine.run_once()
        engine.run_once()
        kinds = self.bus.kinds("event.agent.supervisor")
        self.assertEqual(kinds.count("at_stall"), 0)


class AtMostOnce(Base):
    """J6: the same comment rings once; a new comment rings on its own."""

    def test_j6_the_same_comment_does_not_ring_twice(self):
        engine = self.engine()
        self.deliver()
        self.source.add(9100, AT_READY)
        engine.run_once()
        index = engine.store.load_index()
        event = {"id": 9100, "body": AT_READY, "session_key": "at-demo"}
        engine._on_at_verdict(index, THREAD, 1, event,
                              markers.parse_at_verdict(AT_READY))
        self.assertEqual(len(self.bus.payloads("event.agent.supervisor")), 1,
                         "a re-read comment must be silent")
        # Silence is not enough: the ledger is what stops the frame *before*
        # the send path, so a re-read must not reach the attempt counter and be
        # reported as "could not be delivered". A false alarm on every re-read
        # teaches the reader to ignore the undeliverable queue.
        rec = index["issues"][THREAD]
        reasons = [r.get("reason") for r in rec.get("needs_person") or []]
        self.assertNotIn("route.supervisor_undeliverable[user]", reasons)

    def test_a_second_comment_rings_on_its_own(self):
        engine = self.engine()
        self.deliver()
        self.source.add(9100, AT_READY)
        engine.run_once()
        self.source.add(9200, AT_QUESTIONS)
        engine.run_once()
        self.assertEqual(len(self.bus.payloads("event.agent.supervisor")), 2)

    def test_the_at_frame_key_is_namespaced(self):
        engine = self.engine()
        self.deliver()
        self.source.add(9100, AT_READY)
        engine.run_once()
        rows = engine.store.load_index()["release_frames"]
        self.assertIn("at:owner/demo#1:9100", rows)
        self.assertTrue(all(k.startswith("at:") for k in rows),
                        "an AT frame must never claim a release key")


class SessionKeys(Base):
    """J7: ``at-*`` keys are recognised, and a miss blocks the frame."""

    def test_j7_at_keys_are_found_in_both_sources(self):
        engine = self.engine()
        keys = engine._at_session_keys(THREAD)
        self.assertIn("at-demo", keys)
        self.assertIn("session_at", keys)
        self.assertTrue(engine._at_verified("at-x",
                                            {"session_key": "at-demo"}))

    def test_a_non_at_key_is_not_verified(self):
        engine = self.engine()
        self.assertFalse(engine._at_verified(
            THREAD, {"session_key": "pm-demo"}))

    def test_an_unverified_at_verdict_rings_nothing(self):
        self.sessions_path.write_text(json.dumps(
            {"supervisor": {"id": "session_sup"}}), encoding="utf-8")
        self.issue_sessions.write_text("{}", encoding="utf-8")
        engine = self.engine()
        self.deliver()
        self.source.add(9100, AT_READY, author="some-random-account")
        engine.run_once()
        self.assertEqual(self.bus.payloads("event.agent.supervisor"), [])
        rec = engine.store.load_index()["issues"][THREAD]
        reasons = [r.get("reason") for r in rec.get("needs_person") or []]
        self.assertIn("at.verdict_unverified[user]", reasons)

    def test_a_verdict_for_another_issue_rings_nothing(self):
        engine = self.engine()
        self.deliver()
        self.source.add(9100, "[AT] 可派单 #99 <!-- verdict: ready -->")
        engine.run_once()
        self.assertEqual(self.bus.payloads("event.agent.supervisor"), [])


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class QuestionnaireEndToEnd(Base):
    """J8: link, open, submit, and the AT session really receives the answer."""

    def test_j8_the_answer_reaches_the_at_session(self):
        port = _free_port()
        received = []

        def transport(sid, text):
            received.append((sid, text))

        server = at_serve.serve(root=self.state, port=port, transport=transport)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            engine = self.engine(engine_cfg={"at_question_port": port})
            self.deliver()
            self.source.add(9100, AT_QUESTIONS)
            engine.run_once()
            payload = self.bus.payloads("event.agent.supervisor")[0]
            link = payload["question_url"]

            page = urllib.request.urlopen(link, timeout=5).read().decode("utf-8")
            self.assertIn("AT 有疑问", page)

            data = urllib.parse.urlencode({"answer": "先做 X"}).encode()
            reply = urllib.request.urlopen(link, data=data, timeout=5)
            body = reply.read().decode("utf-8")
            self.assertIn("已提交", body)
            self.assertEqual(received, [("session_at",
                                         at_serve.answer_text(
                                             at_serve.load(self.state,
                                                           link.rsplit("/", 1)[1])))])
            self.assertIn("先做 X", received[0][1])
        finally:
            server.shutdown()
            server.server_close()

    def test_j8b_a_service_without_a_transport_never_says_submitted(self):
        port = _free_port()
        server = at_serve.serve(root=self.state, port=port, transport=None)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            qid = at_serve.create_question(
                root=self.state, thread=THREAD, issue=1, comment_id=9100,
                sid="session_at", question="q")
            data = urllib.parse.urlencode({"answer": "x"}).encode()
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(at_serve.url(port, qid), data=data,
                                       timeout=5)
            self.assertEqual(caught.exception.code, 409)
            body = caught.exception.read().decode("utf-8")
            self.assertIn("没有回传", body)
            self.assertNotIn("已提交", body)
        finally:
            server.shutdown()
            server.server_close()

    def test_a_question_with_no_at_session_is_kept_but_not_sent(self):
        # Verified by the registered name, but there is no session id to send
        # to: the question is still filed, it simply cannot be answered back.
        self.issue_sessions.write_text(json.dumps({
            THREAD: {"at": {"name": "at-demo"}}}), encoding="utf-8")
        self.sessions_path.write_text(json.dumps(
            {"supervisor": {"id": "session_sup"}}), encoding="utf-8")
        engine = self.engine(engine_cfg={"at_question_port": 59999})
        self.deliver()
        self.source.add(9100, AT_QUESTIONS)
        engine.run_once()
        files = list((self.state / "atserve" / "questions").glob("*.json"))
        self.assertEqual(len(files), 1)
        record = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(record["sid"], "")


class PortBinding(unittest.TestCase):
    """Two real instances, one port: the second must refuse.

    The earlier version of this test held the port with a bare ``socket()``.
    That socket never sets ``SO_REUSEADDR``, so it proved nothing: with
    ``allow_reuse_address = 1`` (the ``HTTPServer`` default) the second
    **server** binds happily, both processes exit 0, and the first one's links
    start answering 404. The holder here is a real ``at_serve`` process, which
    is the shape the bug actually takes.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.port = _free_port()
        self.procs = []

    def tearDown(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
        self.tmp.cleanup()

    def _spawn(self):
        proc = subprocess.Popen(
            [sys.executable, "-m", "super_agent.at_serve",
             "--root", str(self.root), "--port", str(self.port)],
            cwd=str(Path(__file__).resolve().parent.parent),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.procs.append(proc)
        return proc

    def test_a_second_instance_cannot_take_the_port(self):
        first = self._spawn()
        deadline = time.time() + 30
        while time.time() < deadline:
            if first.poll() is not None:
                self.fail(f"the first instance exited: {first.stderr.read()}")
            try:
                with socket.create_connection(("127.0.0.1", self.port), 0.5):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            self.fail("the first instance never started listening")

        second = self._spawn()
        rc = second.wait(timeout=60)
        self.assertNotEqual(rc, 0, "a second instance must not exit 0")
        self.assertIn("无法绑定", second.stderr.read())

        # The first instance is untouched: the pid file still names it and the
        # port still answers. A takeover would have shown up as a 404 here.
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/q/none", timeout=5)
        self.assertEqual(caught.exception.code, 404)
        self.assertEqual(int((Path(self.root) / "atserve" / "atserve.pid")
                             .read_text(encoding="utf-8")), first.pid)

    def test_the_server_refuses_to_reuse_an_address(self):
        # The property behind the process test, stated directly so a future
        # edit that flips the class attribute is caught here too.
        self.assertFalse(at_serve._Server.allow_reuse_address)


class OneLinkPerQuestion(Base):
    """P1-2: two ``questions`` on one thread are two links."""

    def test_two_questions_make_two_links(self):
        engine = self.engine(engine_cfg={"at_question_port": 59999})
        self.deliver()
        self.source.add(9100, AT_QUESTIONS)
        engine.run_once()
        self.source.add(9200, AT_QUESTIONS)
        engine.run_once()
        urls = [p["question_url"] for p in
                self.bus.payloads("event.agent.supervisor")]
        self.assertEqual(len(urls), 2)
        self.assertNotEqual(urls[0], urls[1],
                            "one question, one link - a reused link answers "
                            "the wrong question")
        self.assertNotIn(urls[1], urls[0])
        files = list((self.state / "atserve" / "questions").glob("*.json"))
        self.assertEqual(len(files), 2,
                         "each question is its own file with its own id")


class TransportIsOffByDefault(unittest.TestCase):
    """P1-3: ``--allow-send`` is the only switch that wires the real client."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.calls = []
        self._orig = at_serve.run

    def tearDown(self):
        at_serve.run = self._orig
        self.tmp.cleanup()

    def _capture(self, argv):
        def fake_run(*, root, port=at_serve.DEFAULT_PORT, transport=None):
            self.calls.append(transport)
            return 0
        at_serve.run = fake_run
        return at_serve.main(list(argv))

    def test_without_the_flag_nothing_is_sent(self):
        self.assertEqual(self._capture(["--root", self.root, "--port", "59998"]), 0)
        self.assertEqual(self.calls, [None],
                         "a service that can reach production by default is "
                         "one mistake away from answering into it")

    def test_with_the_flag_the_real_client_is_wired(self):
        self.assertEqual(self._capture(["--root", self.root, "--port", "59998",
                                        "--allow-send"]), 0)
        self.assertEqual(self.calls, [at_serve._real_transport])


class AtKeyPrecision(Base):
    """P1-4: ``at-`` is the prefix, not the two letters ``at``."""

    def test_a_key_that_only_starts_with_at_is_not_an_at_key(self):
        self.sessions_path.write_text(json.dumps({
            "supervisor": {"id": "session_sup"},
            "attachment-xyz": {"id": "session_attachment"},
            "atlas": {"id": "session_atlas"},
            "aten": {"id": "session_aten"},
        }), encoding="utf-8")
        engine = self.engine()
        keys = engine._at_session_keys(THREAD)
        for wrong in ("attachment-xyz", "atlas", "aten"):
            self.assertNotIn(wrong, keys, f"{wrong} is not an AT session key")
        self.assertFalse(engine._at_verified(
            THREAD, {"session_key": "attachment-xyz"}))


class AtSessionIsThreadLocal(Base):
    """P1-5: an answer goes to **this** thread's AT, never to another project's.

    Two projects, two ATs, and a thread with no ``at`` binding. The old code
    fell back to the first ``at-*`` key in ``sessions.json`` by sort order,
    which delivered project A's answer into project B's session - a mistake
    nobody can see, because the page still said "submitted".
    """

    OTHER = "owner/other#2"

    def setUp(self):
        super().setUp()
        self.issue_sessions.write_text(json.dumps({
            THREAD: {"at": {"name": "at-demo", "session": "session_at"}},
            self.OTHER: {"at": {"name": "at-other", "session": "session_other"}},
        }), encoding="utf-8")
        self.sessions_path.write_text(json.dumps({
            "supervisor": {"id": "session_sup"},
            "at-aaa-first": {"id": "session_aaa_first"},
            "at-other": {"id": "session_other"},
        }), encoding="utf-8")

    def test_an_unbound_thread_gets_no_global_at_session(self):
        engine = self.engine()
        # A thread that exists in no ledger at all.
        self.assertEqual(engine._at_sid("owner/demo#9"), "",
                         "no binding means no recipient, not somebody else's")

    def test_a_bound_thread_gets_its_own(self):
        engine = self.engine()
        self.assertEqual(engine._at_sid(THREAD), "session_at")
        self.assertEqual(engine._at_sid(self.OTHER), "session_other")

    def test_a_question_carries_the_threads_own_at_session(self):
        # Two engines over one fixture, and only the *unbound* one can tell the
        # two implementations apart: the bound thread answers ``session_at``
        # either way, because the per-thread entry is read first. So the
        # question is filed for a thread that has no ``at`` entry while an
        # ``at-aaa-first`` key sits in ``sessions.json`` - the old fallback
        # spends it, the fail-closed path leaves the question unanswered.
        self.issue_sessions.write_text(json.dumps({
            THREAD: {"at": {"name": "at-demo", "session": "session_at"}},
        }), encoding="utf-8")
        engine = self.engine(engine_cfg={"at_question_port": 59999})
        engine._round_issue = lambda *a, **k: None      # keep the threads off
        index = engine.store.load_index()
        body = "[AT] 有疑问 #9 <!-- verdict: questions -->"
        engine._on_at_verdict(index, "owner/demo#9", 9,
                              {"id": 9100, "body": body},
                              markers.parse_at_verdict(body))
        files = list((self.state / "atserve" / "questions").glob("*.json"))
        self.assertEqual(len(files), 1)
        record = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(record["thread"], "owner/demo#9")
        self.assertEqual(record["sid"], "",
                         "an unbound thread must not borrow another AT")


if __name__ == "__main__":
    unittest.main()
