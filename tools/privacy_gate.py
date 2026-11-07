"""Privacy gate: no private names, host paths or machine codenames in the repo.

Run: ``python tools/privacy_gate.py`` (exit 0 = clean).

The patterns are assembled from fragments at import time on purpose: if they
were written out whole, this file would itself trip every grep it performs.

Three surfaces are scanned and a hit on any of them fails the gate:

* every tracked file (``git ls-files``);
* the author/committer identities on commits reachable from the current branch
  (``git log``; deliberately not ``--all``, because reflog and dangling objects
  are local residue rather than repository content);
* the URL of ``origin``, so a remote pointing at a privately named repository
  cannot slip past a file-only scan.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


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


def _git(*args: str, root: Path = ROOT) -> str | None:
    """Run git in ``root``; None when git is missing or the command fails."""
    try:
        out = subprocess.run(["git", *args], cwd=root, capture_output=True,
                             check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.decode("utf-8", "replace")


def tracked_files(root: Path = ROOT) -> list[Path]:
    out = _git("ls-files", "-z", root=root)
    if out is None:
        return [p for p in root.rglob("*")
                if p.is_file() and ".git" not in p.parts]
    return [root / n for n in out.split("\0") if n]


def scan_files(root: Path = ROOT) -> list[str]:
    hits = []
    for path in tracked_files(root):
        if not path.is_file() or path.suffix in {".pyc", ".png", ".jpg"}:
            continue
        rel = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            hits.append(f"{rel}: unreadable ({exc})")
            continue
        for label, pattern in PATTERNS.items():
            for lineno, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    hits.append(f"{rel}:{lineno}: {label}: {line.strip()[:120]}")
    return hits


def scan_history(root: Path = ROOT) -> list[str]:
    """Scan author and committer identities on the current branch's history."""
    out = _git("log", "--format=%h%x1f%an <%ae>%x1f%cn <%ce>", root=root)
    if not out:
        return []
    hits = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 3:
            continue
        sha, author, committer = parts
        for role, identity in (("author", author), ("committer", committer)):
            for label, pattern in PATTERNS.items():
                if pattern.search(identity):
                    hits.append(
                        f"commit {sha} 的 {role} 命中 {label}: {identity}")
    return hits


def _remote_scan_target(url: str) -> str:
    """Return the part of a remote URL below the owner/organisation segment.

    The owner segment is the account the required ``origin`` already points at,
    so scanning it would make the gate permanently red for this repository.
    The repository path below it is what can leak a private name.
    """
    rest = url.strip()
    if "://" in rest:
        rest = rest.split("://", 1)[1]
        _, _, rest = rest.partition("/")
    elif ":" in rest:                       # scp-like: git@host:owner/repo.git
        rest = rest.split(":", 1)[1]
    segments = [s for s in rest.split("/") if s]
    if len(segments) <= 1:
        return rest
    return "/".join(segments[1:])


def scan_remote(root: Path = ROOT) -> list[str]:
    out = _git("remote", "get-url", "origin", root=root)
    if not out or not out.strip():
        return []
    url = out.strip().splitlines()[0]
    target = _remote_scan_target(url)
    return [f"remote origin 命中 {label}: {url}"
            for label, pattern in PATTERNS.items() if pattern.search(target)]


def scan(root: Path = ROOT) -> list[str]:
    return scan_files(root) + scan_history(root) + scan_remote(root)


def main() -> int:
    files = tracked_files()
    hits = scan()
    if hits:
        print(f"privacy gate: {len(hits)} hit(s) across {len(files)} files")
        for hit in hits:
            print("  " + hit)
        return 1
    print(f"privacy gate: clean ({len(files)} tracked files, 0 hits)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
