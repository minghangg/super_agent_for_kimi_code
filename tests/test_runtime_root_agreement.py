"""The three root readers must answer the same directory (#140 QA 7222834246).

``cli._build`` builds the store the engine writes into; ``at_serve.main``
serves the pages a person reads back out of the same root. They used to spell
the resolution chain out separately, and they drifted: ``at_serve`` kept
``state_root`` alone while ``cli`` also consulted a **declared**
``legacy_state_dir``. With ``data`` empty and the declared old root holding the
engine files -- exactly "upgraded, not migrated yet" -- the page served an empty
directory and said nothing.

Every case here runs the real entry points, because the defect was an
equivalence between two entry points that neither one's own tests could see.

Run: ``python -m pytest tests/test_runtime_root_agreement.py -q``
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from super_agent import agent_config, at_serve, cli, config, config_paths  # noqa: E402

ENGINE = ("index.json", "events.jsonl", "cursor.json", "run_heartbeat.json",
          "health.json")


def _seed(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name in ENGINE:
        (root / name).write_text("{}\n", encoding="utf-8")


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.cwd = self.base / "work"
        self.cwd.mkdir()
        self._old_cwd = Path.cwd()
        os.chdir(self.cwd)
        self._env = os.environ.get("SAFKC_AGENT_CONFIG")
        os.environ["SAFKC_AGENT_CONFIG"] = str(self.base / "absent.json")

    def tearDown(self) -> None:
        os.chdir(self._old_cwd)
        if self._env is None:
            os.environ.pop("SAFKC_AGENT_CONFIG", None)
        else:
            os.environ["SAFKC_AGENT_CONFIG"] = self._env
        self._tmp.cleanup()

    def write_cfg(self, doc: dict) -> str:
        """Write the config *and* point the agent-config layer at it.

        ``at_serve.main`` has no ``--config`` flag: it reads the deployment
        file through ``agent_config``, i.e. ``$SAFKC_AGENT_CONFIG``. Both
        readers are pointed at the one file, and the shape is the deployed one
        (``paths.state_dir``, ``legacy_state_dir``) so neither is compared
        against a spelling only a test uses.
        """
        path = self.base / "cfg.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        os.environ["SAFKC_AGENT_CONFIG"] = str(path)
        agent_config._reset_cache()
        return str(path)

    def write_root_cfg(self, state_dir: Path, legacy: Path) -> str:
        return self.write_cfg({"paths": {"state_dir": str(state_dir)},
                               "legacy_state_dir": str(legacy)})

    def cli_root(self, cfg_path: str) -> Path:
        class Args:
            config = cfg_path
            state_dir = None
            mode = None
            role = None
            source = "none"
            allowlist = None
            session_api = None
            accept_empty_index = False
            allow_empty_cursor = False

        _cfg, store = cli._build(Args())
        return Path(store.root).resolve()

    def at_serve_root(self, cfg_path: str) -> Path:
        """The root ``at_serve.main`` resolves, without opening a port.

        ``run`` is replaced: this pins the **resolution**, and starting an HTTP
        server in a unit test would pin something else.
        """
        captured = {}

        def fake_run(*, root, port, transport):
            captured["root"] = Path(root).resolve()
            return 0

        original_run = at_serve.run
        at_serve.run = fake_run
        try:
            with redirect_stderr(io.StringIO()):
                code = at_serve.main([])
        finally:
            at_serve.run = original_run
        self.assertEqual(code, 0)
        self.assertIn("root", captured, "at_serve.main 没有走到 run()")
        return captured["root"]


class DeclaredLegacyRoot(_Base):
    """A declared legacy root is read by both readers, or by neither."""

    def test_both_readers_pick_the_declared_legacy_root(self):
        old = self.base / "old"
        _seed(old)
        cfg_path = self.write_root_cfg(self.base / "data", old)
        self.assertEqual(self.cli_root(cfg_path), old.resolve())
        self.assertEqual(self.at_serve_root(cfg_path), old.resolve(),
                         "at_serve 没读已声明的 legacy_state_dir ⇒ 页面读空目录")

    def test_both_readers_agree_when_the_new_root_has_data(self):
        new = self.base / "data"
        old = self.base / "old"
        _seed(new)
        _seed(old)
        cfg_path = self.write_root_cfg(new, old)
        self.assertEqual(self.cli_root(cfg_path), new.resolve())
        self.assertEqual(self.at_serve_root(cfg_path), new.resolve())


class UndeclaredLegacyRoot(_Base):
    """No declaration means no legacy directory, for both readers."""

    def test_neither_reader_adopts_a_sibling_state_directory(self):
        _seed(self.cwd / "state")
        cfg_path = self.write_cfg({"paths": {"state_dir": str(self.cwd / "data")}})
        self.assertEqual(self.cli_root(cfg_path), (self.cwd / "data").resolve())
        self.assertEqual(self.at_serve_root(cfg_path),
                         (self.cwd / "data").resolve(),
                         "at_serve 按目录名采纳了同级 state/ ⇒ 又回到按名查找")


class OneChain(unittest.TestCase):
    """The chain itself: declared legacy wins only when the new root is empty."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_declared_legacy_is_read_when_the_new_root_is_empty(self):
        old = self.base / "old"
        _seed(old)
        cfg = config.load(None, {"state_dir": str(self.base / "data"),
                                 "legacy_state_dir": str(old)})
        root, note = config_paths.runtime_root(cfg)
        self.assertEqual(root.resolve(), old.resolve())
        self.assertIn("legacy", note)

    def test_undeclared_legacy_returns_the_new_root_without_a_note(self):
        cfg = config.load(None, {"state_dir": str(self.base / "data")})
        root, note = config_paths.runtime_root(cfg)
        self.assertEqual(root.resolve(), (self.base / "data").resolve())
        self.assertEqual(note, "")

    def test_a_declared_root_equal_to_the_chosen_one_is_not_a_migration(self):
        both = self.base / "same"
        _seed(both)
        cfg = config.load(None, {"state_dir": str(both),
                                 "legacy_state_dir": str(both)})
        root, note = config_paths.runtime_root(cfg)
        self.assertEqual(root.resolve(), both.resolve())
        self.assertEqual(note, "", "同一个目录不该报成「读到旧根」")


if __name__ == "__main__":
    unittest.main()
