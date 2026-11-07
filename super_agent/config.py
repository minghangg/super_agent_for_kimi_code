"""Configuration loading.

Defaults live in ``DEFAULTS`` below and every field can be overridden by a
JSON config file (``--config``) or by CLI flags. The role name is
configuration-only: nothing in the engine compares against a literal role
string, so renaming the role changes no behaviour.
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
    },
    "engine": {
        "allowlist": ["issue_comment.created"],
        "cooldown_s": 900,
        "assignments": {},         # reserved; M0 must leave this empty
        "session_events_path": "",  # workbench lifecycle stream (read-only); "" = resolve
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
    allow = (cfg.get("engine") or {}).get("allowlist")
    if not isinstance(allow, list) or not all(isinstance(x, str) for x in allow):
        raise ConfigError("engine.allowlist must be a list of event type strings")
    events_path = (cfg.get("engine") or {}).get("session_events_path")
    if events_path is not None and not isinstance(events_path, str):
        raise ConfigError("engine.session_events_path must be a path string (or empty)")
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
