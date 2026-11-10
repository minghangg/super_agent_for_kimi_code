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

**``fix_needed`` is not read from prose.** Four revisions tried to decide "does
this review still ask for work" from the wording of the first line: requirement
words, negation tables, count relations, an "all of it is done" phrase. Each
layer closed the shape it was written for and opened another, because the
question is "which batch does this clause talk about" - a parse, not a
substring. The answer is now a **structured marker** the QA writes on purpose::

    [QA] 通过   <!-- fix_needed: false -->
    [QA] 有条件通过（1 条 P1 必须修）  <!-- fix_needed: true -->

and the default is the safe side: **no readable marker means "there is work
left"**, so nothing is announced. A missed announcement leaves a person a row
to look at; a false one tells the reader a release is ready while it is not,
and nothing downstream can notice.
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

# ------------------------------------------------------------- fix_needed bit
#
# The whole mechanism is this pair of patterns. It is deliberately narrow: an
# ASCII key, one colon, one of two ASCII words, **wrapped in an HTML comment**
# and **at the end of the line**.
#
# Both extra conditions exist to separate a *statement* from a *mention*:
#
# * ``<!-- -->`` is invisible in rendered Markdown, so nobody writes it inside
#   an ordinary sentence. A bare ``fix_needed: false`` in running prose is a
#   sentence *about* the marker, not a marker.
# * anchoring at ``$`` excludes the shapes a review about this very protocol
#   produces: "上轮记录是 <!-- fix_needed: false -->，本轮仍须修" and
#   "本轮通过。QA 应写 fix_needed: false 表示无必修项，但本单有 1 条 P1 必须修".
#   In both, the token is a quotation or an instruction, and the clause's own
#   conclusion says the opposite.
#
# Two tokens that disagree still answer the safe side. ``FIX_TOKEN_ANY_RE`` is
# the un-anchored form, used only to notice a second token; it never decides on
# its own.
#
# **The protocol, stated once:**
#
# 1. a wrapped token at the end of the line **is** this review's declaration;
#    nothing else on the line can override it (see ``read_fix_token``).
# 2. if you mean to *quote* someone else's token, **do not leave it at the end
#    of the line** - put it mid-sentence, where it reads as a mention. The
#    parser cannot tell "I quote" from "I declare"; only the position can.
# 3. any wrapped token **anywhere** in the line that disagrees with the tail
#    makes the answer fall back to the safe side (no announcement). That is
#    deliberate: a line containing two conflicting declarations has not said
#    one thing, and announcing a release off it is the failure this bit exists
#    to stop. It costs a missed notice, never a false one.
_TOKEN = r"fix_needed\s*:\s*(true|false)"
FIX_TOKEN_RE = re.compile(r"<!--\s*" + _TOKEN + r"\s*-->\s*$", re.I)
FIX_TOKEN_ANY_RE = re.compile(r"<!--\s*" + _TOKEN + r"\s*-->", re.I)
FIX_TOKEN_CORE_RE = re.compile(_TOKEN, re.I)
TRUE, FALSE = "true", "false"


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


def read_fix_token(text: str) -> tuple[bool, str, bool]:
    """``(fix_needed, matched, structured)`` from the structured marker.

    Only a **comment-wrapped token at the end of the line** is a statement. A
    token quoted or described earlier in the line is prose about the marker:
    ``上轮记录是 <!-- fix_needed: false -->，本轮仍须修`` and
    ``QA 应写 fix_needed: false 表示无必修项，但本单有 1 条 P1 必须修`` both
    carry the token and both conclude the opposite. Anchoring it at the line's
    end is what tells the two apart without reading the sentence.

    Three answers, and the default is the third:

    * one trailing marker, and no other wrapped marker disagreeing with it -
      that value, ``structured``. **A trailing marker outvotes the prose**: it
      is the QA's deliberate declaration, the sentence is the description (see
      :func:`parse_review`);
    * wrapped markers that disagree - ``True`` (the safe side) and **not**
      structured. The line does not say one thing, and announcing a release off
      a self-contradicting line is the failure this bit exists to stop;
    * no trailing marker - ``True`` and not structured.

    ``matched`` is the marker as written (``<!-- fix_needed: false -->``), for
    the event log; it is empty whenever the answer did not come from a readable
    marker.
    """
    wrapped = [m.group(1).lower() for m in FIX_TOKEN_ANY_RE.finditer(text or "")]
    tail = FIX_TOKEN_RE.search(text or "")
    if tail is None:
        return True, "", False
    if len(set(wrapped)) > 1:
        return True, "", False
    core = FIX_TOKEN_CORE_RE.search(tail.group(0))
    return tail.group(1).lower() == TRUE, core.group(0), True


def parse_review(body: str) -> dict | None:
    """``{"marker", "verdict", "matched", "line"}`` for a ``[QA]`` first line.

    ``verdict`` is ``"pass"``, ``"reject"`` or ``""`` (no readable verdict, so a
    person decides - the parser never guesses). ``None`` means "not a QA
    comment at all", which is a different answer from "a QA comment whose
    verdict is unreadable".

    ``fix_needed``/``fix_matched``/``fix_structured`` are **appended** to that
    shape and never change how ``verdict`` is decided: they answer "does this
    review still ask for work", which is what decides whether the release goes
    to the supervisor. ``fix_structured`` is False whenever the answer is the
    fail-safe default rather than a marker - that is the signal a caller uses to
    ask a person to put the marker in.
    """
    line = first_line(body)
    if not line or not QA_PREFIX_RE.match(line):
        return None
    text = _strip_qa_prefix(line)
    fix_needed, fix_matched, fix_structured = read_fix_token(text)
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
            "fix_needed": fix_needed, "fix_matched": fix_matched,
            "fix_structured": fix_structured}
