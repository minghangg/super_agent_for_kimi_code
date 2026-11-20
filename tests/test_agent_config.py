"""``agent_config`` is the one loader for private values, and the tests pin that.

Three properties are worth a test, and each one is a rule that cannot be
checked by reading the module once:

* **absent is not the same as corrupt** -- a missing file is a legitimate fresh
  clone (empty mapping, rc=0), while a file that exists and cannot be parsed
  must raise with its path in the message. Collapsing the two would run a
  deployment on defaults nobody chose.
* **the override dict feeds ``config.load``, not a second resolution chain** --
  the value a caller reads is the value ``config.load`` merged, so
  ``config.validate`` stays the one gate.
* **one declared root, two readers** -- ``state_dir`` and
  ``engine.session_state_dir`` resolve to the same directory, so the ledger and
  the engine cannot disagree about where the session state is.

Every test isolates ``$SAFKC_AGENT_CONFIG`` and restores the cache afterwards:
the module caches the file it read, and a leaked cache would make the next test
assert against the previous file.

Run: ``python -m pytest tests/test_agent_config.py -q``
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from super_agent import agent_config, config, session_events


class _Isolated(unittest.TestCase):
    """Point ``$SAFKC_AGENT_CONFIG`` at a temp file (or at nothing) per test."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in (agent_config.ENV_FILE,)}
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        agent_config._reset_cache()

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        agent_config._reset_cache()
        self._tmp.cleanup()

    def write(self, doc) -> Path:
        path = self.root / "agent.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        os.environ[agent_config.ENV_FILE] = str(path)
        agent_config._reset_cache()
        return path


class AbsentConfigTest(_Isolated):
    """Counter-example 4: no file is not a fault."""

    def test_missing_file_loads_empty(self):
        os.environ[agent_config.ENV_FILE] = str(self.root / "nope.json")
        agent_config._reset_cache()
        self.assertEqual(agent_config.load(), {})

    def test_missing_file_still_injects_a_session_root(self):
        os.environ[agent_config.ENV_FILE] = str(self.root / "nope.json")
        agent_config._reset_cache()
        cfg = config.load(overrides=agent_config.overrides())
        self.assertTrue(cfg["engine"]["session_state_dir"])

    def test_missing_directory_is_not_an_error(self):
        os.environ[agent_config.ENV_FILE] = str(self.root / "no" / "such" / "a.json")
        agent_config._reset_cache()
        self.assertEqual(agent_config.overrides().get("state_dir", ""), "")


class CorruptConfigTest(_Isolated):
    """Counter-example 5: a broken file refuses, and names itself."""

    def test_bad_json_raises_with_path(self):
        path = self.root / "agent.json"
        path.write_text("{bad", encoding="utf-8")
        os.environ[agent_config.ENV_FILE] = str(path)
        agent_config._reset_cache()
        with self.assertRaises(agent_config.AgentConfigError) as caught:
            agent_config.load()
        self.assertIn("agent.json", str(caught.exception))

    def test_non_object_document_raises(self):
        path = self.write([1, 2, 3])
        with self.assertRaises(agent_config.AgentConfigError) as caught:
            agent_config.load()
        self.assertIn(str(path), str(caught.exception))

    def test_the_error_is_a_config_error(self):
        """The CLI has one handler for ``ConfigError``; a second type would need
        a second handler at every entry point."""
        self.assertTrue(issubclass(agent_config.AgentConfigError, config.ConfigError))


class OverrideMappingTest(_Isolated):
    """The declared root reaches both keys through ``config.load``."""

    def test_state_dir_reaches_both_keys(self):
        target = self.root / "session-root"
        self.write({"paths": {"state_dir": str(target)}})
        cfg = config.load(overrides=agent_config.overrides())
        self.assertEqual(cfg["state_dir"], str(target))
        self.assertEqual(cfg["engine"]["session_state_dir"], str(target))

    def test_state_root_and_session_root_agree(self):
        target = self.root / "session-root"
        self.write({"paths": {"state_dir": str(target)}})
        cfg = config.load(overrides=agent_config.overrides())
        self.assertEqual(config.state_root(cfg), session_events.resolve_session_root(cfg))

    def test_inline_projects_win_over_the_file(self):
        self.write({"github": {"projects": {"demo": "owner/repo"},
                               "projects_file": str(self.root / "table.json")}})
        self.assertEqual(agent_config.projects(), {"demo": "owner/repo"})

    def test_only_declared_keys_are_merged(self):
        """A key ``config`` does not declare would bypass ``validate``."""
        self.write({"paths": {"state_dir": str(self.root / "s")},
                    "unknown_section": {"anything": 1}})
        overrides = agent_config.overrides()
        self.assertNotIn("unknown_section", overrides)
        config.load(overrides=overrides)      # validate accepts what we injected


class EnvironmentLayerTest(_Isolated):
    """Environment wins over the file, and the file wins over the default."""

    def test_env_beats_the_file(self):
        self.write({"paths": {"state_dir": str(self.root / "from-file")}})
        os.environ["SESS_EVENTS_STATE_DIR"] = str(self.root / "from-env")
        try:
            self.assertEqual(agent_config.state_dir(), str(self.root / "from-env"))
        finally:
            os.environ.pop("SESS_EVENTS_STATE_DIR", None)

    def test_charter_root_defaults_to_empty(self):
        os.environ[agent_config.ENV_FILE] = str(self.root / "absent.json")
        os.environ.pop("SAFKC_CHARTER_ROOT", None)
        agent_config._reset_cache()
        self.assertEqual(agent_config.charter_root(), "")

    def test_dispatch_console_default_points_at_this_package(self):
        os.environ[agent_config.ENV_FILE] = str(self.root / "absent.json")
        os.environ.pop("SAFKC_DISPATCH_CONSOLE", None)
        agent_config._reset_cache()
        self.assertEqual(agent_config.dispatch_console(), "-m super_agent.dispatch_cli")


class DerivationTest(_Isolated):
    """P0-1: the derived fallback is absolute and does not follow ``cwd``.

    The bare relative path ``"data"`` used to be injected as
    ``engine.session_state_dir``. A relative path is resolved by whoever opens
    it, so one command run from the checkout and another from the workbench
    wrote two different ledgers, and the close guard (which reads
    ``<workbench>/state/ledger.jsonl``) saw neither.
    """

    def setUp(self):
        super().setUp()
        os.environ[agent_config.ENV_FILE] = str(self.root / "absent.json")
        os.environ.pop("SESS_EVENTS_STATE_DIR", None)
        agent_config._reset_cache()

    def derived(self) -> str:
        cfg = config.load(overrides=agent_config.overrides())
        return cfg["engine"]["session_state_dir"]

    def test_the_derived_root_is_an_absolute_path(self):
        self.assertTrue(os.path.isabs(self.derived()), self.derived())

    def test_the_derived_root_does_not_move_with_cwd(self):
        here = Path.cwd()
        first = self.derived()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                second = self.derived()
            finally:
                os.chdir(here)
        self.assertEqual(first, second)
        self.assertEqual(Path(first), Path(first).resolve())

    def test_the_derived_root_is_this_checkouts_data_dir(self):
        self.assertEqual(Path(self.derived()),
                         (agent_config.CHECKOUT / config.DEFAULTS["state_dir"]).resolve())

    def test_the_fallback_is_documented_as_a_degradation(self):
        """The derivation is not a fix: the ledger lands under ``<checkout>/data``
        and the close guard still cannot see it. Pinned so the docstring cannot
        quietly start claiming otherwise."""
        self.assertIn("known degradation", agent_config.__doc__)
        self.assertIn("cannot see", agent_config.__doc__)


class AllowlistNotInjectedTest(_Isolated):
    """P0-2: with nothing configuring it, no placeholder is injected.

    ``github.allowlist_file`` used to default to the checkout's own
    ``config/allowlist.json``. Because overrides win in ``config.load``'s deep
    merge, that placeholder replaced a ``--config`` file's
    ``engine.project_allowlist`` on every run, and the shipped table then
    claimed issue 1 for the project it names -- so a thread key that should have
    stayed bare became ``<owner>/<repo>#1`` (two tests in
    ``test_needs_person_resolved.py``).
    """

    def setUp(self):
        super().setUp()
        os.environ[agent_config.ENV_FILE] = str(self.root / "absent.json")
        os.environ.pop("SAFKC_ALLOWLIST", None)
        agent_config._reset_cache()

    def test_no_allowlist_is_injected_when_nothing_configures_it(self):
        self.assertEqual(agent_config.allowlist_file(), "")
        self.assertNotIn("project_allowlist",
                         (agent_config.overrides().get("engine") or {}))

    def test_a_configured_allowlist_is_injected(self):
        target = self.root / "allow.json"
        self.write({"github": {"allowlist_file": str(target)}})
        self.assertEqual(agent_config.overrides()["engine"]["project_allowlist"],
                         str(target))

    def test_the_config_files_own_value_survives_the_merge(self):
        own = self.root / "own.json"
        cfg = config.load(_write_cfg(self.root / "cfg.json", {"engine": {
            "project_allowlist": str(own)}}), agent_config.overrides())
        self.assertEqual(cfg["engine"]["project_allowlist"], str(own))


def _write_cfg(path: Path, doc: dict) -> Path:
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


class ConfigDeclarationTest(unittest.TestCase):
    """P1-1: every mapped key must be declared, or it bypasses ``config.validate``."""

    def test_projects_is_declared(self):
        self.assertIn("projects", config.DEFAULTS["github"])

    def test_dispatch_console_is_declared(self):
        self.assertIn("console", config.DEFAULTS["engine"]["dispatch"])

    def test_charter_root_is_declared(self):
        self.assertIn("charter_root", config.DEFAULTS["engine"])

    def test_a_non_mapping_project_table_is_rejected(self):
        cfg = config.load()
        cfg["github"]["projects"] = ["not", "a", "mapping"]
        with self.assertRaises(config.ConfigError):
            config.validate(cfg)

    def test_a_non_string_console_is_rejected(self):
        cfg = config.load()
        cfg["engine"]["dispatch"]["console"] = 42
        with self.assertRaises(config.ConfigError):
            config.validate(cfg)


class ArchiveCommandTest(_Isolated):
    """ISS-100 D1/D5: the archive command has one priority chain, and the
    private layer must not outrank a ``--config`` file when it says nothing.

    All three cases mirror ``AllowlistNotInjectedTest``: what matters is **when**
    the private layer wins, not that it can.
    """

    def setUp(self):
        super().setUp()
        os.environ.pop("SAFKC_ARCHIVE_CMD", None)
        os.environ[agent_config.ENV_FILE] = str(self.root / "absent.json")
        agent_config._reset_cache()

    def test_the_public_default_names_this_package(self):
        self.assertEqual(
            config.DEFAULTS["engine"]["session_archive_cmd"],
            "{python} -m super_agent.cli recycle archive {sid}")

    def test_nothing_configured_injects_nothing(self):
        self.assertEqual(agent_config.session_archive_cmd(), "")
        self.assertNotIn("session_archive_cmd",
                         (agent_config.overrides().get("engine") or {}))

    def test_the_config_files_own_value_survives_the_merge(self):
        own = "{python} -m custom_cli archive {sid}"
        cfg = config.load(_write_cfg(self.root / "cfg.json", {"engine": {
            "session_archive_cmd": own}}), agent_config.overrides())
        self.assertEqual(cfg["engine"]["session_archive_cmd"], own)

    def test_the_private_file_wins_over_the_config_file(self):
        """Documented priority, not a defect: the private layer is merged last."""
        own = "{python} -m custom_cli archive {sid}"
        self.write({"engine": {"session_archive_cmd":
                               "{python} -m private_cli archive {sid}"}})
        cfg = config.load(_write_cfg(self.root / "cfg.json", {"engine": {
            "session_archive_cmd": own}}), agent_config.overrides())
        self.assertEqual(cfg["engine"]["session_archive_cmd"],
                         "{python} -m private_cli archive {sid}")

    def test_the_environment_wins_over_the_file(self):
        self.write({"engine": {"session_archive_cmd":
                               "{python} -m from_file archive {sid}"}})
        os.environ["SAFKC_ARCHIVE_CMD"] = "{python} -m from_env archive {sid}"
        try:
            self.assertEqual(agent_config.session_archive_cmd(),
                             "{python} -m from_env archive {sid}")
        finally:
            os.environ.pop("SAFKC_ARCHIVE_CMD", None)

    def test_a_backslash_template_is_refused_with_the_reason(self):
        """A backslash is a silent wrong argv[0]; refusing at load names it."""
        cfg = config.load()
        cfg["engine"]["session_archive_cmd"] = "D:\\x\\python.exe -m super_agent.cli"
        with self.assertRaises(config.ConfigError) as caught:
            config.validate(cfg)
        self.assertIn("反斜杠", str(caught.exception))

    def test_a_non_string_command_is_refused(self):
        cfg = config.load()
        cfg["engine"]["session_archive_cmd"] = ["python", "-m"]
        with self.assertRaises(config.ConfigError):
            config.validate(cfg)


class StaleCacheTest(_Isolated):
    """A caller that changed the file sees the change only on an explicit refresh.

    Absolute paths on purpose: the cache is what this class tests, and a
    relative fixture would make the assertions depend on ``state_dir()``'s
    anchor (ISS-100 D3). With absolute values the comparison stays literal, so
    a broken anchor cannot make a broken cache look right.
    """

    def test_refresh_rereads_the_file(self):
        a, b = self.root / "first", self.root / "second"
        self.write({"paths": {"state_dir": str(a)}})
        self.assertEqual(agent_config.state_dir(), str(a))
        path = self.root / "agent.json"
        path.write_text(json.dumps({"paths": {"state_dir": str(b)}}), encoding="utf-8")
        self.assertEqual(agent_config.state_dir(), str(a))        # cached
        agent_config.load(refresh=True)
        self.assertEqual(agent_config.state_dir(), str(b))


class RelativeStateDirTest(_Isolated):
    """ISS-100 D3: a relative value is anchored to the checkout, not to cwd.

    Before the fix ``{"paths": {"state_dir": "state"}}`` came back verbatim, so
    whoever opened it resolved it against their own working directory: the same
    command run from the checkout and from somewhere else wrote two different
    session roots, and the engine read the one nobody had written.
    """

    def setUp(self):
        super().setUp()
        os.environ.pop("SESS_EVENTS_STATE_DIR", None)

    def test_a_relative_value_is_absolute_and_anchored(self):
        self.write({"paths": {"state_dir": "state"}})
        got = agent_config.state_dir()
        self.assertTrue(os.path.isabs(got), got)
        self.assertEqual(got, str((agent_config.CHECKOUT / "state").resolve()))

    def test_the_environment_layer_is_anchored_the_same_way(self):
        """Same value shape, same answer -- regardless of which layer said it."""
        os.environ[agent_config.ENV_FILE] = str(self.root / "absent.json")
        agent_config._reset_cache()
        os.environ["SESS_EVENTS_STATE_DIR"] = "rel_dir"
        try:
            self.assertEqual(agent_config.state_dir(),
                             str((agent_config.CHECKOUT / "rel_dir").resolve()))
        finally:
            os.environ.pop("SESS_EVENTS_STATE_DIR", None)

    def test_an_absolute_value_passes_through(self):
        target = self.root / "elsewhere"
        self.write({"paths": {"state_dir": str(target)}})
        self.assertEqual(agent_config.state_dir(), str(target.resolve()))

    def test_overrides_hand_the_absolute_form_to_both_keys(self):
        self.write({"paths": {"state_dir": "state"}})
        overrides = agent_config.overrides()
        want = str((agent_config.CHECKOUT / "state").resolve())
        self.assertEqual(overrides["state_dir"], want)
        self.assertEqual(overrides["engine"]["session_state_dir"], want)


class ProjectEnvDirTest(_Isolated):
    """ISS-101: one more path key, under the same anchoring rule as state_dir.

    Two things are worth pinning. ``project_env_dir`` resolves a relative value
    against this checkout, not the working directory -- a second anchor for one
    key family is how two answers start disagreeing (#98 P0-1). And it is
    absent from ``overrides()`` on purpose: ``config`` declares it, so
    ``overrides`` injecting it would bypass ``config.validate`` and outrank a
    ``--config`` file on every run.
    """

    def test_unset_is_empty(self):
        os.environ[agent_config.ENV_FILE] = str(self.root / "absent.json")
        agent_config._reset_cache()
        self.assertEqual(agent_config.project_env_dir(), "")

    def test_relative_value_anchors_to_the_checkout(self):
        target = "new_workbench/config/project_env"
        self.write({"paths": {"project_env_dir": target}})
        self.assertEqual(agent_config.project_env_dir(),
                         str((agent_config.CHECKOUT / target).resolve()))

    def test_absolute_value_passes_through(self):
        target = self.root / "envs"
        self.write({"paths": {"project_env_dir": str(target)}})
        self.assertEqual(agent_config.project_env_dir(), str(target.resolve()))

    def test_environment_variable_wins_over_the_file(self):
        self.write({"paths": {"project_env_dir": str(self.root / "from-file")}})
        wanted = self.root / "from-env"
        os.environ["SAFKC_PROJECT_ENV_DIR"] = str(wanted)
        agent_config._reset_cache()
        try:
            self.assertEqual(agent_config.project_env_dir(), str(wanted.resolve()))
        finally:
            os.environ.pop("SAFKC_PROJECT_ENV_DIR", None)

    def test_injected_by_overrides_so_config_validate_owns_it(self):
        """Decision 2: ``paths`` is declared, so it is mapped like any other key.

        Not mapping it left a ``--config`` file's value answered by another
        layer, so the file looked configured while every lookup used the
        default. An **unset** key injects nothing -- ``overrides`` merges last,
        so an empty entry would replace the file's own value.
        """
        self.write({"paths": {"project_env_dir": str(self.root / "envs")}})
        overrides = agent_config.overrides()
        self.assertEqual(Path(overrides["paths"]["project_env_dir"]),
                         (self.root / "envs").resolve())

    def test_unset_key_injects_nothing(self):
        os.environ[agent_config.ENV_FILE] = str(self.root / "absent.json")
        agent_config._reset_cache()
        self.assertNotIn("paths", agent_config.overrides())

    def test_overrides_value_survives_config_load_and_validate(self):
        target = self.root / "envs"
        self.write({"paths": {"project_env_dir": str(target)}})
        cfg = config.load(overrides=agent_config.overrides())
        self.assertEqual(cfg["paths"]["project_env_dir"], str(target.resolve()))

    def test_describe_names_it(self):
        self.write({"paths": {"project_env_dir": str(self.root / "envs")}})
        self.assertIn("project_env_dir", agent_config.describe())


class AutostartTasksTest(_Isolated):
    """ISS-101: task names are deployment facts, so the public floor is empty."""

    def test_unset_is_empty(self):
        os.environ[agent_config.ENV_FILE] = str(self.root / "absent.json")
        agent_config._reset_cache()
        self.assertEqual(agent_config.autostart_tasks(), {})
        self.assertEqual(agent_config.DEFAULTS["autostart.tasks"], {})

    def test_reads_the_private_table(self):
        self.write({"autostart": {"tasks": {"wss_bus": "KimiWssBus"}}})
        self.assertEqual(agent_config.autostart_tasks(), {"wss_bus": "KimiWssBus"})

    def test_blank_names_are_dropped(self):
        self.write({"autostart": {"tasks": {"wss_bus": "  ", "decide_watch": "X"}}})
        self.assertEqual(agent_config.autostart_tasks(), {"decide_watch": "X"})

    def test_non_mapping_is_empty_not_an_exception(self):
        self.write({"autostart": {"tasks": ["not", "a", "table"]}})
        self.assertEqual(agent_config.autostart_tasks(), {})


if __name__ == "__main__":
    unittest.main()
