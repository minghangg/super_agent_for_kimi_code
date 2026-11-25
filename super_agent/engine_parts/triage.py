"""Which kind of comment is this? Fixed rules, one order, no state.

Why this module exists (ISS-130 §3.1.1 cleanup): triage is the busiest decision in
the engine - every comment goes through it - and it is a pure classification over
the body plus a couple of markers. Keeping it in ``engine.py`` meant every new
marker kind added a branch to a 5,000-line file; here the whole order is visible
at once.

The order is the contract, and each step exists because the step before it would
otherwise answer wrongly:

1. a delivery marker wins over everything - a worker whose role is configured
   would otherwise have its own delivery ignored as role-authored noise;
2. a ``[QA]`` review is the second marker: its verdict is a transition, not
   discussion, and it must be read before role noise too;
3. a ``[PM:TASK]`` line is a dispatch by its own declaration, so it is settled
   before the verdict branch is ever asked - it is always ``ignored``, with one
   ``warn`` when its own first line reads like a conclusion, because a reply that
   answers a task book is the reply somebody forgot to send as a verdict. A task
   book that says ``[blocked]``/``[decision]`` is asking for a person and goes
   down the PM frame path - a prefix change must not turn a bell into silence;
4. a ``[PM]`` verdict is read before role noise for the same reason: the PM's own
   role is ``pm``. A ``[PM]`` line that is **not** a conclusion is discussion and
   is ignored here - reading it as an unreadable verdict is what filed 57 false
   exceptions. Escalation markers are still honoured on that path;
5. role-authored noise is ignored;
6. escalation markers ask for a person;
7. everything else is logged (the historical behaviour).
"""
from __future__ import annotations

import re

from .. import markers

# A PM task book, dispatch or receipt says so on its first line: ``[PM:TASK]``.
# It is the same bracket and the same author as a verdict, and ``PM_PREFIX_RE``
# deliberately does **not** match it - there the character after ``PM`` is a
# colon, not a closing bracket. That is the whole point: the prefix states "this
# is a dispatch, not a state", so the engine never has to guess from the wording
# (#ISS-85). The spelling is matched generously - case, half- and full-width
# brackets, and both colons - because it is written by hand.
TASK_PREFIX_RE = re.compile(
    r"^\s*[\[［]\s*(?:PM|supervisor)\s*[:：]\s*TASK\s*[\]］]\s*", re.I)


def triage(comment: dict, *, role: str = "", escalation=None,
           log_task_conclusion=None) -> tuple[str, str]:
    """``(state, reason)`` for one comment. Pure apart from the two callbacks.

    ``escalation(body, first_line_only=...)`` is the engine's marker reader; it
    is passed in because the marker list is configuration, not a fact of this
    module.
    """
    def escalate(text: str, first_line_only: bool) -> str:
        if escalation is None:
            return ""
        return str(escalation(text, first_line_only=first_line_only) or "")

    body = str(comment.get("body") or "")
    delivery = markers.parse_delivery(body)
    if delivery:
        return "delivery", f"marker {delivery['name']!r}"
    review = markers.parse_review(body)
    if review:
        return "review", f"qa verdict {review['verdict'] or 'unreadable'!r}"
    line = markers.first_line(body)
    task_book = TASK_PREFIX_RE.match(line)
    if task_book:
        # The prefix declares it: a task book, a dispatch, a receipt. It is never
        # a verdict, whatever the rest of the line says, so it is settled here -
        # before ``parse_pm_verdict`` is asked - and it never reaches the
        # "unreadable verdict" branch.
        #
        # The guard below is the other half. A task book that *answers* -
        # ``[PM:TASK] 可关单 #42`` - is a verdict written with the wrong prefix,
        # and silence would lose it. The answer is still silence here (a task
        # book must not change state), but it is **logged**: one ``warn``, no
        # frame, no row. Logging it as an exception would be the noise #75
        # removed, under a new name.
        #
        # Only the prefix line's remainder is read, plus the next non-empty line
        # when the prefix stands alone. The slice starts from ``line`` and not
        # from ``body``: the regex matched the stripped first line, so
        # ``body[end:]`` is off by the leading whitespace it no longer saw
        # (#ISS-85 P2-3).
        lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
        rest = line[task_book.end():].strip()
        if not rest and len(lines) > 1:
            # A real task book writes the prefix alone and the title below it.
            # Reading only the prefix line would make the guard blind to exactly
            # the shape it exists for.
            rest = lines[1]
        if markers.is_pm_conclusion(rest) and log_task_conclusion is not None:
            log_task_conclusion(comment)
        escalated = escalate(body, True)
        if escalated:
            return "pm_verdict", f"escalation marker {escalated!r}"
        return "ignored", "pm task book"
    pm_verdict = markers.parse_pm_verdict(body)
    if pm_verdict and pm_verdict.get("candidate"):
        return "pm_verdict", f"pm verdict {pm_verdict['state'] or 'unreadable'!r}"
    if pm_verdict:
        # A ``[PM]`` line that is not a conclusion: a dispatch, a task book, a
        # note. It used to fall through with ``state == ""``, which the router
        # reads as "the PM answered and the answer was unreadable" - 57 task
        # books and 1251 frames later (#75). The answer is not "unreadable", it
        # is "not an answer".
        #
        # The escalation markers are checked **first**. A note is quiet by
        # default, but a note that says ``[blocked]``/``[decision]`` asks for a
        # person, and returning ``ignored`` here would answer that request with
        # silence - the round-2 P0: a human-intervention signal downgraded to
        # nothing visible at all.
        escalated = escalate(body, True)
        if escalated:
            return "pm_verdict", f"escalation marker {escalated!r}"
        return "ignored", "pm comment is not a verdict"
    at_verdict = markers.parse_at_verdict(body)
    if at_verdict:
        return "at_verdict", f"at verdict {at_verdict['state'] or 'unreadable'!r}"
    author = str(comment.get("author") or "").strip().lower()
    if role and author == role.lower():
        return "ignored", "authored by configured role"
    escalated = escalate(body, False)
    if escalated:
        return "needs_person", f"marker {escalated!r}"
    return "logged", ""
