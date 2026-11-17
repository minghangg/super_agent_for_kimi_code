"""L1 tests for the resolution chain of ``session_events`` (ISS-79).

The chain used to end at a guess: if nothing was declared, it looked for a
workbench checkout beside the working directory and returned that path. That
made a misconfigured engine look healthy - it read somebody else's stream and
never said so. ISS-79 removed the guess, and these tests pin the removal at the
**behaviour** layer, because the docstring alone is not evidence: a copy with
the old fallback re-inserted in front of the ``raise`` keeps every sentence of
the docstring intact.

Three assertions, all on the real module:

* nothing declared (both env vars cleared, empty config) raises
  ``SessionStreamNotConfigured``;
* ``engine.session_state_dir`` points at a directory -> the answer is that
  directory's ``events.jsonl``, not an exception;
* a workbench checkout beside the working directory is **not** a source: the
  directory exists, the file exists, and the chain still raises.

Every case runs in a temporary directory; the live ``state/`` is never read.
Run: ``python -m pytest tests/test_session_stream_config.py -q``
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from super_agent import session_events

ENV_KEYS = ("SESS_EVENTS_STATE_DIR", "LWA_STATE_DIR")


class ChainResolution(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()
        self._env = {k: os.environ.get(k) for k in ENV_KEYS}
        self._cwd = Path.cwd()
        for key in ENV_KEYS:
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        for key, val in self._env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def test_nothing_declared_raises_instead_of_guessing(self):
        with self.assertRaises(session_events.SessionStreamNotConfigured):
            session_events.resolve_events_path({})

    def test_declared_session_root_is_used(self):
        root = self.tmp / "state"
        root.mkdir()
        got = session_events.resolve_events_path({"engine": {"session_state_dir": str(root)}})
        self.assertEqual(got, root / "events.jsonl")

    def test_checkout_beside_cwd_is_not_a_source(self):
        """The old level: a checkout next to the working directory, file present."""
        beside = self.tmp / session_events.LWA_DIRNAME / "state"
        beside.mkdir(parents=True)
        (beside / session_events.EVENTS_NAME).write_text("", encoding="utf-8")
        os.chdir(self.tmp)
        with self.assertRaises(session_events.SessionStreamNotConfigured):
            session_events.resolve_events_path({})


if __name__ == "__main__":
    unittest.main()
