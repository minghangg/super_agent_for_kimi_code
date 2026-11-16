"""Shape rules for role session keys (``pm-<project>-<random>``).

A role key is a **credential written in a comment body**: every role shares one
GitHub token, so the key name is the only thing a verdict can be attributed by.
A key named ``pm-<project>`` is therefore guessable by anyone who can read the
project name off the repository - that was issue #87 P0-1, where three published
keys of one project rang the release bell on twenty threads.

The fix is a random tail: the readable part may stay (people need to recognise
their own key), the entropy goes in the last segment. 8 hex characters is the
floor this module enforces: 32 bits, not guessable by hand.

**A date is not a random tail.** ``pm-demo-20261116`` matches "8 hex digits"
and is trivially guessable, so a calendar-shaped tail is rejected explicitly
instead of being counted as entropy.

Nothing here reads a file: these are pure name predicates, so the engine, the
delivery router and the workbench scripts can share one answer.
"""
from __future__ import annotations

import re

# The random tail: 8..32 hex characters, the last ``-`` segment.
_SUFFIX_RE = re.compile(r"-([0-9a-f]{8,32})$", re.I)

# ...except a date, which is hex-only by accident and guessable by design.
_DATE_RE = re.compile(r"-(?:19|20)\d{6}$")


def random_suffix(name: str) -> str:
    """The random tail of ``name``, or ``""`` when it has none.

    ``""`` is the answer for a key that is guessable - a bare project name, a
    date, a word. Callers treat it as "not a key of this shape".
    """
    text = str(name or "").strip()
    if not text or _DATE_RE.search(text):
        return ""
    match = _SUFFIX_RE.search(text)
    return match.group(1) if match else ""


def has_random_suffix(name: str) -> bool:
    """Does ``name`` end in a random tail of at least 32 bits?"""
    return bool(random_suffix(name))


def role_of(name: str) -> str:
    """``pm`` / ``qa`` / ``at`` for a key name, else ``""``.

    Only the prefix matters here; it is what tells a ``pm-*`` key from a
    ``qa-*`` one without a lookup table.
    """
    text = str(name or "").strip().lower()
    for role in ("pm", "qa", "at"):
        if text == role or text.startswith(role + "-"):
            return role
    return ""


def scoped_by(name: str, role: str, project: str) -> bool:
    """Is ``name`` the ``role`` key of ``project``?

    ``role-<project>`` or ``role-<project>-<anything>``. The readable part is
    kept on purpose: after a rotation the name is the only thing left that says
    which project the session belongs to.
    """
    text = str(name or "").strip().lower()
    want = str(project or "").strip().lower()
    if not text or not want or role_of(text) != role.lower():
        return False
    return text == f"{role.lower()}-{want}" or text.startswith(f"{role.lower()}-{want}-")


def preferred_key(data: dict, role: str, project: str = "") -> str:
    """The key a message to ``role``/``project`` should be sent to, or ``""``.

    Three answers, strongest first:

    * the newest ``role-<project>-<random>`` key - a rotation renames the key
      and leaves the old record behind, so "newest wins" is the only rule that
      follows the live session;
    * a bare ``role-<project>`` key, if a machine still carries one;
    * with no project given, the newest ``role-*`` key at all.

    **Never the first key in file order**: ``sessions.json`` is hand-edited, and
    the first ``pm-*`` record is whichever project was registered first, not
    the one being asked about. Picking it sent one project's idle notice to
    another project's PM. A project that owns no key answers ``""`` - falling
    back to another project's key is the misdelivery this exists to stop.
    """
    if not isinstance(data, dict) or not role:
        return ""
    want = str(project or "").strip().lower()
    rows = []
    for raw_name, item in data.items():
        name = str(raw_name)
        if role_of(name) != role.lower():
            continue
        if want and not scoped_by(name, role, want):
            continue
        stamp = str((item or {}).get("updated") or "") if isinstance(item, dict) else ""
        rows.append((stamp, name))
    if not rows:
        return ""
    rows.sort()
    return rows[-1][1]
