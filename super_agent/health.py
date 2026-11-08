"""``state/health.json``: one atomically replaced snapshot of engine health.

Fields (all always present, so a watchdog never has to guess):

``pid``                        process that wrote the snapshot
``mode``                       observe | assist
``last_loop_at``               last finished round (ISO-8601 UTC)
``last_successful_action_at``  last action that reached ``done``
``active_assignments``         number of assignments the index still tracks
``degraded``                   bool: something is wrong but the loop still runs
``last_error``                 the reason it is degraded, or ""
``running``                    True while a resident loop owns the state dir

Writes go to a temporary file then ``os.replace``, so a reader either sees the
previous snapshot or the new one, never a truncated file. ``running`` separates
a deliberate stop from a crash: otherwise a clean shutdown and a killed process
both look like "no pid lock but health says it ran", and a watchdog could not
tell them apart.
"""
from __future__ import annotations

import json
from pathlib import Path

from .store import atomic_write_text

FIELDS = ("pid", "mode", "last_loop_at", "last_successful_action_at",
          "active_assignments", "degraded", "last_error", "running")


def blank() -> dict:
    return {"pid": None, "mode": "observe", "last_loop_at": None,
            "last_successful_action_at": None, "active_assignments": 0,
            "degraded": False, "last_error": "", "running": False}


def read(path: str | Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = blank()
    out.update({k: data[k] for k in FIELDS if k in data})
    return out


def write(path: str | Path, record: dict) -> dict:
    out = blank()
    out.update({k: record[k] for k in FIELDS if k in record})
    out["active_assignments"] = int(out.get("active_assignments") or 0)
    out["degraded"] = bool(out.get("degraded"))
    out["last_error"] = str(out.get("last_error") or "")
    out["running"] = bool(out.get("running"))
    atomic_write_text(Path(path),
                      json.dumps(out, ensure_ascii=False, sort_keys=True) + "\n")
    return out
