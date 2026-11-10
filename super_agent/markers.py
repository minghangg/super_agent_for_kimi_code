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

# ---------------------------------------------------------------- negation
#
# **One table for "negation".** Three guards read it - the requirement span
# (:func:`_spans_negate`), the "all of it is done" claim (:func:`_all_claim`)
# and the bare completion word (:func:`_done_completion`). Because they share
# the table, a character missing here is missing in all three: the previous
# revision kept two character tables (``SINGLE_NEG``/``ALL_NEGATIONS``) whose
# difference was ``无/免/勿/非``, and every false "ready to close" it produced
# came from that difference - not from one missing word.
NEGATION_CHARS = "未尚没不否无免勿非"

# Whole-word negations, tried before a bare character: ``无需`` must win over
# the ``需`` that starts ``需修``, and marking only the ``无`` would leave the
# candidate looking clean. Completion words are here too because "the P1 has
# already been fixed" is not a demand for work.
NEGATION_WORDS = ("无需", "无须", "不需", "不必", "不用", "没有", "尚无", "暂无",
                  "不存在", "未", "没", "已修", "已补", "已改", "免修", "勿", "非")

# Old names are aliases of the one table, never a second copy: a caller that
# still says ``SINGLE_NEG`` reads the same characters the other guards do.
SINGLE_NEG = NEGATION_CHARS
ALL_NEGATIONS = NEGATION_CHARS

# How far in front of a claim its negation is read. One character is not
# enough: ``并非全部修完`` hides ``非`` two characters back and ``未均已修完``
# hides ``未`` three back.
NEGATION_LOOKBACK = 3
ALL_LOOKBACK = NEGATION_LOOKBACK

# ``没有必须修的项``: a modifier plus an absence is a statement that nothing is
# required, not a requirement.
ABSENT_WORDS = ("没有", "暂无", "不存在", "零")
MODIFIER_TAIL = "的"

# ``必须修的 P1 已在本轮修完`` / ``必须修项为0``: the requirement was met (or
# counts zero), so it must not withhold the notification.
#
# Completion is a **count relation**, never a character distance. A real review
# says ``有条件通过（2 条必须修，已修 1 条）``: both words sit in the same line
# and the work is *not* done. Reading "a completion word appears nearby" as
# "nothing left" turns that into a false "ready to close", which is the one
# failure this whole bit exists to prevent - a missed notice has a
# ``needs_person`` row behind it, a false one has nothing.
DONE_WORDS = ("已修完", "已修", "修完", "已补", "已改", "已完成", "已处理")
# Any of these in the same neighbourhood withdraws the completion claim.
INCOMPLETE_WORDS = ("未修完", "尚未修完", "未全部修完", "未修", "尚未修",
                    "未修复", "尚未修复", "待修", "未完成", "未处理")
# The zero must belong to the requirement (``必须修项为0``), not to an unrelated
# counter that happens to sit on the same line (``P0=0 / P1=1 必须修``).
ZERO_TAIL_RE = re.compile(r"^(?:项|条)?\s*(?:为|=)\s*0")
# ``全部修复了吗`` asks rather than asserts; ``全部完成?`` likewise.
QUESTION_RE = re.compile(r"^\s*(?:了)?\s*[吗么嘛？?]")
# ``已全部修完 2 条``: the number the claim states about itself.
SCOPE_RE = re.compile(r"^\s*(\d+|[零一二两三四五六七八九十]+)\s*(?:条|处|项)")
# ``修完了一半``/``修复了部分``: the claim covers less than the whole.
PART_TAIL_RE = re.compile(r"^\s*了?\s*(?:一半|部分|大半|些许)")
# ``已全部修完，另 1 条另说``: an "all done" that names a remainder is not all.
REMAINDER_RE = re.compile(r"^\s*[，,；;]?\s*[另余]\s*\d+\s*(?:条|处|项)")
# How far around a requirement word its completion claim is read. A clause is
# short; a whole line is not, and a distant "P2 已修完" must not speak for it.
COMPLETION_WINDOW = 30

# An explicit "all of it" claim settles a *counted* requirement that states no
# fixed count (``必须修的 2 条已全部修完``). A one-character lookbehind is not
# enough: ``没有全部修复`` hides its negation one character further back, and
# ``尚未全部修复`` contains none of the words in :data:`INCOMPLETE_WORDS`
# (``未修复`` is not a substring of ``未全部修复``). So the guard reads the few
# characters in front of the claim - from :data:`NEGATION_CHARS`, the same table
# every other negation guard reads - instead of trusting a single neighbour.
ALL_WORDS = ("已全部修完", "全部修完", "已全部修复", "全部修复", "已全部完成",
             "全部完成", "均已修完", "均已修复", "已全部处理", "均已处理")

# A count is written with a digit or a small Chinese numeral (``两条``/
# ``已修一条``), and the measure word may be dropped (``已修 1``). ``处``/``项``
# are what a review writes for a non-line unit (``必须修改 2 处``), so the
# measure word is a set, not ``条`` alone - requiring ``条`` is what made the
# parser fall back to "a completion word is visible, so it is done".
_CN_NUMERALS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
                "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_NUM = r"(\d+|[零一二两三四五六七八九十]+)"
_QTY = r"(?:条|处|项)"
# ``必须修``'s count is one modifier, one bracket and one measure word away at
# most: ``必须修的 2 条``, ``必须修（2 条）``, ``必须修改 2 处``.
_FIX_WORDS_RE = "|".join(["已修复", "已修完", "已修", "已完成", "已处理",
                          "已补", "已改"])
FIXED_COUNT_RE = re.compile(
    rf"(?:{_FIX_WORDS_RE})\s*{_NUM}\s*{_QTY}?"
    rf"|{_NUM}\s*{_QTY}?\s*(?:{_FIX_WORDS_RE})")
_REQ_WORDS_RE = "|".join(re.escape(w) for w in
                         sorted(MUST_FIX_WORDS, key=len, reverse=True))
REQUIRED_COUNT_RE = re.compile(
    rf"(?:{_REQ_WORDS_RE})\s*(?:的|改)?\s*[（(]?\s*{_NUM}\s*{_QTY}?"
    rf"|{_NUM}\s*{_QTY}?\s*(?:{_REQ_WORDS_RE})")


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
    guards run in this order, and the order is the design:

    1. **whole-word negation first.** ``无需``/``无须``/``不必`` are matched as
       whole words from :data:`NEGATION_WORDS`; that alone settles ``无需修改``
       and ``无需修复``, and it is what the ordinary pass relies on. Every
       negation span is remembered, and a rejected *requirement* hit is
       remembered too, so the shorter word inside it (``须修`` inside
       ``必须修``) is not read as a second, clean hit.
    2. **a local overlap the whole-word table cannot cover.** ``非必须修`` has
       no negation word spanning it - the ``非`` is a single character glued to
       the front - so a glued character from :data:`NEGATION_CHARS` (and any
       interval that still intersects a negation span) voids the candidate.
       This guard covers *partial* overlaps; it is not what stops
       ``无需修改``, which the whole word already did.
    3. **the completion claim is a count relation, not a distance.**
       ``2 条必须修，已修 1 条`` states its own arithmetic; reading the nearby
       ``已修`` as "nothing left" is how a partial round turns into a false
       "ready to close". See :func:`_settled`.
    4. **the pick** is ``(position, -length)``, the same comparison
       :func:`_first_hit` uses - one rule, not two.

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
    return _settled(text, start, end)


def _settled(text: str, start: int, end: int) -> bool:
    """Is the requirement at ``[start, end)`` a *finished* one?

    Completion is decided by the **counts the sentence states about itself**,
    never by how close a completion word happens to sit. The order is:

    1. **a contradiction wins.** ``未修完``/``尚未修完``/``待修``/``未修`` near
       the requirement keeps it pending, whatever else the clause claims;
    2. **an explicit zero is settled** - ``必须修项为0`` needs no other proof;
    3. **the arithmetic decides.** ``2 条必须修，已修 1 条`` says exactly how much
       is left, so the two counts are read from **disjoint spans**: the required
       count is masked out before the fixed count is searched, because
       ``必须修2条已修1条`` would otherwise let the ``2`` be read as "two
       fixed". A partial count (``已修 N 条`` with no total) means "some were
       fixed", which is not "all were fixed";
    4. **a counted requirement with no fixed count is still pending** - unless
       the line says *all* of it is done (``已全部修完``). "The count is 2 and
       something was fixed" is not "the two are fixed", and a bare ``已修``
       next to an unknown number must not settle anything. The claim is read
       through the same negation table as every other guard, and it is still
       only a phrase: the number it states about itself is compared, so
       ``已全部修完 2 条`` against 3 required leaves one;
    5. **only with no counts at all** does an un-negated completion word speak
       for the requirement itself, which is why ``必须修的 P1 已在本轮修完`` is
       settled while ``未均已修完``/``没全部修完`` are not - the negation is a
       window in front of the word, not an entry in a phrase list.

    The default is "still needed". A false "ready to close" misleads the reader
    with no way to notice; a withheld one at least leaves a ``needs_person``
    row behind.
    """
    near = text[max(0, start - COMPLETION_WINDOW):end + COMPLETION_WINDOW]
    if any(word in near for word in INCOMPLETE_WORDS):
        return False
    if ZERO_TAIL_RE.match(text[end:]) or ZERO_TAIL_RE.match(near):
        return True
    required, span = _count_span(REQUIRED_COUNT_RE, near)
    claim = _all_claim(near)
    if required is not None:
        # The required count is consumed, so its digits cannot be read again as
        # "how many were fixed": ``必须修2条已修1条`` has one ``\d+条`` and it
        # belongs to the total, not to the fixed count.
        fixed = _first_count(FIXED_COUNT_RE, _blank(near, span))
        if fixed is not None:
            return fixed >= required
        # A count with no fixed count states a remainder, not a completion -
        # unless the line says *all* of it is done. An "all" phrase is only a
        # phrase, though, so the count it states about itself still runs: the
        # sentence saying "all done, 2 of them" with 3 required has one left.
        if claim is None:
            return False
        scope = _claim_scope(near, claim)
        return scope is None or scope >= required
    fixed = _first_count(FIXED_COUNT_RE, near)
    if fixed is not None:
        # Some were fixed and the total is not stated: not provably all.
        return False
    if claim is not None:
        scope = _claim_scope(near, claim)
        # ``全部修完了一半``/``部分`` covers less than the whole; with no total
        # stated there is nothing it could be compared against, and "some" is
        # not "all".
        return scope is None
    return _done_completion(near)


def _negated_at(text: str, position: int) -> bool:
    """Does a negation character stand in the window just before ``position``?

    ``ALL_LOOKBACK`` characters, not one: ``并非全部修完`` puts ``非`` three
    characters in front of the claim, and ``未均已修完`` does the same with
    ``未``. The table is :data:`NEGATION_CHARS`, so this guard, the glued
    requirement guard and the whole-word table cannot disagree about what a
    negation is.
    """
    before = text[max(0, position - NEGATION_LOOKBACK):position]
    return any(ch in NEGATION_CHARS for ch in before)


def _all_claim(text: str) -> tuple[int, int] | None:
    """The span of the earliest un-negated "all of it is done" claim, or ``None``.

    ``尚未全部修复`` and ``没有全部修复`` say the opposite of ``全部修复``, and
    neither contains a word from :data:`INCOMPLETE_WORDS`. The window check is
    what keeps this from becoming the old "a completion word is visible" test
    under a new name. A claim followed by a question is not a claim either:
    ``全部修复了吗`` asks, it does not assert.
    """
    best: tuple[int, int] | None = None
    for word in ALL_WORDS:
        start = text.find(word)
        while start != -1:
            end = start + len(word)
            if not _negated_at(text, start) and not _is_question(text, end):
                if best is None or start < best[0]:
                    best = (start, end)
            start = text.find(word, start + 1)
    return best


def _all_done(text: str) -> bool:
    """Is there an un-negated "all of it is done" claim in ``text``?"""
    return _all_claim(text) is not None


def _is_question(text: str, position: int) -> bool:
    """Is the claim ending at ``position`` immediately asked about?"""
    return bool(QUESTION_RE.match(text[position:]))


def _claim_scope(text: str, span: tuple[int, int]) -> int | None:
    """How much the claim in ``span`` says it covers itself, or ``None``.

    ``已全部修完 2 条`` states its own number; ``修完了一半``/``部分`` states
    less than the whole (``0`` is a value no requirement is satisfied by);
    ``已全部修完`` alone states nothing, so the count relation has no input and
    the phrase stands. This is the arithmetic the task's third fix asks for:
    a phrase must not outvote a count the sentence gives.
    """
    rest = text[span[1]:]
    if PART_TAIL_RE.match(rest) or REMAINDER_RE.match(rest):
        return 0
    match = SCOPE_RE.match(rest)
    if not match:
        return None
    return _to_int(match.group(1))


def _done_completion(text: str) -> bool:
    """Does an un-negated completion word stand in ``text``?

    The bare test this replaces (``any(done in text)``) read
    ``未均已修完``/``没全部修完`` as "nothing left", because neither phrase is
    a word in :data:`INCOMPLETE_WORDS`. Reading the same negation window in
    front of each hit covers the whole family at once instead of one phrase at
    a time.
    """
    for word in sorted(DONE_WORDS, key=len, reverse=True):
        start = text.find(word)
        while start != -1:
            if not _negated_at(text, start):
                return True
            start = text.find(word, start + 1)
    return False


def _blank(text: str, span: tuple[int, int]) -> str:
    """``text`` with ``span`` replaced by spaces, so positions stay comparable."""
    start, end = span
    return text[:start] + " " * (end - start) + text[end:]


def _to_int(raw: str) -> int:
    """A digit run or a small Chinese numeral (``两``/``十二``/``二十``)."""
    if raw.isdigit():
        return int(raw)
    if "十" not in raw:
        return _CN_NUMERALS.get(raw, 0)
    head, _, tail = raw.partition("十")
    high = _CN_NUMERALS.get(head, 1) if head else 1
    low = _CN_NUMERALS.get(tail, 0) if tail else 0
    return high * 10 + low


def _count_span(pattern, text: str) -> tuple[int | None, tuple[int, int]]:
    """``(value, span)`` of the first count ``pattern`` states, or ``(None, (0,0))``."""
    match = pattern.search(text)
    if not match:
        return None, (0, 0)
    for group in match.groups():
        if group is not None:
            return _to_int(group), match.span()
    return None, match.span()


def _first_count(pattern, text: str) -> int | None:
    """The first integer ``pattern`` captures, whichever group held it."""
    return _count_span(pattern, text)[0]


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
