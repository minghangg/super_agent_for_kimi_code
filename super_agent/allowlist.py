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

**Local overlay.** A tracked allowlist must stay publishable, so the real
repository name cannot live in it. ``<name>.local.json`` next to the tracked
file (i.e. ``config/allowlist.local.json``) is read *by the same loader* and
deep-merged over it. Only ``repo``/``issues``/``actions``/``mode``/``roles``
may be overridden; everything else is inherited. The
merged result is validated again, so a broken overlay is refused and the
tracked copy stays in force - the gate never widens because a local file is
malformed. Deleting the overlay restores single-file behaviour exactly.
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

# Fields a local overlay may override. Everything else is inherited from the
# tracked file: the overlay adds a deployment fact (the real repository name),
# it is not a second place to define project policy.
OVERLAY_FIELDS = ("repo", "issues", "actions", "mode", "roles")
OVERLAY_SUFFIX = ".local.json"


def overlay_path(path: str | Path) -> Path:
    """``config/allowlist.json`` -> ``config/allowlist.local.json``."""
    tracked = Path(path)
    stem = tracked.name[:-len(".json")] if tracked.name.endswith(".json") else tracked.name
    return tracked.with_name(stem + OVERLAY_SUFFIX)


def merge_overlay(tracked: dict, overlay: dict) -> dict:
    """Deep-merge the overlay over the tracked data; returns a new dict.

    Only the projects named in the overlay are touched, and only through
    :data:`OVERLAY_FIELDS`. An overlay project that the tracked file does not
    define is ignored rather than added: the overlay may narrow the shipped
    policy for a deployment, never introduce a new project behind its back.
    """
    out = copy.deepcopy(tracked)
    projects = out.setdefault("projects", {})
    for name, raw in ((overlay or {}).get("projects") or {}).items():
        if name not in projects or not isinstance(raw, dict):
            continue
        entry = dict(projects[name])
        for field in OVERLAY_FIELDS:
            if field in raw:
                entry[field] = copy.deepcopy(raw[field])
        projects[name] = entry
    defaults = (overlay or {}).get("defaults")
    if isinstance(defaults, dict) and "mode" in defaults:
        out.setdefault("defaults", {})["mode"] = defaults["mode"]
    return out


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
        roles = raw.get("roles", ["worker", "qa"])
        clean_roles = _string_list(roles, f"project {name!r}: roles")
        normalized["projects"][name.strip()] = {
            "repo": repo.strip(),
            "cwd": cwd.strip(),
            "mode": mode,
            "creation_mode": creation,
            "issues": sorted(set(clean_issues)),
            "actions": sorted(set(clean_actions)),
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
        self.overlay_path = overlay_path(self.path)
        self.data: dict | None = None
        self.load_error = ""
        self.generation = 0
        self.using_previous = False
        self.source_files: list[str] = [str(self.path)]
        self.effective_from: dict[str, str] = {}
        self._stamp: tuple | None = None
        self.reload()

    # ------------------------------------------------------------------ state
    def _stamp_now(self):
        """Cheap mtime/size trigger, plus a content hash for same-tick edits.

        mtime alone misses two writes inside one filesystem tick, and a change
        like ``[28]`` -> ``"28"`` keeps the byte count identical, so the hash is
        what actually decides whether the content changed. The overlay is part
        of the stamp: editing only the local file must trigger a reload too.
        """
        stamp = []
        for path in (self.path, self.overlay_path):
            try:
                stat = path.stat()
                raw = path.read_bytes()
            except OSError:
                stamp.append(None)
                continue
            stamp.append((stat.st_mtime_ns, stat.st_size,
                          hashlib.sha256(raw).hexdigest()))
        return tuple(stamp)

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
        """Read + validate once. Never raises: failures are recorded.

        The local overlay is part of the same load: it is merged before
        validation, so the effective data is always the validated pair. A
        broken *overlay* is reported and ignored (the tracked copy stays in
        force, exactly like a broken tracked file); only a broken tracked file
        falls back to the last-good cache.
        """
        stamp = self._stamp_now()
        if stamp[0] is None:
            self.load_error = f"cannot read allowlist {self.path.name}"
            self._note_sources(None)
            return {"changed": False, "used": "previous" if self.data else "none",
                    "error": self.load_error}
        overlay_error = ""
        try:
            tracked = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
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

        merged, overlaid = tracked, {}
        local_data, overlay_error = self._read_overlay()
        merged = merge_overlay(tracked, local_data)
        overlaid = dict(local_data.get("projects") or {})

        try:
            data = validate(merged)
        except (ValueError, AllowlistError) as exc:
            self._stamp = stamp
            self.load_error = f"allowlist {self.path.name}: {exc}"
            if self.overlay_path.is_file():
                # The merged pair is bad, but the tracked half may be fine: fall
                # back to it so one bad value in an untracked file cannot stop
                # the engine. The reason still reaches doctor via the summary.
                try:
                    data = validate(tracked)
                except (ValueError, AllowlistError):
                    data = None
                if data is not None:
                    self.data = data
                    self.load_error = (f"{self.overlay_path.name}: "
                                       f"{overlay_error or exc}")
                    self.using_previous = True
                    self.generation += 1
                    self._stamp = stamp
                    self._note_sources({})
                    self._save_cache(data)
                    return {"changed": True, "used": "tracked",
                            "error": self.load_error}
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
        self.load_error = overlay_error
        self.using_previous = bool(overlay_error)
        self.generation += 1
        self._stamp = stamp
        self._note_sources({name: "local" for name in overlaid})
        self._save_cache(data)
        return {"changed": True, "used": "new", "error": ""}

    def _note_sources(self, overlaid: dict | None) -> None:
        """Record which files produced ``data`` and per-project provenance."""
        files = [str(self.path)]
        self.effective_from = {}
        if overlaid is not None and self.overlay_path.is_file():
            files.append(str(self.overlay_path))
        if overlaid is not None:
            for name in sorted((self.data or {}).get("projects", {})):
                self.effective_from[name] = (
                    "local" if name in (overlaid or {}) else "tracked")
        self.source_files = files

    def _read_overlay(self) -> tuple[dict, str]:
        """``(data, error)`` for the overlay; ``({}, "")`` when it is absent."""
        if not self.overlay_path.is_file():
            return {}, ""
        try:
            raw = json.loads(self.overlay_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return {}, f"overlay unreadable: {exc}"
        if not isinstance(raw, dict):
            return {}, "overlay root must be a JSON object"
        projects = raw.get("projects", {})
        if not isinstance(projects, dict):
            return {}, "overlay projects must be an object"
        return raw, ""

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

    def sync_issues(self, project: str, issues) -> dict:
        """Record ``issues`` for ``project`` in the untracked overlay.

        The tracked allowlist is a published file: rewriting it from a running
        engine would turn configuration into run state and put a private
        repository's issue numbers into a tracked diff. The overlay
        (``<name>.local.json``) already has ``issues`` in
        :data:`OVERLAY_FIELDS` and is never committed, so that is where the
        engine keeps "which issues are live right now".

        Idempotent: identical content is not rewritten. A write failure is
        reported, never raised - a read-only checkout must still run.
        """
        name = str(project or "").strip()
        wanted = sorted({int(n) for n in (issues or []) if str(n).strip().lstrip("-").isdigit()})
        out = {"project": name, "written": False, "changed": False, "error": ""}
        if not name or self.data is None or name not in self.data["projects"]:
            out["error"] = f"project {name!r} not in the allowlist"
            return out
        current = self.data["projects"][name].get("issues") or []
        if sorted(current) == wanted:
            return out
        raw, error = self._read_overlay()
        if error:
            out["error"] = error
            return out
        overlay = copy.deepcopy(raw) if raw else {}
        projects = overlay.setdefault("projects", {})
        entry = projects.setdefault(name, {})
        if not isinstance(entry, dict):
            out["error"] = f"overlay project {name!r} is not an object"
            return out
        entry["issues"] = wanted
        text = json.dumps(overlay, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
        try:
            self.overlay_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.overlay_path.with_name(self.overlay_path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            os.replace(tmp, self.overlay_path)
        except OSError as exc:
            out["error"] = f"{type(exc).__name__}: {exc}"
            return out
        out["written"] = True
        out["changed"] = True
        self.poll()
        return out

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
                "source_files": list(self.source_files),
                "overlay": str(self.overlay_path),
                "overlay_present": self.overlay_path.is_file(),
                "effective_from": dict(self.effective_from),
                "projects": projects}

    def source_label(self) -> str:
        """``tracked``, ``tracked+local`` or ``tracked+local(生效:local)``."""
        if not self.overlay_path.is_file():
            return "tracked"
        if not self.effective_from:
            return "tracked+local"
        local = sorted(k for k, v in self.effective_from.items() if v == "local")
        if not local:
            return "tracked+local(生效:tracked)"
        return f"tracked+local(生效:local: {','.join(local)})"

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
