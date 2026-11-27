"""The file-backed source: deterministic, offline, no writes.

Split out of ``source.py`` under the QA charter's rule for over-limit files
(``rules/charters/qa.md`` §3.6). It is a complete backend on its own - it reads
one JSON file and answers the same six methods - so the seam is a backend
boundary, not a slice taken to hit a number. ``github_source.FixtureSource`` and
``github.FixtureSource`` still resolve here through the package re-exports.
"""
from __future__ import annotations

import json
from pathlib import Path

from .. import flow as flow_mod
from .. import markers
from ..paged_comments import PagedComments
from .errors import SourceError


def _read_json(path: Path):
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SourceError(f"cannot read fixture {path}: {exc}") from None
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise SourceError(f"fixture {path} is not valid JSON: {exc}") from None


class FixtureSource:
    """Local JSON backend. Deterministic, no network, no writes."""

    def __init__(self, path: str | Path,
                 on_hold_labels: list[str] | tuple[str, ...] | None = None):
        self.path = Path(path)
        self.data = _read_json(self.path)
        if not isinstance(self.data, dict):
            raise SourceError("fixture root must be an object")
        self.on_hold_labels = tuple(
            str(x) for x in (on_hold_labels or ()) if str(x or "").strip())

    def issues(self, limit: int = 20) -> list[int]:
        """Same contract as ``LiveSource.issues``: held rows are skipped.

        A fixture row may carry ``labels`` as a list of ``{"name": …}`` dicts
        (the REST shape) or of plain strings; both are accepted so a fixture
        written by hand stays readable.
        """
        issues = self.data.get("issues") or {}
        held = set(self.on_hold_labels)
        out = []
        for key in sorted(issues, key=lambda k: int(k) if str(k).isdigit() else 0):
            try:
                number = int(key)
            except (TypeError, ValueError):
                continue
            if held:
                rec = issues.get(key) or {}
                names = set()
                for item in (rec.get("labels") or []):
                    if isinstance(item, dict):
                        names.add(str(item.get("name") or ""))
                    else:
                        names.add(str(item or ""))
                if names & held:
                    continue
            out.append(number)
            if len(out) >= int(limit):
                break
        return out

    def comments(self, issue: int) -> list[dict]:
        rec = (self.data.get("issues") or {}).get(str(issue)) or {}
        rows = rec.get("comments") or []
        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                cid = int(row.get("id"))
            except (TypeError, ValueError):
                continue
            out.append({
                "id": cid,
                "issue": int(issue),
                "body": str(row.get("body") or ""),
                "author": str(row.get("author") or ""),
                "created_at": str(row.get("created_at") or ""),
                "session_key": markers.read_session_token(
                    str(row.get("body") or "")),
            })
        return sorted(out, key=lambda c: c["id"])

    def paged(self, issue: int, *, since_id: int = 0, max_pages: int = 10) -> list[dict]:
        """One file is the whole listing, so this walk *is* complete."""
        rows = self.comments(issue)
        return PagedComments(rows, complete=True,
                             pages=[{"page": 1, "rows": len(rows), "status": "ok"}])

    def title(self, issue: int) -> str:
        rec = (self.data.get("issues") or {}).get(str(issue)) or {}
        return str(rec.get("title") or "")

    def issue_state(self, issue: int) -> str:
        """``state`` from the fixture row; ``"open"`` when the row omits it.

        Read-only like everything else here. An issue missing from the fixture
        is ``"unknown"`` rather than ``"open"``: the close reconciliation must
        not read absence as proof of anything.
        """
        rec = (self.data.get("issues") or {}).get(str(issue))
        if not isinstance(rec, dict):
            return "unknown"
        return str(rec.get("state") or "open")

    def flow(self, issue: int) -> str:
        rec = (self.data.get("issues") or {}).get(str(issue)) or {}
        return flow_mod.from_labels(rec.get("labels") or [])
