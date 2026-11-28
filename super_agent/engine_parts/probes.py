"""Process-level probes: which build is running, and what a log tail says.

Split out of ``engine.py`` under ``rules/charters/worker.md`` §3.1.1. These are
answers about *this process and this repo*, not about an Issue, a cursor or an
outbox - so they never touch ``Engine`` state and belong here as plain
functions.

Two rules this module keeps, both learned the hard way:

* **A probe failure is not a crash.** A missing ``git`` binary or a repo without
  history answers ``""`` / ``(commit, None)``; the health record then says
  "unknown" instead of the engine dying at startup. ``None`` dirty means *could
  not tell*, which is a third answer and must not collapse into ``False``.
* **A log tail is bounded by both rows and bytes.** ``events.jsonl`` passed 9000
  rows in production, so reading it whole in a doctor check is not an option.
  A row that does not parse is skipped: the file is a log, and a torn last line
  is not a fault of its own.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

_CODE_VERSION = None


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def quiet_subprocess_kwargs() -> dict:
    """Hide the console window a ``git`` call would otherwise flash on Windows."""
    if os.name != "nt":
        return {}
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": info}


def repo_root() -> Path:
    """The checkout this package lives in - ``super_agent/`` is one level down."""
    return Path(__file__).resolve().parent.parent.parent


def current_head(root: Path | None = None) -> str:
    """Short commit of the checkout, or ``""`` when it cannot be read."""
    root = root or repo_root()
    try:
        return subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=3, check=False,
            **quiet_subprocess_kwargs()).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def code_version(root: Path | None = None) -> tuple[str, bool | None]:
    """``(commit, dirty)``; ``dirty`` is ``None`` when it could not be told.

    Cached in the module because ``git status`` on a large worktree is not free,
    and every round asks. ``commit`` is still refreshed when the cache holds an
    empty one, so a startup that raced a slow ``git`` does not stay blank.
    """
    global _CODE_VERSION
    root = root or repo_root()
    commit = str((_CODE_VERSION or ("", None))[0] or "")
    try:
        if not commit:
            commit = current_head(root)
        dirty = bool(subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True, text=True, timeout=3, check=False,
            **quiet_subprocess_kwargs()).stdout.strip())
        _CODE_VERSION = (commit, dirty)
    except (OSError, subprocess.SubprocessError):
        _CODE_VERSION = (commit, None)
    return _CODE_VERSION


def tail_events(path, *, limit: int, max_bytes: int = 512 * 1024) -> list:
    """The last ``limit`` JSON rows of an append-only JSONL file, bounded by bytes.

    Two bounds and no third: at most ``limit`` rows, and at most ``max_bytes``
    read from the end. A row that does not parse is skipped - the file is a log,
    and a torn last line is not a fault of its own.
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            start = max(0, size - max_bytes)
            fh.seek(start)
            chunk = fh.read()
    except OSError:
        return []
    text = chunk.decode("utf-8", "replace")
    if start > 0:
        # The first line of a mid-file read is almost certainly a fragment.
        text = text.split("\n", 1)[1] if "\n" in text else ""
    rows = []
    for raw in text.splitlines()[-limit:]:
        raw = raw.strip()
        if not raw:
            continue
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows
