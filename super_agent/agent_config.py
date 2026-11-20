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

Note on ``paths.state_dir``: it is injected as both ``state_dir`` and
``engine.session_state_dir`` so that the ledger and the engine share one root.
When it is not configured the derived fallback is ``config.DEFAULTS["state_dir"]``
(``data``) **resolved against this checkout**, so the two keys stay equal and
the value does not move with the process working directory. That absolute path
is still a **known degradation**, not a fix: the close guard reads
``<workbench>/state/ledger.jsonl`` and cannot see a ledger written under
``<checkout>/data/``. Point ``paths.state_dir`` at that workbench's ``state/``
to make the two the same file.

Note on ``github.allowlist_file``: it has **no** default here. This module is
the private layer, and a public placeholder path is not a private fact -- when
it was used as a default, :func:`overrides` overwrote a ``--config`` file's own
``engine.project_allowlist`` on every run (a deep merge puts overrides last),
which silently replaced a deployment's project table with the shipped one. When
nothing configures it, :func:`overrides` injects nothing and the caller's own
fallback decides (``cli._repo_allowlist``, ``issue_cli._allowlist_path``).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .config import ConfigError

CHECKOUT = Path(__file__).resolve().parents[1]
CONFIG_REL = ("new_workbench", "config", "agent.json")
ENV_FILE = "SAFKC_AGENT_CONFIG"

# Per-key environment overrides. A key listed here is read from the variable
# first and from the JSON file second; a key not listed here is file-only.
ENV = {
    "paths.state_dir": "SESS_EVENTS_STATE_DIR",
    "paths.project_env_dir": "SAFKC_PROJECT_ENV_DIR",
    "paths.sessions_dir": "SESS_WIRE_ROOT",
    "github.projects_file": "SAFKC_PROJECTS_FILE",
    "github.token_env": "GITHUB_TOKEN",
    "github.allowlist_file": "SAFKC_ALLOWLIST",
    "quota.config_file": "DEV_QUOTA_CONFIG",
    "quota.cores_dir": "DEV_QUOTA_CORES_DIR",
    "models.profiles_file": "SAFKC_MODEL_PROFILES",
    "charter.root": "SAFKC_CHARTER_ROOT",
    "roles.prefixes": "SAFKC_SUPERVISOR_ALIASES",
    "dispatch.console": "SAFKC_DISPATCH_CONSOLE",
    # ISS-100 D1: the command ``close`` uses to archive the sessions it bound.
    # Deliberately absent from ``DEFAULTS`` below: the public default lives in
    # :mod:`super_agent.config`, and a second copy here would be injected by
    # :func:`overrides` on every run, outranking a ``--config`` file's own value.
    "engine.session_archive_cmd": "SAFKC_ARCHIVE_CMD",
}

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
    "quota.config_file": "",
    "quota.cores_dir": "",
    "models.profiles_file": "",
    "charter.root": "",
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
    section, _, leaf = dotted.partition(".")
    doc = load(path)
    node = doc
    for part in (section, leaf):
        if not isinstance(node, dict):
            node = None
            break
        node = node.get(part)
    if node is not None:
        return node
    return DEFAULTS.get(dotted, default)


def projects_file() -> str:
    """The project table file, or ``""`` when neither layer configures one."""
    raw = str(get("github.projects_file") or "").strip()
    return os.path.expanduser(raw) if raw else ""


def projects() -> dict:
    """The inline project table (``name -> owner/repo``); ``{}`` when unset."""
    table = get("github.projects") or {}
    if not isinstance(table, dict):
        return {}
    return {str(k).lower(): str(v) for k, v in table.items() if str(v).strip()}


def token_source() -> tuple[str, str]:
    """``(env name, file path)`` for the GitHub token. Values are never printed."""
    env_name = str(get("github.token_env") or "GITHUB_TOKEN").strip()
    raw = str(get("github.token_file") or "").strip()
    return env_name, os.path.expanduser(raw) if raw else ""


def allowlist_file() -> str:
    raw = str(get("github.allowlist_file") or "").strip()
    return os.path.expanduser(raw) if raw else ""


def state_dir() -> str:
    """The declared session/state root, or ``""`` (the caller decides).

    A relative value -- from any layer, file or environment -- is resolved
    against **this checkout**, never the process working directory: the same
    command run from the checkout and from elsewhere used to write two
    different roots (P0-1 #98, same shape as the derived fallback). An
    absolute value is passed through unchanged after ``expanduser``.

    Anchoring is by *value shape*, not by which layer answered: "a relative
    path means relative to this checkout" is a path rule, so ``tmp/x`` from the
    environment and ``tmp/x`` from the file must name one directory. A caller
    that wants working-directory semantics passes an absolute path.
    """
    raw = str(get("paths.state_dir") or "").strip()
    if not raw:
        return ""
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute():
        path = (CHECKOUT / path)
    return str(path.resolve())


def project_env_dir() -> str:
    """The declared project-environment directory, or ``""`` (the caller decides).

    Relative values resolve against **this checkout**, never the working
    directory -- the same rule as :func:`state_dir` (#98 P0-1). A second anchor
    for one key family is how two answers start disagreeing.
    """
    raw = str(get("paths.project_env_dir") or "").strip()
    if not raw:
        return ""
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute():
        path = (CHECKOUT / path)
    return str(path.resolve())


def sessions_dir() -> str:
    raw = str(get("paths.sessions_dir") or "").strip()
    return os.path.expanduser(raw) if raw else ""

def quota_config() -> str:
    raw = str(get("quota.config_file") or "").strip()
    return os.path.expanduser(raw) if raw else ""


def quota_cores_dir() -> str:
    raw = str(get("quota.cores_dir") or "").strip()
    return os.path.expanduser(raw) if raw else ""


def model_profiles_file() -> str:
    raw = str(get("models.profiles_file") or "").strip()
    return os.path.expanduser(raw) if raw else ""


def charter_root() -> str:
    raw = str(get("charter.root") or "").strip()
    return os.path.expanduser(raw) if raw else ""


def charter_project_prefixes() -> tuple:
    table = get("charter.project_prefixes") or {}
    if not isinstance(table, dict):
        return ()
    return tuple((str(k).lower(), str(v)) for k, v in table.items() if str(v).strip())


def charter_map() -> tuple:
    table = get("charter.map") or {}
    if not isinstance(table, dict):
        return ()
    return tuple((str(k).lower(), str(v)) for k, v in table.items() if str(v).strip())


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

    The public floor is empty on purpose: a task name is a deployment fact, and
    a made-up name would register or verify a task nobody asked about. Callers
    treat an empty mapping as "not configured" and refuse (rc=2) rather than
    guess.
    """
    table = get("autostart.tasks") or {}
    if not isinstance(table, dict):
        return {}
    return {str(k): str(v) for k, v in table.items() if str(v).strip()}


def overrides() -> dict:
    """The overrides dict for ``config.load(overrides=...)``.

    Only keys ``config`` actually declares are mapped here -- a key with no
    declaration would bypass ``config.validate`` and become a second source of
    truth. ``agent_config`` adds no ``state_dir`` key of its own: it maps the
    one that already exists.

    ``engine.session_state_dir`` is derived here when ``paths.state_dir`` is
    not configured. That derivation is the **only** fallback in the package:
    ``session_ledger.root()`` and ``session_events`` add none, because a second
    one is how two answers start disagreeing about the same file.

    The derived value is **absolute** -- ``<checkout>/data`` -- not the bare
    ``"data"`` relative path it used to be. A relative value is resolved against
    whatever the process's working directory happens to be, so the same command
    run from the checkout and from the workbench wrote two different ledgers,
    and the close guard read neither of the two it expected (P0-1, #98 fix 1).
    """
    from . import config as config_mod

    out: dict = {}
    state = state_dir()
    session_root = state or str(
        (CHECKOUT / str(config_mod.DEFAULTS.get("state_dir") or "data")).resolve())
    if state:
        out["state_dir"] = state
    # ``paths`` is a declared section (``config.DEFAULTS["paths"]``), so
    # mapping it here goes through ``config.validate`` like every other key.
    # Leaving it out was not harmless: a ``--config`` file's
    # ``paths.project_env_dir`` was answered by a *different* layer than the
    # one the user wrote, so the file looked configured while every lookup fell
    # back to the default (#98 P0-2 shape, one section over).
    #
    # Only a value that actually resolves is mapped. An empty entry would
    # recreate the bug: ``overrides`` is merged **last**, so ``""`` would
    # replace a ``--config`` file's own value.
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
    archive = session_archive_cmd()
    if archive:
        engine["session_archive_cmd"] = archive
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
    "describe", "dispatch_console", "get", "load", "model_profiles_file",
    "overrides", "project_env_dir", "projects", "projects_file", "quota_config",
    "quota_cores_dir", "role_prefixes", "sessions_dir", "state_dir",
    "token_source",
]
