"""The read pause: the record that stops a read, and the way a person lifts it.

Reaching a pause means one read could not prove it saw everything it needed -
``max_comment_pages`` ran out before the walk reached the cursor, or the AT
requirement history did not end at the last page. What lies beyond is then
*unknown*, not empty, and the two ways to get that wrong are both damaging:
consuming a slice would skip the rows in between, and reading on every round
while only writing ``needs_person`` is the silent retry #127 names as a
failure. So the pause is a real stop, and ``cli resume-read`` is the way back.

The queue surface (``state``, the ``needs_person`` row, the side table
``resolve`` uses) is kept consistent here so one command clears all of it.
Nothing here holds engine state: the caller hands in the side effects it owns
(``append``/``mark``/``log``), which is what lets the engine, the CLI and a test
double run the same code.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .. import issuekey, needs

#: Reason recorded when one walk could not prove the unread prefix is gap-free.
READ_PAGINATION_INCOMPLETE = "read.pagination_incomplete"

#: Reason recorded when the AT requirement history could not be read whole.
REQUIREMENT_HISTORY_INCOMPLETE = "requirement.history_incomplete"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------- pause
def is_paused(record: dict | None) -> bool:
    """Is this issue entry's read pause live?

    The ``reason`` is the flag: a lifted pause leaves ``{}`` rather than a
    half-alive record, so there is exactly one thing to read.
    """
    paused = (record or {}).get("read_paused")
    return isinstance(paused, dict) and bool(paused.get("reason"))


def pause(index: dict, key: str, *, reason: str, detail: str, summary: dict,
          cursor: int = 0, rows: int = 0, max_pages: int = 0,
          resume_with: str = "", issue: int | None = None,
          append=None, log=None) -> None:
    """Mark the issue as paused and stop touching it until a person says so.

    Reaching this point means one read could not prove it saw everything it
    needed: either ``max_comment_pages`` ran out before the walk reached the
    cursor, or the AT history did not end at the last page. What lies beyond is
    then *unknown*, not empty, and the two ways to get that wrong are both
    damaging - consuming a slice would skip the rows in between, and reading on
    every round while only writing ``needs_person`` is the silent retry the
    issue names as a failure.

    So this is a real stop, with an explicit way back: the record lives on the
    issue entry as ``read_paused`` and only ``cli resume-read`` clears it (see
    :func:`lift`). The reason is written once, never re-appended, so a stuck
    issue stays one row in ``status`` instead of one per round.

    ``append`` is the engine's deduplicating ``needs_person`` writer and ``log``
    its event writer; both are optional so the function stays usable in tests
    with plain dicts.
    """
    rec = index.setdefault("issues", {}).setdefault(
        key, {"state": "idle", "comments_seen": 0, "needs_person": []})
    if not is_paused(rec):
        rec["read_paused"] = {"reason": str(reason), "at": _now(),
                              "cursor": int(cursor), "detail": str(detail)}
    rec["state"] = "needs_person"
    row = {"comment_id": 0, "reason": str(reason), "at": _now(),
           "detail": str(detail)}
    added = append(index, key, row) if append else _plain_append(rec, row)
    if added:
        summary["needs_person"] = int(summary.get("needs_person") or 0) + 1
    summary["read_paused"] = int(summary.get("read_paused") or 0) + 1
    if log:
        log({"type": "read.paused", "at": _now(),
             "issue": int(issue) if issue is not None else None,
             "thread": str(key), "reason": str(reason),
             "detail": str(detail), "cursor": int(cursor),
             "rows_read": int(rows), "max_pages": int(max_pages),
             "resume_with": resume_with})


def _plain_append(rec: dict, row: dict) -> bool:
    """Minimal ``(thread, cid, reason)`` dedupe, for callers without an engine."""
    rows = rec.setdefault("needs_person", [])
    reason = str(row.get("reason") or "")
    for old in rows:
        if (old.get("comment_id") == row.get("comment_id")
                and str(old.get("reason") or "") == reason):
            old["at"] = row.get("at") or _now()
            if row.get("detail") is not None:
                old["detail"] = row["detail"]
            return False
    rows.append(row)
    return True


def pause_read(index: dict, key: str, issue: int, cursor: int, rows: int,
               max_pages: int, summary: dict, *, append=None, log=None) -> None:
    """``pause`` for a page walk that ran out of budget above the cursor."""
    detail = (f"cursor={int(cursor)} rows_read={int(rows)} "
              f"max_pages={int(max_pages)}: the page walk ended above the "
              f"cursor, so the unread prefix may have a hole")
    pause(index, key, reason=READ_PAGINATION_INCOMPLETE, detail=detail,
          summary=summary, cursor=cursor, rows=rows, max_pages=max_pages,
          issue=issue, append=append, log=log,
          resume_with=f"python -m super_agent.cli resume-read --issue {int(issue)}")


def pause_history(index: dict, key: str, issue: int, rows: int,
                  summary: dict, *, reason: str = "", append=None,
                  log=None) -> None:
    """``pause`` for an AT requirement history that did not read whole.

    The projection is **not** written on this path (the caller skips it): a
    prefix would drop requirement packages that live on pages never fetched,
    and a dropped package looks exactly like a package nobody published.
    """
    detail = (f"rows_read={int(rows)}: the requirement history walk did not "
              f"reach the last page, so packages may be missing"
              + (f" ({reason})" if reason else ""))
    pause(index, key, reason=REQUIREMENT_HISTORY_INCOMPLETE, detail=detail,
          summary=summary, rows=rows, issue=issue, append=append, log=log,
          resume_with=f"python -m super_agent.cli resume-read --issue {int(issue)}")


def lift(index: dict, keys: list[str], *, by: str = "",
         max_pages: int | None = None, mark=None) -> dict:
    """Clear the pause on every key naming one issue.

    One command, one state: the pause record, the ``state`` and the
    ``read.pagination_incomplete`` row are cleared together, so an orchestrator
    stops seeing "waiting for a person" the moment a person actually answered.
    Splitting that into ``resume-read`` plus ``resolve --comment 0`` was two
    commands saying the same thing.

    ``max_pages`` is the page budget for the **next** read of this issue and is
    persisted with the lift; the round then uses it for that issue (never for
    others, never as a new default). Without it, lifting a pause on a listing
    larger than the configured window would re-pause on the very next round and
    the command would be lying about its effect. With the budget in hand the
    probe also looks deeper, which is what makes the lift work on listings that
    are a whole number of pages (ISS-128 L2).

    Returns ``{"changed": bool, "reason": str}``; ``mark`` is ``needs.mark``.
    """
    out = {"changed": False, "reason": ""}
    for key in keys:
        rec = (index.get("issues") or {}).get(key) or {}
        if not is_paused(rec):
            continue
        was = str((rec.get("read_paused") or {}).get("reason") or "")
        # A pending budget is kept as ``read_budget`` rather than by leaving
        # ``read_paused`` half-alive: the short-circuit reads ``reason``, so an
        # empty record is unambiguous.
        rec["read_paused"] = {}
        if max_pages:
            rec["read_budget"] = {"max_pages": int(max_pages), "at": _now(),
                                  "by": str(by or "")}
        out["changed"] = True
        out["reason"] = out["reason"] or was
        clear_surface(index, key, by=by, mark=mark)
    return out


def clear_surface(index: dict, key: str, *, by: str = "", mark=None) -> None:
    """Put the issue back in the queue state the pause took it out of.

    ``state`` goes back to ``idle`` (the value a tracked issue holds before any
    comment is consumed; the next round re-derives it) and the pause rows are
    dismissed through the same side table ``resolve`` uses, so ``status`` stops
    listing them without a second command.
    """
    rec = (index.get("issues") or {}).get(key) or {}
    rows = rec.get("needs_person") or []
    for reason in (READ_PAGINATION_INCOMPLETE, REQUIREMENT_HISTORY_INCOMPLETE):
        if not any(str(r.get("reason") or "") == reason for r in rows):
            continue
        if mark is not None:
            mark(index, key, 0, state=needs.RESOLVED, by=by or "resume-read",
                 reason=reason, at=_now())
    if str(rec.get("state") or "") == "needs_person":
        rec["state"] = "idle"


def read_budget(record: dict | None, configured: int) -> tuple[int, bool]:
    """``(budget, pending)`` for this issue's next read.

    A lifted pause may carry the page budget the operator chose when clearing
    it (``resume-read --max-pages``). That budget is what makes the lift a real
    "continue" instead of "pause again on the next round"; it applies to this
    issue only and is spent once, so ``pending`` tells the caller to clear it.
    """
    pending = (record or {}).get("read_budget")
    wanted = 0
    if isinstance(pending, dict):
        try:
            wanted = int(pending.get("max_pages") or 0)
        except (TypeError, ValueError):
            wanted = 0
    if wanted <= 0:
        return int(configured), False
    return max(int(configured), wanted), True


def issue_number(value) -> int | None:
    """``#12``/``12``/``owner/name#12`` → 12; anything else → None."""
    return issuekey.issue_number(value)
