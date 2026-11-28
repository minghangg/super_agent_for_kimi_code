"""The one loader for private deployment values (#98).

This module is the **only** place that reads a private value: a project table,
a token file, a session root, a charter root. Nothing else in the package may
hard-code one, and nothing else may add a second resolution chain.

Three layers, in order, and the result is an *overrides dict* -- not a second
config system:

1. environment variables, one per key (see ``ENV`` below);
2. ``<checkout>/new_workbench/config/agent.json`` -- private, untracked, absent
   in a fresh clone. ``$SAFKC_AGENT_CONFIG`` names the file directly instead;
3. the public defaults in :mod:`super_agent.config` plus the neutral built-ins
   in this module.

``overrides()`` is handed to ``config.load(overrides=...)`` by the caller, so
every key ends up with exactly one answer, and ``config.validate`` stays the one
gate that accepts or rejects it (``ConfigError`` -> rc=2 at the CLI entry).

Two kinds of "missing" are deliberately different, and the distinction is the
whole reason this module exists:

* the file (or its directory) does not exist -> an empty mapping; a fresh
  clone is a legitimate state (#98 counter-example 4);
* the file exists but is not a JSON object -> :class:`AgentConfigError`, which
  every entry point turns into rc=2 **with the path printed** (counter-example
  5). Silently treating a corrupt file as "not configured" would run a
  deployment on defaults nobody chose.

Importing this module reads nothing: :func:`load` reads on first call and
caches the result. That keeps "import has no side effects" true for the whole
package and keeps the tests offline.

Note on ``paths.state_dir``: it is the session-ledger root and the default
engine root. ``paths.engine_state_dir`` may override only the engine/AT
artifact root; both must then stay explicit. The derived fallback is
``config.DEFAULTS["state_dir"]`` resolved against this checkout, so the value
does not move with the process working directory. That absolute path is still a
**known degradation**: the close guard reads the workbench's ``state/ledger``
and cannot see a ledger under this checkout's ``data/``.

Note on ``github.allowlist_file``: it has **no** default here. A public
placeholder path is not a private fact -- as a default, :func:`overrides`
overwrote a ``--config`` file's own ``engine.project_allowlist`` on every run
(overrides merge last). Unconfigured means inject nothing, and the caller's own
fallback decides (``cli._repo_allowlist``, ``issue_cli._allowlist_path``).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import config_paths as path_rules
from .config import ConfigError

CHECKOUT = Path(__file__).resolve().parents[1]
CONFIG_REL = ("new_workbench", "config", "agent.json")
ENV_FILE = "SAFKC_AGENT_CONFIG"

# Per-key environment overrides. A key listed here is read from the variable
# first and from the JSON file second; a key not listed here is file-only.
# ``github.token_env`` is absent on purpose: see tests/test_token_env_collision.py (#135).
ENV = {
    "paths.state_dir": "SESS_EVENTS_STATE_DIR",
    "paths.engine_state_dir": "SAFKC_ENGINE_STATE_DIR",
    "paths.project_env_dir": "SAFKC_PROJECT_ENV_DIR",
    "paths.sessions_dir": "SESS_WIRE_ROOT",
    "github.projects_file": "SAFKC_PROJECTS_FILE",
    "github.allowlist_file": "SAFKC_ALLOWLIST",
    "quota.config_file": "DEV_QUOTA_CONFIG",
    "quota.cores_dir": "DEV_QUOTA_CORES_DIR",
    "models.profiles_file": "SAFKC_MODEL_PROFILES",
    "charter.root": "SAFKC_CHARTER_ROOT",
    # ISS-95: the two halves of a charter. The baseline is the general text that
    # ships with the package; the supplement is this deployment's private
    # addition. Both are overridable so a test can point them at a temp dir.
    "charter.baseline_root": "SAFKC_CHARTER_BASELINE",
    "charter.supplement_root": "SAFKC_CHARTER_SUPPLEMENT",
    # ISS-95: the session contract ships with the package. A deployment may point
    # this at its own copy; unset means "use the shipped one", never "use the
    # workbench's" -- that dependency is what made the package unusable elsewhere.
    "charter.contract_file": "SAFKC_CHARTER_CONTRACT",
    "roles.prefixes": "SAFKC_SUPERVISOR_ALIASES",
    "dispatch.console": "SAFKC_DISPATCH_CONSOLE",
    # ISS-100 D1: the command ``close`` uses to archive the sessions it bound.
    # Deliberately absent from ``DEFAULTS`` below: the public default lives in
    # :mod:`super_agent.config`, and a second copy here would be injected by
    # :func:`overrides` on every run, outranking a ``--config`` file's own value.
    "engine.session_archive_cmd": "SAFKC_ARCHIVE_CMD",
    "engine.supervisor_sid": "SAFKC_SUPERVISOR_SID",
}

#: Built-in session-name prefix → charter file (ISS-95).
#:
#: A prefix rule is a **general** fact -- ``wk-`` is a worker, and a worker
#: reads ``worker.md`` -- so it belongs in the public floor, not in a private
#: file. ``charter.map`` in ``agent.json`` can add or override entries; the
#: merged table puts configured entries first.
#:
#: Deliberately absent: any entry naming a private project (the old
#: ``pm-<workbench-name>`` line). ``("pm-", "pm.md")`` covers it, and a private
#: project name has no business in an open repository.
DEFAULT_CHARTER_MAP: tuple[tuple[str, str], ...] = (
    ("supervisor", "supervisor.md"),
    ("planner", "planner.md"),
    ("analyst", "analyst.md"),
    ("reviewer", "reviewer.md"),
    ("auditor", "auditor.md"),
    # ``("steward", "steward.md")`` removed 2026-11-28 (user order): the role is
    # retired, and its charter is kept as a non-loaded archive. A key written as
    # ``steward-*`` no longer resolves to any charter, so a dispatch to one fails
    # loudly at warm-up instead of loading a document nothing maintains.
    ("scout", "scout.md"),
    ("worker", "worker.md"),
    ("at-", "analyst.md"),
    ("at", "analyst.md"),
    ("qa", "qa.md"),
    ("pm-", "pm.md"),
    ("pm", "pm.md"),
    ("wk", "worker.md"),
)

# Neutral built-ins. None of these names a real project, account, host path or
# private role spelling: those belong in the untracked file.
DEFAULTS = {
    "github.projects": {},
    "github.projects_file": "",
    "github.token_env": "GITHUB_TOKEN",
    "github.token_file": str(CHECKOUT / "auth" / "github.env"),
    # No default for ``github.allowlist_file``: see the module docstring. A
    # placeholder path here would outrank a deployment's own ``--config``.
    "github.allowlist_file": "",
    "paths.state_dir": "",
    "paths.project_env_dir": "",
    "paths.sessions_dir": "",
    # #140 QA 7222834246: the pre-migration runtime root. Without a declaration
    # here the value a deployment writes is never read, so every reader saw
    # "no legacy root" no matter what the file said.
    "legacy_state_dir": "",
    "quota.config_file": "",
    "quota.cores_dir": "",
    "models.profiles_file": "",
    "charter.root": "",
    "charter.baseline_root": "",
    "charter.supplement_root": "",
    "charter.project_prefixes": {},
    "charter.map": {},
    "roles.prefixes": {},
    "dispatch.console": "-m super_agent.dispatch_cli",
    # ISS-101: scheduled-task names per autostart part. Empty in the public
    # floor -- a task name is a deployment fact, and guessing one would register
    # or verify the wrong task instead of refusing.
    "autostart.tasks": {},
}

# ``env`` names for the two GitHub token layers: the variable to read the value
# from, and the file to read it out of. Kept out of ``ENV`` because a token is
# never merged into a config dict -- ``issue_cli`` asks for it when it needs it.
TOKEN_ENV = "github.token_env"
TOKEN_FILE = "github.token_file"


class AgentConfigError(ConfigError):
    """The private config exists but cannot be used; the path is in the message.

    A ``config.ConfigError`` so the one handler at the CLI entry
    (``except config_mod.ConfigError -> rc=2``) covers it without a second
    exception type at every entry point.
    """


_CACHE: dict | None = None
_CACHE_PATH: str | None = None


def config_path() -> Path:
    """The private file this checkout would read: ``$SAFKC_AGENT_CONFIG`` or the
    untracked ``new_workbench/config/agent.json``."""
    raw = (os.environ.get(ENV_FILE) or "").strip()
    if raw:
        return Path(os.path.expanduser(raw)).resolve()
    return CHECKOUT.joinpath(*CONFIG_REL).resolve()


def _read(path: Path) -> dict:
    """Parse the file. Absent -> ``{}``; existing but unusable -> raise."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise AgentConfigError(f"读不出 {path}：{type(exc).__name__}: {exc}") from None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise AgentConfigError(f"{path} 不是合法 JSON：{exc}") from None
    if not isinstance(data, dict):
        raise AgentConfigError(
            f"{path} 顶层应是 JSON 对象，实际是 {type(data).__name__}")
    return data


def load(path: Path | str | None = None, *, refresh: bool = False) -> dict:
    """The raw private mapping (dotted keys, ``{}`` for a missing file).

    Cached after the first read. ``refresh=True`` re-reads -- the CLI does that
    once per process so a long-lived caller is not stuck with a stale table.
    """
    global _CACHE, _CACHE_PATH
    target = Path(path).resolve() if path is not None else config_path()
    key = str(target)
    if _CACHE is not None and not refresh and _CACHE_PATH == key:
        return _CACHE
    _CACHE = _read(target)
    _CACHE_PATH = key
    return _CACHE


def get(dotted: str, default=None, *, path: Path | str | None = None):
    """One value: environment first, then the file, then ``DEFAULTS``."""
    env_name = ENV.get(dotted)
    if env_name:
        raw = (os.environ.get(env_name) or "").strip()
        if raw:
            return os.path.expanduser(raw)
    # ``a.b`` walks two levels; a bare ``a`` is a top-level key. Splitting on a
    # missing dot used to leave ``leaf == ""``, so ``node.get("")`` answered
    # ``None`` and a top-level key was unreadable no matter what the file said
    # (#140 QA 7222834246: that is why ``legacy_state_dir`` never arrived).
    node = load(path)
    for part in dotted.split("."):
        if not isinstance(node, dict):
            node = None
            break
        node = node.get(part)
    if node is not None:
        return node
    return DEFAULTS.get(dotted, default)


def projects_file() -> str:
    """The project table file, or ``""`` when neither layer configures one."""
    return path_rules.declared(get("github.projects_file"))


def projects() -> dict:
    """The inline project table (``name -> owner/repo``); ``{}`` when unset."""
    table = get("github.projects") or {}
    if not isinstance(table, dict):
        return {}
    return {str(k).lower(): str(v) for k, v in table.items() if str(v).strip()}


def token_source() -> tuple[str, str]:
    """``(env name, file path)`` for the GitHub token. Values are never printed."""
    env_name = str(get("github.token_env") or "GITHUB_TOKEN").strip()
    return env_name, path_rules.declared(get("github.token_file"))


def allowlist_file() -> str:
    return path_rules.declared(get("github.allowlist_file"))


def state_dir() -> str:
    """The declared session/state root anchored at this checkout (see #98)."""
    return path_rules.anchored(get("paths.state_dir"), CHECKOUT)


def engine_state_dir() -> str:
    """The engine/AT artifact root, independent of the session ledger root."""
    return path_rules.anchored(get("paths.engine_state_dir"), CHECKOUT)


def project_env_dir() -> str:
    """The declared project-environment directory, anchored at this checkout."""
    return path_rules.anchored(get("paths.project_env_dir"), CHECKOUT)


def sessions_dir() -> str:
    return path_rules.declared(get("paths.sessions_dir"))


def quota_config() -> str:
    return path_rules.declared(get("quota.config_file"))


def quota_cores_dir() -> str:
    return path_rules.declared(get("quota.cores_dir"))


def model_profiles_file() -> str:
    return path_rules.declared(get("models.profiles_file"))


def charter_root() -> str:
    return path_rules.declared(get("charter.root"))


def lwa_root() -> str:
    """The declared workbench checkout root (``engine.lwa_root``), or ``""``.

    Routed into ``cfg`` by :func:`overrides` (#140): readable here but never
    reaching ``cfg`` is why ``brief`` printed "not found" for a root the
    deployment had declared.
    """
    return path_rules.declared(get("engine.lwa_root"))


def legacy_state_dir() -> str:
    """The declared pre-migration runtime root, or ``""`` (#140 QA 7222834246).

    Same shape as :func:`lwa_root`, and routed the same way: without the route
    ``at_serve`` could not learn that a legacy root was declared at all.
    """
    return path_rules.declared(get("legacy_state_dir"))


def charter_project_prefixes() -> tuple:
    table = get("charter.project_prefixes") or {}
    if not isinstance(table, dict):
        return ()
    return tuple((str(k).lower(), str(v)) for k, v in table.items() if str(v).strip())


def charter_map() -> tuple:
    """``(prefix, leaf)`` pairs, longest prefix first.

    The built-in table is a **floor, not a default to be replaced**: a session
    name prefix is a general fact (``wk-`` reads ``worker.md``), and a fresh
    clone that configured nothing must still get its charters. A configured
    ``charter.map`` is merged **in front** so a deployment can override a
    prefix or add one, without restating the whole table. Before ISS-95 only the
    configured table was returned -- empty in every deployment, so
    ``charter_for()`` answered "" for every name and warm-up injected nothing.
    """
    configured = get("charter.map") or {}
    merged: list[tuple[str, str]] = []
    if isinstance(configured, dict):
        merged.extend((str(k).lower(), str(v)) for k, v in configured.items()
                      if str(v).strip())
    seen = {k for k, _ in merged}
    merged.extend((k, v) for k, v in DEFAULT_CHARTER_MAP if k not in seen)
    # Longest prefix first: ``pm-<project>`` must win over ``pm``.
    merged.sort(key=lambda kv: len(kv[0]), reverse=True)
    return tuple(merged)


def role_prefixes() -> dict:
    table = get("roles.prefixes") or {}
    if not isinstance(table, dict):
        return {}
    return {str(k): str(v) for k, v in table.items() if str(v).strip()}


def dispatch_console() -> str:
    return str(get("dispatch.console") or "").strip()


def session_archive_cmd() -> str:
    """The ``close`` archive command, or ``""`` when no layer configures one.

    Empty means "inject nothing", not "inject the empty string": ``overrides()``
    is merged **last** in ``config.load``, so an empty entry here would replace a
    ``--config`` file's own ``engine.session_archive_cmd`` (the same shape as
    #98 P0-2). The public default stays in ``config.DEFAULTS`` -- the floor of
    the chain -- where it cannot outrank anyone.
    """
    return str(get("engine.session_archive_cmd") or "").strip()


def autostart_tasks() -> dict:
    """``{part: scheduled task name}`` -- ``{}`` when no layer configures one.

    The public floor is empty on purpose: a task name is a deployment fact, so
    callers treat an empty mapping as "not configured" and refuse (rc=2).
    """
    table = get("autostart.tasks") or {}
    if not isinstance(table, dict):
        return {}
    return {str(k): str(v) for k, v in table.items() if str(v).strip()}


def overrides() -> dict:
    """The overrides dict for ``config.load(overrides=...)``.

    Only keys ``config`` declares are mapped: an undeclared key would bypass
    ``config.validate`` and become a second source of truth.

    ``engine.session_state_dir`` is derived here when ``paths.state_dir`` is not
    configured -- the **only** fallback in the package, because a second one is
    how two answers start disagreeing about one file. The derived value is
    absolute (``<checkout>/data``): a relative one moves with the process's
    working directory, so the same command run from two places wrote two
    ledgers (#98 P0-1).
    """
    from . import config as config_mod

    out: dict = {}
    state = state_dir()
    session_root = state or str(
        (CHECKOUT / str(config_mod.DEFAULTS.get("state_dir") or "data")).resolve())
    if state:
        out["state_dir"] = state
    engine_state = engine_state_dir()
    if engine_state:
        out["state_dir"] = engine_state
    # ``paths`` is a declared section, so mapping it here goes through
    # ``config.validate`` like every other key. Only a value that resolves is
    # mapped: ``overrides`` is merged **last**, so ``""`` would replace a
    # ``--config`` file's own value (#98 P0-2, one section over).
    penv = project_env_dir()
    if penv:
        out.setdefault("paths", {})["project_env_dir"] = penv
    engine: dict = {}
    if session_root:
        engine["session_state_dir"] = session_root
    profiles = model_profiles_file()
    if profiles:
        engine["model_profiles_path"] = profiles
    allow = allowlist_file()
    if allow:
        engine["project_allowlist"] = allow
    console = dispatch_console()
    if console:
        engine.setdefault("dispatch", {})["console"] = console
    lwa = lwa_root()
    if lwa:
        engine["lwa_root"] = lwa
    # #140 QA 7222834246: a top-level key, mapped at the top level. Every
    # reader builds its cfg from ``overrides()``, so leaving it out made the
    # declared pre-migration root invisible to all of them.
    legacy = legacy_state_dir()
    if legacy:
        out["legacy_state_dir"] = legacy
    archive = session_archive_cmd()
    if archive:
        engine["session_archive_cmd"] = archive
    supervisor_sid = str(get("engine.supervisor_sid") or "").strip()
    if supervisor_sid:
        engine["supervisor_sid"] = supervisor_sid
    supervisor_keys = get("engine.supervisor_keys")
    if isinstance(supervisor_keys, list):
        engine["supervisor_keys"] = supervisor_keys
    char = charter_root()
    if char:
        engine["charter_root"] = char
    if engine:
        out["engine"] = engine
    table = projects()
    if table:
        out.setdefault("github", {})["projects"] = table
    pfile = projects_file()
    if pfile:
        out.setdefault("github", {})["projects_file"] = pfile
    return out


def describe() -> dict:
    """A printable summary for ``doctor``-style checks. Never includes a secret."""
    return {
        "config_path": str(config_path()),
        "exists": config_path().is_file(),
        "state_dir": state_dir() or "(未配置，派生 config.DEFAULTS.state_dir)",
        "engine_state_dir": engine_state_dir() or "(未配置，复用 state_dir)",
        "sessions_dir": sessions_dir() or "(未配置，回落 super_agent.paths)",
        "project_env_dir": project_env_dir() or "(未配置)",
        "projects": len(projects()),
        "projects_file": projects_file() or "(未配置)",
        "charter_root": charter_root() or "(未配置)",
    }


def _reset_cache() -> None:
    """Drop the cached mapping (tests and ``--refresh`` style callers only)."""
    global _CACHE, _CACHE_PATH
    _CACHE = None
    _CACHE_PATH = None


__all__ = [
    "AgentConfigError", "ENV", "DEFAULTS", "allowlist_file", "autostart_tasks",
    "charter_map", "charter_project_prefixes", "charter_root", "config_path",
    "lwa_root",
    "describe", "dispatch_console", "engine_state_dir", "get", "load", "model_profiles_file",
    "overrides", "project_env_dir", "projects", "projects_file", "quota_config",
    "quota_cores_dir", "role_prefixes", "sessions_dir", "state_dir",
    "token_source",
]
