"""Whole-map replacement, allowed only when every legacy writer has stopped.

Split out of ``session_broker`` for the 300-line cap (charter §3.1). It is the
**only** whole-map write in the system, and it exists for one job: a migration
that rewrites both maps at once. Everything that writes one key goes through
:mod:`session_broker` instead.

Refusal is the default. Two independent conditions must both hold:

1. no marker file names a live pid (:func:`live_writers`);
2. the operator has named every writer in ``attest``.

The second is not paperwork. #129's boundary says a *missing* marker must not be
read as "that writer is not running" -- a stopped launcher, a stale lock, or a
renamed file all look identical to a clean one. So absence is never evidence:
the person who stopped the processes says so, by name.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .session_map_store import BrokerError, paths, transact

LEGACY_WRITERS = ("launcher", "engine", "wss_bus", "atserve")

# Marker files, relative to the session root.
_MARKERS = {
    "launcher": ("launcher.pid",),
    "engine": ("engine.pid",),
    "wss_bus": ("wss_bus.lock", "wss_bus.pid"),
    "atserve": (os.path.join("atserve", "atserve.pid"),),
}


class MigrationRefused(BrokerError):
    """A whole-map replacement was asked for while a writer may still be alive."""


def _marker_pids(path: Path) -> list[int]:
    """pids recorded in a marker file, whatever shape it has (empty if unreadable)."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return []
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        pid = data.get("pid")
    elif isinstance(data, int):
        pid = data
    else:
        try:
            pid = int(text.split()[0])
        except (ValueError, IndexError):
            return []
    try:
        return [int(pid)]
    except (TypeError, ValueError):
        return []


def live_writers(cfg: dict | None = None) -> list[str]:
    """``"<name> (pid N)"`` for every legacy writer whose marker names a live pid."""
    from .pidlock import process_alive

    root = paths(cfg)[0].parent
    alive: list[str] = []
    for name in LEGACY_WRITERS:
        for relative in _MARKERS.get(name, ()):
            for pid in _marker_pids(root / relative):
                if process_alive(pid):
                    alive.append(f"{name} (pid {pid})")
                    break
    return alive


def refusals(cfg=None, attest=()) -> list[str]:
    """Every reason a whole-map replacement must not proceed."""
    reasons = [f"存活中的写方：{item}" for item in live_writers(cfg)]
    attested = {str(item) for item in (attest or ())}
    missing = [name for name in LEGACY_WRITERS if name not in attested]
    if missing:
        reasons.append(
            "缺少操作者确认（attest）的写方：" + "、".join(missing)
            + "；标记文件缺失不等于该写方已停止，必须逐个确认已停后显式传入")
    return reasons


def replace_all_for_migration(sessions: dict, issues: dict, cfg: dict | None = None,
                             attest=()) -> dict:
    """Replace **both** maps wholesale -- only with every legacy writer stopped.

    New in #129. Refused while any legacy writer is alive, and refused unless
    ``attest`` names all of :data:`LEGACY_WRITERS` (the operator's statement
    that they were stopped and the old locks released). The replacement itself
    still runs under the map lock and is read back.
    """
    reasons = refusals(cfg, attest)
    if reasons:
        raise MigrationRefused("整份迁移被拒绝：" + "；".join(reasons))
    if not isinstance(sessions, dict) or not isinstance(issues, dict):
        raise MigrationRefused("整份迁移被拒绝：两份映射都必须是对 key 的 dict")

    def mutate(current_sessions, current_issues):
        current_sessions.clear()
        current_sessions.update(sessions)
        current_issues.clear()
        current_issues.update(issues)
        return {"replaced": True,
                "sessions_keys": len(sessions), "issue_keys": len(issues)}

    return transact(mutate, cfg)
