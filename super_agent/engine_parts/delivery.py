"""Which delivery does a comment judge? Five levels, strongest first.

Why this module exists (ISS-130 §3.1.1 cleanup): the answer decides which
delivery a QA pass or a PM verdict credits, so a guess here credits the wrong
work - or closes the wrong issue. The rule is a fixed ladder over plain records
(the index), which is exactly the shape that belongs in its own module.

The ladder, strongest first:

1. the ``delivery:`` coordinate in the verdict (exact);
2. the cid **is** a worker assignment's delivery;
3. the cid is that assignment's review (current or last);
4. time: the newest delivery not later than the cid (old format only);
5. nothing links it - the comment is its own identity.

**Level 4 is a fallback for the old format, nothing else.** A verdict written
with ``delivery:`` never reaches it: the coordinate is exactly what the time
guess was standing in for, and a guess after an exact statement is how the wrong
issue gets closed. Levels 2 and 3 answer for a QA pass, which names no delivery
but is linked through its assignment.
"""
from __future__ import annotations


def delivery_for(index: dict, thread: str, cid, declared=None,
                 missed=None) -> tuple[int, bool]:
    """``(delivery_id, unlinked)`` for one comment.

    ``unlinked`` is True when the ladder fell through to level 5. The caller
    records that once per ``(thread, cid)`` - the miss is a warning to look at,
    not one line per round - which is why the flag is returned rather than
    logged here.
    """
    if declared is not None:
        return int(declared), False
    try:
        wanted = int(cid)
    except (TypeError, ValueError):
        wanted = -1
    best = None
    eligible = []
    for rec in (index.get("assignments") or {}).values():
        if not isinstance(rec, dict):
            continue
        if str(rec.get("thread") or "") != thread:
            continue
        if str(rec.get("role") or "") != "worker":
            continue
        delivery = rec.get("delivery_comment_id")
        try:
            delivery = int(delivery)
        except (TypeError, ValueError):
            continue
        # The comment is either the delivery itself, or a verdict on it.
        if wanted == delivery:
            return delivery, False
        related = [rec.get("review_comment_id"),
                   rec.get("last_review_comment_id")]
        related_ok = any(str(value) == str(wanted) for value in related
                         if value is not None)
        if related_ok and (best is None or delivery > best):
            best = delivery
        # A PM verdict names no delivery, so it is attributed by time: the
        # release it is about is the newest delivery that already exists when
        # the comment was written. A delivery **after** the comment cannot be
        # what it judged, which keeps the rework case (delivery 9300 judged by a
        # comment at 9200) on the first key.
        if delivery <= wanted:
            eligible.append(delivery)
    if best is not None:
        return best, False
    if eligible:
        return max(eligible), False
    if missed is not None:
        missed.add(f"{thread}|{wanted}")
    return wanted, True


def verify_declared(index: dict, thread: str, wanted: int, states,
                    *, owners_of, newest_owner, output_cids) -> dict:
    """Why a ``delivery:`` coordinate cannot be credited, or ``{"ok": True}``.

    The three readers are :mod:`super_agent.assignments`' public functions - the
    coordinate contract (worker delivery first, a QA-only review as the stage
    output) is defined there, and a second implementation here would disagree
    with it the first time that rule changed.

    Dirty data is checked **before** the target is located: if the target were
    found first, two live owners of one cid would let the newest one pass the
    ownership check and ambiguity would be unreachable.

    The refusals have three shapes and the wording must say which one happened -
    "archived" printed for a live assignment is a contradiction a reader cannot
    resolve, so a live owner wins the branch even when an archived one sorts
    first.
    """
    owners = owners_of(index, thread, int(wanted))
    live = [rec for rec in owners if str(rec.get("status") or "") in states]
    if len(live) > 1:
        return {"ok": False, "kind": "ambiguous",
                "detail": (f"delivery #{int(wanted)} belongs to {len(live)} live "
                           "assignments: "
                           + ", ".join(str(r.get("id")) for r in live)),
                "owners": [str(r.get("id")) for r in owners]}
    owner = newest_owner(index, thread, states)
    if owner is None or int(wanted) not in output_cids(owner):
        if not owners:
            detail = f"delivery #{int(wanted)} is not a delivery of this thread"
        elif live:
            detail = (f"delivery #{int(wanted)} belongs to another open "
                      f"assignment {live[0].get('id')} "
                      f"({live[0].get('status')}), which is not this thread's "
                      "newest delivery")
        else:
            detail = (f"delivery #{int(wanted)} belongs to an archived "
                      f"assignment {owners[0].get('id')} "
                      f"({owners[0].get('status')})")
        return {"ok": False, "kind": "unknown", "detail": detail,
                "owners": [str(r.get("id")) for r in owners]}
    return {"ok": True, "owner": owner}


def sup_eligible(review: dict, record: dict | None) -> bool:
    """Does this verdict want the supervisor told?

    ``fix_needed`` is the parser's answer and is recomputed on a fresh parse;
    ``sup_eligible`` on the stored record is the **decision snapshot**, and it is
    the only thing a retry may trust. The retry re-enters with a stub review that
    carries the verdict and nothing else - re-deriving from that stub would answer
    "not eligible" for a release the first round already decided to announce, so
    the notice would be lost by a re-check rather than by a decision.

    Neither answer available means "do not send": guessing here would either
    withhold a release or send a false one.
    """
    if "fix_needed" in (review or {}):
        return (str(review.get("verdict") or "") == "pass"
                and review.get("fix_needed") is False)
    if isinstance(record, dict) and "sup_eligible" in record:
        return bool(record["sup_eligible"])
    return False


def dispatch_refusal(action: dict, *, dispatch_enabled: bool, mode: str,
                     sid_archived) -> dict | None:
    """The three gates ``_dispatch`` applies **before** any side effect.

    Returns ``{"refused": bool, "reason": str}`` plus, for an archived target,
    the sid the caller must log once - or ``None`` when the action may proceed.

    The order is the contract and each gate exists for its own reason:

    1. **a verdict never goes to the worker it judges** - that covers the engine's
       own routing path; a process talking to the session API directly is outside
       this gate;
    2. **dispatch disabled** - observe mode performs no external action;
    3. **the target session is archived** - a refusal here is not an external
       action and leaves no attempt behind, and it is logged **once** (archive is
       reversible, so it is not a permanent refusal: marking the record would kill
       a route a ``restore`` makes deliverable again).

    The allowlist is checked by the caller, because its verdict carries the
    refusal wording that policy owns.
    """
    origin = str(action.get("origin_role") or "")
    target = str(action.get("target_role") or "")
    if origin == "qa" and target == "worker":
        return {"refused": True, "reason": "qa verdict routed to a worker",
                "gate": "qa_to_worker"}
    if not dispatch_enabled:
        return {"refused": False, "reason": "dispatch disabled",
                "gate": "disabled", "mode": mode}
    sid = str((action.get("payload") or {}).get("sid") or "")
    if sid and sid_archived(sid):
        return {"refused": True, "reason": "target session is archived",
                "gate": "archived", "sid": sid}
    return None
