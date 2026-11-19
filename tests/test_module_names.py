"""Two pairs of near-identical module names, and the guard that keeps them apart.

The copies from the private workbench landed under names that differ by a
single suffix from modules already here (``sessions_ledger`` vs
``session_ledger``) or by word order (``sess_events`` vs ``session_events``).
Renaming them would break the "source -> target" table QA diffs against, so the
risk is contained instead:

* the **symbol sets do not overlap**, so importing the wrong one fails loudly
  with ``AttributeError`` at the call, not silently somewhere downstream;
* the **docstrings name each other**, so a reader who lands in the wrong file is
  told in the first paragraph;
* the **direction of the shared responsibility** is pinned by a symbol that
  exists on exactly one side (``emit`` writes, ``replay`` reads).

Run: ``python -m pytest tests/test_module_names.py -q``
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from super_agent import console, session_events, session_ledger, sessions_ledger

ROOT = Path(__file__).resolve().parents[1] / "super_agent"


class LedgerPairTest(unittest.TestCase):
    """``sessions_ledger`` is the sessions.json CLI; ``session_ledger`` is the issue side."""

    def test_they_are_distinct_modules(self):
        self.assertIsNot(sessions_ledger, session_ledger)

    def test_the_symbol_sets_do_not_overlap(self):
        cli_symbols = {n for n in dir(sessions_ledger) if n.startswith("cmd_")}
        issue_symbols = {n for n in dir(session_ledger) if n.startswith("cmd_")}
        self.assertEqual(cli_symbols & issue_symbols, set())
        self.assertTrue({"cmd_set", "cmd_retire"} <= cli_symbols)

    def test_the_close_guard_reader_does_not_live_on_the_cli_side(self):
        self.assertTrue(hasattr(session_ledger, "sid_problem"))
        self.assertFalse(hasattr(sessions_ledger, "FULL_SID_RE"))

    def test_each_docstring_names_the_other(self):
        self.assertIn("session_ledger.py", (sessions_ledger.__doc__ or ""))
        self.assertIn("sessions_ledger", (session_ledger.__doc__ or ""))


class EventsPairTest(unittest.TestCase):
    """``sess_events`` writes the stream; ``session_events`` only reads it."""

    def test_they_are_distinct_modules(self):
        self.assertIsNot(session_events, session_events.__class__)
        from super_agent import sess_events
        self.assertIsNot(sess_events, session_events)

    def test_the_write_side_emits_and_the_read_side_replays(self):
        from super_agent import sess_events
        self.assertTrue(callable(sess_events.emit))
        self.assertTrue(callable(session_events.replay))

    def test_the_read_side_has_no_emitter(self):
        self.assertFalse(hasattr(session_events, "emit"))

    def test_each_docstring_names_the_other(self):
        from super_agent import sess_events
        self.assertIn("session_events", (sess_events.__doc__ or ""))
        self.assertIn("sess_events", (session_events.__doc__ or ""))


class OneSidShapeTest(unittest.TestCase):
    """D4.5: the full-sid regex exists exactly once in the package.

    The engine's second copy is removed in #98 step 8; until that commit lands
    the assertion below would be red, and a red test is not a guard. What is
    checked here is the shape that survives: the console validates through the
    ledger's shared helpers rather than holding a regex of its own.
    """

    def test_the_console_reader_uses_the_shared_shape(self):
        self.assertTrue(hasattr(console, "sid_ok_or_explain"))
        source = (ROOT / "console.py").read_text(encoding="utf-8")
        self.assertNotIn("re.compile", source)

    def test_the_ledger_owns_the_shape(self):
        self.assertTrue(hasattr(session_ledger, "FULL_SID_RE"))
        self.assertTrue(callable(session_ledger.sid_problem))


if __name__ == "__main__":
    unittest.main()
