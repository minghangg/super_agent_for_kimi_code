"""Find role session-key names in comment bodies (issue #87 round 2).

The rule this enforces: **a session key name never goes into an issue or a
comment.** The key is the credential - every role shares one GitHub token, so
the name in the body is the only thing a verdict is attributed by. A name that
was published once is public for good, and a rotation is a manual operation
nobody wants to do twice.

The scan is deliberately dumb: it looks for the *shape* (``pm-``/``qa-``/``at-``
followed by a word), not for the keys in ``sessions.json``. A scan that only
knows today's keys cannot see yesterday's leak, which is the one that matters.

Exit codes: ``0`` = no key name, ``1`` = at least one found, ``2`` = could not
read the input (a scan that did not run must never look clean).

Usage::

    # one comment of one issue, read through this repository's issue CLI
    python tools/keyname_scan.py --issue 86 --cid 6900753114

    # a whole issue (body + every comment), or a local file
    python tools/keyname_scan.py --issue 87
    python tools/keyname_scan.py --file D:/temp_safkc/some_comment.md

    # a directory tree (documentation gate)
    python tools/keyname_scan.py --dir assistants --dir playbooks
"""
from __future__ import annotations

import argparse
import importlib
import os
import re
import sys
from pathlib import Path

# The implementation is this repository's own module, found from this file's
# location - the same rule ``tools/gh_issue.py`` uses. An environment variable
# naming another checkout was the old mechanism, and it required a private
# checkout to run a documentation gate.
CHECKOUT = Path(__file__).resolve().parents[1]

# A key name: role prefix + at least one character of the readable part.
# ``at-`` is **off by default**: ``at-most-once`` and friends are English words,
# and a gate that fires on prose gets switched off. Use ``--roles pm,qa,at``
# when scanning for AT keys specifically.
KEY_RE = re.compile(r"(?<![A-Za-z0-9_-])(?:%s)-[A-Za-z0-9][A-Za-z0-9_*.-]*",
                    re.I)

# Placeholders and prose that look like keys and are not: a glob, a bare
# prefix with the readable part cut off, and the engine's own check names
# (``pm-task-book`` is a doctor row, not a session).
PLACEHOLDERS = {"pm-*", "qa-*", "at-*"}

# Non-key tokens in this repository that share the shape. Kept short and
# explicit: an ignore list that grows silently is a gate that stops checking.
DEFAULT_IGNORE = {"pm-task-book", "pm-stall", "qa-ISS-"}


def find_keys(text: str, roles=("pm", "qa"), ignore=()) -> list[str]:
    """Key-shaped tokens in ``text``, in order, de-duplicated."""
    pattern = re.compile(KEY_RE.pattern % "|".join(roles), re.I)
    skip = {t.lower() for t in PLACEHOLDERS} | {t.lower() for t in ignore}
    skip |= {t.lower() for t in DEFAULT_IGNORE}
    out: list[str] = []
    for match in pattern.finditer(str(text or "")):
        token = match.group(0).rstrip(".")
        if token.endswith("-") and token.lower() not in skip:
            # ``qa-lwa-ISS-`` - the readable part was cut off on purpose.
            continue
        if token.lower() in skip:
            continue
        # ``pm-<项目>`` and friends: a placeholder, not a name.
        if "<" in token or ">" in token:
            continue
        if token.lower() in {t.lower() for t in out}:
            continue
        out.append(token)
    return out


def load_impl():
    """This checkout's ``super_agent.issue_cli``, or a refusal naming the path.

    The path comes from this file's location, so the gate runs on a plain clone.
    It used to require an environment variable pointing at a private checkout,
    which meant the documentation gate could only run on one machine.
    """
    expected = (CHECKOUT / "super_agent" / "issue_cli.py").resolve()
    sys.path.insert(0, str(CHECKOUT))
    try:
        module = importlib.import_module("super_agent.issue_cli")
    except Exception as exc:            # noqa: BLE001  any import failure is a refusal
        raise SystemExit(f"cannot load {expected}: {type(exc).__name__}: {exc}")
    actual = Path(getattr(module, "__file__", "") or "").resolve()
    if actual != expected:
        raise SystemExit(
            f"issue_cli was loaded from another checkout: actual={actual} "
            f"expected={expected}")
    return module


def issue_text(project: str, issue: int, cid: str = "") -> str:
    """The issue body plus every comment body, or just ``cid``'s comment."""
    impl = load_impl()
    try:
        repo = impl._repo(project, impl._load_projects(None))
    except impl.Denied as exc:
        raise SystemExit(f"[拒绝] {exc}")
    ctx = {"env_file": None, "table": impl._roles(None)}
    rows = impl._call("GET", f"/repos/{repo}/issues/{issue}/comments?per_page=100", **ctx)
    rows = rows if isinstance(rows, list) else []
    if cid:
        rows = [r for r in rows if str(r.get("id")) == str(cid)]
        if not rows:
            raise SystemExit(f"#{issue} has no comment {cid}")
        return "\n".join(str(r.get("body") or "") for r in rows)
    head = impl._call("GET", f"/repos/{repo}/issues/{issue}", **ctx)
    return (str((head or {}).get("body") or "") + "\n"
            + "\n".join(str(r.get("body") or "") for r in rows))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--issue", type=int, help="issue number (needs --project)")
    ap.add_argument("--project", default="", help="project name for --issue")
    ap.add_argument("--cid", default="", help="only this comment id")
    ap.add_argument("--file", action="append", default=[],
                    help="scan these files (repeatable)")
    ap.add_argument("--dir", action="append", default=[],
                    help="scan *.md/*.py under these directories (repeatable)")
    ap.add_argument("--quiet", action="store_true", help="counts only")
    ap.add_argument("--roles", default="pm,qa",
                    help="role prefixes to look for (default pm,qa)")
    ap.add_argument("--ignore", action="append", default=[],
                    help="extra token to treat as not-a-key (repeatable)")
    args = ap.parse_args(argv)

    sources: list[tuple[str, str]] = []
    if args.issue:
        if not args.project:
            raise SystemExit("--issue needs --project")
        sources.append((f"#{args.issue}", issue_text(args.project, args.issue,
                                                     args.cid)))
    for path in args.file:
        p = Path(path)
        sources.append((str(p), p.read_text(encoding="utf-8", errors="replace")))
    for root in args.dir:
        base = Path(root)
        for p in sorted(base.rglob("*")):
            if p.suffix.lower() in (".md", ".py", ".sh", ".ps1", ".txt"):
                sources.append((str(p),
                                p.read_text(encoding="utf-8", errors="replace")))
    if not sources:
        raise SystemExit("nothing to scan: give --issue, --file or --dir")

    total = 0
    for label, text in sources:
        keys = find_keys(text, tuple(r.strip() for r in args.roles.split(",") if r.strip()),
                         tuple(args.ignore))
        if not keys:
            continue
        total += len(keys)
        if not args.quiet:
            print(f"{label}: {len(keys)} key name(s)")
            for key in keys:
                print(f"  {key}")
    if total:
        print(f"FAIL {total} key name(s) in {len(sources)} source(s): "
              f"a key name is a credential, keep it out of issues")
        return 1
    if not args.quiet:
        print(f"OK 0 key names in {len(sources)} source(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
