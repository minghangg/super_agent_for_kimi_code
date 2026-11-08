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

import copy
import json
import os
from pathlib import Path

DEFAULTS: dict = {
    "role": "supervisor",
    "mode": "observe",
    "state_dir": "state",
    "bus": {
        "host": "127.0.0.1",
        "port": 58800,
        "timeout_s": 2.0,
    },
    "github": {
        "source": "none",          # none | fixture | live
        "repo": "",                # owner/name, only for source=live
        "fixture": "",             # path to a JSON fixture, for source=fixture
        "poll_interval_s": 60,
        "max_issues": 20,
        "max_comments_per_issue": 50,
    },
    "store": {
        "max_event_lines": 10_000,
        "max_outbox_lines": 5_000,
        "outbox_stale_s": 300,     # in_flight/uncertain older than this is stuck
    },
    "engine": {
        # Event-type allowlist: which event types the fast path reads.
        # The project/issue/action allowlist is a different dimension and lives
        # in config/allowlist.json (see super_agent.allowlist).
        "allowlist": ["issue_comment.created"],
        "cooldown_s": 900,
        "assignments": {},         # reserved; M0 must leave this empty
        "session_events_path": "",  # workbench lifecycle stream (read-only); "" = resolve
        "project_allowlist": "",   # "" = not configured; the CLI resolves the repo default
        "creation_mode": "pm_created",  # only pm_created is accepted
        # Model profile table (ISS-32). "" = not configured: choose_profile then
        # falls back to the built-in default pair and no file is read.
        "model_profiles_path": "",
    },
}

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
    profiles_path = (cfg.get("engine") or {}).get("model_profiles_path", "")
    if profiles_path is not None and not isinstance(profiles_path, str):
        raise ConfigError("engine.model_profiles_path must be a path string (or empty)")
    poll = (cfg.get("github") or {}).get("poll_interval_s")
    if not isinstance(poll, (int, float)) or poll <= 0:
        raise ConfigError("github.poll_interval_s must be a positive number")
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
