"""Role charters: a baseline that always loads, plus an optional supplement.

Why this module exists (ISS-95)
-------------------------------
Before this, the charter a fresh session read was a single file path handed to
``dispatch_cli.charter_for()``. Two things were wrong with that:

* the path came from the workbench, so a public checkout had no charter at all
  and the failure was a silent empty string;
* there was no way to keep private facts (project short names, host paths) out
  of the general charter without forking the whole file.

The rule now: **baseline always loads, supplement loads only if configured**.

    baseline   <checkout>/rules/charters/<role>.md      missing ⇒ CharterMissing
    supplement <charter.supplement_root>/<role>.md      missing ⇒ normal

The asymmetry is the point. A missing baseline is a broken install: a session
that never read its charter is the 2026-11-16 failure where a worker was born
without the testing-level rules and nobody noticed. A missing supplement is
ordinary -- most deployments have none -- and must not block anything.

The two are **not** a merge: the caller reads both files and the model applies
them in order. Concatenating them here would hide which line came from where,
and the whole reason for two files is that one of them is private.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from . import agent_config

#: Leaf names the built-in map may point at. A deployment can extend the map,
#: but a leaf that is not one of these is a configuration mistake, not a file
#: name to guess: the charter directory is flat and public.
CHARTER_DIR = "charters"


class CharterMissing(RuntimeError):
    """A session was about to be created without its baseline charter.

    Deliberately an exception rather than an empty return: the caller must
    fail loudly. Returning "" is exactly the shape that let a worker run with
    no charter for a whole round before anyone noticed.
    """

    def __init__(self, message: str, *, name: str = "", path: str = ""):
        super().__init__(message)
        self.name = name
        self.path = path


def baseline_root() -> Path:
    """Where the general charters live.

    ``charter.baseline_root`` when configured, else ``<checkout>/rules/charters``.
    The fallback is a public path on purpose: a fresh clone must be able to
    create a session, and the charters ship with the package.
    """
    raw = str(agent_config.get("charter.baseline_root") or "").strip()
    if raw:
        return Path(os.path.expanduser(raw)).resolve()
    return (Path(__file__).resolve().parents[1] / "rules" / CHARTER_DIR).resolve()


def supplement_root() -> str:
    """Where this deployment's private charter additions live; ``""`` if none.

    Empty is the normal state, not an error. A relative value resolves against
    the checkout, the same rule every other path in ``agent_config`` follows.
    """
    raw = str(agent_config.get("charter.supplement_root") or "").strip()
    if not raw:
        return ""
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute():
        path = agent_config.CHECKOUT / path
    return str(path.resolve())


def leaf_for(name: str) -> str:
    """The charter file name for a session name; ``""`` when the map has no hit.

    The map is prefix-based and ordered, longest prefix first, so
    ``pm-<project>`` wins over a bare ``pm`` entry. ``agent_config.charter_map()``
    already returns the merged (built-in + configured) table in order.
    """
    low = str(name or "").strip().lower()
    if not low:
        return ""
    for prefix, leaf in agent_config.charter_map():
        if low.startswith(prefix):
            return str(leaf or "").strip()
    return ""


def charter_files(name: str) -> tuple[str, str]:
    """``(baseline, supplement)`` for a session name.

    ``baseline`` is always a real file; ``supplement`` is ``""`` when the
    deployment configured no root or has no file for this role.

    Raises :class:`CharterMissing` when the name maps to no charter, when the
    mapped file does not exist, or when the baseline root itself is missing.
    Each of those is a broken install, and each used to be a silent empty
    string.
    """
    leaf = leaf_for(name)
    if not leaf:
        raise CharterMissing(
            f"会话名 {name!r} 没有对应的章程（charter.map 未命中任何前缀）",
            name=name)

    root = baseline_root()
    baseline = root / leaf
    if not baseline.is_file():
        raise CharterMissing(
            f"基线章程缺失：{baseline}（会话名 {name!r} → {leaf}）",
            name=name, path=str(baseline))

    sup = ""
    sup_root = supplement_root()
    if sup_root:
        candidate = Path(sup_root) / leaf
        if candidate.is_file():
            sup = str(candidate)
    return str(baseline), sup


def broken_charters(names) -> dict[str, list[str]]:
    """``{session name: missing refs}`` for names whose charter points nowhere.

    The check that ``charter_files`` cannot do for itself: a charter that is
    present but names a rule file this package does not ship reads as a working
    install and is not one (ISS-107). This is what a doctor-style report asks,
    and it answers with every name it was given rather than stopping at the
    first fault -- one run should show the whole picture.

    Names that map to no charter, or whose charter is absent, are **not**
    reported here: ``charter_files`` already refuses those outright, and
    repeating them would turn one install fault into two lines.
    """
    out: dict[str, list[str]] = {}
    for name in names:
        leaf = leaf_for(name)
        if not leaf:
            continue
        baseline = baseline_root() / leaf
        if not baseline.is_file():
            continue
        try:
            missing = unresolved_refs(baseline.read_text(encoding="utf-8"))
        except OSError:
            continue
        if missing:
            out[name] = missing
    return out


#: A cross-reference a charter makes to another rule file, written relative to
#: the package root (``rules/...``). Absolute and workbench paths are not
#: matched: those are a different defect (a private dependency) and the privacy
#: gate already refuses them.
REF_RE = re.compile(r"`(rules/[A-Za-z0-9_./-]+\.md)`")


def unresolved_refs(text: str, root: Path | None = None) -> list[str]:
    """Every ``rules/...`` path a charter text names that does not exist.

    Why this check exists (ISS-107): ``charter_files`` only proves **the charter
    file itself** is present. A charter whose body says "see
    `rules/shared/rules/newtrack_common.md`" passed every check while that file
    did not exist at all -- the reader could open the charter and not the rule
    it points at. ``describe()`` answered ``ok: True`` throughout.

    Absence of the referent is never evidence about the referent, so an
    unreadable reference is a fault, not a skip. Only paths that start with
    ``rules/`` are inspected: those are the ones this package is supposed to
    ship.
    """
    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    missing = []
    for ref in REF_RE.findall(text):
        if not (root / ref).is_file() and ref not in missing:
            missing.append(ref)
    return missing


def describe(name: str) -> dict:
    """A printable summary for doctor-style checks; never raises.

    ``ok`` covers the whole install, not just the file lookup: a charter that
    exists but points at a rule file this package does not ship is broken in the
    way ISS-107 was raised for, and answering ``ok: True`` there is what let it
    go unnoticed. Broken references are reported, not raised.
    """
    try:
        base, sup = charter_files(name)
    except CharterMissing as exc:
        return {"name": name, "leaf": leaf_for(name), "baseline": "",
                "supplement": "", "supplement_root": supplement_root() or "(未配置)",
                "ok": False, "error": str(exc)}
    text = Path(base).read_text(encoding="utf-8")
    if sup:
        text += "\n" + Path(sup).read_text(encoding="utf-8")
    broken = unresolved_refs(text)
    out = {"name": name, "leaf": leaf_for(name), "baseline": base,
           "supplement": sup or "(未配置)",
           "supplement_root": supplement_root() or "(未配置)",
           "ok": not broken}
    if broken:
        out["broken_refs"] = broken
    return out
