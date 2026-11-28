"""Which ``needs_person`` rows a confirmed-closed Issue turns into resolved rows.

Split out of ``engine.py`` under ``rules/charters/worker.md`` §3.1.1. This is a
pure decision over an index snapshot: it names the rows that should be marked
resolved and says why each was skipped. The caller applies it, because applying
it moves the side table and writes events - host state.

Three rules the decision has to keep, each learned from a real failure:

* **A foreign repo is skipped, once.** ``#7`` in another repository is not our
  ``#7``. The first sighting writes one ``needs_person.resolution_skipped`` row;
  repeating it every round turned the log into noise.
* **A live Issue is never resolved.** ``live`` is the set of issue numbers this
  round actually read - asking the source again would spend a probe per row.
* **Only a closed Issue resolves.** ``unknown`` is not closed; an unreadable
  probe must not clear a person's queue.
"""
from __future__ import annotations

from .. import issuekey, needs


def pending_rows(index: dict, *, current_repo: str, limit: int) -> list[tuple]:
    """``[(thread, issue, rows)]`` for Issues that still hold open rows."""
    pending = []
    for thread, rec in (index.get("issues") or {}).items():
        repo, issue = issuekey.split_key(thread)
        repo = repo or current_repo
        if repo != current_repo:
            continue
        if issue is None:
            continue
        rows = [row for row in (rec.get("needs_person") or [])
                if needs.is_open(index, thread, row)]
        if rows:
            pending.append((thread, issue, rows))
    return sorted(pending)[:limit]


def foreign_threads(index: dict, *, current_repo: str) -> list[tuple]:
    """``[(thread, repo)]`` that belong to another repository, ours excluded.

    Returned rather than logged so the caller can keep its own
    "already reported once" set - that set is per-process state.
    """
    out = []
    for thread in (index.get("issues") or {}):
        repo, issue = issuekey.split_key(thread)
        repo = repo or current_repo
        if issue is None:
            continue
        if repo != current_repo:
            out.append((thread, repo))
    return sorted(out)


def resolve(index: dict, rows_plan: list[tuple], *, live: set,
            issue_state, log_row) -> int:
    """Mark the qualifying rows resolved; return how many actually changed.

    ``issue_state(issue)`` is the caller's cached probe (an unreadable answer
    must read as ``unknown``, never as ``closed``). ``log_row(event)`` is how
    each change is recorded - this function never touches the store.
    """
    resolved = 0
    for thread, issue, rows in rows_plan:
        if issue in live:
            continue
        if issue_state(issue) != "closed":
            continue
        for row in rows:
            cid = needs.cid_of(row)
            if cid is None:
                continue
            entries = sum(needs.cid_of(other) == cid for other in rows)
            marked = needs.mark(index, thread, cid, state=needs.RESOLVED,
                                by="engine", reason="issue closed",
                                entries=entries)
            if marked.get("changed"):
                resolved += 1
                log_row({"type": "needs_person.closed_resolved",
                         "thread": thread, "comment_id": cid,
                         "entries": entries})
    return resolved
