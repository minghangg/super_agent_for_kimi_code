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
    "state_dir": "data",
    # Where the pre-migration runtime root is looked for. "" means "the
    # ``state`` sibling of the chosen root" (see ``cli._legacy_root``).
    "legacy_state_dir": "",
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
        # Labels that mean "suspend this issue": the issue stays open but the
        # engine does not look at it. Matched as a set against the labels the
        # list endpoint already returns, so no extra request. Empty list means
        # no filter (fail open) - missing an issue silently hides its frames,
        # which is worse than looking at one too many.
        "on_hold_labels": ["stage:on-hold"],
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
        # Ask the session service whether a candidate sid is archived. The
        # lifecycle stream has no archive row for an API-side archive, so this
        # is what stops a closed session from being treated as a live binding.
        # False = stream-only (no server call; used by tests that stay offline).
        "archived_crosscheck": True,
        # Model profile table (ISS-32). "" = not configured: choose_profile then
        # falls back to the built-in default pair and no file is read.
        "model_profiles_path": "",
        # A-1 (ISS-57): the contract file a new session reads first.
        # "" = fall back to the inert ping.
        "contract_path": "",
        # How many held threads one round asks the close reconciliation about.
        # ``waiting_release`` and ``close_reconcile`` are the only states that
        # need it, so the cap bounds a poll loop rather than all assignments.
        "max_closed_thread_checks": 10,
        # Where a shippable verdict goes (ISS-53). ``supervisor_sid`` wins when
        # set; otherwise ``supervisor_keys`` are looked up in ``sessions.json``
        # in order. The default list carries only the public role name: a
        # private key belongs in the untracked overlay, never here.
        "supervisor_sid": "",
        "supervisor_keys": ["supervisor"],
        # A thread held by close reconciliation for longer than this reminds
        # the supervisor once (at most once per start time).
        "release_timeout_s": 1800,
        # Worker/QA liveness watch (ISS-69). A bound worker or QA session that
        # is not busy for this many consecutive probes, with no new comment or
        # verdict on its thread in between, gets one exception frame to the
        # supervisor. Off by default: nobody configured it means nobody runs it.
        # busy only answers "alive or dead", never "finished" - completion stays
        # with the verdict contract.
        "worker_liveness_enabled": False,
        "worker_liveness_rounds": 2,
        "worker_liveness_interval_s": 60,
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
