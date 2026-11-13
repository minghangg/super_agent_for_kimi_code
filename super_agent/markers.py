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

from . import issuekey

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


def _strip_prefix(line: str, pattern: re.Pattern) -> str:
    """Drop every leading ``pattern`` match and normalise the rest to NFKC.

    NFKC makes the full-width brackets, colons and dashes of hand-written
    comments comparable with the ASCII ones ``gh_issue.py`` writes, so one word
    table covers both.
    """
    previous = None
    while previous != line:
        previous = line
        line = pattern.sub("", line, count=1).strip()
        line = unicodedata.normalize("NFKC", line)
    return line


def _strip_qa_prefix(line: str) -> str:
    """Drop every leading ``[QA]``/``［QA］`` from ``line``."""
    return _strip_prefix(line, QA_PREFIX_RE)


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


# ---------------------------------------------------------------- pm verdict
#
# A ``[PM]`` first line is the third marker. It carries a three-state verdict,
# and the states are **not** equally safe:
#
# * ``working`` - the work is in hand, so nobody is woken;
# * ``blocked`` - an exception frame goes to the supervisor;
# * ``ready``   - the release frame goes to the supervisor.
#
# Announcing a release is the only answer that costs someone real work if it is
# wrong, so it is the only answer that demands a **structured declaration** -
# the contract form ``<!-- verdict: ready|blocked|working -->`` (issue #57 §二),
# and the older ``<!-- ready: true|false -->`` still honoured for comments
# written before it. Prose alone can say "working" or "blocked" (both are safe:
# one is silence, the other is a look), but a bare "可关单" is the wording this
# repository has already seen lie - it is exactly what the QA ``fix_needed``
# bit exists for. No declaration therefore means "no ready", which is the
# fail-closed side the contract asks for.
#
# Positions decide the state word, never table order: ``不可关单`` contains
# ``可关单``, so a negation prefix skips that hit instead of flipping it.
PM = "PM"
PM_PREFIX_RE = re.compile(r"^\s*[\[［]\s*(?:PM|supervisor)\s*[\]］]\s*", re.I)

# The three-state table. Order carries no precedence (see
# :func:`_first_state_hit`); it only fixes which state a word belongs to.
READY_WORDS = ("可关单", "可发布", "可上线", "ready")
BLOCKED_WORDS = ("受阻", "阻塞", "返工", "blocked")
WORKING_WORDS = ("修复中", "处理中", "进行中", "working")
STATE_TABLE = tuple([(w, "ready") for w in READY_WORDS]
                    + [(w, "blocked") for w in BLOCKED_WORDS]
                    + [(w, "working") for w in WORKING_WORDS])

# ``不可关单`` / ``未受阻`` are not verdicts of that state; the negation is part
# of the statement, so the hit is skipped and the next one is tried.
NEGATIONS = ("不", "未", "无", "非", "别")

# Two spellings of the same declaration, both anchored at the end of the line
# like the QA ``fix_needed`` token: a token quoted mid-sentence is a mention,
# not a declaration.
#
# * ``<!-- verdict: ready|blocked|working -->`` is the contract form and takes
#   precedence. It is the only spelling that can state ``blocked`` or
#   ``working`` explicitly, so a PM who follows the contract never depends on
#   the prose word table.
# * ``<!-- verdict: ready delivery: 9300 -->`` adds the **exact delivery** the
#   verdict judges. Without it the engine has to attribute a PM verdict by
#   time ("the newest delivery that already existed"), which misfires on a
#   reworked thread: two worker assignments, and a verdict about the first
#   delivery lands on the second one's key.
# * ``<!-- ready: true|false -->`` is the older form, kept working because
#   comments written before the contract exist and must not change meaning
#   overnight.
#
# ``delivery:`` is **optional** - every comment written before this field is
# still a valid declaration. ``_DELIVERY_TOKEN`` is deliberately separate from
# the state word so a malformed value can be *reported* instead of silently
# dropped: ``delivery: abc`` is a PM who tried to be exact, and quietly
# falling back to a guess is worse than saying the value did not parse.
# The state word, then an optional exact-delivery coordinate. Both parts live in
# one token so a verdict and its coordinate can never be read from different
# places in the line.
_VERDICT_TOKEN = r"verdict\s*:\s*(ready|blocked|working)(?:\s+delivery\s*:\s*(\d+))?"
VERDICT_TOKEN_RE = re.compile(r"<!--\s*" + _VERDICT_TOKEN + r"\s*-->\s*$", re.I)
VERDICT_TOKEN_ANY_RE = re.compile(r"<!--\s*" + _VERDICT_TOKEN + r"\s*-->", re.I)
DELIVERY_TOKEN_RE = re.compile(r"<!--\s*" + _VERDICT_TOKEN + r"\s*-->\s*$", re.I)
# Anything that looks like a delivery declaration, right or wrong: used only to
# notice a malformed one.
DELIVERY_LOOSE_RE = re.compile(r"\bdelivery\s*:\s*(\S+)", re.I)

_READY_TOKEN = r"ready\s*:\s*(true|false)"
READY_TOKEN_RE = re.compile(r"<!--\s*" + _READY_TOKEN + r"\s*-->\s*$", re.I)
READY_TOKEN_ANY_RE = re.compile(r"<!--\s*" + _READY_TOKEN + r"\s*-->", re.I)

# ``#42`` or ``owner/repo#42``. The look-behind stops ``C#7`` and ``## 42``
# from being read as a coordinate; the character class stops trailing Chinese
# punctuation from swallowing the number.
COORD_RE = re.compile(
    r"(?<![\w#/])(?:([A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*))?"
    r"#([^\s，。、,;；:：)）\]】\-]+)")
_TRAILING = "，。、,;；:：)）]】\"'`}"


def _first_state_hit(text: str) -> tuple[str, str, str]:
    """``(state, word, negated)`` for the earliest usable word.

    Same rule as :func:`_first_hit`, over :data:`STATE_TABLE`. A hit directly
    after a negation character is skipped: ``不可关单`` must not read as
    ``可关单``, and ``未受阻`` must not read as ``受阻``. A tail
    (``可关单项``) is skipped too, for the same reason ``通过项`` is.

    ``negated`` is the first word that was skipped because of a negation, and
    it is what lets the caller notice a line that both denies and declares:
    ``[PM] 不可关单 #42 <!-- ready: true -->`` has not said one thing.
    """
    lower = text.lower()
    best: tuple[int, int, str, str] | None = None
    negated = ""
    for word, state in STATE_TABLE:
        needle = word.lower()
        start = lower.find(needle)
        while start != -1:
            before = lower[start - 1: start] if start else ""
            after = lower[start + len(needle): start + len(needle) + 1]
            if before and before in NEGATIONS:
                negated = negated or state
                start = lower.find(needle, start + 1)
                continue
            if after and after in TAILS:
                start = lower.find(needle, start + 1)
                continue
            candidate = (start, -len(needle), word, state)
            if best is None or candidate[:2] < best[:2]:
                best = candidate
            break
    if best is None:
        return "", "", negated
    return best[3], best[2], negated


def read_verdict_token(text: str) -> tuple[str | None, str, str]:
    """``(state, matched, source)`` from a trailing HTML marker.

    Recognises both spellings, and the contract form wins::

        <!-- verdict: ready|blocked|working -->   contract v1 (preferred)
        <!-- ready: true|false -->                legacy

    ``state`` is ``"ready"``, ``"blocked"``, ``"working"``, or ``None`` when
    neither marker is present. ``source`` says which one answered:
    ``"verdict"`` for the contract form, ``"marker"`` for the legacy one, ``""``
    for no marker at all.

    Two rules decide the answers that are not just "read the word":

    * **the contract form outranks the legacy form** when both are present and
      agree - it is the newer declaration, and a line carrying both is a
      comment being migrated;
    * they **must agree**. A line that says ``verdict: blocked`` and
      ``ready: true`` has not said one thing, so the answer is ``blocked``:
      the safe side, exactly like ``不可关单`` beside ``ready: true``. Two
      contract tokens that disagree also answer ``blocked``.

    Only the trailing token is a declaration; a token earlier in the line is a
    quotation. That anchor is what lets this protocol be described in a comment
    about itself.
    """
    body = text or ""
    verdicts = [m.group(1).lower() for m in VERDICT_TOKEN_ANY_RE.finditer(body)]
    legacies = [m.group(1).lower() for m in READY_TOKEN_ANY_RE.finditer(body)]
    verdict_tail = VERDICT_TOKEN_RE.search(body)
    ready_tail = READY_TOKEN_RE.search(body)
    if verdict_tail is None and ready_tail is None:
        return None, "", ""
    legacy_state = {"true": "ready", "false": "blocked"}
    if verdict_tail is not None:
        # The contract form is trailing, so it is the declaration. Anything
        # else on the line may only *contradict* it: a disagreement is itself
        # the answer, and the answer is the safe side.
        declared = verdict_tail.group(1).lower()
        token = verdict_tail.group(0).strip()
        if len(set(verdicts)) > 1 or any(
                legacy_state[val] != declared for val in legacies):
            return ("blocked",
                    " + ".join(sorted(set(verdicts) | set(legacies))), "verdict")
        return declared, token, "verdict"
    if verdicts:
        # No trailing contract token, but one appears earlier in the line and
        # the trailing token is the legacy spelling: that is a comment being
        # migrated. They must agree, and the contract form is what decides.
        want = {legacy_state[val] for val in legacies}
        if len(want) > 1 or len(set(verdicts)) > 1 or verdicts[-1] not in want:
            return ("blocked",
                    " + ".join(sorted(set(verdicts) | set(legacies))), "verdict")
        return verdicts[-1], f"<!-- verdict: {verdicts[-1]} -->", "verdict"
    # Legacy only: the bit is a single boolean, so ``false`` means the same
    # thing the contract form spells out as ``blocked``.
    if len(set(legacies)) > 1:
        # Two legacy tokens that disagree are not a declaration; the prose word
        # decides (the pre-v1 behaviour, unchanged).
        return None, "", ""
    if ready_tail.group(1).lower() == TRUE:
        return "ready", ready_tail.group(0).strip(), "marker"
    return "blocked", ready_tail.group(0).strip(), "marker"


def read_ready_token(text: str) -> tuple[bool | None, str]:
    """``(declared_ready, matched)`` - the legacy-only view of the marker.

    Kept for callers that only ask the old question. ``None`` means "no
    declaration", which is **not** the same as ``False``:
    ``<!-- ready: false -->`` is a PM saying "not ready" and it is honoured; a
    missing token only means the line never declared anything, so it cannot
    reach ``ready`` either. A contract token answers through
    :func:`read_verdict_token`; here ``verdict: ready`` reads as True and the
    other two states as ``None`` (neither of them is a legacy boolean).
    """
    state, _matched, source = read_verdict_token(text)
    if source == "marker":
        return state == "ready", _matched
    if source == "verdict":
        return (True if state == "ready" else None), _matched
    return None, ""


def _find_coord(text: str) -> tuple[int | None, str]:
    """``(issue, error)`` from the first coordinate in ``text``.

    ``error`` is ``""`` only when ``issue`` is a number. The two messages name
    what a person has to fix, and the caller decides how loud to be about it.
    """
    for match in COORD_RE.finditer(text or ""):
        repo, token = match.group(1) or "", match.group(2)
        token = token.rstrip(_TRAILING)
        if not token:
            return None, "issue coordinate is not a number"
        repo, number = issuekey.split_key(f"{repo}#{token}" if repo else token)
        if number is not None:
            return number, ""
        return None, "issue coordinate is not a number"
    return None, "no issue coordinate"


def read_delivery_token(text: str) -> tuple[int | None, str]:
    """``(delivery, error)`` from a ``delivery: <cid>`` inside the verdict marker.

    ``(None, "")`` means "no delivery field", which is the old format and is
    **not** an error: every comment written before the field existed is still a
    valid declaration, and the engine falls back to its older attribution.

    ``(None, <message>)`` means the PM named a coordinate and it is not the one
    this marker declares. That is reported rather than dropped: a PM who names
    a coordinate and gets a silent guess instead has no way to know it was
    ignored.

    **One message covers every wrong position, and that is deliberate.** The
    three shapes - ``delivery: 9300 <!-- verdict: ready -->`` (before the
    marker), ``<!-- verdict: ready --> 见 delivery: 9300`` (after it) and
    ``<!-- verdict: ready --> <!-- delivery: 9300 -->`` (its own comment) - are
    all "the coordinate was stated but is not part of this marker". Splitting
    them would ask the parser to guess intent from layout, and the reader's
    action is the same in all three: move the field inside the marker. A
    message that varies by position would also hide the one distinction that
    *does* matter, which is already carried separately: a value that is not a
    number says so.
    """
    body = text or ""
    tail = DELIVERY_TOKEN_RE.search(body)
    if tail is not None:
        value = tail.group(2)
        if value is not None:
            return int(value), ""
        # The marker is here but carries no coordinate. Something *else* on the
        # line may still name one - ``delivery: 9300 <!-- verdict: ready -->``
        # used to end up here and be dropped silently, which is exactly the
        # guess this field exists to prevent. Look around the marker only.
        remainder = body[:tail.start()] + body[tail.end():]
        stray, error = _read_stray_delivery(remainder)
        if error:
            return None, error
        return None, ""
    return _read_stray_delivery(body)


def _read_stray_delivery(text: str) -> tuple[int | None, str]:
    """``(None, error)`` for a delivery declaration outside the marker."""
    for match in DELIVERY_LOOSE_RE.finditer(text or ""):
        value = str(match.group(1) or "").strip()
        if not value.isdigit():
            return None, "delivery coordinate is not a number"
        return None, "delivery coordinate is not at the end of the verdict marker"
    return None, ""


def parse_pm_verdict(body: str) -> dict | None:
    """Three-state verdict for a ``[PM]`` first line.

    Returns ``None`` when the body is not a PM comment at all, and a dict
    otherwise::

        {"marker": "PM", "state": "ready"|"blocked"|"working"|"",
         "matched": <what decided it>, "line": <first line>,
         "issue": <int|None>, "coords_ok": <bool>, "coords_error": <str>,
         "delivery": <int|None>, "delivery_error": <str>,
         "structured": <bool>, "source": <see below>}

    ``delivery`` is the **exact delivery comment this verdict judges**, read
    from ``delivery: <cid>`` inside the trailing verdict marker. ``None`` means
    the comment uses the old format and names no delivery, so the caller must
    attribute it the older way; ``None`` with a non-empty ``delivery_error``
    means the field was present and did not parse - a PM who tried to be exact,
    whose coordinate was not readable. A malformed value never silently becomes
    "no value": that would turn a stated coordinate into a guess.

    ``source`` says which layer decided ``state``:

    * ``"verdict"`` - a trailing ``<!-- verdict: ready|blocked|working -->``.
      The contract form; it wins over everything.
    * ``"marker"`` - a trailing ``<!-- ready: true|false -->``. The legacy form:
      ``true`` reads as ``ready``, ``false`` as ``blocked``.
    * ``"prose"`` - a state word is present but **no marker is**, so ``state`` is
      ``""``. The word is kept in ``matched`` so a caller can ask the PM to add
      the marker; it never decides a state on its own.
    * ``"none"`` - nothing readable at all. ``state`` is ``""``.

    **Every state needs a structured marker.** The contract says an unmarked
    line is "blocked / not finished", and that is exactly what ``""`` means to
    the caller (fail-closed). Prose alone is not enough even for ``working``:
    the three states are one protocol, and a state that only some spellings can
    state is a state nobody can rely on.

    ``state`` is ``""`` when nothing decides it, or when ``ready`` was read
    without a usable coordinate in the **first line**. ``ready`` requires all
    three of: a trailing marker saying so, a first-line coordinate, and no
    blocked word or denial beside it. Two statements that disagree answer
    ``blocked``: a line that says both "ready" and "受阻" has not said one
    thing, and this is the failure the marker exists to stop.

    ``structured`` is True exactly when ``source`` is ``"verdict"`` or
    ``"marker"`` - the state came from a deliberate declaration, not from
    wording. It is the signal a caller uses to ask a PM to add
    ``<!-- verdict: ... -->``.

    The coordinate comes from the **first line only**. A ``#99`` mentioned in
    the body is a reference, not this verdict's subject; judging it as the
    coordinate files a release against an issue the PM never spoke about.
    """
    line = first_line(body)
    if not line or not PM_PREFIX_RE.match(line):
        return None
    text = _strip_prefix(line, PM_PREFIX_RE)
    declared, token, token_source = read_verdict_token(text)
    prose_state, prose_word, negated = _first_state_hit(text)
    issue, coords_error = _find_coord(text)
    delivery, delivery_error = read_delivery_token(text)
    if delivery_error and not coords_error:
        # The delivery coordinate is a coordinate too: a bad one is reported in
        # the same field so a caller has one place to read.
        coords_error = delivery_error
    coords_ok = issue is not None and not coords_error
    state, source, matched = "", "none", ""
    if declared is not None:
        # A line that both denies and declares has not said one thing. The
        # safe side wins: a release announced off a self-contradicting line is
        # the failure this marker exists to stop.
        if prose_state == "blocked" or negated == "ready":
            state = "blocked"
            matched = " + ".join(x for x in (token, prose_word or "不可关单") if x)
        else:
            state, matched = declared, token
        source = token_source
    elif prose_state:
        # A state word with no marker: the contract's "unmarked means blocked",
        # and the word is reported so a caller can ask for the marker.
        source, matched = "prose", prose_word
    if state == "ready" and not coords_ok:
        # "No coordinate, no verdict" - a release frame that names no issue
        # cannot be acted on, and guessing one is how a wrong issue closes.
        state = ""
    return {"marker": PM, "state": state, "matched": matched, "line": line,
            "issue": issue, "coords_ok": coords_ok,
            "coords_error": coords_error, "delivery": delivery,
            "delivery_error": delivery_error,
            "structured": source in ("verdict", "marker"), "source": source}


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
