"""Configuration loading.

Defaults live in ``DEFAULTS`` below and every field can be overridden by a
JSON config file (``--config``) or by CLI flags. The role name is
configuration-only: nothing in the engine compares against a literal role
string, so renaming the role changes no behaviour.

Two allowlists exist and they are different dimensions, never to be merged:

* ``engine.allowlist`` here lists **event types** the fast path reads.
* ``engine.project_allowlist`` points at ``config/allowlist.json``, which is the
  project/issue/action allowlist enforced by :mod:`super_agent.allowlist`.
  Reading an event type says nothing about being allowed to write a repo.

``engine.creation_mode`` accepts only ``pm_created``; ``engine_preapproved`` is
rejected at load time because M0 grants no session-creation path.
"""
from __future__ import annotations

from .config_defaults import DEFAULTS

import copy
import json
import os
from pathlib import Path


MODES = ("observe", "assist")
SOURCES = ("none", "fixture", "live")


class ConfigError(ValueError):
    pass


def _deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for key, val in (extra or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def _read_json(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ConfigError(f"config {path} is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"config {path} must be a JSON object")
    return data


def validate(cfg: dict) -> dict:
    mode = str(cfg.get("mode") or "").strip()
    if mode not in MODES:
        raise ConfigError(f"mode must be one of {MODES}, got {mode!r}")
    role = str(cfg.get("role") or "").strip()
    if not role:
        raise ConfigError("role must be a non-empty string")
    source = str((cfg.get("github") or {}).get("source") or "").strip()
    if source not in SOURCES:
        raise ConfigError(f"github.source must be one of {SOURCES}, got {source!r}")
    for key in ("max_event_lines", "max_outbox_lines"):
        val = (cfg.get("store") or {}).get(key)
        if not isinstance(val, int) or val < 10:
            raise ConfigError(f"store.{key} must be an integer >= 10")
    stale = (cfg.get("store") or {}).get("outbox_stale_s", 300)
    if not isinstance(stale, (int, float)) or stale <= 0:
        raise ConfigError("store.outbox_stale_s must be a positive number")
    allow = (cfg.get("engine") or {}).get("allowlist")
    if not isinstance(allow, list) or not all(isinstance(x, str) for x in allow):
        raise ConfigError("engine.allowlist must be a list of event type strings")
    project_allow = (cfg.get("engine") or {}).get("project_allowlist", "")
    if project_allow is not None and not isinstance(project_allow, str):
        raise ConfigError("engine.project_allowlist must be a path string (or empty)")
    creation = (cfg.get("engine") or {}).get("creation_mode", "pm_created")
    if creation != "pm_created":
        raise ConfigError(
            f"engine.creation_mode {creation!r} is forbidden: only 'pm_created' "
            "is accepted (session creation needs an approval M0 does not grant)")
    events_path = (cfg.get("engine") or {}).get("session_events_path")
    if events_path is not None and not isinstance(events_path, str):
        raise ConfigError("engine.session_events_path must be a path string (or empty)")
    session_dir = (cfg.get("engine") or {}).get("session_state_dir", "")
    if session_dir is not None and not isinstance(session_dir, str):
        raise ConfigError("engine.session_state_dir must be a path string (or empty)")
    # ISS-101: the declared project-environment directory. Same shape as the
    # check above -- "" means "not configured", and a non-string is refused
    # here rather than reaching ``doctor --project`` as a repr.
    paths_cfg = cfg.get("paths")
    if paths_cfg is not None and not isinstance(paths_cfg, dict):
        raise ConfigError("paths must be a mapping")
    penv = (paths_cfg or {}).get("project_env_dir", "")
    if penv is not None and not isinstance(penv, str):
        raise ConfigError("paths.project_env_dir must be a path string (or empty)")
    lwa_root = (cfg.get("engine") or {}).get("lwa_root", "")
    if lwa_root is not None and not isinstance(lwa_root, str):
        raise ConfigError("engine.lwa_root must be a path string (or empty)")
    console_path = (cfg.get("engine") or {}).get("session_console_path", "")
    if console_path is not None and not isinstance(console_path, str):
        raise ConfigError("engine.session_console_path must be a path string (or empty)")
    charter_root = (cfg.get("engine") or {}).get("charter_root", "")
    if charter_root is not None and not isinstance(charter_root, str):
        raise ConfigError("engine.charter_root must be a path string (or empty)")
    archive_cmd = (cfg.get("engine") or {}).get("session_archive_cmd", "")
    if archive_cmd is not None and not isinstance(archive_cmd, str):
        raise ConfigError("engine.session_archive_cmd must be a command string (or empty)")
    # A backslash in the template is a silent wrong path, not a style choice:
    # ``shlex.split`` treats ``\x`` as an escape, so ``D:\x\python.exe`` becomes
    # argv[0] ``D:xpython.exe`` and ``subprocess.run`` raises FileNotFoundError
    # far from the config that caused it. Refusing here names the cause.
    if isinstance(archive_cmd, str) and "\\" in archive_cmd:
        raise ConfigError(
            "engine.session_archive_cmd 不许含反斜杠（shlex.split 会把它当转义，"
            f"argv[0] 变成错路径）：{archive_cmd!r}；模板只用正斜杠")
    dispatch_cfg = (cfg.get("engine") or {}).get("dispatch") or {}
    if not isinstance(dispatch_cfg, dict):
        raise ConfigError("engine.dispatch must be a mapping")
    dispatch_console = dispatch_cfg.get("console", "")
    if dispatch_console is not None and not isinstance(dispatch_console, str):
        raise ConfigError("engine.dispatch.console must be a path or '-m <module>' string")
    projects = (cfg.get("github") or {}).get("projects") or {}
    if not isinstance(projects, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in projects.items()):
        raise ConfigError("github.projects must be a mapping of project name -> owner/repo")
    projects_file = (cfg.get("github") or {}).get("projects_file", "")
    if projects_file is not None and not isinstance(projects_file, str):
        raise ConfigError("github.projects_file must be a path string (or empty)")
    profiles_path = (cfg.get("engine") or {}).get("model_profiles_path", "")
    if profiles_path is not None and not isinstance(profiles_path, str):
        raise ConfigError("engine.model_profiles_path must be a path string (or empty)")
    crosscheck = (cfg.get("engine") or {}).get("archived_crosscheck", True)
    if not isinstance(crosscheck, bool):
        raise ConfigError("engine.archived_crosscheck must be a boolean")
    max_checks = (cfg.get("engine") or {}).get("max_closed_thread_checks", 10)
    if not isinstance(max_checks, int) or isinstance(max_checks, bool) \
            or max_checks < 1:
        raise ConfigError("engine.max_closed_thread_checks must be an integer >= 1")
    sup_sid = (cfg.get("engine") or {}).get("supervisor_sid", "")
    if sup_sid is not None and not isinstance(sup_sid, str):
        raise ConfigError("engine.supervisor_sid must be a string (or empty)")
    sup_keys = (cfg.get("engine") or {}).get("supervisor_keys", ["supervisor"])
    if not isinstance(sup_keys, list) or not all(isinstance(x, str) for x in sup_keys):
        raise ConfigError("engine.supervisor_keys must be a list of session key strings")
    timeout = (cfg.get("engine") or {}).get("release_timeout_s", 1800)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) \
            or timeout <= 0:
        raise ConfigError("engine.release_timeout_s must be a positive number")
    liveness_on = (cfg.get("engine") or {}).get("worker_liveness_enabled", False)
    if not isinstance(liveness_on, bool):
        raise ConfigError("engine.worker_liveness_enabled must be a boolean")
    liveness_rounds = (cfg.get("engine") or {}).get("worker_liveness_rounds", 2)
    if not isinstance(liveness_rounds, int) or isinstance(liveness_rounds, bool) \
            or liveness_rounds < 1:
        raise ConfigError("engine.worker_liveness_rounds must be an integer >= 1")
    liveness_interval = (cfg.get("engine") or {}).get("worker_liveness_interval_s", 60)
    if not isinstance(liveness_interval, (int, float)) \
            or isinstance(liveness_interval, bool) or liveness_interval <= 0:
        raise ConfigError("engine.worker_liveness_interval_s must be a positive number")
    poll = (cfg.get("github") or {}).get("poll_interval_s")
    if not isinstance(poll, (int, float)) or poll <= 0:
        raise ConfigError("github.poll_interval_s must be a positive number")
    held = (cfg.get("github") or {}).get("on_hold_labels", ["stage:on-hold"])
    if not isinstance(held, list) or not all(isinstance(x, str) for x in held):
        raise ConfigError("github.on_hold_labels must be a list of label strings")
    return cfg


def load(path: str | Path | None = None, overrides: dict | None = None) -> dict:
    """Return a validated config dict (defaults <- file <- overrides)."""
    cfg = copy.deepcopy(DEFAULTS)
    if path:
        cfg = _deep_merge(cfg, _read_json(Path(path)))
    if overrides:
        cfg = _deep_merge(cfg, overrides)
    cfg["state_dir"] = str(cfg.get("state_dir") or "state")
    return validate(cfg)


def state_root(cfg: dict, override: str | None = None) -> Path:
    root = override or cfg.get("state_dir") or "state"
    root = os.path.expanduser(str(root))
    return Path(root).resolve()
