"""#75: a ``[PM]`` line is read as a verdict only when it is a conclusion.

The failure these pin: every ``[PM]`` comment used to enter the verdict branch,
so a task book or a dispatch came out as ``state == ""`` - which the router
reads as "the PM answered and the answer was unreadable" - and filed an
exception plus a ``needs_person`` row on every round. 57 comment ids, 1251
frames.

The three shapes are pinned separately because each one was a counter-example
in the issue:

* a task book with no conclusion word is **discussion**, not an unreadable
  verdict;
* a real conclusion with no marker is **still** unreadable - the fix must not
  buy quiet by swallowing the one failure that needs a person;
* a bracketed heading is a title: ``【返工 · ISS-68 第 2 轮】`` hands out work,
  it does not report on it.

Run: ``python -m pytest tests/test_iss75_pm_candidate.py -q``
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from super_agent import config, markers, session_events
from super_agent.allowlist import ProjectAllowlist
from super_agent.engine import Engine
from super_agent.store import Store

THREAD = "owner/demo#1"

# The three real task books named in #75. Their first lines only - the bodies
# are long, and the parse reads the first line.
TASK_69 = "[PM] 【任务书 · ISS-69 · E61 引擎原生活性看护】派单：pm-lwa 第十任（2026-11-14）"
TASK_71 = "[PM] 【任务书 · ISS-71 方案轮】pm-lwa 第十任 · 2026-11-14"
TASK_73 = "[PM] 【任务书 · ISS-73 · scout 违 recycle 查明＋修规则】派单：pm-lwa 第十任 · 2026-11-14"

GOOD = {
    "schema_version": 1,
    "defaults": {"mode": "observe", "creation_mode": "pm_created"},
    "projects": {
        "demo": {"repo": "owner/demo", "cwd": ".", "mode": "assist",
                 "creation_mode": "pm_created", "issues": [1],
                 "actions": ["bus_inject", "collect", "reconcile", "archive"],
                 "max_active_writers": 1, "roles": ["worker", "qa", "pm"]},
    },
}


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


class FakeSessions:
    def __init__(self):
        self.archived = set()

    def archive_session(self, sid):
        self.archived.add(sid)
        return True, {"ids": [sid]}

    def session_info(self, sid):
        return {"archived": sid in self.archived}


class Comments:
    def __init__(self):
        self.rows = []

    def add(self, cid, body, author="pm"):
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
        self.events.write_text("\n".join(json.dumps(r) for r in [
            {"seq": 1, "at": "2026-11-10T10:00:00+00:00",
             "type": session_events.CREATED, "sid": "session_w1",
             "key": "wk-probe-1"},
            {"seq": 2, "at": "2026-11-10T10:00:01+00:00",
             "type": session_events.BOUND, "sid": "session_w1", "key": THREAD,
             "role": "worker", "issue": 1},
            {"seq": 3, "at": "2026-11-10T10:00:02+00:00",
             "type": session_events.CREATED, "sid": "session_q1",
             "key": "qa-demo"},
            {"seq": 4, "at": "2026-11-10T10:00:03+00:00",
             "type": session_events.BOUND, "sid": "session_q1", "key": THREAD,
             "role": "qa", "issue": 1},
            {"seq": 5, "at": "2026-11-10T10:00:04+00:00",
             "type": session_events.CREATED, "sid": "session_pm",
             "key": "pm-demo"},
            {"seq": 6, "at": "2026-11-10T10:00:05+00:00",
             "type": session_events.BOUND, "sid": "session_pm", "key": THREAD,
             "role": "pm", "issue": 1},
        ]) + "\n", encoding="utf-8")
        (self.events.parent / "sessions.json").write_text(
            json.dumps({"supervisor": {"id": "session_sup"},
                        "pm-demo": {"id": "session_pm"}}), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def engine(self):
        cfg = config.load(None, {
            "mode": "assist", "role": "supervisor",
            "github": {"source": "none", "repo": "owner/demo"},
            "engine": {"session_events_path": str(self.events),
                       "allowlist": ["issue_comment.created"]},
        })
        engine = Engine(cfg, Store(self.state), self.source, self.bus,
                        project_allowlist=ProjectAllowlist(self.allow),
                        session_api=self.sessions)
        engine._round_closed_threads = lambda index: []
        return engine

    def rows(self, engine):
        rec = (engine.store.load_index().get("issues") or {}).get(THREAD) or {}
        return rec.get("needs_person") or []


class _Case(Base):
    """One isolated engine, without the test runner's fixture protocol.

    Several cases walk a list of bodies and need a fresh store per body; going
    through ``subTest`` and ``setUp`` reads worse than a small helper.
    """

    def runTest(self):
        pass


class Candidate(unittest.TestCase):
    """The parse-level rule: candidacy is decided on the first line only."""

    def test_the_three_task_books_are_not_candidates(self):
        for body in (TASK_69, TASK_71, TASK_73):
            out = markers.parse_pm_verdict(body)
            self.assertIsNotNone(out, body)
            self.assertFalse(out["candidate"], body)
            self.assertEqual(out["state"], "", body)

    def test_the_four_prose_patterns(self):
        # heading: a dispatch title, not a statement
        self.assertFalse(markers.parse_pm_verdict(
            "[PM] 【返工 · ISS-68 第 2 轮】pm-lwa 第十任 · 2026-11-14")["candidate"])
        # note: no conclusion word, only a coordinate
        self.assertFalse(markers.parse_pm_verdict(
            "[PM] #61 收单：scout 对标报告（cid `6854178627`）")["candidate"])
        # note: nothing at all
        self.assertFalse(markers.parse_pm_verdict(
            "[PM] 知悉并落执行（2026-11-09 16:4x）")["candidate"])
        # conclusion: the word is outside any heading
        self.assertTrue(markers.parse_pm_verdict(
            "[PM] 可关单 #42")["candidate"])

    def test_a_word_quoted_in_the_body_is_not_a_candidate(self):
        # #75's own mechanism: a task book explains the verdict contract
        # without ever concluding. Only the first line is read.
        body = TASK_69 + "\n\n完工仍靠 verdict 契约；该 thread 期间出现新评论/verdict ⇒ 计数清零"
        self.assertFalse(markers.parse_pm_verdict(body)["candidate"])

    def test_the_word_verdict_alone_is_never_a_candidate(self):
        self.assertFalse(markers.parse_pm_verdict(
            "[PM] verdict 契约说明 · 见 #42")["candidate"])
        self.assertTrue(markers.parse_pm_verdict(
            "[PM] 受阻 #42 <!-- verdict: blocked -->")["candidate"])

    def test_a_negated_word_is_not_a_conclusion(self):
        self.assertFalse(markers.parse_pm_verdict("[PM] 不可关单 #42")["candidate"])

    def test_the_countersign_is_carried_on_the_parse(self):
        out = markers.parse_pm_verdict("[PM] 可关单 #42 <!-- verdict: ready -->")
        self.assertTrue(out["candidate"])
        self.assertTrue(out["structured"])
        self.assertEqual(out["state"], "ready")


class WiderClosingTable(unittest.TestCase):
    """P0-1: a conclusion word missing from the table becomes silence.

    The table held only ``STATE_TABLE`` and ``WORD_TABLE``, so ``关单吧``,
    ``可以关了``, ``LGTM`` and five more spellings fell out of candidacy and
    their exception frames disappeared - the failure #75 forbids.
    """

    OUTSIDE = (
        "[PM] 关单吧 #1",
        "[PM] 本单可以关了",
        "[PM] 可以关单了 #42",
        "[PM] 同意关闭 #42",
        "[PM] done #42",
        "[PM] LGTM #42",
        "[PM] approve #42",
        "[PM] 没问题，关吧",
    )

    def test_eight_spellings_are_candidates(self):
        for body in self.OUTSIDE:
            self.assertTrue(markers.parse_pm_verdict(body)["candidate"], body)

    def test_a_bare_word_inside_a_compound_is_not_a_conclusion(self):
        # ``关于``/``相关``/``合计``/``配合`` contain 关 and 合, and a note that
        # mentions one of them must not become an exception.
        for body in ("[PM] 关于 #42 的说明", "[PM] 相关问题见 #42",
                     "[PM] 合计 3 条", "[PM] 配合 QA 复核"):
            self.assertFalse(markers.parse_pm_verdict(body)["candidate"], body)

    def test_the_three_task_books_stay_quiet(self):
        for body in (TASK_69, TASK_71, TASK_73):
            self.assertFalse(markers.parse_pm_verdict(body)["candidate"], body)


class EscalationMarkers(Base):
    """P0-2: a note asking for a person must not be answered with silence."""

    BODIES = ("[PM] [blocked] 卡在 allowlist", "[PM] [decision] 需要人拍板")

    def test_an_escalation_in_a_pm_note_rings_and_leaves_a_row(self):
        for body in self.BODIES:
            with self.subTest(body=body):
                self.setUp()
                try:
                    engine = self.engine()
                    self.source.add(9100, body)
                    engine.run_once()
                    frames = self.bus.payloads("event.agent.supervisor")
                    self.assertEqual(len(frames), 1)
                    self.assertTrue(frames[0]["exception"])
                    reasons = [r.get("reason") for r in self.rows(engine)]
                    self.assertIn("pm.escalation[user]", reasons)
                finally:
                    self.tearDown()

    def test_a_plain_note_is_still_silent(self):
        engine = self.engine()
        self.source.add(9100, "[PM] 知悉并落执行（2026-11-09 16:4x）")
        engine.run_once()
        self.assertEqual(self.bus.payloads("event.agent.supervisor"), [])
        self.assertEqual(self.rows(engine), [])


class EscalationIsReadFromTheFirstLineOnly(Base):
    """P0-3: a comment that *quotes* a marker is discussion, not a request.

    The round-2 fix scanned the whole body, so a task book that explains
    ``[blocked]``／``[decision]`` rang the exception bell every round - the
    shape this issue exists to remove, reintroduced through a different word.
    """

    # The real task book that reproduced it (cid 6877947836): first line is an
    # ordinary title, and the body quotes both markers. Abbreviated to the
    # lines that matter, so the test says why it exists.
    BODIES = ("[PM] [blocked] 卡在 allowlist", "[PM] [decision] 需要人拍板")

    QUOTING_BOOK = (
        "[PM] ISS-75 第二轮：修 QA 判定的两条 P0（降噪过头）＋两条 P1\n"
        "\n"
        "4. P0-2：escalation_markers（默认 [decision]／[blocked]）之前返回，于是：\n"
        "   [PM] [blocked] 卡在 allowlist      改前 frames=1 exc=[True]\n"
        "   [PM] [decision] 需要人拍板         改前 frames=1 exc=[True]\n"
    )

    def test_a_task_book_quoting_both_markers_is_silent(self):
        engine = self.engine()
        self.source.add(6877947836, self.QUOTING_BOOK)
        for _ in range(3):
            engine.run_once()
        self.assertEqual(self.bus.payloads("event.agent.supervisor"), [])
        self.assertEqual(self.rows(engine), [])

    def test_a_marker_inside_a_fenced_block_is_silent(self):
        engine = self.engine()
        self.source.add(9100, "[PM] 三点说明\n\n```\n[decision] 需要人拍板\n```\n")
        engine.run_once()
        self.assertEqual(self.bus.payloads("event.agent.supervisor"), [])
        self.assertEqual(self.rows(engine), [])

    def test_a_marker_on_the_first_line_still_rings(self):
        # The other half: narrowing the detection must not silence a real one.
        for body in self.BODIES:
            with self.subTest(body=body):
                self.setUp()
                try:
                    engine = self.engine()
                    self.source.add(9100, body)
                    engine.run_once()
                    frames = self.bus.payloads("event.agent.supervisor")
                    self.assertEqual(len(frames), 1)
                    self.assertTrue(frames[0]["exception"])
                    self.assertIn("pm.escalation[user]",
                                  [r.get("reason") for r in self.rows(engine)])
                finally:
                    self.tearDown()

    def test_case_insensitive_on_the_first_line(self):
        for body in ("[PM] [BLOCKED] 卡在 allowlist",
                     "[PM] [Decision] 需要人拍板"):
            with self.subTest(body=body):
                self.setUp()
                try:
                    engine = self.engine()
                    self.source.add(9100, body)
                    engine.run_once()
                    self.assertEqual(
                        len(self.bus.payloads("event.agent.supervisor")), 1)
                finally:
                    self.tearDown()


class ClosingWordGuard(unittest.TestCase):
    """``关闭`` is a verb as often as a conclusion (#75 round 3).

    It cannot sit in ``PM_CONCLUSION_WORDS`` unguarded: ``关闭窗口`` and
    ``门禁已关闭`` describe things, and reading them as "close the issue" files
    a false exception. The guard is the verb tail and the state head, so the
    answer forms - which carry neither - still count.
    """

    ANSWERS = ("[PM] 关单吧 #1", "[PM] 关闭 #42", "[PM] 可以关 #42",
               "[PM] 同意关闭 #42")
    VERBS = ("[PM] 关闭窗口已开", "[PM] 门禁已关闭",
             "[PM] 会话关闭后归档", "[PM] 关闭前先报清单",
             "[PM] 综合评估见下", "[PM] 合并分支说明")

    def test_a_closing_answer_is_a_candidate(self):
        for body in self.ANSWERS:
            self.assertTrue(markers.parse_pm_verdict(body)["candidate"], body)

    def test_the_verb_reading_is_not_a_candidate(self):
        for body in self.VERBS:
            self.assertFalse(markers.parse_pm_verdict(body)["candidate"], body)

    def test_the_answers_ring_and_the_verbs_do_not(self):
        class Case(Base):
            def runTest(self):
                pass

        for body in self.ANSWERS + self.VERBS:
            with self.subTest(body=body):
                case = Case()
                case.setUp()
                try:
                    engine = case.engine()
                    case.source.add(9100, body)
                    engine.run_once()
                    frames = case.bus.payloads("event.agent.supervisor")
                    rows = case.rows(engine)
                    if body in self.ANSWERS:
                        self.assertTrue(frames or rows, body)
                    else:
                        self.assertEqual(frames, [], body)
                        self.assertEqual(rows, [], body)
                finally:
                    case.tearDown()


class QuotedConclusionStillCounts(unittest.TestCase):
    """P0-B: a quoted conclusion is a conclusion.

    Round 3 removed every quoted span before looking for a conclusion word, so
    a PM who wrote ``「可关单 #42」`` - a common way to state an answer - went
    silent. The removal now applies to the **guards only**: the main table
    reads the line as written.
    """

    QUOTED = ("[PM] 「可关单 #42」", "[PM] “可关单 #42”",
              "[PM] 我说「可关单 #42」了", "[PM] 马上 `可关单 #42`")

    def test_a_quoted_answer_is_a_candidate(self):
        for body in self.QUOTED:
            self.assertTrue(markers.parse_pm_verdict(body)["candidate"], body)

    def test_a_quoted_answer_rings(self):
        for body in self.QUOTED:
            with self.subTest(body=body):
                case = _Case()
                case.setUp()
                try:
                    engine = case.engine()
                    case.source.add(9100, body)
                    engine.run_once()
                    # A frame, or a row naming the mismatch: ``#42`` in a body
                    # on the ``#1`` thread is audible either way, and either
                    # way the answer was not swallowed.
                    self.assertTrue(
                        case.bus.payloads("event.agent.supervisor")
                        or case.rows(engine), body)
                finally:
                    case.tearDown()

    def test_a_quoted_verb_stays_quiet(self):
        # The guard still reads the de-quoted text, or every explanation of
        # ``关闭窗口`` would file an exception.
        for body in ("[PM] 关于「关闭窗口」的说明", "[PM] 关闭窗口已开"):
            self.assertFalse(markers.parse_pm_verdict(body)["candidate"], body)


class ClosingIssueAsAnObject(unittest.TestCase):
    """P0-C: ``挡关单`` is about blocking a close, not announcing one."""

    OBJECTS = ("[PM] 挡关单", "[PM] 拒绝关单", "[PM] 阻止关单",
               "[PM] 防止关单", "[PM] 避免关单", "[PM] 停止关单")
    ANSWERS = ("[PM] 关单吧 #1", "[PM] 可关单 #42",
               "[PM] 同意关单 #42", "[PM] 可以关单了 #42")

    def test_the_object_reading_is_not_a_candidate(self):
        for body in self.OBJECTS:
            self.assertFalse(markers.parse_pm_verdict(body)["candidate"], body)

    def test_the_answers_are_still_candidates(self):
        for body in self.ANSWERS:
            self.assertTrue(markers.parse_pm_verdict(body)["candidate"], body)

    def test_the_negation_chain_still_wins(self):
        # ``不可关单`` must answer the same way as ``挡关单``: the negation and
        # the object verb are two guards on one word, not rivals.
        for body in ("[PM] 不可关单 #42", "[PM] 未可关单 #42"):
            self.assertFalse(markers.parse_pm_verdict(body)["candidate"], body)

    def test_a_task_book_that_mentions_blocking_is_silent(self):
        # The real first line of cid 6881482959.
        body = ("[PM] ISS-75 第三轮 fix 轮独立复核（qa-lwa-ISS-75 复用，同 issue "
                "多轮合法；本轮只 P0 挡关单，P1 记 backlog）\n\n"
                "5. P0-C：escalation_markers（默认 [decision]／[blocked]）\n")
        engine_case = _Case()
        engine_case.setUp()
        try:
            self.assertFalse(markers.parse_pm_verdict(body)["candidate"])
            engine = engine_case.engine()
            engine_case.source.add(6881482959, body)
            for _ in range(3):
                engine.run_once()
            self.assertEqual(
                engine_case.bus.payloads("event.agent.supervisor"), [])
            self.assertEqual(engine_case.rows(engine), [])
        finally:
            engine_case.tearDown()


class EngineQuiet(Base):
    """The router: a non-candidate is discussion, and discussion is silent."""

    def test_the_three_task_books_ring_nothing(self):
        engine = self.engine()
        self.source.add(6873336441, TASK_69)
        self.source.add(6873670756, TASK_71)
        self.source.add(6874076559, TASK_73)
        engine.run_once()
        self.assertEqual(self.bus.payloads("event.agent.supervisor"), [])
        self.assertEqual(self.bus.payloads("event.agent.needs_person"), [])
        self.assertEqual(self.rows(engine), [])

    def test_a_task_book_is_logged_as_ignored_not_processed_as_a_verdict(self):
        engine = self.engine()
        self.source.add(6873336441, TASK_69)
        engine.run_once()
        rows = [json.loads(x) for x in
                (self.state / "events.jsonl").read_text("utf-8").splitlines()
                if x.strip()]
        events = [e for e in rows if e.get("type") == "comment.processed"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["state"], "ignored")
        self.assertIn("not a verdict", events[0]["reason"])

    def test_a_body_mentioning_the_closing_words_is_still_discussion(self):
        # Counter-example 4: no first-line conclusion word, no marker, but the
        # body says "本单可关单了". #75 asks for silence here, and the reason is
        # that the PM contract puts the answer on the first line: a conclusion
        # buried in prose is exactly what the marker exists to replace.
        engine = self.engine()
        self.source.add(9100, "[PM] 三点说明\n\n本单可关单了，见上文。")
        engine.run_once()
        self.assertEqual(self.bus.payloads("event.agent.supervisor"), [])
        self.assertEqual(self.rows(engine), [])


class EngineStillLoud(Base):
    """Counter-examples 1-3: the quiet must not swallow a real answer."""

    def test_a_real_conclusion_without_a_marker_still_rings_once(self):
        engine = self.engine()
        self.source.add(9100, "[PM] 可关单 #1")
        engine.run_once()
        payloads = self.bus.payloads("event.agent.supervisor")
        self.assertEqual(len(payloads), 1, "a dropped PM answer must be visible")
        self.assertTrue(payloads[0]["exception"])

    def test_a_real_conclusion_with_the_marker_passes(self):
        engine = self.engine()
        self.source.add(9100, "[PM] 可关单 #1 <!-- verdict: ready -->")
        engine.run_once()
        payloads = self.bus.payloads("event.agent.supervisor")
        self.assertEqual(len(payloads), 1)
        self.assertFalse(payloads[0]["exception"])

    def test_a_verdict_word_mid_body_with_a_conclusion_first_line_is_still_loud(self):
        engine = self.engine()
        self.source.add(9100, "[PM] 可关单 #1\n\nverdict 标记忘了写。")
        engine.run_once()
        payloads = self.bus.payloads("event.agent.supervisor")
        self.assertEqual(len(payloads), 1)
        self.assertTrue(payloads[0]["exception"])


class Dedupe(Base):
    """#75 B: one row per ``(thread, comment_id, reason)``."""

    def test_five_rounds_of_one_comment_write_one_row(self):
        # A verdict naming the wrong issue files a row and rings nothing. The
        # router filed one row and the loop's fallback path filed a second with
        # the prose reason; five rounds must leave one.
        engine = self.engine()
        self.source.add(9100, "[PM] 可关单 #99")
        for _ in range(5):
            engine.run_once()
        rows = self.rows(engine)
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["comment_id"], 9100)

    def test_the_undeliverable_route_row_does_not_pile_either(self):
        engine = self.engine()
        index = engine.store.load_index()
        for _ in range(5):
            engine._append_needs_person(index, THREAD, {
                "comment_id": 9100, "reason": "route.undeliverable",
                "at": "t"})
        rows = index["issues"][THREAD]["needs_person"]
        self.assertEqual(len(rows), 1, rows)

    def test_two_reasons_for_one_comment_are_two_rows(self):
        engine = self.engine()
        index = engine.store.load_index()
        engine._append_needs_person(index, THREAD, {
            "comment_id": 9100, "reason": "route.undeliverable", "at": "t"})
        engine._append_needs_person(index, THREAD, {
            "comment_id": 9100, "reason": "pm.verdict[user]", "at": "t"})
        self.assertEqual(len(index["issues"][THREAD]["needs_person"]), 2)


if __name__ == "__main__":
    unittest.main()
