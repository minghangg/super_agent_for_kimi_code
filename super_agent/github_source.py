"""Issue comment source. M0 is read-only: this module only ever issues GETs.

Two backends:

* ``fixture`` — reads a local JSON file, used by tests and by anyone who wants
  to exercise the engine without network access. Shape::

      {"issues": {"42": {"title": "...", "comments": [
          {"id": 9001, "body": "...", "author": "someone", "created_at": "..."}]}}}

* ``live`` — ``GET /repos/{owner}/{name}/issues`` plus ``.../comments`` against
  the public API. A token is read from ``GITHUB_TOKEN`` when present; it is
  never logged and never written anywhere.

New comments are selected by an integer id cursor that the caller persists, so
a second poll of the same issue yields nothing. No method here can mutate the
remote repository — there is deliberately no POST/PATCH/PUT/DELETE path.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.github.com"
PER_PAGE = 100


class SourceError(RuntimeError):
    pass


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

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data = _read_json(self.path)
        if not isinstance(self.data, dict):
            raise SourceError("fixture root must be an object")

    def issues(self, limit: int = 20) -> list[int]:
        issues = self.data.get("issues") or {}
        out = []
        for key in issues:
            try:
                out.append(int(key))
            except (TypeError, ValueError):
                continue
        return sorted(out)[:limit]

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
            })
        return sorted(out, key=lambda c: c["id"])

    def paged(self, issue: int, *, since_id: int = 0, max_pages: int = 10) -> list[dict]:
        """Fixture backend of the paged reader: all rows, one synthetic page.

        The fixture is small and local, so "paging" is a no-op that still has to
        honour the same contract: rows are ordered by id and ``since_id`` is a
        hint, not a filter (the caller applies the cursor).
        """
        return self.comments(issue)

    def title(self, issue: int) -> str:
        rec = (self.data.get("issues") or {}).get(str(issue)) or {}
        return str(rec.get("title") or "")


class LiveSource:
    """Public GitHub REST, GET-only."""

    def __init__(self, repo: str, *, token: str | None = None, timeout_s: float = 15.0):
        if not repo or "/" not in repo:
            raise SourceError("github.repo must be 'owner/name' for source=live")
        self.repo = repo
        self.token = token if token is not None else (os.environ.get("GITHUB_TOKEN") or "")
        self.timeout_s = timeout_s

    def _get(self, path: str, query: dict | None = None):
        url = API + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        req = urllib.request.Request(url, method="GET")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "super-agent-engine")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except OSError:
                pass
            raise SourceError(f"GitHub {exc.code} on {path}: {detail}") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise SourceError(f"{type(exc).__name__}: {exc}") from None

    def issues(self, limit: int = 20) -> list[int]:
        rows = self._get(f"/repos/{self.repo}/issues",
                         {"state": "open", "per_page": min(int(limit), 100)})
        if not isinstance(rows, list):
            raise SourceError("unexpected issues payload")
        out = []
        for row in rows:
            if isinstance(row, dict) and row.get("number") and not row.get("pull_request"):
                out.append(int(row["number"]))
        return out[:limit]

    def comments(self, issue: int) -> list[dict]:
        rows = self._get(f"/repos/{self.repo}/issues/{int(issue)}/comments",
                         {"per_page": 100})
        if not isinstance(rows, list):
            raise SourceError("unexpected comments payload")
        out = []
        for row in rows:
            if not isinstance(row, dict) or row.get("id") is None:
                continue
            out.append({
                "id": int(row["id"]),
                "issue": int(issue),
                "body": str(row.get("body") or ""),
                "author": str((row.get("user") or {}).get("login") or ""),
                "created_at": str(row.get("created_at") or ""),
            })
        return sorted(out, key=lambda c: c["id"])

    def paged(self, issue: int, *, since_id: int = 0,
              max_pages: int = 10) -> list[dict]:
        """Comments from the oldest page forward, stopping at ``since_id``.

        Pages are requested in ascending order (``per_page=100&page=n``) and the
        walk stops as soon as a page contains an id ``<= since_id``: everything
        after that page is newer than the cursor, so there is nothing left to
        collect. Any non-200 / network / JSON failure raises :class:`SourceError`
        **instead of returning what was gathered so far** - a partial list would
        be silently mistaken for "no new comments" and the cursor would advance
        past comments that were never read.
        """
        since_id = int(since_id or 0)
        max_pages = max(1, int(max_pages or 1))
        collected: list[dict] = []
        for page in range(1, max_pages + 1):
            rows = self._get(f"/repos/{self.repo}/issues/{int(issue)}/comments",
                             {"per_page": PER_PAGE, "page": page,
                              "sort": "created", "direction": "asc"})
            if not isinstance(rows, list):
                raise SourceError("unexpected comments payload")
            if not rows:
                break
            page_rows = self._normalise(rows, issue)
            collected.extend(page_rows)
            if since_id and page_rows and min(c["id"] for c in page_rows) <= since_id:
                break
        return sorted(collected, key=lambda c: c["id"])

    @staticmethod
    def _normalise(rows: list, issue: int) -> list[dict]:
        out = []
        for row in rows:
            if not isinstance(row, dict) or row.get("id") is None:
                continue
            out.append({
                "id": int(row["id"]),
                "issue": int(issue),
                "body": str(row.get("body") or ""),
                "author": str((row.get("user") or {}).get("login") or ""),
                "created_at": str(row.get("created_at") or ""),
            })
        return sorted(out, key=lambda c: c["id"])

    def title(self, issue: int) -> str:
        row = self._get(f"/repos/{self.repo}/issues/{int(issue)}")
        return str(row.get("title") or "") if isinstance(row, dict) else ""


class NullSource:
    """``github.source=none``: no issues, no network."""

    def issues(self, limit: int = 20) -> list[int]:
        return []

    def comments(self, issue: int) -> list[dict]:
        return []

    def paged(self, issue: int, *, since_id: int = 0,
              max_pages: int = 10) -> list[dict]:
        return []

    def title(self, issue: int) -> str:
        return ""


class BrokenSource:
    """Stands in when a source cannot be constructed (missing file, bad repo).

    Keeping construction lazy means ``doctor`` can report the problem as a
    failed check instead of dying with a traceback, and ``status`` (which only
    reads the local index) still works with no source at all.
    """

    def __init__(self, error: str):
        self.error = error

    def issues(self, limit: int = 20) -> list[int]:
        raise SourceError(self.error)

    def comments(self, issue: int) -> list[dict]:
        raise SourceError(self.error)

    def paged(self, issue: int, *, since_id: int = 0,
              max_pages: int = 10) -> list[dict]:
        raise SourceError(self.error)

    def title(self, issue: int) -> str:
        raise SourceError(self.error)


def comments_from(source, issue: int, *, since_id: int = 0,
                  max_pages: int = 10) -> list[dict]:
    """``source.paged()`` when the backend has it, else the whole list.

    Keeping this behind one helper means the engine never needs to know which
    backend it was given, and a test double that only implements ``comments()``
    keeps working unchanged.
    """
    paged = getattr(source, "paged", None)
    if callable(paged):
        return paged(issue, since_id=since_id, max_pages=max_pages)
    rows = source.comments(issue)
    return [c for c in rows if int(c["id"]) >= int(since_id or 0)]


def build(cfg: dict):
    gh = cfg.get("github") or {}
    source = gh.get("source") or "none"
    if source == "none":
        return NullSource()
    try:
        if source == "fixture":
            return FixtureSource(gh.get("fixture") or "")
        if source == "live":
            return LiveSource(gh.get("repo") or "")
    except SourceError as exc:
        return BrokenSource(str(exc))
    raise SourceError(f"unknown github.source {source!r}")


def new_comments(source, issue: int, cursor: int) -> list[dict]:
    """Comments with ``id > cursor``; the caller persists the new max."""
    return [c for c in source.comments(issue) if int(c["id"]) > int(cursor)]
