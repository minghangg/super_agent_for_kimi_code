"""L1 tests for ``DispatchAdapter.choose_profile`` (ISS-32).

The adapter is the engine-side consumer of the external model profile table.
These tests cover the two paths the workbench relies on: a readable table answers
with ``source == "config"``, and a broken or absent table answers with
``source == "fallback"`` instead of raising.

Run: ``python -m unittest discover -s tests -v``
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from super_agent.dispatch_adapter import DEFAULT_MODEL_PROFILE, DispatchAdapter


def _table(profiles: dict) -> dict:
    return {"schema_version": 1,
            "rotation": [{"model": "cb-deepseek-v4.1-flash", "effort": "high"}],
            "profiles": profiles}


class ChooseProfile(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "model_profiles.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def adapter(self, path=None) -> DispatchAdapter:
        return DispatchAdapter(model_profiles_path=str(path if path is not None else self.path))

    def test_reads_role_from_table(self):
        self.path.write_text(json.dumps(_table({"qa": {"model": "qa-model",
                                                       "effort": "medium",
                                                       "thinking": "medium"}})),
                             encoding="utf-8")
        got = self.adapter().choose_profile({"role": "qa"})
        self.assertEqual(got["source"], "config")
        self.assertEqual(got["profile"], "qa")
        self.assertEqual(got["model"], "qa-model")
        self.assertEqual(got["effort"], "medium")
        self.assertEqual(got["thinking"], "medium")

    def test_role_falls_back_through_profile_then_kind(self):
        self.path.write_text(json.dumps(_table({"worker": {"model": "w-model",
                                                           "effort": "low"}})),
                             encoding="utf-8")
        got = self.adapter().choose_profile({"kind": "worker"})
        self.assertEqual(got["source"], "config")
        self.assertEqual(got["model"], "w-model")

    def test_missing_file_returns_fallback(self):
        got = self.adapter().choose_profile({"role": "qa"})
        self.assertEqual(got["source"], "fallback")
        self.assertEqual(got["model"], DEFAULT_MODEL_PROFILE["model"])
        self.assertEqual(got["effort"], DEFAULT_MODEL_PROFILE["effort"])

    def test_broken_table_returns_fallback_without_raising(self):
        for bad in ("{not json", json.dumps({"profiles": {"qa": {"model": "x"}}}),
                    json.dumps({"schema_version": 1, "rotation": [], "profiles": {}}),
                    "[]"):
            self.path.write_text(bad, encoding="utf-8")
            got = self.adapter().choose_profile({"role": "qa"})
            self.assertEqual(got["source"], "fallback", bad)
            self.assertEqual(got["model"], DEFAULT_MODEL_PROFILE["model"], bad)

    def test_unconfigured_path_returns_fallback(self):
        got = self.adapter(path="").choose_profile({"role": "qa"})
        self.assertEqual(got["source"], "fallback")

    def test_missing_role_returns_fallback(self):
        self.path.write_text(json.dumps(_table({"qa": {"model": "qa-model", "effort": "high"}})),
                             encoding="utf-8")
        got = self.adapter().choose_profile({"role": "worker"})
        self.assertEqual(got["source"], "fallback")
        self.assertEqual(got["profile"], "worker")


if __name__ == "__main__":
    unittest.main()
