"""Local store: compact index, append-only event log, outbox.

Three properties are load-bearing for M0 and each is testable on its own:

* ``index.json`` is replaced atomically. A crash after the temporary file is
  written but before the rename leaves the previous index intact.
* ``events.jsonl`` and ``outbox.jsonl`` are bounded; when the current segment
  reaches its line limit it rotates to ``.1`` (and ``.1`` to ``.2``), so disk
  usage does not grow without limit.
* ``cursor.json`` records per-issue comment cursors so a second run does not
  re-read comments the first run already saw.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

# Test seam: called with the temporary path right before the atomic rename.
# A test can set this to a callable that raises, proving the old file survives.
FAULT_HOOK = None


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        hook = FAULT_HOOK
        if hook is not None:
            hook(tmp)
        os.replace(tmp, path)
    finally:
        # After a successful rename the temporary file is already gone, so this
        # is a no-op there; after a failed write it removes the residue.
        tmp.unlink(missing_ok=True)


def _count_lines(path: Path) -> int:
    try:
        with open(path, "rb") as fh:
            return sum(1 for _ in fh)
    except FileNotFoundError:
        return 0
    except OSError:
        return 0


class Store:
    def __init__(self, root: str | Path, *, max_event_lines: int = 10_000,
                 max_outbox_lines: int = 5_000):
        self.root = Path(root)
        self.max_event_lines = max_event_lines
        self.max_outbox_lines = max_outbox_lines
        self.index_path = self.root / "index.json"
        self.events_path = self.root / "events.jsonl"
        self.outbox_path = self.root / "outbox.jsonl"
        self.cursor_path = self.root / "cursor.json"
        self.counters_path = self.root / "counters.json"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lines = {
            self.events_path: _count_lines(self.events_path),
            self.outbox_path: _count_lines(self.outbox_path),
        }

    # ------------------------------------------------------------------ index
    def load_index(self) -> dict:
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"version": 1, "meta": {}, "issues": {}, "assignments": {}}
        if not isinstance(data, dict):
            return {"version": 1, "meta": {}, "issues": {}, "assignments": {}}
        data.setdefault("version", 1)
        data.setdefault("meta", {})
        data.setdefault("issues", {})
        data.setdefault("assignments", {})
        return data

    def save_index(self, data: dict) -> None:
        atomic_write_text(self.index_path,
                          json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n")

    # ----------------------------------------------------------------- events
    def _rotate(self, path: Path) -> None:
        second = path.with_name(path.name + ".2")
        first = path.with_name(path.name + ".1")
        if second.exists():
            second.unlink()
        if first.exists():
            os.replace(first, second)
        if path.exists():
            os.replace(path, first)
        self._lines[path] = 0

    def log_event(self, event: dict) -> None:
        self._append_bounded(self.events_path, event, self.max_event_lines)

    def log_outbox(self, record: dict) -> None:
        self._append_bounded(self.outbox_path, record, self.max_outbox_lines)

    def _append_bounded(self, path: Path, record: dict, limit: int) -> None:
        if self._lines.get(path, 0) >= limit:
            self._rotate(path)
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        self._lines[path] = self._lines.get(path, 0) + 1

    # ----------------------------------------------------------------- cursor
    def load_cursor(self) -> dict:
        try:
            data = json.loads(self.cursor_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"version": 1, "issues": {}}
        if not isinstance(data, dict):
            return {"version": 1, "issues": {}}
        data.setdefault("version", 1)
        data.setdefault("issues", {})
        return data

    def save_cursor(self, data: dict) -> None:
        atomic_write_text(self.cursor_path,
                          json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n")

    # --------------------------------------------------------------- counters
    def load_counters(self) -> dict:
        try:
            data = json.loads(self.counters_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"external_actions": 0}
        if not isinstance(data, dict):
            return {"external_actions": 0}
        data.setdefault("external_actions", 0)
        return data

    def save_counters(self, data: dict) -> None:
        atomic_write_text(self.counters_path,
                          json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n")

    def bump_external_actions(self, n: int = 1) -> int:
        data = self.load_counters()
        data["external_actions"] = int(data.get("external_actions") or 0) + n
        data["last_external_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.save_counters(data)
        return data["external_actions"]
