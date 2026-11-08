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
``phase``                      ``running`` | ``startup-failed`` | ``stopped``

Writes go to a temporary file then ``os.replace``, so a reader either sees the
previous snapshot or the new one, never a truncated file. ``running`` separates
a deliberate stop from a crash: otherwise a clean shutdown and a killed process
both look like "no pid lock but health says it ran", and a watchdog could not
tell them apart. ``phase`` adds the third case: a process that wrote its health
snapshot, failed during startup and exited before any round ran. It is not a
crash, and ``running=false`` alone would read as a clean stop.

``write()`` guarantees the invariant: ``startup-failed``/``stopped`` are only
ever stored with ``running=false``, and ``phase="running"`` only with
``running=true``. A caller passing a contradictory pair gets it corrected and a
WARN on stderr - the snapshot is never dropped, and a watchdog never sees a
phase and a ``running`` flag that disagree.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .store import atomic_write_text

FIELDS = ("pid", "mode", "last_loop_at", "last_successful_action_at",
          "active_assignments", "degraded", "last_error", "running", "phase")

PHASES = ("running", "startup-failed", "stopped")


def blank() -> dict:
    return {"pid": None, "mode": "observe", "last_loop_at": None,
            "last_successful_action_at": None, "active_assignments": 0,
            "degraded": False, "last_error": "", "running": False,
            "phase": "stopped"}


def read(path: str | Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = blank()
    out.update({k: data[k] for k in FIELDS if k in data})
    if data.get("phase") not in PHASES:
        # A snapshot written before ``phase`` existed: derive it so the two
        # fields never disagree.
        out["phase"] = "running" if out.get("running") else "stopped"
    return out


def write(path: str | Path, record: dict) -> dict:
    out = blank()
    out.update({k: record[k] for k in FIELDS if k in record})
    out["active_assignments"] = int(out.get("active_assignments") or 0)
    out["degraded"] = bool(out.get("degraded"))
    out["last_error"] = str(out.get("last_error") or "")
    out["running"] = bool(out.get("running"))
    phase = record.get("phase")
    out["phase"] = (phase if phase in PHASES else
                    ("running" if out["running"] else "stopped"))
    enforce_invariant(out, where="health.write")
    atomic_write_text(Path(path),
                      json.dumps(out, ensure_ascii=False, sort_keys=True) + "\n")
    return out


def enforce_invariant(out: dict, *, where: str = "health") -> dict:
    """``write()`` guarantees ``phase``/``running`` agree in what it stores.

    The correction is deliberate over a refusal: refusing the write would make
    the caller lose its snapshot silently, and a missing snapshot reads as "no
    engine ever ran" - the exact ambiguity ``phase`` exists to remove. The
    correction keeps the more specific field (``phase`` carries whether this was
    a startup failure or a clean stop) and forces ``running`` to match it, then
    says so on stderr so the mistake in the caller does not go unnoticed.
    """
    if out["running"] and out["phase"] in ("startup-failed", "stopped"):
        _warn(f"{where}: phase={out['phase']} 与 running=True 矛盾，"
              f"已纠正为 running=False")
        out["running"] = False
    elif not out["running"] and out["phase"] == "running":
        _warn(f"{where}: phase=running 与 running=False 矛盾，"
              f"已纠正为 stopped")
        out["phase"] = "stopped"
    return out


def _warn(message: str) -> None:
    sys.stderr.write(message + "\n")
