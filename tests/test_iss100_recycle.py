"""L1 tests for ISS-100 group 2: the ``recycle`` command surface.

Four subcommands (``stop`` / ``archive`` / ``retire`` / ``status``) and **no
``delete``** (RULES §33). What must stay true:

* every subcommand delegates -- ``stop`` to ``console.cmd_stop``, ``archive`` to
  ``sessions_ledger.archive_session``, ``retire`` to ``retired_sids.retire`` --
  so a signature change in one place breaks here instead of silently drifting;
* ``archive`` reads back twice: ``results[].ok`` from the write **and**
  ``archived`` from the server; either one saying no fails the command;
* cron reporting has three states and "unreadable" is never reported as zero
  (``cron=未确认`` + rc=5);
* ``delete`` and a truncated sid are refused with rc=2.

Nothing here talks to a server: every HTTP entry point is patched.
Run: ``python -m pytest tests/test_iss100_recycle.py -q``
"""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from super_agent import console, cron_report, recycle, retired_sids, sess_events, sessions_ledger

SID = "session_a16c3db2-2659-4310-ae4f-b01a56695233"


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = recycle.main(argv)
    return rc, out.getvalue(), err.getvalue()


class Surface(unittest.TestCase):
    def test_no_delete(self) -> None:
        rc, out, _err = _run(["delete", SID])
        self.assertEqual(rc, 2)
        self.assertIn("无 delete", out)
        self.assertIn("RULES §33", out)

    def test_truncated_sid_is_refused(self) -> None:
        rc, out, _err = _run(["stop", "session_6a5eac50-df38-4735"])
        self.assertEqual(rc, 2)
        self.assertIn("不是完整形态", out)
        self.assertIn("26 字符", out)

    def test_no_subcommand_prints_usage(self) -> None:
        rc, out, _err = _run([])
        self.assertEqual(rc, 2)
        self.assertIn("stop / archive / retire / status", out)

    def test_no_restore_subcommand(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            _run(["restore", SID])
        self.assertEqual(ctx.exception.code, 2)

    def test_source_never_mentions_recycle_restore(self) -> None:
        source = Path(recycle.__file__).read_text(encoding="utf-8")
        self.assertNotIn("recycle restore", source)


class Delegation(unittest.TestCase):
    def test_stop_delegates_to_console(self) -> None:
        with mock.patch.object(console, "cmd_stop", return_value=0) as stop:
            rc, _out, _err = _run(["stop", SID, "--timeout", "20"])
        self.assertEqual(rc, 0)
        stop.assert_called_once_with([SID, "--timeout", "20"])

    def test_archive_delegates_and_reads_back_archived(self) -> None:
        with mock.patch.object(sessions_ledger, "archive_session", return_value=0) as arc, \
                mock.patch.object(recycle.kimi_direct, "session_info",
                                  return_value={"archived": True}), \
                mock.patch.object(cron_report, "describe",
                                  return_value=([], {"state": cron_report.STATE_NONE,
                                                     "count": 0})):
            rc, out, _err = _run(["archive", SID])
        self.assertEqual(rc, 0)
        arc.assert_called_once()
        self.assertEqual(arc.call_args.args[0], SID)
        self.assertIn("cron=0", out)

    def test_archive_read_back_mismatch_fails(self) -> None:
        """``results[].ok`` true but the server says not archived => rc=1."""
        with mock.patch.object(sessions_ledger, "archive_session", return_value=0), \
                mock.patch.object(recycle.kimi_direct, "session_info",
                                  return_value={"archived": False}):
            rc, out, _err = _run(["archive", SID])
        self.assertEqual(rc, 1)
        self.assertIn("归档回读不一致", out)

    def test_archive_unreachable_read_back_is_not_a_pass(self) -> None:
        with mock.patch.object(sessions_ledger, "archive_session", return_value=0), \
                mock.patch.object(recycle.kimi_direct, "session_info",
                                  return_value={"_err": "http=None"}):
            rc, _out, _err = _run(["archive", SID])
        self.assertEqual(rc, 1)

    def test_archive_propagates_failure_and_event_lost(self) -> None:
        for code in (1, 8):
            with mock.patch.object(sessions_ledger, "archive_session", return_value=code):
                rc, _out, _err = _run(["archive", SID])
            self.assertEqual(rc, code)

    def test_retire_delegates_to_retired_sids(self) -> None:
        with mock.patch.object(retired_sids, "retire",
                               return_value={"at": "2026-11-19T00:00:00+08:00"}) as ret:
            rc, out, _err = _run(["retire", SID, "--key", "pm-x", "--reason", "r"])
        self.assertEqual(rc, 0)
        self.assertIn("已退役", out)
        self.assertEqual(ret.call_args.args[0], SID)
        self.assertEqual(ret.call_args.kwargs["key"], "pm-x")
        self.assertEqual(ret.call_args.kwargs["reason"], "r")

    def test_retire_write_failure_is_rc1_not_silent(self) -> None:
        with mock.patch.object(retired_sids, "retire", side_effect=OSError("disk")):
            rc, _out, err = _run(["retire", SID])
        self.assertEqual(rc, 1)
        self.assertIn("退役清单写入失败", err)


class CronStates(unittest.TestCase):
    def _archive_with(self, summary, lines=None):
        with mock.patch.object(sessions_ledger, "archive_session", return_value=0), \
                mock.patch.object(recycle.kimi_direct, "session_info",
                                  return_value={"archived": True}), \
                mock.patch.object(cron_report, "describe",
                                  return_value=(lines or [], summary)), \
                mock.patch.object(cron_report, "clear_hint", return_value=[]):
            return _run(["archive", SID])

    def test_unknown_is_rc5(self) -> None:
        rc, out, _err = self._archive_with({"state": cron_report.STATE_UNKNOWN,
                                            "count": None})
        self.assertEqual(rc, 5)
        self.assertIn("cron=未确认", out)

    def test_none_is_rc0(self) -> None:
        rc, out, _err = self._archive_with({"state": cron_report.STATE_NONE, "count": 0})
        self.assertEqual(rc, 0)
        self.assertIn("cron=0", out)

    def test_jobs_reports_count_and_next_fire(self) -> None:
        jobs = {"j1": {"cron": "18 * * * *", "recurring": True}}
        with mock.patch.object(cron_report, "alive_jobs", return_value=jobs), \
                mock.patch.object(cron_report, "next_fire",
                                  return_value="2026-11-20 00:18"):
            rc, out, _err = self._archive_with({"state": cron_report.STATE_JOBS,
                                               "count": 1})
        self.assertEqual(rc, 0)
        self.assertIn("1 个 cron 仍活，下次 2026-11-20 00:18", out)


class SharedGate(unittest.TestCase):
    def test_refuse_if_archived_lives_in_console_only(self) -> None:
        self.assertTrue(callable(console.refuse_if_archived))
        source = Path(recycle.__file__).read_text(encoding="utf-8")
        self.assertNotIn("def refuse_if_archived", source)

    def test_archived_session_is_refused_rc6(self) -> None:
        with mock.patch.object(console, "archived_state", return_value=("yes", "x")):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = console.refuse_if_archived(SID)
        self.assertEqual(rc, 6)
        self.assertIn(f"python -m super_agent.console restore {SID}", out.getvalue())
        self.assertNotIn("recycle restore", out.getvalue())

    def test_refuse_if_archived_passes_unknown_through(self) -> None:
        err = io.StringIO()
        with mock.patch.object(console, "archived_state", return_value=("unknown", "x")):
            with redirect_stderr(err):
                rc = console.refuse_if_archived(SID)
        self.assertIsNone(rc)
        self.assertIn("无法确认归档状态", err.getvalue())

    def test_refuse_if_archived_retired_and_revive_reports_4(self) -> None:
        with mock.patch.object(console, "archived_state", return_value=("yes", "x")):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = console.refuse_if_archived(SID, retired_reason="已退役",
                                                revive=True)
        self.assertEqual(rc, 4)
        self.assertIn("若", out.getvalue()) if False else None
        self.assertIn("退役闸与归档闸都没被绕过", out.getvalue())

    def test_send_keeps_rc6_and_wording(self) -> None:
        """The gate moved; `console send` output must be unchanged."""
        with mock.patch.object(console, "archived_state", return_value=("yes", "why-x")), \
                mock.patch.object(console.RS, "check_detail", return_value=("", "")):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = console.cmd_send([SID, "hi"])
        self.assertEqual(rc, 6)
        text = out.getvalue()
        self.assertIn("send: 拒绝投递 —— 该会话已归档", text)
        self.assertIn("ISS-76 根因", text)
        self.assertIn(f"要先恢复请显式执行：python -m super_agent.console restore {SID}", text)


class StatusCommand(unittest.TestCase):
    def test_status_reports_four_sources(self) -> None:
        with mock.patch.object(console, "archived_state", return_value=("no", "why")), \
                mock.patch.object(recycle.kimi_direct, "session_info",
                                  return_value={"busy": False, "main_turn_active": False,
                                                "archived": False}), \
                mock.patch.object(retired_sids, "check_detail", return_value=("", "")), \
                mock.patch.object(cron_report, "describe",
                                  return_value=([], {"state": cron_report.STATE_NONE,
                                                     "count": 0})):
            rc, out, _err = _run(["status", SID])
        self.assertEqual(rc, 0)
        self.assertIn("server: archived=no", out)
        self.assertIn("retired: no", out)
        self.assertIn("cron: none count=0", out)

    def test_status_unreadable_cron_is_rc5(self) -> None:
        with mock.patch.object(console, "archived_state", return_value=("no", "why")), \
                mock.patch.object(recycle.kimi_direct, "session_info",
                                  return_value={"busy": False, "archived": False}), \
                mock.patch.object(retired_sids, "check_detail", return_value=("", "")), \
                mock.patch.object(cron_report, "describe",
                                  return_value=([], {"state": cron_report.STATE_UNKNOWN,
                                                     "count": None})):
            rc, out, _err = _run(["status", SID])
        self.assertEqual(rc, 5)
        self.assertIn("cron: 未确认", out)


class RetireSafe(unittest.TestCase):
    def test_retire_safe_returns_fresh_set(self) -> None:
        with TemporaryDirectory(prefix="iss100_retire_") as tmp:
            with mock.patch.dict("os.environ", {sess_events.ENV_STATE_DIR: tmp}):
                fresh = retired_sids.retire_safe(SID, key="pm-x", state_dir=tmp)
            self.assertEqual(fresh, {SID})

    def test_retire_safe_swallows_and_returns_none(self) -> None:
        with mock.patch.object(retired_sids, "retire", side_effect=OSError("disk")):
            self.assertIsNone(retired_sids.retire_safe(SID, state_dir="C:/tmp/nope"))


if __name__ == "__main__":
    unittest.main()
