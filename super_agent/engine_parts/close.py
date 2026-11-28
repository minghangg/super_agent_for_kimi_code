"""The close decision: four conditions, two consecutive rounds, one record.

Why this module exists (ISS-130 §3.1.1 cleanup): the whole "may I call this
thread finished" question lived inside ``engine.py``, where it was the main
reason the file kept growing - every close fix added a condition next to the
last one. It is one responsibility and it is testable on plain dicts, so it
belongs in its own module.

The rules it encodes, in the order they decide:

* a closed thread is proved by **four** conditions, each a live read (an archive
  read-back is asked, never remembered);
* ``completed`` needs those four in **two consecutive rounds**, and a round is a
  ``run_count``, so a loop inside one round can never confirm itself;
* a refusal keeps the record ``pending`` with the refusing id, so the next round
  looks again - a ``completed`` record would drop the thread out of the scan and
  nobody would ever look again;
* the hold cycle start is the start of the **current** cycle, not of the
  thread's history, or a fresh hold is dated by the previous one.

Nothing here touches the network or the store: the caller hands in the readers
it owns, exactly like :mod:`super_agent.engine_parts.pause`.
"""
from __future__ import annotations


def conditions(recs, *, issue_state, binding_roles, archived_of,
               running_of) -> dict:
    """``{"conditions": ..., "detail": ...}`` for one thread's assignments."""
    unarchived, running, skipped = [], [], []
    for rec in recs:
        sid = str(rec.get("sid") or "")
        if not sid:
            continue
        if not archived_of(sid):
            unarchived.append(sid)
            continue
        tasks_running, why = running_of(sid)
        if why:
            skipped.append(f"{sid}:{why}")
        running.extend(tasks_running)
    return {
        "conditions": {
            "issue_closed": issue_state == "closed",
            "binding_cleared": not binding_roles,
            "sid_archived": not unarchived,
            # Archived sids cannot be judged; the archive read-back is what
            # carries concurrency, so an unjudgeable table is not a "false".
            "no_running_task": not running,
        },
        "detail": {"issue_state": issue_state,
                   "binding_roles": binding_roles,
                   "unarchived_sids": sorted(unarchived),
                   "running_tasks": sorted(running),
                   "tasks_skipped": "; ".join(skipped)},
    }


def blocked_reason(snapshot: dict) -> str:
    """The one sentence a person reads, naming the first failing condition."""
    cond, detail = snapshot["conditions"], snapshot["detail"]
    if not cond["issue_closed"]:
        return f"issue_state={detail['issue_state']}"
    if not cond["binding_cleared"]:
        return f"binding still registered: {detail['binding_roles']}"
    if not cond["sid_archived"]:
        return f"unarchived sids: {detail['unarchived_sids']}"
    if not cond["no_running_task"]:
        return f"running tasks: {detail['running_tasks']}"
    return ""


def cycle_start(rec: dict, now: str) -> str:
    """The start of the **current** hold cycle, not of the thread's history.

    The ``close_reconcile`` record outlives a completed thread (nothing deletes
    it), so ``first_checked_at`` carried forward would date the second release
    to the first release's start - a "held for 313 days" reminder on the very
    first round of a fresh cycle, which is worse than silence. A cycle begins
    whenever the previous round's status was not ``pending``; from then on the
    start is kept, so a run of pending rounds cannot keep pushing its own
    deadline out.
    """
    previous = str(rec.get("status") or "")
    if previous and previous != "pending":
        return now
    return str(rec.get("first_checked_at") or "") or now


def streak_after(previous_ok: int, *, ok_now: bool, last_round, round_no) -> int:
    """How many consecutive good rounds this observation makes it.

    ``round_no`` is ``run_count``: the same round called twice must not add
    twice, or a single loop could satisfy a two-round requirement.
    """
    if not ok_now:
        return 0
    if previous_ok == 0 or last_round != round_no:
        return previous_ok + 1
    return 1


def record_for(rec: dict, snap: dict, *, round_no: int, now: str) -> dict:
    """The ``close_reconcile`` record this observation writes."""
    previous_ok = int(rec.get("consecutive_ok") or 0)
    ok_now = all(snap["conditions"].values())
    return {
        "status": "pending", "attempts": int(rec.get("attempts") or 0) + 1,
        "consecutive_ok": streak_after(previous_ok, ok_now=ok_now,
                                      last_round=rec.get("last_checked_round"),
                                      round_no=round_no),
        "first_checked_at": cycle_start(rec, now),
        "checked_at": now, "last_checked_round": round_no,
        "conditions": snap["conditions"], "detail": snap["detail"],
        "blocked_reason": blocked_reason(snap),
    }


def held_threads(index: dict, states=("waiting_release", "close_reconcile")) -> list[str]:
    """Threads with at least one assignment in a held state."""
    found = {str(rec.get("thread") or "")
             for rec in (index.get("assignments") or {}).values()
             if str(rec.get("status") or "") in states}
    return sorted(found - {""})


def reconcile(index: dict, thread: str, *, conditions, enter_reconcile,
              complete, clear_refused, log, now: str) -> dict:
    """One observation of one held thread.

    ``completed`` needs the **same** four conditions in two consecutive rounds,
    and the second round must be a later ``run_round`` - the counter is read from
    the index and only ever advanced by a call from a new round, so a loop inside
    one round cannot confirm itself. Any failed observation resets the streak to
    zero and moves a release into ``close_reconcile``.

    The record says ``completed`` only when the gate let every held assignment
    through; a refusal keeps it ``pending`` with the refusing id in
    ``blocked_reason``, so the next round checks the thread again.
    """
    records = index.setdefault("close_reconcile", {})
    if not isinstance(records, dict):
        records = index["close_reconcile"] = {}
    snap = conditions()
    rec = records.get(thread) if isinstance(records.get(thread), dict) else {}
    round_no = int((index.get("meta") or {}).get("run_count") or 0)
    record = record_for(rec, snap, round_no=round_no, now=now)
    streak = record["consecutive_ok"]
    records[thread] = record
    if not all(snap["conditions"].values()):
        for assignment in (index.get("assignments") or {}).values():
            if str(assignment.get("thread") or "") != thread:
                continue
            if str(assignment.get("status") or "") != "waiting_release":
                continue
            moved = enter_reconcile(assignment, record["blocked_reason"])
            log({"type": ("assignment.reopened" if moved.get("changed")
                          else "close_reconcile.refused"),
                 "at": now, "thread": thread,
                 "assignment": assignment.get("id"),
                 "reason": moved.get("reason") or ""})
        log({"type": "close_reconcile.blocked", "at": now, "thread": thread,
             "reason": record["blocked_reason"], "streak": streak})
        return {"thread": thread, "status": "pending", "completed": [],
                "reason": record["blocked_reason"],
                "conditions": snap["conditions"], "consecutive_ok": streak}
    if streak < 2:
        log({"type": "close_reconcile.passed", "at": now, "thread": thread,
             "streak": streak, "conditions": snap["conditions"]})
        return {"thread": thread, "status": "pending", "completed": [],
                "reason": "waiting for a second round",
                "conditions": snap["conditions"], "consecutive_ok": streak}
    completed, refused = [], []
    for assignment in (index.get("assignments") or {}).values():
        if str(assignment.get("thread") or "") != thread:
            continue
        if str(assignment.get("status") or "") not in ("waiting_release",
                                                       "close_reconcile"):
            continue
        moved = complete(assignment)
        if not moved.get("changed"):
            refused.append(str(assignment.get("id") or ""))
            log({"type": "assignment.qa_gate_refused", "at": now,
                 "thread": thread, "assignment": assignment.get("id"),
                 "reason": moved.get("reason") or "",
                 "level": "info", "consumers": ["doctor"]})
            continue
        completed.append(assignment["id"])
        # A late event after this finds a terminal assignment, so it is audited
        # by on_delivery/late_event and never restarts work.
        log({"type": "assignment.completed", "at": now, "thread": thread,
             "assignment": assignment.get("id"),
             "reason": moved.get("reason") or ""})
    # The thread record may only claim ``completed`` when every held assignment
    # actually moved: a gate refusal leaves the assignments held, and a
    # ``completed`` record would drop the thread out of the scan - no later
    # round would ever look again.
    if completed and not refused:
        record["status"] = "completed"
        records[thread] = record
        cleared = clear_refused(index, thread)
        log({"type": "thread.completed", "at": now, "thread": thread,
             "assignments": completed, "conditions": snap["conditions"],
             "refused_cleared": cleared})
        return {"thread": thread, "status": "completed",
                "completed": completed, "reason": "",
                "conditions": snap["conditions"], "consecutive_ok": streak}
    record["status"] = "pending"
    record["blocked_reason"] = (
        "qa_gate_refused: " + ", ".join(sorted(refused)) if refused
        else "no held assignment to complete")
    records[thread] = record
    return {"thread": thread, "status": "pending", "completed": completed,
            "reason": record["blocked_reason"],
            "conditions": snap["conditions"], "consecutive_ok": streak}


def binding_roles(ledger) -> list[str]:
    """Roles still registered for one thread in ``issue_sessions.json``.

    A role whose entry is not a non-empty dict is not registered: a retired
    binding leaves its key behind, and counting that as "still bound" would make
    a closed thread look live forever.
    """
    if not isinstance(ledger, dict):
        return []
    return sorted(str(role) for role, entry in ledger.items()
                  if isinstance(entry, dict) and entry)


def running_tasks(rows) -> tuple[list, str]:
    """``(running ids, skip reason)`` from one session's task table.

    An archived session answers ``[]`` from ``tasks()``, which is neither "idle"
    nor evidence. The concurrency question is carried by the un-cached archive
    read-back instead, so an unreadable table is reported as skipped rather than
    contributing a false "no running task".
    """
    running = [str(t.get("id") or "") for t in (rows or [])
               if str(t.get("status") or "") == "running"]
    return running, ""
