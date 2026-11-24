"""One writer for the two session maps (#129, parent #124 C).

``sessions.json`` (key -> sid) and ``issue_sessions.json`` (thread -> role ->
session) are read by the engine and used to be written by four CLIs, each doing
the same thing by hand: read the whole file, mutate one key, write the whole
file back. Two processes writing *different* keys each dropped the other's row,
and nothing reported it -- the key was simply gone, and the session it named was
never told about its issue (#92).

So every write of either map goes through here, and the entries are **functional
and per key/role**: :func:`register_key`, :func:`set_key`, :func:`remove_key`,
:func:`bind_role`, :func:`close_thread`, :func:`retire`. No caller passes a
whole map to be written; that is what made the lost update possible.

Supporting modules:

* :mod:`super_agent.session_map_store` -- the lock, the atomic save, the
  read-back, and both maps opened as one critical section;
* :mod:`super_agent.session_migration` -- the only whole-map write,
  ``replace_all_for_migration``, which refuses while any legacy writer lives.

Paths are not resolved here: :func:`super_agent.session_map_store.paths` asks
``session_ledger``, the one place the session root is resolved (the chain in
``session_events``). A second chain is how a binding lands where the engine
never looks.
"""
from __future__ import annotations

import datetime as dt

from .session_map_store import (BrokerError, FileLock, ISSUE_SESSIONS_NAME,
                                LOCK_NAME, SESSIONS_NAME, load_map, lock_path,
                                paths, save_and_verify, transact)
from .session_migration import (LEGACY_WRITERS, MigrationRefused,
                                live_writers, replace_all_for_migration)

__all__ = [
    "BrokerError", "LEGACY_WRITERS", "MigrationRefused", "SessionWriterBroker",
    "bind_role", "close_thread", "live_writers", "lock_path", "paths",
    "read_issue_sessions", "read_sessions", "register_key", "remove_key",
    "replace_all_for_migration", "retire", "set_key",
]


def _stamp() -> str:
    return dt.datetime.now().strftime("%Y-%m-%dT%H:%M")


# --------------------------------------------------------------------- reads
def read_sessions(cfg: dict | None = None) -> dict:
    return load_map(paths(cfg)[0])


def read_issue_sessions(cfg: dict | None = None) -> dict:
    return load_map(paths(cfg)[1])


# ------------------------------------------------------------ key/role writes
def register_key(name: str, sid: str, cfg: dict | None = None) -> str:
    """Record ``name -> sid`` in ``sessions.json``; one-line report.

    **Raises** :class:`BrokerError` when the write fails (QA P0-1 ③). Returning
    a warning string made the failure invisible: ``at_cli.cmd_start`` discarded
    the value, so an AT session could exist with no registered key and exit
    code 0 -- the exact "has a binding, has no key, nobody knows" shape this
    change exists to remove. Callers either handle it or let it surface.
    """
    key = str(name or "").strip()
    if not key:
        return ""
    result = set_key(key, sid, cfg=cfg)
    verb = "已改指" if result["old_sid"] and result["old_sid"] != sid else "已登记"
    return f"{verb}会话键 {key} → {sid[:12]}"


def set_key(name: str, sid: str, note: str | None = None,
            cfg: dict | None = None) -> dict:
    """Replace ``sessions.json[name]``; returns ``{"old_sid", "old_note"}``."""
    key = str(name or "").strip()
    if not key:
        raise BrokerError("set_key: 会话键为空")

    def mutate(sessions, _issues):
        old = sessions.get(key)
        old_sid = str(old.get("id") or "") if isinstance(old, dict) else ""
        old_note = str(old.get("note") or "") if isinstance(old, dict) else ""
        sessions[key] = {"id": str(sid),
                         "updated": _stamp(),
                         "note": old_note if note is None else str(note)}
        return {"old_sid": old_sid, "old_note": old_note}

    return transact(mutate, cfg)


def remove_key(name: str, cfg: dict | None = None) -> dict:
    """Drop ``sessions.json[name]``; returns the removed record (``{}`` if none)."""
    key = str(name or "").strip()

    def mutate(sessions, _issues):
        return sessions.pop(key, {}) if key else {}

    return transact(mutate, cfg)


def bind_role(thread: str, role: str, sid: str, name: str = "",
              extra: dict | None = None, cfg: dict | None = None) -> dict:
    """Record ``issue_sessions[thread][role]`` and, when named, its key.

    Both maps move inside one critical section, so a reader never sees the
    binding without the registration the context tools look up.
    """
    thread = str(thread or "").strip()
    role = str(role or "").strip()
    if not thread or not role:
        raise BrokerError("bind_role: thread 与 role 都不能为空")
    record = {"session": str(sid), "name": str(name or ""),
              "bound": dt.datetime.now().isoformat(timespec="seconds")}
    record.update(extra or {})

    def mutate(sessions, issues):
        entry = issues.get(thread)
        entry = dict(entry) if isinstance(entry, dict) else {}
        entry[role] = record
        issues[thread] = entry
        if name:
            old = sessions.get(str(name))
            sessions[str(name)] = {
                "id": str(sid), "updated": _stamp(),
                "note": str((old or {}).get("note") or "") if isinstance(old, dict) else ""}
        return {"thread": thread, "role": role, "record": dict(record)}

    return transact(mutate, cfg)


def close_thread(thread: str, cfg: dict | None = None) -> dict:
    """Drop ``issue_sessions[thread]`` after its sessions were archived."""
    thread = str(thread or "").strip()

    def mutate(_sessions, issues):
        return issues.pop(thread, {}) if thread else {}

    return transact(mutate, cfg)


def retire(old_sid: str, new_sid: str, stamp: str | None = None,
           cfg: dict | None = None) -> dict:
    """Repoint every position that names ``old_sid``, in both maps.

    Two shapes in ``sessions.json`` (``id``, and the ``session``/``worker.session``
    spellings other readers use) and one in ``issue_sessions.json``
    (``role.session``). Missing any of them leaves a session that is still fed.
    Returns ``{"sessions": [...], "issues": [...]}`` with the moved positions, so
    the caller can emit one ``session.key_changed`` per position as before.
    """
    old_sid, new_sid = str(old_sid or "").strip(), str(new_sid or "").strip()
    if not old_sid or not new_sid:
        raise BrokerError("retire: 需要旧 sid 与新 sid")
    if old_sid == new_sid:
        raise BrokerError("retire: 旧 sid 与新 sid 相同，拒绝")
    stamp = stamp or _stamp()

    def mutate(sessions, issues):
        moved: list[str] = []
        issue_moved: list[str] = []
        for key, val in list(sessions.items()):
            if not isinstance(val, dict):
                continue
            entry = dict(val)
            touched = False
            if str(entry.get("id") or "") == old_sid:
                entry["id"], touched = new_sid, True
                moved.append(f"{key}.id")
            if str(entry.get("session") or "") == old_sid:
                entry["session"], touched = new_sid, True
                moved.append(f"{key}.session")
            worker = entry.get("worker")
            if isinstance(worker, dict) and str(worker.get("session") or "") == old_sid:
                worker = dict(worker)
                worker["session"] = new_sid
                entry["worker"], touched = worker, True
                moved.append(f"{key}.worker.session")
            if not touched:
                continue
            entry["updated"] = stamp
            entry["note"] = (entry.get("note") or "") + f" [retired {old_sid[:12]}]"
            sessions[key] = entry
        for issue_key, roles in list(issues.items()):
            if not isinstance(roles, dict):
                continue
            roles = dict(roles)
            touched = False
            for role, rec in list(roles.items()):
                if not isinstance(rec, dict) or str(rec.get("session") or "") != old_sid:
                    continue
                rec = dict(rec)
                rec["session"], rec["updated"] = new_sid, stamp
                roles[role], touched = rec, True
                issue_moved.append(f"{issue_key}.{role}.session")
            if touched:
                issues[issue_key] = roles
        return {"sessions": moved, "issues": issue_moved}

    return transact(mutate, cfg)


class SessionWriterBroker:
    """Named entry point for callers that prefer an object over loose functions."""

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg

    def read_sessions(self) -> dict:
        return read_sessions(self.cfg)

    def read_issue_sessions(self) -> dict:
        return read_issue_sessions(self.cfg)

    def register_key(self, name: str, sid: str) -> str:
        return register_key(name, sid, cfg=self.cfg)

    def set_key(self, name: str, sid: str, note: str | None = None) -> dict:
        return set_key(name, sid, note=note, cfg=self.cfg)

    def bind_role(self, thread: str, role: str, sid: str, name: str = "",
                  extra: dict | None = None) -> dict:
        return bind_role(thread, role, sid, name=name, extra=extra, cfg=self.cfg)

    def close_thread(self, thread: str) -> dict:
        return close_thread(thread, cfg=self.cfg)

    def retire(self, old_sid: str, new_sid: str, stamp: str | None = None) -> dict:
        return retire(old_sid, new_sid, stamp=stamp, cfg=self.cfg)

    def replace_all_for_migration(self, sessions: dict, issues: dict,
                                  attest=()) -> dict:
        return replace_all_for_migration(sessions, issues, cfg=self.cfg, attest=attest)
