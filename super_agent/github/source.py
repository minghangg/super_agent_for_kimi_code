"""GitHub source backends, moved from ``github_source.py`` in step 1 of #134.

No behavior changes here: the facade keeps every historical import working.
Every HTTP request goes through :mod:`super_agent.github.client`.
"""
from __future__ import annotations

import urllib.parse

from .. import flow as flow_mod
from .. import markers
from ..paged_comments import (PagedBatch, PagedComments, comments_from,
                             paged_batch, paged_result, walk_complete)
from ..paged_walk import link_last_page, locate_last_page, walk_listing
from . import client
from . import token as token_mod
from .cached import CachedLiveSource
from .errors import SourceError
from .fixture import FixtureSource  # noqa: F401  (re-exported, see __init__)

API = "https://api.github.com"
PER_PAGE = 100






class LiveSource:
    """Public GitHub REST, GET-only."""

    #: Rows per page; the "short page ends the listing" rule reads this.
    page_size = PER_PAGE

    def __init__(self, repo: str, *, token: str | None = None, timeout_s: float = 15.0,
                 on_hold_labels: list[str] | tuple[str, ...] | None = None):
        if not repo or "/" not in repo:
            raise SourceError("github.repo must be 'owner/name' for source=live")
        self.repo = repo
        if token is not None:
            self.token = token
        else:
            # 2026-11-25 (#134 regression): the env var alone is not enough.
            # The engine runs under a scheduled task with no GITHUB_TOKEN,
            # so every read became anonymous -- and GitHub answers that with
            # 404 for a private repo, which the engine read as "unreadable"
            # and turned into a permanent read.paused. The token chain lives
            # in github/token.py now, shared with issue_cli.
            self.token = token_mod.read_token_or_empty()
        self.timeout_s = timeout_s
        self.on_hold_labels = tuple(
            str(x) for x in (on_hold_labels or ()) if str(x or "").strip())
        self._labels_by_issue: dict[int, list] = {}

    def _get_with_headers(self, path: str, query: dict | None = None):
        """``(payload, headers)``: the same GET, keeping the response headers.

        The HTTP call itself lives in :mod:`super_agent.github.client`, the one
        exit every GitHub request uses. Headers are returned unchanged, so a
        304 revalidation still yields the cached ``Link``: that header is how
        the pagination walk learns the page count, and a live 304 carries none.
        """
        url = API + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        req_headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "super-agent-engine",
        }
        if self.token:
            req_headers["Authorization"] = f"Bearer {self.token}"
        try:
            resp = client.get(url, headers=req_headers, timeout_s=self.timeout_s)
        except client.HttpError as exc:
            # ``from None`` threw the cause away: the message said "TimeoutError"
            # while the frame that timed out - which host, which proxy - was
            # gone. The rule is "state the cause" (2026-11-28), so the chain
            # stays attached; the string is unchanged for existing readers.
            if exc.status:
                raise SourceError(
                    f"GitHub {exc.status} on {path}: {exc.detail}") from exc
            raise SourceError(f"{exc.kind}: {exc.detail}") from exc
        return resp.body, dict(resp.headers)

    def _get(self, path: str, query: dict | None = None):
        return self._get_with_headers(path, query)[0]

    def issues(self, limit: int = 20) -> list[int]:
        """Open issues that are not held, newest first, capped at ``limit``.

        The page is asked for at the API maximum (100), not at ``limit``: held
        issues are filtered out *after* the fetch, so asking for ``limit`` rows
        could drop a live issue that sat behind 20 held ones. One request
        either way.

        ``on_hold_labels`` is matched as a **set membership test** on the
        labels the list endpoint already returns - never by position, never by
        the ``stage:`` prefix. An empty set means "no filter" (fail open): a
        missed issue is a frame nobody sees, which is worse than an extra one.
        """
        return self.issues_with_labels(limit)[0]

    def issues_with_labels(self, limit: int = 20) -> tuple[list[int], dict]:
        """``(issue numbers, {number: labels})`` from **one** list request.

        The labels ride along because the caller that caches the listing must
        also cache them: ``flow()`` answers from ``_labels_by_issue``, and a
        cached listing without them would make every cached round fetch one
        detail request per issue (#134 P1-1). The dict holds the whole page,
        not just the returned prefix, so a later ``flow()`` for an issue the
        ``limit`` cut off is still answerable from the same read.
        """
        rows = self._get(f"/repos/{self.repo}/issues",
                         {"state": "open", "per_page": 100})
        if not isinstance(rows, list):
            raise SourceError("unexpected issues payload")
        held = set(self.on_hold_labels)
        out, labels = [], {}
        for row in rows:
            if not isinstance(row, dict) or not row.get("number"):
                continue
            if row.get("pull_request") is not None:
                continue
            number = int(row["number"])
            labels[number] = list(row.get("labels") or [])
            self._labels_by_issue[number] = labels[number]
            if held:
                names = {str((x or {}).get("name") or "")
                         for x in (row.get("labels") or []) if isinstance(x, dict)}
                if names & held:
                    continue
            out.append(number)
            if len(out) >= int(limit):
                break
        return out, labels

    def flow(self, issue: int) -> str:
        number = int(issue)
        if number not in self._labels_by_issue:
            row = self._get(f"/repos/{self.repo}/issues/{number}")
            if not isinstance(row, dict):
                raise SourceError("unexpected issue payload")
            self._labels_by_issue[number] = list(row.get("labels") or [])
        return flow_mod.from_labels(self._labels_by_issue[number])

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
                "session_key": markers.read_session_token(
                    str(row.get("body") or "")),
            })
        return sorted(out, key=lambda c: c["id"])

    def _comment_page(self, issue: int, page: int) -> tuple[int, list[dict], dict]:
        """``(raw row count, normalised rows, headers)``; the page number selects."""
        body, headers = self._get_with_headers(
            f"/repos/{self.repo}/issues/{int(issue)}/comments",
            {"per_page": self.page_size, "page": page})
        if not isinstance(body, list):
            raise SourceError("unexpected comments payload")
        return len(body), self._normalise(body, issue), headers

    def paged(self, issue: int, *, since_id: int = 0,
              max_pages: int = 10) -> list[dict]:
        """One walk; the inference lives in :func:`paged_walk.walk_listing`."""
        return walk_listing(
            lambda page: self._comment_page(issue, page),
            page_size=self.page_size, since_id=since_id, max_pages=max_pages)

    def _locate_last_page(self, issue: int, max_pages: int):
        """Forwarder: how many pages exist, probed as deep as the budget allows."""
        return locate_last_page(lambda page: self._comment_page(issue, page),
                                max_pages, self.page_size)

    @staticmethod
    def _link_last_page(headers: dict) -> int | None:
        """Forwarder: the ``page`` of ``rel="last"``, or None."""
        return link_last_page(headers)

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
                "session_key": markers.read_session_token(
                    str(row.get("body") or "")),
            })
        return sorted(out, key=lambda c: c["id"])

    def title(self, issue: int) -> str:
        row = self._get(f"/repos/{self.repo}/issues/{int(issue)}")
        return str(row.get("title") or "") if isinstance(row, dict) else ""

    def issue_state(self, issue: int) -> str:
        """``GET /issues/{n}`` and read ``state``. Still a GET and nothing else.

        This is the only way to learn that the supervisor closed the Issue: the
        list endpoint only ever returns open ones.
        """
        row = self._get(f"/repos/{self.repo}/issues/{int(issue)}")
        if not isinstance(row, dict):
            raise SourceError("unexpected issue payload")
        return str(row.get("state") or "unknown")


class NullSource:
    """``github.source=none``: no issues, no network."""

    def issues(self, limit: int = 20) -> list[int]:
        return []

    def comments(self, issue: int) -> list[dict]:
        return []

    def paged(self, issue: int, *, since_id: int = 0,
              max_pages: int = 10) -> list[dict]:
        """No backend, no listing - and an empty one is not a proof either.

        ``complete`` stays False: "there is no source" and "the walk reached the
        end of the source" are different facts, and the second one must never be
        inferred from the first.
        """
        return PagedComments((), complete=False, pages=[],
                             reason="no backend configured")

    def title(self, issue: int) -> str:
        return ""

    def flow(self, issue: int) -> str:
        return ""

    def issue_state(self, issue: int) -> str:
        """No backend, no opinion: ``"unknown"`` is never read as closed."""
        return "unknown"


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

    def issue_state(self, issue: int) -> str:
        raise SourceError(self.error)


def build(cfg: dict):
    gh = cfg.get("github") or {}
    source = gh.get("source") or "none"
    held = gh.get("on_hold_labels") or []
    # The HTTP client's policy is part of the same section, adopted here so the
    # single place that constructs a backend is also the single place that
    # configures how it talks to the network.
    client.configure(gh)
    if source == "none":
        return NullSource()
    try:
        if source == "fixture":
            return FixtureSource(gh.get("fixture") or "", on_hold_labels=held)
        if source == "live":
            live = LiveSource(gh.get("repo") or "", on_hold_labels=held)
            if client.policy()["etag_cache"]:
                return CachedLiveSource(
                    live, ttl_s=client.policy()["local_cache_ttl_s"])
            return live
    except SourceError as exc:
        return BrokenSource(str(exc))
    raise SourceError(f"unknown github.source {source!r}")


def new_comments(source, issue: int, cursor: int) -> list[dict]:
    """Comments with ``id > cursor``; the caller persists the new max."""
    return [c for c in source.comments(issue) if int(c["id"]) > int(cursor)]
