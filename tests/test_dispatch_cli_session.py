"""L1 tests for the two ``dispatch_cli`` ergonomics fixes of 2026-11-21.

Both come from one measured complaint: a PM creating a QA session reported the
tool as "stuck" while the warm-up had actually finished in 5 seconds. Two things
made that reading inevitable, and neither was the warm-up being slow:

* **nothing was printed while it waited.** ``--detach`` returns only after the
  warm-up turn completes, so a silent 2m17s looks exactly like a hang. A marker
  every 15 seconds costs nothing and separates "working" from "hung".
* **the flag was named after the mechanism, not the intent.** Callers who want
  "just make me a session" had to find ``--activate-only`` and, having found it,
  still hit ``--task is required`` at argparse level because ``--task ""`` reads
  as "not given". ``--create-session`` names the action, and an empty task is
  accepted with it.

Run: ``python -m pytest tests/test_dispatch_cli_session.py -q``
"""
from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

from super_agent import dispatch_cli as D


def _parser():
    """The real parser, built the way ``main`` builds it."""
    return D.build_parser() if hasattr(D, "build_parser") else None


class EmptyTaskIsAcceptedForCreateSession(unittest.TestCase):
    """``--task ""`` must not be rejected when only a session is wanted.

    The rejection came from ``if not args.task``: an empty string is falsy, so
    argparse's "you did not pass --task" and "you passed an empty --task" were
    the same branch. Callers worked around it with a single space, which works
    and teaches the wrong lesson.
    """

    def test_create_session_without_a_task_is_not_a_parser_error(self):
        with mock.patch.object(D, "read_token", side_effect=SystemExit(0)), \
             mock.patch.object(D, "quota_gate"), \
             mock.patch.object(D, "load_catalog", side_effect=SystemExit(0)), \
             mock.patch.object(D, "discover_base", side_effect=SystemExit(0)):
            err = io.StringIO()
            with contextlib.redirect_stderr(err), \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    D.main(["--create-session", "--cwd", "."])
            self.assertNotIn("--task is required", err.getvalue())

    def test_an_empty_task_still_errors_without_the_flag(self):
        """The guard must stay: a task run with no task text is a real mistake."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                D.main(["--task", "", "--cwd", "."])
        self.assertIn("--task is required", err.getvalue())

    def test_the_old_flag_name_still_works(self):
        """``--activate-only`` is the published name; renaming must not break it."""
        self.assertTrue(hasattr(D, "main"))
        err = io.StringIO()
        with mock.patch.object(D, "read_token", side_effect=SystemExit(0)):
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    D.main(["--activate-only", "--cwd", "."])
        self.assertNotIn("--task is required", err.getvalue())


class WarmUpReportsProgress(unittest.TestCase):
    """The wait must be visible, and the completion line must carry the duration.

    The 15-second beat is not asserted by sleeping: the test drives the tick
    function through a short interval so the assertion stays about behaviour
    (a line reaches stderr) rather than about wall-clock timing.
    """

    def test_a_marker_reaches_stderr_while_the_turn_runs(self):
        printed = []

        def slow_turn(*_a, **_k):
            # Let the beat fire at least once before returning.
            import time as _t
            _t.sleep(0.35)
            return "pong"

        with mock.patch.object(D, "run_turn", side_effect=slow_turn), \
             mock.patch.object(D.time, "sleep", wraps=D.time.sleep):
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                # 15s is the production interval; patch it down for the test only.
                with mock.patch.object(D, "WARMUP_BEAT_S", 0.1, create=True):
                    D.activate_session("http://x", "t", "session_x", 30)
        out = err.getvalue()
        self.assertIn("warm-up still running", out)
        printed.append(out)

    def test_the_completion_line_reports_the_elapsed_seconds(self):
        with mock.patch.object(D, "run_turn", return_value="pong"):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                D.activate_session("http://x", "t", "session_x", 30)
        line = buf.getvalue()
        self.assertIn("warm-up turn completed in", line)
        self.assertRegex(line, r"in \d+\.\ds")


if __name__ == "__main__":
    unittest.main()
