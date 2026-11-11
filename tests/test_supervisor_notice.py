"""L1 tests for the supervisor notice delivery leg (ISS-55).

The leg consumes ``event.agent.supervisor`` and really sends ``payload.text``
into ``payload.sid``. Three invariants are pinned here, each with the reverse
case the ticket names:

* ``retry_later`` is retried from the **ledger** (the shared cursor has already
  advanced, so ``/events`` will never return that event again) and the ledger
  keeps the **whole** text;
* at-most-once comes from the event fingerprint, so a replay or a restart never
  sends twice;
* a missing heartbeat registration refuses to start, because an unregistered
  name never writes a heartbeat and never appears in ``/healthz`` at all.

Every test runs against a temporary state root. Nothing here touches a
production file.

Run: ``python -m pytest tests/test_supervisor_notice.py -q``
"""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from super_agent import decide_watch as dw

ROOT = Path(__file__).resolve().parent.parent


class Bus:
    PORT = 58800

    def __init__(self, events=None, seq=1, oldest=1, registered=True, down=False):
        self.events = list(events or [])
        self.seq = seq
        self.oldest = oldest
        self.registered = registered
        self.down = down
        self.gets = []

    def guardian_thresholds(self):
        names = {"decide_watch": ("heartbeat/decide.json", 900.0, "主循环"),
                 "delivery_watch": ("heartbeat/delivery.json", 900.0, "收单哨")}
        if self.registered:
            names["supervisor_notice"] = ("heartbeat/supervisor_notice.json",
                                          900.0, "投递哨")
        return names

    def bus_get(self, path, timeout=0, port=0):
        self.gets.append((path, timeout))
        if self.down:
            raise OSError("daemon unreachable")
        since = 0
        for part in str(path).split("?", 1)[-1].split("&"):
            if part.startswith("since="):
                try:
                    since = int(part.split("=", 1)[1])
                except ValueError:
                    since = 0
        # 真实 /events 是排他的：只返回 seq > since
        visible = [e for e in self.events if int(e.get("seq") or 0) > since]
        return {"events": visible, "next_seq": self.seq,
                "seq": self.seq,          # 真实 /events 必带；回退判据靠它
                "oldest_seq": self.oldest, "boot_id": "isolated"}

    def bus_health(self, port=0):
        if self.down:
            raise OSError("daemon unreachable")
        return {"boot_id": "isolated", "seq": self.seq}


class NoticeFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="iss55_")
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.ws = self.root / "ws"
        self.fixtures = self.root / "fixtures"
        for directory in (self.state, self.ws, self.fixtures):
            directory.mkdir(parents=True, exist_ok=True)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: dw.configure(dw.DecideWatchConfig()))
        (self.fixtures / "sessions.json").write_text(json.dumps({
            "supervisor": {"id": "session_supervisor_fixture"},
        }), encoding="utf-8")
        (self.fixtures / "issue_sessions.json").write_text("{}", encoding="utf-8")
        (self.fixtures / "ledger.jsonl").write_text("", encoding="utf-8")
        # 生产 root 里这个文件一直在；写它以避开 HEAD 既有的
        # `_LAST_SUPERVISOR_STATE` 未定义隐患（`_last_state_read() or <未定义全局>`
        # 只在文件缺失时才会真去求值右操作数）。该隐患不在本单范围，只登记不修。
        (self.state / "supervisor_health_state.json").write_text(
            json.dumps({"state": "ok", "ts": 0}), encoding="utf-8")
        self.beats, self.injections, self.sends = [], [], []
        self.probe_answer = (True, {"sid": "session_probe_fixture"})
        self.send_rc = 0
        self.send_stderr = ""
        self.bus = Bus()
        self.config = self.make_config(self.bus)
        dw.configure(self.config)

    def make_config(self, bus, **overrides):
        cfg = dw.DecideWatchConfig(
            state_root=self.state, workspace_root=self.ws,
            session_script=self.ws / "session_client.py",
            event_source=bus,
            heartbeat=lambda name, extra: self.beats.append((name, extra)),
            inject_event=lambda path, body, port=0: self.injections.append((path, body)),
            ps_json=lambda script, timeout=60.0: (0, {}, ""),
            windowless_python=lambda: (sys.executable, ""),
            probe_session=lambda sid: self.probe_answer,
            send_transport=self._deliver_via_transport,
            self_test_root=self.root / "selftest",
            fixture_sessions_path=self.fixtures / "sessions.json",
            fixture_bind_path=self.fixtures / "issue_sessions.json",
            fixture_ledger_path=self.fixtures / "ledger.jsonl",
        )
        return replace(cfg, **overrides) if overrides else cfg

    def _deliver_via_transport(self, sid, text, timeout=60.0):
        self.sends.append((sid, text))
        return subprocess.CompletedProcess([], self.send_rc, "",
                                           self.send_stderr)

    def notice(self, seq=10, sid="session_probe_fixture", text="【可关单】#55，QA cid 1（P0=0，无必修项）",
               kind=None, **payload):
        body = {"thread": "owner/repo#55", "issue": 55, "sid": sid,
                "at": "2026-11-11T00:00:00+00:00", "text": text}
        if kind:
            body["kind"] = kind
        body.update(payload)
        return {"seq": seq, "ts": time.time(), "sid": "file",
                "type": dw.SUPERVISOR_NOTICE_EVENT, "payload": body}

    def feed(self, *events):
        """One notice round: consume the batch, then retry from the ledger."""
        dw.handle_supervisor_notices(list(events), dry=False)
        dw._retry_pending(dry=False)

    def ledger_rows(self):
        return list(dw._notice_rows().values())

    def last(self):
        return json.loads(dw._notice_last_path().read_text(encoding="utf-8"))


class Positive(NoticeFixture):
    def test_payload_text_reaches_the_sid_in_payload(self):
        ev = self.notice(seq=11)
        dw.handle_supervisor_notices([ev])
        self.assertEqual(self.sends, [("session_probe_fixture", ev["payload"]["text"])])
        self.assertEqual(self.ledger_rows()[-1]["state"], "delivered")
        self.assertEqual(self.last()["last_ok_seq"], 11)

    def test_release_timeout_shape_needs_no_review_fields(self):
        ev = self.notice(seq=12, kind="release_timeout",
                         assignment="ISS-55", status="waiting_release",
                         started_at="2026-11-11T00:00:00+00:00",
                         text="【超时未关】#55 已滞留 3600s")
        dw.handle_supervisor_notices([ev])
        self.assertEqual(len(self.sends), 1)
        self.assertIn("超时未关", self.sends[0][1])
        self.assertEqual(self.ledger_rows()[-1]["kind"], "release_timeout")


class AtMostOnce(NoticeFixture):
    def test_same_seq_replay_is_not_resent(self):
        ev = self.notice(seq=20)
        dw.handle_supervisor_notices([ev])
        dw.handle_supervisor_notices([ev])
        self.assertEqual(len(self.sends), 1)

    def test_restart_does_not_resent_already_delivered(self):
        ev = self.notice(seq=21)
        dw.handle_supervisor_notices([ev])
        dw.configure(self.make_config(self.bus))   # 新进程，同一 state root
        dw.handle_supervisor_notices([ev])
        self.assertEqual(len(self.sends), 1)
        self.assertEqual(len(self.ledger_rows()), 1)

    def test_three_events_while_down_are_all_delivered_on_start(self):
        events = [self.notice(seq=30), self.notice(seq=31, text="第二条"),
                  self.notice(seq=32, text="第三条")]
        dw.handle_supervisor_notices(events)
        self.assertEqual([t for _s, t in self.sends],
                         [e["payload"]["text"] for e in events])

    def test_downtime_events_are_caught_up_through_main_once(self):
        """停机期间注 3 条（远少于 5000），重启后经 `_loop` 追回并全投。"""
        dw.save_cursor(40, "isolated")
        self.bus.seq = 43
        self.bus.events = [self.notice(seq=41, text="停机期一"),
                           self.notice(seq=42, text="停机期二"),
                           self.notice(seq=43, text="停机期三")]
        with mock.patch.object(dw, "acquire_lock", return_value=(True, "")), \
                mock.patch.object(dw, "release_lock"), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = dw.main(["--once"])
        self.assertEqual(rc, 0)
        self.assertEqual([t for _s, t in self.sends],
                         ["停机期一", "停机期二", "停机期三"])
        self.assertEqual(json.loads(dw.CURSOR().read_text(encoding="utf-8"))["seq"], 43)


class CounterOne_LedgerDrivenRetry(NoticeFixture):
    def test_event_not_returned_again_is_still_retried_from_ledger(self):
        self.probe_answer = (None, "探针不可达")
        ev = self.notice(seq=40)
        dw.handle_supervisor_notices([ev])
        self.assertEqual(self.sends, [])
        self.assertEqual(self.ledger_rows()[-1]["state"], "retry_later")

        # 下一轮 /events 不再返回该事件（游标已越过的语义）
        self.probe_answer = (True, {"sid": "session_probe_fixture"})
        dw._retry_pending()
        self.assertEqual(len(self.sends), 1)
        self.assertEqual(self.ledger_rows()[-1]["state"], "delivered")


class CounterTwo_ProbeUnknownEscalates(NoticeFixture):
    def test_three_rounds_of_unknown_end_as_probe_unknown_and_speak_once(self):
        self.probe_answer = (None, "探针不可达")
        dw.handle_supervisor_notices([self.notice(seq=50)])
        dw._retry_pending()
        dw._retry_pending()
        row = self.ledger_rows()[-1]
        self.assertEqual(row["state"], "probe_unknown")
        self.assertEqual(row["attempts"], dw.NOTICE_RETRY_LIMIT)
        types = [b["event"] for _p, b in self.injections]
        self.assertEqual(types.count(dw.SUPERVISOR_FAILED_EVENT), 1)
        self.assertEqual(self.last()["last_fail_state"], "probe_unknown")

        # 终态之后不再重试、不再出声
        dw._retry_pending()
        self.assertEqual(len(self.sends), 0)
        self.assertEqual(len(self.injections), 1)

    def test_one_loop_round_counts_as_exactly_one_attempt(self):
        """「消费＋重试」共用一份 attempted：一轮记一次，否则 3 轮会被折成 2 轮。"""
        self.probe_answer = (None, "探针不可达")
        attempted: set = set()
        dw.handle_supervisor_notices([self.notice(seq=51)], attempted=attempted)
        dw._retry_pending(attempted=attempted)
        self.assertEqual(self.ledger_rows()[-1]["attempts"], 1)
        self.assertEqual(self.ledger_rows()[-1]["state"], "retry_later")


class CounterThree_FullTextRetry(NoticeFixture):
    def test_retry_sends_the_whole_text_not_a_truncated_head(self):
        long_text = "【可关单】#55 " + "很长的正文" * 120
        self.assertGreater(len(long_text), 200)
        self.probe_answer = (None, "探针不可达")
        dw.handle_supervisor_notices([self.notice(seq=60, text=long_text)])

        self.probe_answer = (True, {"sid": "session_probe_fixture"})
        dw._retry_pending()
        self.assertEqual(len(self.sends), 1)
        self.assertEqual(self.sends[0][1], long_text)
        self.assertGreater(len(self.sends[0][1]), 200)
        self.assertEqual(len(self.ledger_rows()[-1]["text_head"]), 100)


class CounterFour_Archived(NoticeFixture):
    def test_archived_target_is_refused_with_zero_sends(self):
        self.probe_answer = (True, {"sid": "session_probe_fixture",
                                    "archived": True})
        dw.handle_supervisor_notices([self.notice(seq=70)])
        self.assertEqual(self.sends, [])
        self.assertEqual(self.ledger_rows()[-1]["state"], "refused_archived")
        self.assertEqual(self.last()["last_fail_state"], "refused_archived")


class CounterFive_Retired(NoticeFixture):
    def test_send_rc_4_is_refused_retired_not_delivered(self):
        self.send_rc = 4
        self.send_stderr = "send: 拒绝投递 —— 会话已退役"
        dw.handle_supervisor_notices([self.notice(seq=80)])
        self.assertEqual(len(self.sends), 1)     # 尝试过一次
        self.assertEqual(self.ledger_rows()[-1]["state"], "refused_retired")
        self.assertEqual(self.last().get("last_ok_seq"), None)


class CounterSix_DaemonDown(NoticeFixture):
    def test_cursor_not_written_and_backoff_doubles_to_thirty(self):
        self.bus = Bus(down=True)
        dw.configure(self.make_config(self.bus))
        backoffs = []

        def fake_sleep(sec):
            backoffs.append(sec)
            if len(backoffs) >= 6:
                raise KeyboardInterrupt("stop after six rounds")

        args = mock.Mock(dry_run=True, once=False, wait=0.0, supervisor="",
                         max_age=dw.MAX_AGE_S)
        with mock.patch.object(dw, "check_supervisor_health", return_value={}), \
                mock.patch.object(dw, "delivery_tick", return_value={}), \
                mock.patch.object(dw.time, "sleep", side_effect=fake_sleep), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                dw._loop(args, 58800, 5, "isolated", dw.BACKOFF_MIN)
        self.assertEqual(backoffs, [1.0, 2.0, 4.0, 8.0, 16.0, 30.0])
        self.assertFalse(dw.CURSOR().exists())


class CounterSeven_UnregisteredHeartbeat(NoticeFixture):
    def test_missing_registration_refuses_to_start(self):
        self.bus = Bus(registered=False)
        dw.configure(self.make_config(self.bus))
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = dw.main(["--once", "--dry-run"])
        self.assertEqual(rc, dw.EXIT_NOTICE_UNREGISTERED)
        self.assertIn("supervisor_notice", err.getvalue())
        self.assertIn("未注册", err.getvalue())
        self.assertEqual(self.sends, [])
        self.assertNotEqual(dw.EXIT_NOTICE_UNREGISTERED, dw.EXIT_LOCKED)

    def test_registered_name_starts_normally(self):
        self.assertIsNone(dw.notice_registration_problem() or None)


class SendRcTiers(NoticeFixture):
    """`tools/sess.py:72-75` 六档退出码，逐档都要有独立用例，且**每条都断言发送次数**。

    只断言终态字符串不够：rc=3/5 的旧实现在终态上最终也会变成 `probe_unknown`，
    光看状态名抓不到「同一事件实发 3 次」这个 P0。
    """

    def _run_rounds(self, rounds=3):
        attempted: set = set()
        dw.handle_supervisor_notices([self.notice(seq=100)], attempted=attempted)
        dw._retry_pending(attempted=attempted)
        for _ in range(rounds - 1):
            attempted = set()
            dw._retry_pending(attempted=attempted)

    def test_rc_3_is_indeterminate_sent_exactly_once(self):
        self.send_rc = 3
        self.send_stderr = "send: 不确定（可能已投、禁止重发）"
        self._run_rounds(3)
        self.assertEqual(len(self.sends), 1, "rc=3 可能已投，跑 3 轮也只能发 1 次")
        row = self.ledger_rows()[-1]
        self.assertEqual(row["state"], "indeterminate")
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(self.last()["last_fail_state"], "indeterminate")
        self.assertEqual(len(self.injections), 1)

    def test_rc_5_is_indeterminate_sent_exactly_once(self):
        self.send_rc = 5
        self._run_rounds(3)
        self.assertEqual(len(self.sends), 1, "rc=5 已受理，跑 3 轮也只能发 1 次")
        self.assertEqual(self.ledger_rows()[-1]["state"], "indeterminate")
        self.assertEqual(self.last()["last_fail_state"], "indeterminate")

    def test_indeterminate_never_returns_to_the_retry_path(self):
        """终态之后 `_retry_pending` 再也不碰它——这是 at-most-once 的最后一道闸。"""
        self.send_rc = 3
        self._run_rounds(2)
        self.assertEqual(len(self.sends), 1)
        for _ in range(3):
            dw._retry_pending(attempted=set())
        self.assertEqual(len(self.sends), 1)
        self.assertEqual(len(self.injections), 1, "出声也只该一次")

    def test_rc_2_is_refused_payload_and_not_retried(self):
        self.send_rc = 2
        self.send_stderr = "send: 正文为空，拒绝投递"
        self._run_rounds(3)
        self.assertEqual(len(self.sends), 1)
        self.assertEqual(self.ledger_rows()[-1]["state"], "refused_payload")

    def test_rc_1_retries_and_then_succeeds(self):
        """rc=1 是「上游明确拒绝、没有投出去」，堵死它会让可恢复的失败永远发不出去。"""
        self.send_rc = 1
        self.send_stderr = "send: 上游明确拒绝"
        dw.handle_supervisor_notices([self.notice(seq=101)])
        self.assertEqual(len(self.sends), 1)
        self.assertEqual(self.ledger_rows()[-1]["state"], "retry_later")

        self.send_rc = 0
        dw._retry_pending(attempted=set())
        self.assertEqual(len(self.sends), 2)
        self.assertEqual(self.ledger_rows()[-1]["state"], "delivered")

    def test_rc_0_is_delivered(self):
        self.send_rc = 0
        dw.handle_supervisor_notices([self.notice(seq=102)])
        self.assertEqual(len(self.sends), 1)
        self.assertEqual(self.ledger_rows()[-1]["state"], "delivered")

    def test_every_rc_tier_is_asserted_on_sends_not_only_on_state(self):
        """六档各自的 (终态, 发送次数)。每档用**独立夹具**，避免台账与 sends 串味。"""
        expected = {0: ("delivered", 1), 1: ("retry_later", 3),
                    2: ("refused_payload", 1), 3: ("indeterminate", 1),
                    4: ("refused_retired", 1), 5: ("indeterminate", 1)}
        for rc, (state, sends) in expected.items():
            with self.subTest(rc=rc):
                self.setUp()
                try:
                    self.send_rc = rc
                    dw.handle_supervisor_notices([self.notice(seq=110)])
                    for _ in range(2):
                        dw._retry_pending(attempted=set())
                    self.assertEqual(self.ledger_rows()[-1]["state"], state, f"rc={rc}")
                    self.assertEqual(len(self.sends), sends, f"rc={rc} 发送次数")
                finally:
                    self.tearDown()


class CounterEight_CursorRollback(NoticeFixture):
    def test_cursor_ahead_of_daemon_records_delta_and_resends_nothing(self):
        ev = self.notice(seq=41)
        dw.handle_supervisor_notices([ev])            # 先投过一条
        self.assertEqual(len(self.sends), 1)

        dw.save_cursor(50, "isolated")
        self.bus.seq = 40                             # daemon 的 seq 落后了
        self.bus.events = []
        args = mock.Mock(dry_run=False, once=True, wait=0.0, supervisor="",
                         max_age=dw.MAX_AGE_S)
        with mock.patch.object(dw, "acquire_lock", return_value=(True, "")), \
                mock.patch.object(dw, "release_lock"), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = dw.main(["--once"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.last()["delta"], 10)
        self.assertEqual(len(self.sends), 1)          # 回退不重发已投过的
        # 游标确实退回去了（不再是那个超前的 50），且没超过 daemon 的 seq
        self.assertLessEqual(json.loads(dw.CURSOR().read_text(encoding="utf-8"))["seq"], 40)

    def test_rollback_window_resends_nothing_delivered_and_catches_the_rest(self):
        """P1-A：回退窗口里既要有已投的也要有未投的。

        已投的 `seq=41` 靠指纹判重；未投的 `seq=42/43` 必须补投。只记 `delta`
        不重扫的实现会让 42/43 永久丢失（`/events?since=50` 永远取不到它们）。
        """
        delivered = self.notice(seq=41, text="回退前已投")
        dw.handle_supervisor_notices([delivered])
        self.assertEqual(len(self.sends), 1)

        dw.save_cursor(50, "isolated")
        self.bus.seq = 43                     # daemon 回退：环里现在只到 43
        self.bus.events = [delivered,
                           self.notice(seq=42, text="回退窗口内未投一"),
                           self.notice(seq=43, text="回退窗口内未投二")]
        args = mock.Mock(dry_run=False, once=False, wait=0.0, supervisor="",
                         max_age=dw.MAX_AGE_S)

        class _Stop(Exception):
            pass

        calls = {"n": 0}
        real_handle = dw.handle_supervisor_notices

        def _handle(evs, **kw):
            calls["n"] += 1
            if calls["n"] > 1:      # 回退轮不调它；下一轮补投，再下一轮收网
                raise _Stop()
            return real_handle(evs, **kw)

        with mock.patch.object(dw, "acquire_lock", return_value=(True, "")), \
                mock.patch.object(dw, "release_lock"), \
                mock.patch.object(dw, "handle_supervisor_notices", side_effect=_handle), \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                dw.main([])
            except _Stop:
                pass
        # 回退那一轮只退回重扫、不投；下一轮才是真正的补投
        self.assertEqual([t for _s, t in self.sends],
                         ["回退前已投", "回退窗口内未投一", "回退窗口内未投二"])
        states = {(r["seq"], r["state"]) for r in self.ledger_rows()}
        self.assertIn((41, "delivered"), states)
        self.assertIn((43, "delivered"), states)
        self.assertEqual(self.last()["delta"], 7)

    def test_rollback_is_a_no_op_when_cursor_is_in_sync(self):
        self.assertIsNone(dw.notice_cursor_rollback(40, 40))
        self.assertIsNone(dw.notice_cursor_rollback(40, 0))


class Fingerprint(NoticeFixture):
    def test_payload_key_order_does_not_change_the_fingerprint(self):
        a = {"seq": 1, "ts": 2.0, "type": dw.SUPERVISOR_NOTICE_EVENT,
             "payload": {"sid": "s", "text": "t", "issue": 1}}
        b = {"seq": 1, "ts": 2.0, "type": dw.SUPERVISOR_NOTICE_EVENT,
             "payload": {"issue": 1, "text": "t", "sid": "s"}}
        self.assertEqual(dw.notice_fingerprint(a), dw.notice_fingerprint(b))

    def test_same_seq_different_payload_is_a_different_event(self):
        """判重靠 payload，不是靠 seq——这是 QA 变异①（指纹退化成 seq）要抓的点。"""
        first = self.notice(seq=7, text="同一事件")
        dw.handle_supervisor_notices([first])
        dw.handle_supervisor_notices([first])
        self.assertEqual(len(self.sends), 1, "同一事件重放只能发 1 次")

        other = self.notice(seq=7, text="同 seq 不同 payload")
        dw.handle_supervisor_notices([other])
        self.assertEqual(len(self.sends), 2, "同 seq 不同事件必须照投")
        self.assertNotEqual(dw.notice_fingerprint(first), dw.notice_fingerprint(other))

    def test_event_outer_sid_is_never_used_as_the_target(self):
        ev = self.notice(seq=90, sid="session_probe_fixture")
        ev["sid"] = "file"
        dw.handle_supervisor_notices([ev])
        self.assertEqual(self.sends[0][0], "session_probe_fixture")

    def test_missing_sid_or_text_is_refused_and_speaks(self):
        dw.handle_supervisor_notices([self.notice(seq=91, sid="")])
        self.assertEqual(self.sends, [])
        self.assertEqual(self.ledger_rows()[-1]["state"], "refused_payload")
        self.assertEqual(self.injections[-1][1]["event"], dw.SUPERVISOR_FAILED_EVENT)


if __name__ == "__main__":
    unittest.main()
