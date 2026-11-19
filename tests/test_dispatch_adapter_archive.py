"""The archive branch the engine takes when an adapter is injected (#98 D4.2).

``engine._archive_one`` has two branches:

* adapter present -> ``adapter.archive(...)`` (``engine.py:3836``);
* no adapter -> the session API.

``build_engine()`` never passes an adapter, so the first branch is unreachable
in production today. That is exactly why it needs a test that **runs it**: a
branch nobody executes is a branch nobody has checked, and the copy that landed
here is the first thing an injected adapter would call.

The console is this package's ``dispatch_cli``, which has no ``archive``
subcommand. So the expected answer is an explicit ``unsupported_verb`` with the
verb named -- not a success, and not a generic non-zero code the caller cannot
tell apart from a transport failure.

Run: ``python -m pytest tests/test_dispatch_adapter_archive.py -q``
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from super_agent import config
from super_agent.dispatch_adapter import DispatchAdapter
from super_agent.engine import Engine
from super_agent.github_source import NullSource
from super_agent.store import Store

SID = "session_0b7c9f1e-2a34-4c56-8d90-1a2b3c4d5e6f"


class _Store:
    """Minimal stand-in: the branch under test never touches the store."""

    def __getattr__(self, name):          # pragma: no cover - defensive
        raise AssertionError(f"archive branch must not touch store.{name}")


def _engine(adapter) -> Engine:
    cfg = config.load()
    return Engine(cfg, _Store(), NullSource(), adapter=adapter)


class ArchiveThroughAdapterTest(unittest.TestCase):
    def test_engine_archive_reaches_the_adapter(self):
        """The injected adapter is what the engine calls (branch reachable)."""
        calls: list[list[str]] = []

        def runner(argv):
            calls.append(list(argv))
            return 0, '{"ok": true, "code": "ok"}', ""

        adapter = DispatchAdapter(console="-m super_agent.dispatch_cli", runner=runner)
        ok, err = _engine(adapter)._archive_one(SID)
        self.assertTrue(ok, err)
        self.assertEqual(calls[0][0], "archive")
        self.assertIn(SID, calls[0])

    def test_the_real_console_reports_the_missing_verb(self):
        """No runner injected: the console really runs, and answers honestly."""
        adapter = DispatchAdapter(console="-m super_agent.dispatch_cli")
        ok, err = _engine(adapter)._archive_one(SID)
        self.assertFalse(ok)
        self.assertIn("不支持", err)
        self.assertIn("archive", err)

    def test_the_missing_verb_is_not_reported_as_a_failed_verb(self):
        adapter = DispatchAdapter(console="-m super_agent.dispatch_cli")
        result = adapter._call(["archive", "--json", "--assignment", SID])
        self.assertEqual(result["code"], "unsupported_verb")
        self.assertNotIn(result["code"], ("adapter_rc_nonzero", "adapter_spawn_failed"))


class DefaultConsoleTest(unittest.TestCase):
    """``build()`` must point somewhere that exists (#98 D4.2)."""

    def test_build_defaults_to_this_package_module(self):
        adapter = DispatchAdapter(console="")          # explicit empty
        self.assertEqual(adapter.console, "")
        built = __import__("super_agent.dispatch_adapter", fromlist=["build"]).build(
            config.load())
        self.assertTrue(built.console)
        self.assertTrue(built.console.startswith("-m ")
                        or Path(built.console).is_file(), built.console)

    def test_a_module_console_is_not_treated_as_a_path(self):
        """The '-m' form is executed as a module, not as a file named '-m'."""
        seen: list[list[str]] = []
        adapter = DispatchAdapter(console="-m super_agent.dispatch_cli",
                                  runner=lambda argv: (seen.append(argv), (0, "{}", ""))[1])
        adapter._call(["resolve", "--json"])
        self.assertEqual(seen, [["resolve", "--json"]])


class VerbTableGapTest(unittest.TestCase):
    """The gap is recorded where a reader will look (docs/STATUS.md)."""

    def test_status_lists_the_adapter_gap(self):
        text = (Path(__file__).resolve().parents[1] / "docs" / "STATUS.md").read_text(
            encoding="utf-8")
        self.assertIn("dispatch_adapter", text)
        for verb in ("create-worker", "archive", "reconcile-close"):
            self.assertIn(verb, text)


if __name__ == "__main__":
    unittest.main()
