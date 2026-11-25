"""Which session keys belong to a role, and the one read of ``sessions.json``.

Why this module exists (ISS-130 addendum 2): five call sites in ``engine.py``
read ``sessions.json`` through ``read_text_shared``, which answers ``""`` for a
file it cannot read. ``json.loads("")`` then raises, the ``except`` folds it
into ``data = {}``, and the AT/PM/QA key tables silently lose every key. A run
in that state is indistinguishable from a run where nobody has a key, which is
the same "unreadable read as empty" shape #129 fixed in ``reconcile``.

So the read lives here, once, and it returns **two** answers: the map and a
problem sentence. ``""`` is a real (empty) ledger; a sentence means "I could not
read this" and every caller must make that visible instead of swallowing it.

The key filtering is pure and takes the map as an argument, so a caller that
cannot read the map still answers from the explicit ledger binding - which is
what keeps an unreadable map from turning a bound thread into an unbound one.
"""
from __future__ import annotations

import json
from pathlib import Path

from .. import session_map_io, sessionkeys


def read_session_map(path: Path) -> tuple[dict, str]:
    """``(map, problem)``. ``problem == ""`` means the map was actually read.

    A missing file is **not** a problem: a fresh state dir has no
    ``sessions.json``, and that is the normal empty case. Only "the file is
    there and I could not get a whole one out of it" is a problem.
    """
    text = session_map_io.read_text_retry(Path(path))
    if text is None:
        return {}, f"{Path(path).name} 读不出：{path}"
    try:
        data = json.loads(text)
    except ValueError as exc:
        return {}, f"{Path(path).name} 解析失败（{type(exc).__name__}）"
    if not isinstance(data, dict):
        return {}, f"{Path(path).name} 顶层不是对象"
    return data, ""


def ledger_keys(entry: dict | None, role: str) -> set[str]:
    """The ``name``/``session`` fields of one ledger role entry."""
    out: set[str] = set()
    for name, item in (entry or {}).items():
        if str(name).lower() != role or not isinstance(item, dict):
            continue
        for field in ("name", "session"):
            value = str(item.get(field) or "")
            if value:
                out.add(value)
    return out


def keys_from_map(data: dict | None, role: str, *, released=(),
                  sessions: dict | None = None, accept=None) -> set[str]:
    """``<role>-*`` keys that pass the random-tail and liveness filters.

    Every role applies the same two gates - a random tail (the name is public,
    so the tail is what proves a key was issued) and "still live" (not handed
    back, session not archived). ``accept`` adds the role's own condition, e.g.
    the PM scope check or the AT server read-back.
    """
    out: set[str] = set()
    if not isinstance(data, dict):
        return out
    handed_back = set(released or ())
    table = sessions if isinstance(sessions, dict) else {}
    for raw_name, item in data.items():
        name = str(raw_name)
        if sessionkeys.role_of(name) != role:
            continue
        if not sessionkeys.has_random_suffix(name):
            continue
        if name in handed_back:
            continue
        sid = str((item or {}).get("id") or "") if isinstance(item, dict) else ""
        if (table.get(sid) or {}).get("archived"):
            continue
        if accept is not None and not accept(name, sid):
            continue
        out.add(name)
    return out
