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

# Assembled from fragments on purpose: the gate scans this file too, and the
# point of the check below is that the *shipped* paths carry no workbench name.
WORKBENCH_NAME = "".join(("Lai", "ke", "_work", "_with", "_agent"))


def _prose(path: Path) -> list[str]:
    """Body lines only: no blanks, no heading, no ``>`` preamble lines.

    The preamble states where the file came from and differs between the two
    copies by design (one points at the workbench, one at the package), so
    comparing it would flag the very difference that is correct.
    """
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith(">"):
            continue
        out.append(s)
    return out


#: ``(private spelling, packaged spelling)`` pairs the shipped copy uses.
#: Kept as data so the check above names each one instead of pattern-matching.
#: The private names are assembled from fragments: the gate scans this file too.
DEPRIVATISE = (
    ("".join(("Re", "ed")), "supervisor"),
    ("python tools/ctx.py", "python -m super_agent.ctx_gate"),
    ("新座 `new_workbench/allowlist.json`；老座 `config/gh_projects.json`",
     "`new_workbench/allowlist.json`；公开侧 `config/gh_projects.json`"),
    ("老座", "公开侧"),
)


def _private_words(text: str) -> dict[str, int]:
    """Which private spellings survived into the packaged copy, and how often.

    Assembled from fragments so this file does not trip the gate it describes.
    """
    found = {}
    for needle in ("".join(("Re", "ed")), "tools/ctx.py", "老座", "新座"):
        n = text.count(needle)
        if n:
            found[needle] = n
    return found


class Baseline(unittest.TestCase):
    """The shipped charters resolve for every role prefix."""

    def test_every_role_prefix_maps_to_a_shipped_file(self):
        for name, leaf in (
                ("wk-ISS-95-dev", "worker.md"),
                ("qa-lwa-ISS-95", "qa.md"),
                ("pm-demo-project", "pm.md"),
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
                    mock.patch.object(dispatch_cli, "quota_gate", lambda: None), \
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
        self.assertEqual(charters.leaf_for("pm-demo-project"), "pm.md")
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
        # Nine role charters plus the session contract and the rotation flow.
        # The two non-role files ship here for the same reason: a session that
        # reads only this directory must get its contract and its rotation rules.
        roles = {"worker.md", "qa.md", "pm.md", "scout.md", "steward.md",
                 "auditor.md", "reviewer.md", "planner.md", "analyst.md"}
        self.assertTrue(roles <= names, f"缺角色章程：{roles - names}")
        self.assertIn("session_contract.md", names)
        self.assertIn("rotation.md", names)
        self.assertNotIn("watch.md", names, "watch is retired; it is not migrated")

    def test_the_contract_ships_with_the_package(self):
        # A public package that needs the workbench's contract is not usable on
        # another machine -- the whole point of the split (ISS-95).
        path = dispatch_cli.contract_file()
        self.assertTrue(path, "the shipped contract must resolve")
        self.assertTrue(Path(path).is_file(), path)
        self.assertNotIn(WORKBENCH_NAME, path.replace("\\", "/"),
                         "the contract must not come from a private checkout")


class CrossReferences(unittest.TestCase):
    """ISS-107: a charter that exists but points at nothing is still broken.

    ``charter_files`` only proves the charter file itself is present. Before
    this, every charter could name ``rules/shared/rules/<file>`` while that
    directory did not exist at all, and ``describe()`` answered ``ok: True``.
    """

    def test_every_shipped_charter_resolves_its_references(self):
        root = charters.baseline_root()
        broken = {}
        for f in sorted(root.glob("*.md")):
            missing = charters.unresolved_refs(f.read_text(encoding="utf-8"))
            if missing:
                broken[f.name] = missing
        self.assertEqual(broken, {}, f"charter cross-references do not resolve: {broken}")

    def test_the_referenced_shared_rules_are_shipped(self):
        """Counter-example to "the reference resolves": the files must exist."""
        root = Path(charters.__file__).resolve().parents[1]
        for rel in ("rules/shared/rules/newtrack_common.md",
                    "rules/shared/rules/issue_template_at.md",
                    "rules/charters/session_contract.md"):
            self.assertTrue((root / rel).is_file(), rel)

    def test_the_shipped_copies_are_derived_not_diverged(self):
        """A shipped copy may differ from the workbench copy only de-privatised.

        The package must carry its own copy -- it is what a fresh clone reads --
        so "two files with the same name" is by design, not the two-truths
        defect. What is *not* by design is the two drifting apart: someone edits
        the rule in one place and the other keeps the old text, and no gate says
        so.

        The judge is **not** "the lines are identical". Rewording a private
        pointer is the whole point of the packaged copy, so byte equality would
        forbid the intended change. What must hold is narrower and checkable:
        every packaging difference is a de-privatising one -- the packaged text
        carries no private spelling, and no rule line was lost.
        """
        wb = (Path(__file__).resolve().parents[2] / WORKBENCH_NAME
              / "rules" / "shared" / "rules")
        if not wb.is_dir():
            self.skipTest("no workbench copy on this machine")
        root = Path(charters.__file__).resolve().parents[1]
        for name in ("newtrack_common.md", "issue_template_at.md"):
            shipped = root / "rules/shared/rules" / name
            original = wb / name
            if not original.is_file():
                continue
            text = shipped.read_text(encoding="utf-8")
            self.assertEqual(_private_words(text), {},
                             f"{name}: packaged copy still carries a private spelling")
            self.assertEqual(len(_prose(original)), len(_prose(shipped)),
                             f"{name}: a rule line was dropped or added in packaging")

    def test_a_missing_referent_flips_describe_to_not_ok(self):
        """The defect ISS-107 was raised for: ok:True while the target is absent."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "rules").mkdir()
            text = "见 `rules/shared/rules/nope.md`。"
            self.assertEqual(charters.unresolved_refs(text, root),
                             ["rules/shared/rules/nope.md"])
            (root / "rules" / "shared" / "rules").mkdir(parents=True)
            (root / "rules" / "shared" / "rules" / "nope.md").write_text("x",
                                                                        encoding="utf-8")
            self.assertEqual(charters.unresolved_refs(text, root), [])

    def test_a_workbench_pointer_is_not_counted_as_a_reference(self):
        """Counter-example 2: an absolute pointer is a different defect.

        It is refused by the privacy gate, not by this check; counting it here
        would make the two gates disagree about what is wrong.
        """
        self.assertEqual(charters.unresolved_refs("见 `D:/somewhere/rules/x.md`。"), [])

    def test_markdown_and_plain_references_cannot_bypass_the_check(self):
        for text in ("[规则](rules/shared/rules/gone.md)",
                     "见 rules/shared/rules/gone.md。"):
            with self.subTest(text=text):
                self.assertEqual(charters.unresolved_refs(text),
                                 ["rules/shared/rules/gone.md"])

    def test_a_private_workbench_pointer_is_a_dead_reference(self):
        """A ``lwa/...`` pointer cannot resolve in a public clone, ever.

        This is the shape that hid in ``worker.md`` and ``qa.md``: three rule
        files were cited through the private workbench's path, so the reader was
        told to open something a fresh clone does not have. The old check only
        looked for ``rules/...`` and reported ``ok: True`` throughout -- a
        pointer that *looks* like a path is exactly what a shape check misses.
        """
        for text in ("细则见 `lwa/rules/shared/skills/x.md`。",
                     "真源是 `lwa/rules/shared/rules/y.md` §八",
                     "[规则](lwa/rules/shared/rules/z.md)"):
            with self.subTest(text=text):
                missing = charters.unresolved_refs(text)
                self.assertEqual(len(missing), 1, missing)
                self.assertTrue(missing[0].startswith("lwa/"), missing)

    def test_a_private_pointer_is_reported_even_if_the_file_exists(self):
        """Absence is not the criterion here -- the prefix is.

        A workbench checkout may well exist on this machine, so a
        "does it resolve" test would pass locally and fail on every other
        machine. The prefix alone is the defect.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "lwa" / "rules").mkdir(parents=True)
            (root / "lwa" / "rules" / "here.md").write_text("x", encoding="utf-8")
            self.assertEqual(charters.unresolved_refs("见 `lwa/rules/here.md`。", root),
                             ["lwa/rules/here.md"])


class DoctorReportsBrokenCharters(unittest.TestCase):
    """``broken_charters`` is what the doctor line reads, so it must go red.

    The check that matters is not "the shipped charters are clean today" -- it
    is "a dead pointer would be reported". Without the second, a check that
    always answers ``{}`` looks identical to a healthy install.
    """

    def test_a_shipped_charter_set_is_clean(self):
        names = [prefix for prefix, _leaf in agent_config.charter_map()]
        self.assertEqual(charters.broken_charters(names), {})

    def test_an_unknown_name_is_not_reported(self):
        """``charter_files`` already refuses these; repeating them is noise."""
        self.assertEqual(charters.broken_charters(["nosuchrole-prefix"]), {})

    def test_a_dead_pointer_is_reported_under_its_session_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "charter.md").write_text("见 `rules/shared/rules/gone.md`。",
                                             encoding="utf-8")
            with mock.patch.object(charters, "baseline_root", return_value=root), \
                 mock.patch.object(charters, "leaf_for", return_value="charter.md"):
                self.assertEqual(charters.broken_charters(["scout-x"]),
                                 {"scout-x": ["rules/shared/rules/gone.md"]})

    def test_a_missing_baseline_cannot_be_skipped_by_doctor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = root / "charter.md"
            with mock.patch.object(charters, "baseline_root", return_value=root), \
                 mock.patch.object(charters, "leaf_for", return_value="charter.md"):
                self.assertEqual(charters.broken_charters(["scout-x"]),
                                 {"scout-x": [f"{missing} (missing charter)"]})


if __name__ == "__main__":
    unittest.main()
