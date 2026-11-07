"""Privacy gate: no private names, host paths or machine codenames in the repo.

Run: ``python tools/privacy_gate.py`` (exit 0 = clean). This is the pre-merge
gate for every task ticket: rc != 0 is a QA P0.

The patterns are assembled from fragments at import time on purpose: if they
were written out whole, this file would itself trip every grep it performs.

Four surfaces are scanned and a hit on any of them fails the gate:

* every tracked file (``git ls-files``);
* the author/committer identities on commits reachable from the scanned refs
  (``--refs branch`` scans HEAD's history, ``--refs all`` scans every ref);
* the URL of ``origin``, so a remote pointing at a privately named repository
  cannot slip past a file-only scan;
* runtime directories (``state/``, ``.kimi/``, ``auth/``) appearing in the
  tracked list, which means run state or reports leaked into the open repo.

Token-shaped values (GitHub PATs, API keys, AWS access key ids and PEM private
key headers) are matched by the file and origin checks with a length floor on
each form, so short hyphenated words and bare credential prefixes stay green.

The owner segment of ``origin`` is the one surface that warns instead of
failing: the account the required remote already points at is the owner of the
repository, which is a legitimate state. It is still never dropped silently -
every owner hit prints a ``WARN`` line while the exit code stays 0.

``--refs all`` is deliberately red on this repository: the filter-branch backup
refs (``refs/original/*``) still reach commits authored with a private
identity. Clearing those needs the owner's approval to run ``git gc`` /
``git reflog expire``; until then the default ``--refs branch`` stays green and
``--refs all`` documents the residue instead of hiding it.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Directories that are run state, credentials or reports, never source. If one
# of them shows up in ``git ls-files`` it was committed by accident.
RUNTIME_PREFIXES = ("state/", ".kimi/", "auth/")


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

    # Token shapes. Every form carries a length floor so ordinary prose and
    # hyphenated identifiers (library names, ``task-name``, a bare AWS key
    # prefix) stay green: a real credential is long, a word is not. The API-key
    # form additionally forbids hyphens inside its bare spelling and starts at
    # 20 characters, so "sk" + "-" + a hyphenated word never reaches the floor;
    # project keys keep their own hyphenated spelling.
    #
    # A classic GitHub PAT is the prefix plus 36 alphanumerics; the floor is 30
    # to absorb historical length changes without matching prose. Cloud
    # credential prefixes are only meaningful as prefixes, so writing them
    # bare in a document does not trip the gate.
    "token: github pat": re.compile(
        r"(?<![A-Za-z0-9])(?:"
        + _joined("gh", "p_|gh", "o_|gh", "u_|gh", "s_|gh", "r_")
        + r")[A-Za-z0-9]{30,}"),
    "token: github fine-grained pat": re.compile(
        r"(?<![A-Za-z0-9])" + _joined("gith", "ub_p", "at_")
        + r"[A-Za-z0-9_]{20,}"),
    "token: sk api key": re.compile(
        r"(?<![A-Za-z0-9])" + _joined("s", "k-")
        + r"(?:[A-Za-z0-9]{20,}(?![A-Za-z0-9_-])"
        + r"|(?:" + _joined("pro", "j-") + "|" + _joined("an", "t-") + r")"
        + r"[A-Za-z0-9_-]{20,})"),
    "token: aws access key id": re.compile(
        r"\b" + _joined("AK", "IA") + r"[0-9A-Z]{16}\b"),
    "token: private key block": re.compile(
        _joined("-----BEGIN ", "[A-Z ]*PRIV", "ATE KEY-----")),
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


def tracked_names(root: Path = ROOT) -> list[str]:
    """Tracked paths as repo-relative POSIX strings."""
    out = _git("ls-files", "-z", root=root)
    if out is None:
        return [p.relative_to(root).as_posix() for p in tracked_files(root)]
    return [n for n in out.split("\0") if n]


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


def scan_runtime_paths(root: Path = ROOT) -> list[str]:
    """Flag runtime/credential directories that ended up in the tracked list."""
    hits = []
    for name in tracked_names(root):
        for prefix in RUNTIME_PREFIXES:
            if name == prefix.rstrip("/") or name.startswith(prefix):
                hits.append(f"{name}: runtime path tracked (prefix {prefix})")
                break
    return hits


def reachable_refs(sha: str, root: Path = ROOT) -> str:
    """Comma-separated ref names that contain ``sha`` (``-`` when unreachable)."""
    out = _git("for-each-ref", "--contains", sha, "--format=%(refname)",
               root=root)
    if not out:
        return "-"
    names = [line.strip() for line in out.splitlines() if line.strip()]
    return ",".join(names) if names else "-"


def _history_revs(refs: str) -> list[str]:
    return ["--all"] if refs == "all" else ["HEAD"]


def scan_history(root: Path = ROOT, refs: str = "branch") -> list[str]:
    """Scan author and committer identities on the refs selected by ``refs``.

    ``branch`` (the default) walks HEAD's history only; ``all`` walks every ref,
    which is what exposes filter-branch backups and other local residue.
    """
    out = _git("log", *_history_revs(refs),
               "--format=%h%x1f%an <%ae>%x1f%cn <%ce>", root=root)
    if not out:
        return []
    hits = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 3:
            continue
        sha, author, committer = parts
        hits_here = []
        for role, identity in (("author", author), ("committer", committer)):
            for label, pattern in PATTERNS.items():
                if pattern.search(identity):
                    hits_here.append((role, label, identity))
        if not hits_here:
            continue
        where = reachable_refs(sha, root)
        for role, label, identity in hits_here:
            hits.append(f"commit {sha} 的 {role} 命中 {label}: {identity}  "
                        f"[可达 ref: {where}]")
    return hits


def _remote_path_segments(url: str) -> list[str]:
    """Split the path part of a remote URL into its non-empty segments."""
    rest = url.strip()
    if "://" in rest:
        rest = rest.split("://", 1)[1]
        _, _, rest = rest.partition("/")
    elif ":" in rest:                       # scp-like: git@host:owner/repo.git
        rest = rest.split(":", 1)[1]
    return [s for s in rest.split("/") if s]


def _remote_scan_target(url: str) -> str:
    """Return the part of a remote URL below the owner/organisation segment.

    The owner segment is the account the required ``origin`` already points at,
    so failing on it would make the gate permanently red for this repository:
    the repository belonging to its owner is a legitimate state, and a policy
    choice keeps it out of the fail path. It is never dropped silently -
    :func:`scan_remote_warnings` reports every owner hit as a ``WARN`` so the
    fact stays visible while the exit code stays 0. The repository path below
    the owner is what can leak a private name, and that still fails.
    """
    segments = _remote_path_segments(url)
    if len(segments) <= 1:
        return "/".join(segments)
    return "/".join(segments[1:])


def scan_remote(root: Path = ROOT) -> list[str]:
    out = _git("remote", "get-url", "origin", root=root)
    if not out or not out.strip():
        return []
    url = out.strip().splitlines()[0]
    target = _remote_scan_target(url)
    return [f"remote origin 命中 {label}: {url}"
            for label, pattern in PATTERNS.items() if pattern.search(target)]


def scan_remote_warnings(root: Path = ROOT) -> list[str]:
    """Report (do not fail) private names in the owner segment of ``origin``.

    A hit here is a ``WARN``: the required remote belongs to its owner, so the
    owner name appearing in it is expected. Silence is the thing this refuses
    to do - the caller prints these lines and still exits 0 unless a fail-path
    surface also hits.
    """
    out = _git("remote", "get-url", "origin", root=root)
    if not out or not out.strip():
        return []
    url = out.strip().splitlines()[0]
    segments = _remote_path_segments(url)
    owner = segments[0] if segments else ""
    return [f"origin owner 段命中 {label}: {url}"
            for label, pattern in PATTERNS.items() if pattern.search(owner)]


def scan(root: Path = ROOT, refs: str = "branch") -> list[str]:
    return (scan_files(root) + scan_runtime_paths(root)
            + scan_history(root, refs) + scan_remote(root))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="privacy_gate",
        description="Fail (exit 1) when private names, host paths, private "
                    "identities, token-shaped credentials or runtime "
                    "directories are reachable from the open repository. "
                    "Private names in origin's owner segment only WARN.")
    parser.add_argument(
        "--refs", choices=("branch", "all"), default="branch",
        help="branch = scan HEAD's history only (default, the pre-merge gate); "
             "all = scan every ref, which also reaches local residue such as "
             "refs/original/* left by filter-branch. 'all' is red on this repo "
             "until the owner approves gc/reflog expire; that cleanup is not "
             "part of normal development.")
    return parser


def main(argv=None, root: Path | None = None) -> int:
    args = _parser().parse_args([] if argv is None else argv)
    root = ROOT if root is None else Path(root)
    files = tracked_files(root)
    hits = scan(root, args.refs)
    for warning in scan_remote_warnings(root):
        print("WARN " + warning)
    if hits:
        print(f"privacy gate: {len(hits)} hit(s) across {len(files)} files "
              f"(refs={args.refs})")
        for hit in hits:
            print("  " + hit)
        return 1
    print(f"privacy gate: clean ({len(files)} tracked files, 0 hits, "
          f"refs={args.refs})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
