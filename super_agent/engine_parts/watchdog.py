"""The three silence alarms: PM stall, AT stall, release timeout.

Why this module exists (ISS-130 §3.1.1 cleanup): three watchdogs that each ask
"has a role gone quiet on a held thread?" lived in ``engine.py`` and shared one
shape - a per-round counter, a per-hold sequence, one notice per hold. That shape
is the responsibility, so it lives here once.

The rules, in the order they matter:

* **counting is by round**, not by wall clock: a round is what the engine acts
  in, and the counter lives in the index so a restart resumes it;
* **each hold is its own notice** - ``hold_seq`` moves only when the thread
  leaves the held states and comes back, so a second stuck release is reported
  instead of being swallowed by the first one's attempt counter;
* **a closed issue is not stalled**: the hold reached its end state and only the
  archive is outstanding, so a growing counter there is a false alarm (#78);
* **PM and AT have separate ledgers** (``pm_stall`` / ``at_stall``): two silences
  over two questions, and sharing a counter would let one role's answer erase
  the other's alarm.

Nothing here dispatches or writes events: the callbacks are handed in, so the
same code runs under the engine, a test double and (for the PM/AT notices) a
reader that only wants the decision.
"""
from __future__ import annotations

from datetime import datetime


def bump_hold_seq(index: dict, table: str, thread: str) -> None:
    """A hold ended: the next one gets a new notice, not the old counter."""
    seqs = index.setdefault(table, {})
    if isinstance(seqs, dict) and thread in seqs:
        seqs[thread] = int(seqs[thread] or 0) + 1


def hold_seq(index: dict, table: str, thread: str) -> int:
    """The current hold's sequence number (first hold is 1)."""
    seqs = index.setdefault(table, {})
    if not isinstance(seqs, dict):
        seqs = index[table] = {}
    seq = int(seqs.get(thread) or 1)
    seqs[thread] = seq
    return seq


def table(index: dict, name: str) -> dict:
    """``index[name]`` as a dict, replacing a wrong-typed value in place."""
    value = index.setdefault(name, {})
    if not isinstance(value, dict):
        value = index[name] = {}
    return value


def round_count(entry: dict | None, thread: str, now: str) -> dict:
    """One more round on this hold's counter."""
    entry = dict(entry) if isinstance(entry, dict) else {
        "thread": thread, "rounds": 0, "fired": False, "since": now}
    entry["rounds"] = int(entry.get("rounds") or 0) + 1
    return entry


def due(entry: dict, limit: int) -> bool:
    """Is this hold due its one notice?"""
    return not entry.get("fired") and int(entry["rounds"]) >= int(limit)


def remember(result: dict, entry: dict, seq: int) -> dict:
    """The record written after a notice, whether or not it was delivered."""
    entry["fired"] = True
    entry["hold_seq"] = seq
    entry["result"] = str((result or {}).get("reason") or "dispatched")
    return entry


def release_start(index: dict, thread: str, assignment: dict) -> str:
    """The stable start time of a held thread, or ``""`` when there is none.

    ``first_checked_at`` is written once by the reconcile pass and kept;
    ``updated_at`` is rewritten by ``enter_close_reconcile`` every round and
    would keep pushing the deadline out forever. The fallbacks are the archive
    record, and nothing else is guessed.
    """
    rec = (index.get("close_reconcile") or {}).get(thread)
    if isinstance(rec, dict) and str(rec.get("first_checked_at") or ""):
        return str(rec["first_checked_at"])
    tombstone = assignment.get("tombstone")
    if isinstance(tombstone, dict) and str(tombstone.get("at") or ""):
        return str(tombstone["at"])
    return str(assignment.get("archived_at") or "")


def overdue(start: str, timeout_s: float, *, now: datetime | None = None,
            parse=None) -> tuple[bool, float, str]:
    """``(is_overdue, seconds, problem)`` for one hold start.

    A start that cannot be parsed is a **fault**, not "not overdue": silently
    treating it as fresh would hide a hold forever, which is the alarm this
    function exists for.
    """
    parsed = (parse or _parse)(start)
    if parsed is None:
        return False, 0.0, "release timeout start time is unusable"
    moment = now or datetime.now(parsed.tzinfo)
    seconds = (moment - parsed).total_seconds()
    return seconds >= float(timeout_s), seconds, ""


def _parse(value: str):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def pm_held(index: dict, *, issue_state) -> dict:
    """``{thread: (assignment_id, record)}`` for holds the PM watchdog watches.

    Only a held release whose QA already passed is a PM question - before that
    the PM has nothing to answer. **A closed issue is not stalled**: the hold
    reached its end state and only the archive is outstanding, so a counter that
    kept growing there would be a false alarm (``#78`` held 878 rounds while
    already closed).
    """
    held = {}
    for aid, rec in (index.get("assignments") or {}).items():
        if not isinstance(rec, dict):
            continue
        if str(rec.get("status") or "") not in ("waiting_release",
                                                "close_reconcile"):
            continue
        thread = str(rec.get("thread") or "")
        if not thread or str(rec.get("qa_verdict") or "") != "pass":
            continue
        issue = rec.get("issue")
        if issue is not None and issue_state(issue) == "closed":
            continue
        held[thread] = (aid, rec)
    return held


def at_registered(issue_sessions: dict, thread_key, *, needs_answer) -> dict:
    """``{thread_key: at_entry}`` for AT bindings still waiting for an answer.

    The scan reads whatever spelling ``gh_issue.bind`` wrote (``<project>#<n>``)
    and normalises it through ``thread_key``, so the watchdog's counter and the
    verdict reader speak **one** key space. Without this the binding is
    registered under a key nothing ever asks about, and the watchdog watches a
    thread that does not exist.
    """
    import re

    registered: dict = {}
    for raw_key, entry in (issue_sessions or {}).items():
        if not isinstance(entry, dict) or not isinstance(entry.get("at"), dict):
            continue
        match = re.search(r"#(\d+)$", str(raw_key))
        if not match:
            continue
        key = thread_key(int(match.group(1)))
        registered[str(key)] = entry["at"]
    for thread in list(registered):
        if not needs_answer(thread, registered[thread]):
            registered.pop(thread, None)
    return registered


def pm_spoke(assignments, rows) -> bool:
    """Did the PM speak after the delivery it judges?

    The watchdog asks "has the PM gone quiet", **not** "has the PM filed a
    signed verdict". Those were one question until an unsigned verdict stopped
    being a verdict: such a comment is dropped before the verdict ledger is
    written, so the ledger stays empty and the watchdog counts rounds for a PM
    that answered.

    So ``needs_person`` rows carrying a ``pm.*`` reason count as speech, and the
    row must be **newer than the delivery** - that is what keeps an old row from
    silencing a new hold. ``pm.stall`` is excluded by name, not by luck: this
    watchdog writes it, and reading it back would let the alarm silence itself.

    A ``pm.escalation`` row counts too: asking for a person is the PM speaking,
    and it is the louder kind. The catch, stated so the next reader is not
    surprised: one escalation row keeps a hold quiet for as long as it is newer
    than the delivery, so a PM that escalates once and then stops talking is not
    reported stalled for that hold. Leaving the hold clears the counter, so a new
    hold starts from zero and needs a fresh row to go quiet again.
    """
    delivery = 0
    for rec in assignments:
        try:
            delivery = max(delivery, int(rec.get("delivery_comment_id") or 0))
        except (TypeError, ValueError):
            continue
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        reason = str(row.get("reason") or "")
        if not reason.startswith("pm.") or reason.startswith("pm.stall"):
            continue
        try:
            cid = int(row.get("comment_id") or 0)
        except (TypeError, ValueError):
            continue
        if cid > delivery:
            return True
    return False


def at_needs_answer(*, issue, thread, at_entry: dict, flow_of, standard: str,
                    live, issue_state, at_sid, current, verdict,
                    question_answered, split_key) -> bool:
    """Is this AT registration still waiting for an answer?

    Six refusals, each of which would otherwise turn the watchdog into noise:

    * the Issue is on the **standard** branch - no AT question was ever asked;
    * the Issue is not in this round's live set - a binding for a closed or held
      Issue is not a silence;
    * the Issue is **closed** - the end state, not a stuck one;
    * no AT session resolves - a watchdog with nobody to watch fires on
      everything;
    * the requirement already reached ``ready``/``approved``/``dispatched``/
      ``completed`` - the question was answered by the package;
    * a verdict exists (an answered ``questions`` verdict ends it).

    What is left is "a question is outstanding and nobody answered", which is
    exactly what the counter measures.
    """
    if issue is None:
        _repo, issue = split_key(thread)
    if issue is not None:
        selected = flow_of(issue)
        if selected == standard:
            return False
    if issue is not None and live and int(issue) not in live:
        return False
    if issue is not None and issue_state(issue) == "closed":
        return False
    if not at_sid(thread):
        return False
    if current and str(current.get("status")) in (
            "ready", "approved", "dispatched", "completed"):
        return False
    if verdict:
        if str(verdict.get("state") or "") == "questions":
            return bool(question_answered(thread, verdict.get("comment_id")))
        return False
    if current and current.get("open_questions"):
        return bool(question_answered(thread, current.get("source_comment_id")))
    return bool(at_entry.get("awaiting") or current)
