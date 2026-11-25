"""Local full cache for GitHub listings (#134 step 3).

The cache is a read-through mirror, not a second source of truth. A hit replaces
a network read of ``issues()`` or ``comments()``; it never replaces the engine's
collection path. In particular the rows from a cached ``comments()`` still reach
``BoundedBatchQueue.put()``, so #130 backpressure applies to them exactly as it
does to live rows.

Three rules are enforced here, each with a cheap wrong version:

* **write only after success.** An exception writes zero bytes, so a failed
  refresh cannot overwrite a good listing with an empty one;
* **one atomic writer.** All writes go through
  :func:`super_agent.session_map_store.write_atomic`, which is the #129
  ``atomic_replace`` path. A second in-place writer would let a reader see a
  half-written listing;
* **no new config key.** The root comes from ``engine_state_dir()`` with the
  public checkout's ignored ``data/`` as fallback.

``paged()`` is deliberately absent: rebuild must walk real pages (#134 反例 13).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .. import agent_config
from ..session_map_store import write_atomic

CACHE_DIR = "github_cache"


def cache_root() -> Path:
    """Where the cache lives: ``<engine-state>/github_cache``."""
    base = agent_config.engine_state_dir()
    if not base:
        base = str(Path(__file__).resolve().parents[2] / "data")
    return Path(base) / CACHE_DIR


def _read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write(path: Path, payload) -> None:
    write_atomic(path, json.dumps(payload, ensure_ascii=False, sort_keys=True))


def read_issues(root: Path | None = None) -> list | None:
    """The last full issues listing, or ``None`` when no good copy exists.

    Kept for callers that only need the last successful snapshot. The live path
    does **not** read it to answer ``issues()``: #134 §3 says the listing is
    refreshed every round, and a cached answer here would freeze the live set.
    """
    payload = _read((root or cache_root()) / "issues.json")
    return payload if isinstance(payload, list) else None


def write_issues(rows: list, *, labels: dict | None = None,
                 etags: dict | None = None, root: Path | None = None) -> None:
    """Store the **full** issues listing after a successful network read.

    Callers must pass the whole listing, never a ``limit``-truncated answer:
    ``doctor()`` asks for ``issues(limit=1)``, and letting that one row become
    the snapshot would narrow every later round to a single issue (#134 P0-1).

    ``labels`` is the ``{number: labels}`` map from the same request. It is
    stored beside the numbers so a ``flow()`` answer needs no second request
    (#134 P1-1).
    """
    base = root or cache_root()
    _write(base / "issues.json", list(rows))
    _write(base / "labels.json", {str(k): list(v or [])
                                 for k, v in (labels or {}).items()})
    _write(base / "meta.json", {
        "fetched_at": time.time(), "count": len(rows), "etags": dict(etags or {}),
    })


def read_labels(root: Path | None = None) -> dict:
    """``{number: labels}`` from the last issues read; ``{}`` when absent."""
    payload = _read((root or cache_root()) / "labels.json")
    if not isinstance(payload, dict):
        return {}
    out = {}
    for key, value in payload.items():
        try:
            out[int(key)] = list(value or [])
        except (TypeError, ValueError):
            continue
    return out


def read_comments(issue: int, ttl_s: float, *,
                  root: Path | None = None, now: float | None = None) -> list | None:
    """The full comment listing while it is younger than ``ttl_s``."""
    path = (root or cache_root()) / "comments" / f"{int(issue)}.json"
    payload = _read(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        return None
    age = (time.time() if now is None else now) - float(payload.get("saved_at", 0))
    if age < 0 or age > float(ttl_s):
        return None
    return payload["rows"]


def write_comments(issue: int, rows: list, *,
                   root: Path | None = None) -> None:
    """Store one issue's full comment listing after a successful read."""
    base = root or cache_root()
    _write(base / "comments" / f"{int(issue)}.json",
           {"saved_at": time.time(), "rows": list(rows)})
