"""L1 tests for the session bootstrap message of ``DispatchAdapter`` (ISS-57).

A newly created session must open with the session contract, not with a bare
ping: the first message asks the agent to read the contract file once and then
confirm. The wording is copied verbatim from the workbench dispatcher
(its ``warmup_prompt``, lines 57-82) so both sides produce
identical sessions; only the path differs, because this side never hardcodes a
private workbench root.

The adapter only *builds* that message. M0 keeps session creation outside the
engine, so ``session_bootstrap`` must never spawn the console.

Run: ``python -m pytest tests/test_dispatch_adapter.py -q``
"""
from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path

from super_agent import config, dispatch_adapter
from super_agent.dispatch_adapter import (WARMUP_PROMPT, DispatchAdapter,
                                          contract_warmup_prompt)

# Copied from the workbench dispatcher (dispatch.py:57-60 and 70-82). Do not
# import across repositories: the point of these tests is to freeze the wording.
LWA_WARMUP_PROMPT = ("Reply with exactly: ping. Do not call any tools. "
                     "Do not read files. Do not run commands.")


def _lwa_contract_message(posix_path: str) -> str:
    return (f"Read {posix_path} with the Read tool. "
            "Reply with exactly: Read done. "
            "Do not use any other tool. Do not run any command.")


class ContractWarmupPrompt(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.contract = self.dir / "session_contract.md"
        self.contract.write_text("# contract\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_existing_file_returns_the_three_sentences(self):
        text = contract_warmup_prompt(str(self.contract))
        self.assertIn(f"Read {self.contract.as_posix()} with the Read tool.", text)
        self.assertIn("Reply with exactly: Read done.", text)
        self.assertIn("Do not use any other tool.", text)
        self.assertIn("Do not run any command.", text)

    def test_none_returns_the_bare_ping(self):
        self.assertEqual(contract_warmup_prompt(None), WARMUP_PROMPT)

    def test_missing_file_falls_back_without_raising(self):
        missing = self.dir / "nope" / "session_contract.md"
        self.assertEqual(contract_warmup_prompt(str(missing)), WARMUP_PROMPT)

    def test_windows_separators_are_not_repeated_in_the_message(self):
        text = contract_warmup_prompt(str(self.contract))
        self.assertNotIn("\\", text)
        self.assertIn(self.contract.as_posix(), text)

    def test_wording_matches_the_workbench_verbatim(self):
        self.assertEqual(WARMUP_PROMPT, LWA_WARMUP_PROMPT)
        self.assertEqual(contract_warmup_prompt(str(self.contract)),
                         _lwa_contract_message(self.contract.as_posix()))


class SessionBootstrap(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.contract = self.dir / "session_contract.md"
        self.contract.write_text("# contract\n", encoding="utf-8")
        self.calls: list[list[str]] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def adapter(self, contract_path=None) -> DispatchAdapter:
        def runner(argv):
            self.calls.append(list(argv))
            return 0, "{}", ""

        return DispatchAdapter(runner=runner, contract_path=contract_path)

    def test_configured_contract_is_used(self):
        got = self.adapter(self.contract).session_bootstrap()
        self.assertTrue(got["ok"])
        self.assertEqual(got["code"], "ok")
        self.assertEqual(got["source"], "contract")
        self.assertEqual(got["contract_path"], self.contract.as_posix())
        self.assertEqual(got["text"], _lwa_contract_message(self.contract.as_posix()))

    def test_unconfigured_contract_falls_back_to_the_ping(self):
        got = self.adapter().session_bootstrap()
        self.assertEqual(got["source"], "fallback")
        self.assertEqual(got["text"], WARMUP_PROMPT)
        self.assertEqual(got["contract_path"], "")

    def test_missing_contract_falls_back_to_the_ping(self):
        got = self.adapter(self.dir / "missing.md").session_bootstrap()
        self.assertEqual(got["source"], "fallback")
        self.assertEqual(got["text"], WARMUP_PROMPT)
        self.assertEqual(got["contract_path"], "")

    def test_bootstrap_never_spawns_the_console(self):
        got = self.adapter(self.contract).session_bootstrap()
        self.assertTrue(got["ok"])
        self.assertEqual(self.calls, [])

    def test_session_id_is_none_and_documented(self):
        got = self.adapter(self.contract).session_bootstrap()
        self.assertIsNone(got["session_id"])
        doc = DispatchAdapter.session_bootstrap.__doc__ or ""
        self.assertIn("always ``None`` by contract", doc)

    def test_bootstrap_takes_no_assignment(self):
        params = [p for name, p in
                  inspect.signature(DispatchAdapter.session_bootstrap)
                  .parameters.items() if name != "self"]
        self.assertEqual(params, [], "an unused assignment parameter invites misuse")

    def test_creation_stays_forbidden_under_pm_created(self):
        adapter = DispatchAdapter(creation_mode="pm_created", runner=self.runner)
        for result in (adapter.create_worker({"id": "A1"}),
                       adapter.create_qa({"id": "A2"})):
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], "creation_mode_forbidden")
            self.assertEqual(result["draft"]["status"], "draft_only")
        self.assertEqual(self.calls, [])

    def runner(self, argv):
        self.calls.append(list(argv))
        return 0, "{}", ""


class BuildWiring(unittest.TestCase):
    def test_contract_path_is_a_registered_config_key(self):
        self.assertEqual(config.DEFAULTS["engine"]["contract_path"], "")

    def test_build_reads_contract_path_from_config(self):
        adapter = dispatch_adapter.build(
            {"engine": {"contract_path": "D:/repo/rules/session_contract.md"}})
        self.assertEqual(adapter.contract_path,
                         "D:/repo/rules/session_contract.md")

    def test_build_defaults_to_empty_contract_path(self):
        self.assertEqual(dispatch_adapter.build({}).contract_path, "")


if __name__ == "__main__":
    unittest.main()
