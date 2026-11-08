"""Project allowlist: which project, which issue, which action may be touched.

Two allowlists live in this engine and they are different dimensions:

* ``engine.allowlist`` in :mod:`super_agent.config` lists **event types** the
  fast path reads (``issue_comment.created``). It answers "which events do we
  look at".
* this module answers "which project, which issue, which action may we act
  on". Being allowed to read an event type says nothing about being allowed to
  write to a repository, so the two are never merged.

Rules encoded here:

* an unlisted project (repository), issue or action is refused;
* a newly added issue is observe-only: it never inherits write permission, it
  must be listed in ``issues`` and its action must be listed in ``actions``;
* ``creation_mode`` accepts only ``pm_created``. The value
  ``engine_preapproved`` is rejected at load time: M0 has no approved path for
  the engine to create sessions.

Hot reload is mtime polling, not inotify. A changed file is parsed and
validated in full before it replaces the live copy. If it is malformed the
previous copy stays in force, the error is recorded, and the engine keeps
behaving as before. A reload only affects later events, never the round that
is already in flight.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from pathlib import Path

SCHEMA_VERSION = 1
MODES = ("observe", "assist")
CREATION_MODES = ("pm_created",)
ACTIONS = frozenset({
    "collect", "reconcile", "send", "comment", "bind", "assign", "fetch",
    "archive", "reconcile_close", "close", "dispatch", "bus_inject",
    "create_worker", "create_qa",
})
CREATION_ACTIONS = frozenset({"create_worker", "create_qa"})
DEFAULT_ACTIONS = ("collect", "reconcile")


class AllowlistError(ValueError):
    pass


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise AllowlistError(message)


def _string_list(value, key: str) -> list[str]:
    _require(isinstance(value, list), f"{key} must be a list of strings")
    out = []
    for item in value:
        _require(isinstance(item, str) and item.strip(),
                 f"{key} must contain non-empty strings")
        out.append(item.strip())
    return out


def validate(data) -> dict:
    """Return a normalized copy of ``data`` or raise :class:`AllowlistError`."""
    _require(isinstance(data, dict), "allowlist root must be a JSON object")
    _require(data.get("schema_version") == SCHEMA_VERSION,
             f"schema_version must be {SCHEMA_VERSION}")

    defaults = data.get("defaults") or {}
    _require(isinstance(defaults, dict), "defaults must be an object")
    default_mode = defaults.get("mode", "observe")
    _require(default_mode in MODES, f"defaults.mode must be one of {MODES}")
    default_creation = defaults.get("creation_mode", "pm_created")
    if default_creation not in CREATION_MODES:
        raise AllowlistError(
            f"defaults.creation_mode {default_creation!r} is forbidden: only "
            "'pm_created' is accepted (session creation needs an explicit "
            "approval that M0 does not grant)")

    projects = data.get("projects")
    _require(isinstance(projects, dict), "projects must be an object")
    normalized = {"schema_version": SCHEMA_VERSION,
                  "defaults": {"mode": default_mode,
                               "creation_mode": default_creation},
                  "projects": {}}

    for name, raw in projects.items():
        _require(isinstance(name, str) and name.strip(),
                 "each project key must be a non-empty string")
        _require(isinstance(raw, dict), f"project {name!r} must be an object")
        repo = raw.get("repo")
        _require(isinstance(repo, str) and repo.count("/") == 1
                 and all(part.strip() for part in repo.split("/")),
                 f"project {name!r}: repo must be 'owner/name'")
        cwd = raw.get("cwd")
        _require(isinstance(cwd, str) and cwd.strip(),
                 f"project {name!r}: cwd must be a non-empty path string")
        mode = raw.get("mode", default_mode)
        _require(mode in MODES,
                 f"project {name!r}: mode must be one of {MODES}")
        creation = raw.get("creation_mode", default_creation)
        if creation not in CREATION_MODES:
            raise AllowlistError(
                f"project {name!r}: creation_mode {creation!r} is forbidden: "
                "only 'pm_created' is accepted")
        issues = raw.get("issues", [])
        _require(isinstance(issues, list),
                 f"project {name!r}: issues must be a list of integers")
        clean_issues = []
        for issue in issues:
            _require(isinstance(issue, int) and not isinstance(issue, bool)
                     and issue >= 1,
                     f"project {name!r}: issues must contain positive integers")
            clean_issues.append(int(issue))
        actions = raw.get("actions", list(DEFAULT_ACTIONS))
        clean_actions = _string_list(actions, f"project {name!r}: actions")
        for action in clean_actions:
            _require(action in ACTIONS,
                     f"project {name!r}: unknown action {action!r} "
                     f"(known: {sorted(ACTIONS)})")
        writers = raw.get("max_active_writers", 1)
        _require(isinstance(writers, int) and not isinstance(writers, bool)
                 and writers >= 1,
                 f"project {name!r}: max_active_writers must be an integer >= 1")
        roles = raw.get("roles", ["worker", "qa"])
        clean_roles = _string_list(roles, f"project {name!r}: roles")
        normalized["projects"][name.strip()] = {
            "repo": repo.strip(),
            "cwd": cwd.strip(),
            "mode": mode,
            "creation_mode": creation,
            "issues": sorted(set(clean_issues)),
            "actions": sorted(set(clean_actions)),
            "max_active_writers": int(writers),
            "roles": clean_roles,
        }
    return normalized


class ProjectAllowlist:
    """One allowlist file, hot-reloadable, failing closed only on first load.

    ``data`` is ``None`` until a valid copy has been seen. ``load_error`` holds
    the last rejection. ``generation`` grows on every accepted reload, which is
    what ``doctor`` reports as proof that the previous copy is still in use.
    """

    def __init__(self, path: str | Path, *, cache_path: str | Path | None = None):
        self.path = Path(path)
        # The last known-good copy lives outside the allowlist file so a fresh
        # process (doctor, or a restart after a bad edit) can still say "the
        # previous copy is in force" instead of having no copy at all.
        self.cache_path = Path(cache_path) if cache_path else \
            self.path.with_name(self.path.name + ".last-good")
        self.data: dict | None = None
        self.load_error = ""
        self.generation = 0
        self.using_previous = False
        self._stamp: tuple | None = None
        self.reload()

    # ------------------------------------------------------------------ state
    def _stamp_now(self):
        """Cheap mtime/size trigger, plus a content hash for same-tick edits.

        mtime alone misses two writes inside one filesystem tick, and a change
        like ``[28]`` -> ``"28"`` keeps the byte count identical, so the hash is
        what actually decides whether the content changed.
        """
        try:
            stat = self.path.stat()
            raw = self.path.read_bytes()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size,
                hashlib.sha256(raw).hexdigest())

    def _save_cache(self, data: dict) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_name(self.cache_path.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n",
                           encoding="utf-8")
            os.replace(tmp, self.cache_path)
        except OSError:
            pass          # the cache is a convenience; never fail the load for it

    def _load_cache(self) -> dict | None:
        try:
            return validate(json.loads(self.cache_path.read_text(encoding="utf-8")))
        except (OSError, ValueError, AllowlistError):
            return None

    def reload(self) -> dict:
        """Read + validate once. Never raises: failures are recorded."""
        stamp = self._stamp_now()
        if stamp is None:
            self.load_error = f"cannot read allowlist {self.path.name}"
            return {"changed": False, "used": "previous" if self.data else "none",
                    "error": self.load_error}
        try:
            data = validate(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError, AllowlistError) as exc:
            self._stamp = stamp
            self.load_error = f"allowlist {self.path.name}: {exc}"
            if self.data is None:
                cached = self._load_cache()
                if cached is not None:
                    self.data = cached
                    self.using_previous = True
                    return {"changed": False, "used": "previous",
                            "error": self.load_error}
            return {"changed": False, "used": "previous" if self.data else "none",
                    "error": self.load_error}
        self.data = data
        self.load_error = ""
        self.using_previous = False
        self.generation += 1
        self._stamp = stamp
        self._save_cache(data)
        return {"changed": True, "used": "new", "error": ""}

    def poll(self) -> dict:
        """Hot reload when the file content changes; a bad file keeps the last copy.

        The trigger is ``(mtime, size, content hash)``, not mtime alone: two
        writes inside one filesystem tick, or an edit that keeps the byte count
        (``[28]`` -> ``"28"``), would otherwise be missed.
        """
        stamp = self._stamp_now()
        if stamp == self._stamp:
            return {"changed": False,
                    "used": "previous" if (self.using_previous or self.data) else "none",
                    "error": self.load_error}
        return self.reload()

    # ----------------------------------------------------------------- checks
    def project(self, name: str | None) -> dict | None:
        if self.data is None or not name:
            return None
        return self.data["projects"].get(str(name))

    def summary(self) -> dict:
        projects = sorted((self.data or {}).get("projects", {}))
        return {"path": str(self.path), "generation": self.generation,
                "valid": self.data is not None, "error": self.load_error,
                # ``poll()`` reports ``used: previous`` for any rejection that
                # leaves a copy in force, so the summary must agree with it
                # rather than only tracking the cold-start cache path.
                "using_previous": bool(self.load_error) or bool(self.using_previous),
                "cache": str(self.cache_path),
                "projects": projects}

    def check(self, project: str | None, issue: int | None = None,
              action: str | None = None) -> tuple[bool, str]:
        """``(allowed, reason)``. Unlisted project/issue/action is refused."""
        if self.data is None:
            return False, (self.load_error or "allowlist has no usable copy")
        entry = self.project(project)
        if entry is None:
            return False, f"project {project!r} not allowlisted (repository unlisted)"
        if issue is not None:
            try:
                issue_number = int(issue)
            except (TypeError, ValueError):
                return False, f"issue {issue!r} is not an integer"
            if issue_number not in entry["issues"]:
                return False, (f"issue #{issue_number} not allowlisted "
                               "(observe only; write permission is never inherited)")
        if action is not None:
            if action not in entry["actions"]:
                return False, f"action {action!r} not allowlisted"
            if action in CREATION_ACTIONS:
                return False, ("creation_mode_forbidden: pm_created does not "
                               "create sessions")
        return True, "allowed"


def load(path: str | Path) -> ProjectAllowlist:
    return ProjectAllowlist(path)


def clone(data: dict) -> dict:
    return copy.deepcopy(data)
