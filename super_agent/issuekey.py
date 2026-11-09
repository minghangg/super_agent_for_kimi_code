"""Thread keys: ``owner/repo#issue``, with a read-only numeric compatibility.

The engine used to key an issue by its bare number (``"42"``). That is not a
thread identity: the same number in two repositories is two different threads,
and a workspace that talks to more than one repository cannot tell them apart.

The qualified key is ``owner/repo#42`` (lower case, whitespace stripped). Both
forms are read: a legacy numeric key is still honoured so an existing
``index.json``/``cursor.json`` keeps working. The rules are:

* **writes always use the qualified key** - a legacy key is never written back;
* a qualified key **wins** when both forms exist with different values, so a
  mixed file converges on the new form instead of oscillating;
* migration copies a legacy value to the qualified key only when the target is
  absent, so it is idempotent and never overwrites newer data.

Nothing here imports the bus or the network: this module must stay cheap enough
to be imported by the engine, the brief and the CLI.
"""
from __future__ import annotations

import re

_SPLIT = re.compile(r"^([^#\s]+)#(\d+)$")
_DIGITS = re.compile(r"^\d+$")


def repo_full(repo) -> str:
    """Normalise ``owner/name`` (lower case, no surrounding space)."""
    return str(repo or "").strip().strip("/").lower()


def is_repo(repo) -> bool:
    """True when ``repo`` looks like ``owner/name``."""
    text = repo_full(repo)
    return text.count("/") == 1 and all(part for part in text.split("/"))


def thread_key(repo, issue) -> str:
    """``owner/repo#42``. Falls back to the bare number without a usable repo.

    The fallback keeps a repository-less caller (a fixture, an old config)
    working exactly as before rather than inventing a key that can never match.
    """
    try:
        number = int(issue)
    except (TypeError, ValueError):
        return ""
    name = repo_full(repo)
    if not is_repo(name):
        return str(number)
    return f"{name}#{number}"


def split_key(key) -> tuple[str, int | None]:
    """``owner/repo#42`` -> ``("owner/repo", 42)``; a bare number -> ``("", 42)``."""
    text = str(key or "").strip()
    match = _SPLIT.match(text)
    if match:
        return repo_full(match.group(1)), int(match.group(2))
    if _DIGITS.match(text):
        return "", int(text)
    return "", None


def issue_number(key) -> int | None:
    """The issue number of a qualified *or* legacy key; None when unparsable."""
    return split_key(key)[1]


def is_legacy(key) -> bool:
    """True for a bare numeric key (the pre-qualified form)."""
    return bool(_DIGITS.match(str(key or "").strip()))


def _pick(new, old):
    """Qualified key wins; the legacy value is only used when new is absent."""
    if new is not None and new != "":
        return new
    return old


def migrate(index: dict, cursor: dict, repo, *, log_event=None) -> dict:
    """Copy legacy numeric entries to qualified keys, in place, idempotently.

    Returns ``{"migrated": [...], "issues": n, "cursors": n, "skipped": [...]}``
    and records ``index["meta"]["keys_migrated"]`` so a later run can tell that
    the file has been through this once. Legacy keys are **kept**: a read-only
    consumer that has not learned the new form yet must not lose its data.
    """
    out = {"migrated": [], "issues": 0, "cursors": 0, "skipped": []}
    issues = index.setdefault("issues", {})
    for key in sorted(list(issues), key=str):
        if not is_legacy(key):
            continue
        qualified = thread_key(repo, key)
        if not qualified or qualified == key:
            out["skipped"].append(key)
            continue
        if qualified in issues:
            out["skipped"].append(key)      # new key already wins
            continue
        issues[qualified] = issues[key]
        out["issues"] += 1
        out["migrated"].append(key)
    cursors = cursor.setdefault("issues", {})
    for key in sorted(list(cursors), key=str):
        if not is_legacy(key):
            continue
        qualified = thread_key(repo, key)
        if not qualified or qualified == key or qualified in cursors:
            continue
        cursors[qualified] = cursors[key]
        out["cursors"] += 1
        if key not in out["migrated"]:
            out["migrated"].append(key)
    if out["issues"] or out["cursors"]:
        index.setdefault("meta", {})["keys_migrated"] = True
        if callable(log_event):
            log_event({"type": "index.keys_migrated", "repo": repo_full(repo),
                       "issues": out["issues"], "cursors": out["cursors"],
                       "keys": out["migrated"]})
    return out


def read_both(mapping: dict, key, legacy_key) -> object:
    """Read the qualified value, falling back to the legacy one."""
    mapping = mapping or {}
    return _pick(mapping.get(key), mapping.get(legacy_key))
