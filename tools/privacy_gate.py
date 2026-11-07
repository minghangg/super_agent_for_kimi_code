"""Privacy gate: no private names, host paths or machine codenames in the repo.

Run: ``python tools/privacy_gate.py`` (exit 0 = clean).

The patterns are assembled from fragments at import time on purpose: if they
were written out whole, this file would itself trip every grep it performs.

``README.md`` predates this gate and documents the roadmap with milestone
labels; only its private-name and host-path patterns are enforced. Every other
tracked file is checked against the full pattern set.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXEMPT_MACHINE_CODES = {"README.md"}


def _joined(*parts: str) -> str:
    return "".join(parts)


PATTERNS = {
    "role name": re.compile(r"\b" + _joined("Re", "ed") + r"\b", re.IGNORECASE),
    "project name": re.compile(_joined("lai", "ke"), re.IGNORECASE),
    "private account": re.compile(_joined("yuan", "hai", "|", "ming", "hangg"),
                                  re.IGNORECASE),
    "host path": re.compile(
        r"[A-Za-z]:[/\\](?:" + _joined("github[/\\\\]Lai", "ke")
        + r"|" + _joined("Users[/\\\\]ming", "h") + r")", re.IGNORECASE),
    "machine code": re.compile(r"\bM[12]\b"),
}
NAME_ONLY = {"role name", "project name", "private account", "host path"}


def tracked_files() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT,
                             capture_output=True, check=True)
        names = [n for n in out.stdout.decode("utf-8", "replace").split("\0") if n]
    except (OSError, subprocess.CalledProcessError):
        names = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*")
                 if p.is_file() and ".git" not in p.parts]
    return [ROOT / n for n in names]


def scan() -> list[str]:
    hits = []
    for path in tracked_files():
        if not path.is_file() or path.suffix in {".pyc", ".png", ".jpg"}:
            continue
        rel = path.relative_to(ROOT).as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            hits.append(f"{rel}: unreadable ({exc})")
            continue
        for label, pattern in PATTERNS.items():
            if rel in EXEMPT_MACHINE_CODES and label not in NAME_ONLY:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    hits.append(f"{rel}:{lineno}: {label}: {line.strip()[:120]}")
    return hits


def main() -> int:
    hits = scan()
    files = len(tracked_files())
    if hits:
        print(f"privacy gate: {len(hits)} hit(s) across {files} files")
        for hit in hits:
            print("  " + hit)
        return 1
    print(f"privacy gate: clean ({files} tracked files, 0 hits)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
