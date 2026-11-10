"""Delivery and review markers, read from the **first non-empty line** only.

``gh_issue.py comment`` prefixes every worker comment with
``[WK:<worker-name>] `` and every QA comment with ``[QA] ``. Those prefixes are
how the engine recognises a delivery and a review, so parsing has to be precise
in two ways:

* **only the first non-empty line is a marker.** A comment that *quotes* a
  delivery (``see [WK:foo] above``) is discussion, not a delivery; scanning the
  whole body would turn every quotation into a second delivery.
* the name is the worker's session key, so it is compared verbatim (after
  stripping). A marker that does not match a live worker falls back to "the
  only live worker on this thread" in the caller, never to a guess.

Legacy behaviour is unchanged: a comment without a marker is handed back to the
keyword triage exactly as before.

**The QA verdict is not a substring test.** ``"通过" in "不通过"`` is true, so a
table ordered "check PASS first" turns every rejection into a pass - which is
exactly what real corpora do (7 of 10 sampled rejections were flipped that way).
:func:`parse_review` therefore compares **positions**, not table order: the
earliest hit wins, and at the same position the longest word wins.
"""
from __future__ import annotations

import re
import unicodedata

# ``[WK:name]`` with a name of 1..64 non-] characters, case-insensitive.
WK_RE = re.compile(r"^\s*\[WK:([^\]\n]{1,64})\]", re.I)

# A delivery body may quote the marker later; that is not a marker. Only the
# first line is examined, so nothing else is needed here.
MARKER = "WK"

# ``[QA]`` in either bracket width. ``gh_issue.py`` writes the ASCII form;
# hand-written comments use the full-width one, and real corpora contain both
# doubled (``[QA] [QA] ...``) because two tools prefix the same comment.
QA = "QA"
QA_PREFIX_RE = re.compile(r"^\s*[\[［]\s*QA\s*[\]］]\s*", re.I)

# A verdict is only read inside this clause when the line has one: a body that
# starts with the conclusion and then quotes the judging rules must not have
# the quote judged instead.
CLAUSE_RE = re.compile(r"(?:结论|判定)\s*[:：]")

# Unlike a substring table, order here carries no precedence: the verdict is
# decided by position (see :func:`_first_hit`).
PASS_WORDS = ("有条件通过", "通过", "pass")
REJECT_WORDS = ("不通过", "未通过", "退回", "reject")
WORD_TABLE = tuple([(w, "pass") for w in PASS_WORDS]
                   + [(w, "reject") for w in REJECT_WORDS])

# A hit followed by one of these is not a verdict: ``通过项先说`` and
# ``参与者`` are ordinary prose, not conclusions.
TAILS = ("项", "者")

# ---------------------------------------------------------------- must-fix bit
#
# Wording convention for a QA first line: a **requirement** is written
# ``必须修`` (or ``仍需修``/``需修``); work that may wait is written
# ``可随下轮``; finished work is written ``已修``/``已修完``. Only the pure
# requirement words below take part: state descriptions such as ``待修`` and
# ``未修复`` are deliberately absent, because ``P2 待修`` does not mean "this
# round must fix it" and would otherwise withhold the supervisor notification
# for a perfectly shippable pass.
MUST_FIX_WORDS = ("必须修", "还需要修", "仍需修", "仍须修", "需要修", "需修", "须修")

# Negation spans. ``无需`` must be tried before any bare character: it covers
# the ``需`` that starts ``需修``, and marking only the ``无`` would leave the
# candidate looking clean.
NEGATION_WORDS = ("无需", "无须", "不需", "不必", "不用", "没有", "尚无", "暂无",
                  "不存在", "未", "没", "已修", "已补", "已改", "免修", "勿", "非")

# A candidate glued to one of these characters on its left (``非必须修``) is
# negated even though no negation word spans it.
SINGLE_NEG = "无不免勿非"

# ``没有必须修的项``: a modifier plus an absence is a statement that nothing is
# required, not a requirement.
ABSENT_WORDS = ("没有", "暂无", "不存在", "零")
MODIFIER_TAIL = "的"

# ``必须修的 P1 已在本轮修完`` / ``必须修项为0``: the requirement was met (or
# counts zero), so it must not withhold the notification.
DONE_WORDS = ("已修", "修完", "已改", "已补", "已完成", "已处理")
ZERO_WORDS = ("为0", "为 0", "=0", "= 0")


def first_line(body: str) -> str:
    """The first non-empty line of ``body`` (whitespace-trimmed)."""
    for line in str(body or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def parse_delivery(body: str) -> dict | None:
    """``{"marker", "name", "line"}`` when the body opens with a delivery marker."""
    line = first_line(body)
    if not line:
        return None
    match = WK_RE.match(line)
    if not match:
        return None
    name = match.group(1).strip()
    if not name:
        return None
    return {"marker": MARKER, "name": name, "line": line}


def has_marker(body: str) -> bool:
    return parse_delivery(body) is not None


def _strip_qa_prefix(line: str) -> str:
    """Drop every leading ``[QA]``/``［QA］`` and normalise the rest to NFKC.

    NFKC makes the full-width brackets, colons and dashes of hand-written
    comments comparable with the ASCII ones ``gh_issue.py`` writes, so one word
    table covers both.
    """
    previous = None
    while previous != line:
        previous = line
        line = QA_PREFIX_RE.sub("", line, count=1).strip()
        line = unicodedata.normalize("NFKC", line)
    return line


def _first_hit(text: str) -> tuple[str, str] | None:
    """``(verdict, word)`` for the earliest word, longest at that position.

    Positions are compared, never table order: ``不通过`` starts one character
    before the ``通过`` it contains, so it wins on position alone. ``有条件通过``
    and ``通过`` can start at the same index only if one contains the other, and
    there the longer word wins. A hit immediately followed by a tail
    (``通过项``/``通过者``) is skipped and the next occurrence is tried.
    """
    lower = text.lower()
    best: tuple[int, int, str, str] | None = None
    for word, verdict in WORD_TABLE:
        needle = word.lower()
        start = lower.find(needle)
        while start != -1:
            tail = lower[start + len(needle): start + len(needle) + 1]
            if tail and tail in TAILS:
                start = lower.find(needle, start + 1)
                continue
            candidate = (start, -len(needle), word, verdict)
            if best is None or candidate[:2] < best[:2]:
                best = candidate
            break
    if best is None:
        return None
    return best[3], best[2]


def _spans(text: str, words) -> list[tuple[int, int]]:
    """Every occurrence of every word, longest word first, as ``(start, end)``."""
    spans = []
    for word in sorted(words, key=len, reverse=True):
        start = text.find(word)
        while start != -1:
            spans.append((start, start + len(word)))
            start = text.find(word, start + 1)
    return spans


def _intersects(span: tuple[int, int], spans) -> bool:
    start, end = span
    return any(start < other_end and other_start < end
               for other_start, other_end in spans)


def _must_fix(text: str) -> str:
    """The requirement word that makes this review ask for more work, or ``""``.

    A substring test is as wrong here as it is for the verdict: ``无需修改``
    contains ``需修``, so the naive reader turns the most common pass into a
    demand for work and the supervisor never hears about the release. The
    guards below are the answer, and they must stay together:

    1. **long word first, and a rejected hit propagates.** Negation words are
       scanned longest first (``无需`` before any bare character) and every hit
       interval is remembered; a candidate whose interval intersects one is
       void. A rejected *requirement* hit is remembered too, so the shorter word
       inside it (``须修`` inside ``必须修``) is not read as a second, clean hit.
    2. **a glued single-character negation** - ``非必须修`` - is void even though
       no negation word spans the candidate; consecutive characters are eaten.
    3. **interval intersection** is the form rule 1 uses, spelled out because it
       is the only one that stops ``无需修改``: the ``需`` is shared.
    4. **the pick** is ``(position, -length)``, the same comparison
       :func:`_first_hit` uses - one rule, not two.
    5. **a modifier plus an absence** - ``没有必须修的项`` - states that nothing is
       required.
    6. **a completed or zero-count requirement** - ``必须修项为0``,
       ``必须修的 P1 已在本轮修完`` - is not pending work.

    ``fix_matched`` is for the event log only; callers decide on
    ``fix_needed``.
    """
    text = unicodedata.normalize("NFKC", str(text or ""))
    if not text:
        return ""
    rejected = _spans(text, NEGATION_WORDS)
    best: tuple[int, int, str] | None = None
    for word in sorted(MUST_FIX_WORDS, key=len, reverse=True):
        start = text.find(word)
        while start != -1:
            end = start + len(word)
            if _spans_negate(text, start, end, rejected):
                rejected.append((start, end))
            elif best is None or (start, -len(word)) < best[:2]:
                best = (start, -len(word), word)
            start = text.find(word, start + 1)
    return "" if best is None else best[2]


def _spans_negate(text: str, start: int, end: int, rejected) -> bool:
    """Is the requirement at ``[start, end)`` voided by a guard?"""
    if _intersects((start, end), rejected):
        return True
    left, glued = start - 1, False
    while left >= 0 and text[left] in SINGLE_NEG:
        glued = True
        left -= 1
    if glued:
        return True
    if text[end:end + 1] == MODIFIER_TAIL and any(
            absent in text[max(0, start - 6):start] for absent in ABSENT_WORDS):
        return True
    tail = text[end:end + 12]
    return any(done in tail for done in DONE_WORDS) or any(
        zero in tail for zero in ZERO_WORDS)


def parse_review(body: str) -> dict | None:
    """``{"marker", "verdict", "matched", "line"}`` for a ``[QA]`` first line.

    ``verdict`` is ``"pass"``, ``"reject"`` or ``""`` (no readable verdict, so a
    person decides - the parser never guesses). ``None`` means "not a QA
    comment at all", which is a different answer from "a QA comment whose
    verdict is unreadable".

    ``fix_needed``/``fix_matched`` are **appended** to that shape and never
    change how ``verdict`` is decided: they answer "does this review still ask
    for work", which is what decides whether the release goes to the supervisor.
    """
    line = first_line(body)
    if not line or not QA_PREFIX_RE.match(line):
        return None
    text = _strip_qa_prefix(line)
    fix_matched = _must_fix(text)
    verdict, matched = "", ""
    clause = CLAUSE_RE.search(text)
    if clause:
        hit = _first_hit(text[clause.end():])
        if hit:
            verdict, matched = hit
    if not verdict:
        # No clause, or a clause with nothing readable in it: judge the line.
        hit = _first_hit(text)
        if hit:
            verdict, matched = hit
    return {"marker": QA, "verdict": verdict, "matched": matched, "line": line,
            "fix_needed": bool(fix_matched), "fix_matched": fix_matched}
