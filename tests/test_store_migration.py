"""Tests for the runtime-root selection and the migration guard in ``store.py``.

Two behaviours are load-bearing before the first write:

* ``choose_root`` reads the legacy ``state/`` only when ``data/`` holds no
  engine file, and it keys off presence rather than a file count, because a
  normal round writes 5 of 9 files.
* ``check_migration`` refuses a half-finished migration: the ``.migrating``
  marker, or one of the six JSON files that a loader would otherwise read as
  empty defaults.

Run: ``python -m pytest tests/test_store_migration.py -q``
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from super_agent import store as store_mod  # noqa: E402


class ChooseRoot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.data = base / "data"
        self.state = base / "state"

    def tearDown(self):
        self.tmp.cleanup()

    def test_fresh_install_uses_data(self):
        chosen, warn = store_mod.choose_root(self.data, self.state)
        self.assertEqual(chosen, self.data)
        self.assertEqual(warn, "")

    def test_legacy_state_is_read_when_data_is_empty(self):
        self.state.mkdir()
        (self.state / "index.json").write_text("{}", encoding="utf-8")
        chosen, warn = store_mod.choose_root(self.data, self.state)
        self.assertEqual(chosen, self.state)
        self.assertIn("legacy path", warn)

    def test_five_engine_files_are_enough_to_trigger_fallback(self):
        # A real ``state/`` holds 5 of the 9 engine files; a count-based
        # criterion would never fire here.
        self.state.mkdir()
        for name in ("index.json", "events.jsonl", "cursor.json",
                     "run_heartbeat.json", "health.json"):
            (self.state / name).write_text("{}", encoding="utf-8")
        chosen, _warn = store_mod.choose_root(self.data, self.state)
        self.assertEqual(chosen, self.state)

    def test_empty_data_dir_does_not_block_fallback(self):
        self.data.mkdir()
        self.state.mkdir()
        (self.state / "index.json").write_text("{}", encoding="utf-8")
        chosen, _warn = store_mod.choose_root(self.data, self.state)
        self.assertEqual(chosen, self.state)

    def test_both_present_uses_data_and_warns(self):
        self.data.mkdir()
        self.state.mkdir()
        (self.data / "index.json").write_text("{}", encoding="utf-8")
        (self.state / "index.json").write_text("{}", encoding="utf-8")
        chosen, warn = store_mod.choose_root(self.data, self.state)
        self.assertEqual(chosen, self.data)
        self.assertIn("both", warn)

    def test_marker_alone_is_not_an_engine_file(self):
        self.data.mkdir()
        (self.data / store_mod.MIGRATION_MARKER).write_text("", encoding="utf-8")
        self.state.mkdir()
        (self.state / "index.json").write_text("{}", encoding="utf-8")
        chosen, _warn = store_mod.choose_root(self.data, self.state)
        self.assertEqual(chosen, self.state)


class MigrationGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_clean_directory_passes(self):
        store_mod.check_migration(self.root)

    def test_marker_refuses(self):
        (self.root / store_mod.MIGRATION_MARKER).write_text("", encoding="utf-8")
        with self.assertRaises(RuntimeError) as ctx:
            store_mod.check_migration(self.root)
        self.assertIn("迁移未完成", str(ctx.exception))

    def test_every_strict_json_file_refuses_when_half_written(self):
        for name in store_mod.STRICT_JSON:
            with self.subTest(name=name):
                path = self.root / name
                path.write_text('{"half": ', encoding="utf-8")
                try:
                    with self.assertRaises(RuntimeError) as ctx:
                        store_mod.check_migration(self.root)
                    self.assertIn(name, str(ctx.exception))
                    self.assertIn("不是合法 JSON", str(ctx.exception))
                finally:
                    path.unlink()

    def test_guard_list_is_exactly_six(self):
        self.assertEqual(len(store_mod.STRICT_JSON), 6)

    def test_store_refuses_before_creating_anything(self):
        data = self.root / "data"
        data.mkdir()
        (data / store_mod.MIGRATION_MARKER).write_text("", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            store_mod.Store(data)
        self.assertEqual(sorted(p.name for p in data.iterdir()),
                         [store_mod.MIGRATION_MARKER])

    def test_jsonl_torn_line_is_tolerated(self):
        # events.jsonl is append-only JSONL: a torn last line is normal.
        (self.root / "events.jsonl").write_text('{"a": 1}\n{"b"', encoding="utf-8")
        store_mod.check_migration(self.root)


if __name__ == "__main__":
    unittest.main()
