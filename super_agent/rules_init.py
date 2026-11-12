"""First-run rule selection: copy chosen candidates into ``rules/active/``.

    python -m super_agent.rules_init --list
    python -m super_agent.rules_init --rules a,b --dry-run
    python -m super_agent.rules_init --rules a,b
    python -m super_agent.rules_init --all
    python -m super_agent.rules_init --rules a,b --force
    python -m super_agent.rules_init --root <path>

The candidate pool keeps its two levels (``candidates/rules`` and
``candidates/skills``) because the category matters while choosing; the active
directory is flat because once chosen only "in effect or not" is left.

Five rules keep a re-run safe, and none of them deletes a file:

* identical content is skipped (``same``);
* different content is reported, never overwritten without ``--force``
  (``conflict`` when the active copy is newer, ``stale`` when the candidate is);
* an active file that is not in the selection is reported (``orphan``) and
  left alone, so un-ticking a rule never destroys local edits;
* ``--dry-run`` writes nothing at all;
* a name that is not in the pool stops the run before anything is written.

The pool is read, never modified.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

CANDIDATES = "candidates"
ACTIVE = "active"
SUFFIX = ".md"
LEVELS = ("rules", "skills")


def default_root() -> Path:
    """``RULES_ROOT`` when set, else ``<repo_root>/rules``."""
    env = os.environ.get("RULES_ROOT")
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parent.parent / "rules"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def candidate_files(root: Path) -> dict[str, Path]:
    """``{stem: path}`` for every candidate, keyed by file stem.

    The stem is the name the user types, so it must be unique across both
    levels; a duplicate is a hard error rather than a silent pick.
    """
    pool: dict[str, Path] = {}
    for level in LEVELS:
        for path in sorted((root / CANDIDATES / level).glob("*" + SUFFIX)):
            if path.stem in pool:
                raise SystemExit(
                    f"备选池重名：{path.stem} 同时存在于 "
                    f"{pool[path.stem]} 与 {path}；请改用全路径区分。")
            pool[path.stem] = path
    return pool


def active_files(root: Path) -> dict[str, Path]:
    return {p.stem: p for p in sorted((root / ACTIVE).glob("*" + SUFFIX))}


def _level_of(path: Path) -> str:
    return path.parent.name


def format_listing(pool: dict[str, Path]) -> str:
    lines = [f"{'name':<28} {'level':<7} {'bytes':>7}"]
    for name, path in pool.items():
        lines.append(f"{name:<28} {_level_of(path):<7} "
                     f"{path.stat().st_size:>7}")
    return "\n".join(lines)


def plan(root: Path, names: list[str], *, force: bool = False) -> dict:
    """Compute the copy plan. Reads the filesystem, writes nothing."""
    pool = candidate_files(root)
    active = active_files(root)
    unknown = [n for n in names if n not in pool]
    if unknown:
        raise SystemExit(f"未知候选：{', '.join(sorted(unknown))}")
    steps = {"same": [], "copied": [], "conflict": [], "stale": []}
    target_dir = root / ACTIVE
    for name in names:
        src = pool[name]
        dst = target_dir / (name + SUFFIX)
        if not dst.is_file():
            steps["copied"].append(name)
            continue
        if _sha256(src) == _sha256(dst):
            steps["same"].append(name)
            continue
        # Content differs. Without ``--force`` nothing is written: a candidate
        # newer than the active copy is reported as stale (the active copy is
        # out of date), an older or equally old one as a conflict (the active
        # copy was edited locally).
        if force:
            steps["copied"].append(name)
        elif src.stat().st_mtime > dst.stat().st_mtime:
            steps["stale"].append(name)
        else:
            steps["conflict"].append(name)
    steps["orphan"] = sorted(n for n in active if n not in set(names))
    steps["force"] = force
    steps["target"] = target_dir
    steps["pool"] = pool
    return steps


def apply(steps: dict) -> None:
    """Write the plan. Only ``copied`` entries are written."""
    target = steps["target"]
    target.mkdir(parents=True, exist_ok=True)
    for name in steps["copied"]:
        src = steps["pool"][name]
        (target / (name + SUFFIX)).write_bytes(src.read_bytes())


def summary(steps: dict) -> str:
    return (f"same={len(steps['same'])} copied={len(steps['copied'])} "
            f"conflict={len(steps['conflict'])} "
            f"stale={len(steps['stale'])} "
            f"orphan={len(steps['orphan'])}")


def hints(steps: dict) -> list[str]:
    out = []
    if steps["conflict"]:
        out.append(f"conflict: {', '.join(steps['conflict'])}（本地已改，未覆盖；"
                   "确认后加 --force）")
    if steps["stale"]:
        out.append(f"stale: {', '.join(steps['stale'])}（候选比 active 新，未覆盖；"
                   "确认后加 --force）")
    if steps["orphan"]:
        out.append(f"orphan: {', '.join(steps['orphan'])}（未选中，保留未删）")
    return out


def _parse_selection(text: str, names: list[str]) -> list[str]:
    answer = (text or "").strip()
    if not answer:
        return []
    if answer.lower() == "none":
        return []
    if answer.lower() == "all":
        return list(names)
    picked = []
    for token in answer.replace("，", ",").split(","):
        token = token.strip()
        if not token:
            continue
        if not token.isdigit():
            raise SystemExit(f"无效编号：{token}（用 1,4,7 / all / none）")
        index = int(token) - 1
        if not 0 <= index < len(names):
            raise SystemExit(f"编号越界：{token}（共 {len(names)} 项）")
        picked.append(names[index])
    return picked


def _interactive(names: list[str]) -> list[str]:
    for index, name in enumerate(names, start=1):
        print(f"{index:>3}  {name}")
    print("选择规则：1,4,7 / all / none")
    return _parse_selection(input("> "), names)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rules_init",
        description="Copy selected rules from rules/candidates into "
                    "rules/active. Re-running never overwrites local edits "
                    "and never deletes a file.")
    parser.add_argument("--list", action="store_true",
                        help="list the candidate pool; writes nothing")
    parser.add_argument("--rules", default=None,
                        help="comma-separated candidate names")
    parser.add_argument("--all", action="store_true", help="select every candidate")
    parser.add_argument("--dry-run", action="store_true", help="write nothing")
    parser.add_argument("--force", action="store_true",
                        help="overwrite a differing active copy")
    parser.add_argument("--root", default=None,
                        help="rules root (default: RULES_ROOT, else <repo>/rules)")
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    root = Path(args.root).expanduser() if args.root else default_root()
    try:
        pool = candidate_files(root)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.list:
        print(format_listing(pool))
        return 0
    names = list(pool) if args.all else None
    if names is None and args.rules:
        names = [n.strip() for n in args.rules.replace("，", ",").split(",")
                 if n.strip()]
    if names is None:
        names = _interactive(list(pool))
    try:
        steps = plan(root, names, force=args.force)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not args.dry_run:
        apply(steps)
    print(summary(steps) + (" dry_run=1" if args.dry_run else ""))
    for line in hints(steps):
        print("  " + line)
    blocked = len(steps["conflict"]) + len(steps["stale"])
    return 1 if blocked and not args.force else 0


if __name__ == "__main__":
    raise SystemExit(main())
