"""ISS-95: baseline charters always load, supplements only when configured.

The properties pinned here, and why each is a counter-example rather than a
happy path:

* **a missing baseline is fatal.** A session that never read its charter is
  the 2026-11-16 failure: a worker ran a whole round with no testing-level
  rules and nobody noticed. Returning "" is how that happened, so the refusal
  is the test;
* **a missing supplement is normal.** Most deployments have none. Treating it
  as an error would make every fresh clone unable to create a session;
* **the two are additive, never either/or.** A supplement that replaces the
  baseline is two sources of truth, and the reason there are two files is that
  one of them is private;
* **one table, not two.** ``dispatch_cli`` used to carry its own copy of the
  prefix map; a rule changed in one did not reach the other.

Run: ``python -m pytest tests/test_charters.py -q``
"""
from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from super_agent import agent_config, charters, dispatch_cli


class Baseline(unittest.TestCase):
    """The shipped charters resolve for every role prefix."""

    def test_every_role_prefix_maps_to_a_shipped_file(self):
        for name, leaf in (
                ("wk-ISS-95-dev", "worker.md"),
                ("qa-lwa-ISS-95", "qa.md"),
                ("pm-laike_work_with_agent", "pm.md"),
                ("scout-20261120", "scout.md"),
                ("steward-20261120", "steward.md"),
                ("auditor-20261120", "auditor.md"),
                ("reviewer-20261120", "reviewer.md"),
                ("planner-20261120", "planner.md"),
                ("at-demo", "analyst.md"),
                ("analyst-demo", "analyst.md"),
        ):
            with self.subTest(name=name):
                self.assertEqual(charters.leaf_for(name), leaf)
                base, sup = charters.charter_files(name)
                self.assertTrue(Path(base).is_file(), base)
                self.assertEqual(sup, "", "no supplement is configured here")

    def test_an_unknown_name_raises_instead_of_returning_empty(self):
        with self.assertRaises(charters.CharterMissing):
            charters.charter_files("unknown-role-x")

    def test_charter_for_still_returns_empty_for_unknown_names(self):
        # The read-only callers (--fetch and friends) must not start raising.
        self.assertEqual(dispatch_cli.charter_for("unknown-role-x"), "")


class MissingBaseline(unittest.TestCase):
    """A baseline that is not there must stop the session, not be skipped."""

    def test_a_deleted_baseline_raises_with_the_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "worker.md").write_text("x", encoding="utf-8")
            with mock.patch.object(charters, "baseline_root", return_value=root):
                base, _ = charters.charter_files("wk-ISS-95-dev")
                self.assertEqual(Path(base), root / "worker.md")
                (root / "worker.md").unlink()
                with self.assertRaises(charters.CharterMissing) as ctx:
                    charters.charter_files("wk-ISS-95-dev")
                self.assertIn("worker.md", str(ctx.exception))

    def test_dispatch_refuses_before_creating_a_session(self):
        # The refusal must come before create_session, or the run leaves an
        # orphan session that nobody asked for.
        created = []

        def _boom(*a, **k):
            created.append(a)
            return "session_should_not_exist"

        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp)
            with mock.patch.object(charters, "baseline_root", return_value=empty), \
                    mock.patch.object(dispatch_cli, "create_session", _boom), \
                    mock.patch.object(dispatch_cli, "read_token", lambda: "t"), \
                    mock.patch.object(dispatch_cli, "discover_base", lambda *a, **k: "http://x"), \
                    mock.patch.object(dispatch_cli, "load_catalog",
                                      side_effect=dispatch_cli.DispatchError("skip")), \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(dispatch_cli.DispatchError):
                    dispatch_cli.main(["--cwd", str(empty), "--name", "wk-ISS-95-dev",
                                       "--task", "x"])
        self.assertEqual(created, [], "no session may be created without a charter")


class Supplement(unittest.TestCase):
    """The supplement is additive and optional."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sup = self.root / "sup"
        self.sup.mkdir()
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def _with_supplement(self, leaf: str, body: str = "# 私有补充\n"):
        (self.sup / leaf).write_text(body, encoding="utf-8")
        return mock.patch.object(charters, "supplement_root",
                                 return_value=str(self.sup))

    def test_a_present_supplement_is_returned_alongside_the_baseline(self):
        with self._with_supplement("worker.md"):
            base, sup = charters.charter_files("wk-ISS-95-dev")
        self.assertTrue(Path(base).is_file())
        self.assertEqual(Path(sup), self.sup / "worker.md")

    def test_no_supplement_root_is_the_normal_state(self):
        with mock.patch.object(charters, "supplement_root", return_value=""):
            base, sup = charters.charter_files("wk-ISS-95-dev")
        self.assertTrue(Path(base).is_file())
        self.assertEqual(sup, "")

    def test_a_configured_root_without_the_file_is_not_an_error(self):
        with mock.patch.object(charters, "supplement_root",
                               return_value=str(self.sup)):
            base, sup = charters.charter_files("wk-ISS-95-dev")
        self.assertTrue(Path(base).is_file())
        self.assertEqual(sup, "", "no file for this role is normal")

    def test_warmup_reads_three_files_when_a_supplement_exists(self):
        with self._with_supplement("worker.md"):
            base, sup = charters.charter_files("wk-ISS-95-dev")
        text = dispatch_cli.warmup_prompt(base, "wk-ISS-95-dev", sup)
        self.assertIn("worker.md", text)
        self.assertIn(str(self.sup).replace(os.sep, "/"), text)
        self.assertIn("and", text)

    def test_warmup_reads_two_files_without_a_supplement(self):
        base, _ = charters.charter_files("wk-ISS-95-dev")
        text = dispatch_cli.warmup_prompt(base, "wk-ISS-95-dev")
        self.assertIn("worker.md", text)
        self.assertNotIn("私有补充", text)


class OneTable(unittest.TestCase):
    """One prefix table, not two."""

    def test_dispatch_cli_has_no_private_copy_of_the_map(self):
        self.assertFalse(hasattr(dispatch_cli, "GENERIC_CHARTER_MAP"),
                         "the map lives in agent_config now; a second copy is "
                         "how a rule change reaches only one of them")

    def test_the_builtin_table_is_a_floor_not_a_default(self):
        # A deployment that configures nothing must still get charters.
        self.assertGreaterEqual(len(agent_config.charter_map()), 10)
        self.assertEqual(charters.leaf_for("wk-x"), "worker.md")

    def test_a_configured_entry_overrides_a_builtin_one(self):
        with mock.patch.object(agent_config, "get",
                               side_effect=lambda k, d=None, **kw: (
                                   {"wk-": "special.md"} if k == "charter.map" else d)):
            merged = dict(agent_config.charter_map())
        self.assertEqual(merged.get("wk-"), "special.md")

    def test_longest_prefix_wins(self):
        pairs = dict(agent_config.charter_map())
        self.assertIn("pm-", pairs)
        self.assertIn("pm", pairs)
        # Resolution order is what decides, so check it directly.
        self.assertEqual(charters.leaf_for("pm-laike_work_with_agent"), "pm.md")
        self.assertEqual(charters.leaf_for("pm"), "pm.md")


class NoPrivateNames(unittest.TestCase):
    """The migrated charters carry no deployment facts."""

    def test_the_shipped_charters_pass_the_privacy_gate_patterns(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        from privacy_gate import PATTERNS  # noqa: PLC0415

        root = Path(__file__).resolve().parent.parent / "rules" / "charters"
        hits = {}
        for f in sorted(root.glob("*.md")):
            text = f.read_text(encoding="utf-8")
            for label, rx in PATTERNS.items():
                found = rx.findall(text)
                if found:
                    hits[f"{f.name}:{label}"] = len(found)
        self.assertEqual(hits, {}, f"private markers in shipped charters: {hits}")

    def test_the_charter_directory_holds_the_nine_roles(self):
        root = Path(__file__).resolve().parent.parent / "rules" / "charters"
        names = {f.name for f in root.glob("*.md")}
        # Nine role charters plus the session contract, which ships here too so
        # the package needs no private checkout to create a session.
        self.assertEqual(names, {
            "worker.md", "qa.md", "pm.md", "scout.md", "steward.md",
            "auditor.md", "reviewer.md", "planner.md", "analyst.md",
            "session_contract.md"})
        self.assertNotIn("watch.md", names, "watch is retired; it is not migrated")

    def test_the_contract_ships_with_the_package(self):
        # A public package that needs the workbench's contract is not usable on
        # another machine -- the whole point of the split (ISS-95).
        path = dispatch_cli.contract_file()
        self.assertTrue(path, "the shipped contract must resolve")
        self.assertTrue(Path(path).is_file(), path)
        self.assertNotIn("Laike_work_with_agent", path.replace("\\", "/"),
                         "the contract must not come from a private checkout")


if __name__ == "__main__":
    unittest.main()
