"""GitHub Issue comment entry point for this repository: a shell, not a copy.

Every role shares one GitHub token, so the comment's author login cannot say
who wrote a verdict. The fix (issue #87) is a marker the writer appends itself::

    [PM] 可关单 #42 <!-- verdict: ready -->
    <!-- session: pm-demo -->

The token goes on **its own line** because every verdict marker is anchored at
the end of its line; appending it inline hides the verdict from the parser.

**The implementation is not here.** The policy table, the role prefixes, the
project registry, the ledger and the HTTP calls all live in
``super_agent/issue_cli.py``, and this file loads that module and calls its
``main()``. Nothing about *who may comment* or *what prefix a role gets* is
restated here - a second copy of that table is how two entry points start
disagreeing about permissions.

Where the implementation is comes from this file's own location and nowhere
else: ``<checkout>/super_agent/issue_cli.py``, where the checkout is the one
this shell sits in. No environment variable, so no private path is written into
the open repository and no wrongly set variable can point the shell at another
checkout's policy. The loaded module is then compared against that exact path,
because a same-named package already installed in the process would otherwise
answer the import silently.

A ``comment`` without ``--session-key`` is refused here, before the
implementation is even loaded: a verdict that does not name its session is
unattributed, and the engine reads it as such (#87). The refusal lives in the
shell so it survives any change of implementation.

Usage::

    python tools/gh_issue.py comment --role pm --project <project> \\
        --issue 87 --session-key pm-demo --body "probe" --dry-run
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

# The retired variable that used to name the checkout holding the
# implementation. Assembled from fragments so that neither the shell's source
# nor a grep of it carries a live name for a mechanism that no longer works.
RETIRED_ENV = "SAFKC_BRIDGE_" + "ROOT"

# The marker the engine reads back out of the body (``markers.read_session_token``).
SESSION_MARKER = "<!-- session: {key} -->"

# The one subcommand this shell adds anything to. Every other subcommand is
# handed to the implementation untouched: adding ``--session-key`` there would
# restate a rule that belongs to the implementation.
COMMENT = "comment"

# Where the implementation lives, relative to this checkout.
IMPL_PACKAGE = "super_agent"
IMPL_MODULE = "super_agent.issue_cli"
IMPL_FILE = Path(IMPL_PACKAGE) / "issue_cli.py"


class BridgeError(RuntimeError):
    """The shell cannot reach the implementation, or was called wrongly."""


def checkout() -> Path:
    """The checkout this shell sits in: ``tools/gh_issue.py`` -> its parent's parent."""
    return Path(__file__).resolve().parents[1]


def load_impl():
    """Import ``<checkout>/super_agent/issue_cli.py`` and return it.

    The checkout is put first on ``sys.path`` so the import resolves to this
    shell's own module. The result is then checked against the exact path we
    meant to load: an editable install, a copy left on ``sys.path``, or an
    earlier import in the same process would all answer ``import_module`` with
    a module from somewhere else, and running another checkout's policy is the
    one failure this shell exists to prevent. Both paths are printed on
    failure - "wrong module" alone leaves the reader unable to see which one
    answered.
    """
    root = checkout()
    expected = (root / IMPL_FILE).resolve()
    sys.path.insert(0, str(root))
    try:
        module = importlib.import_module(IMPL_MODULE)
    except Exception as exc:            # noqa: BLE001  any import failure is a refusal
        raise BridgeError(
            f"cannot load {expected}: {type(exc).__name__}: {exc}") from None
    actual = Path(getattr(module, "__file__", "") or "").resolve()
    if actual != expected:
        raise BridgeError(
            f"issue_cli was loaded from another checkout: actual={actual} "
            f"expected={expected} (another copy of the package is on sys.path)")
    return module


def take_option(argv: list[str], name: str) -> tuple[list[str], str | None]:
    """Remove ``--name <value>`` / ``--name=<value>`` and return the value.

    Absent means ``None`` so "not given" stays distinguishable from
    "given empty", which is what lets a caller omit the key on purpose.
    """
    rest: list[str] = []
    value: str | None = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == name:
            if i + 1 >= len(argv):
                raise BridgeError(f"{name} needs a value")
            value = argv[i + 1]
            i += 2
            continue
        if arg.startswith(name + "="):
            value = arg.split("=", 1)[1]
            i += 1
            continue
        rest.append(arg)
        i += 1
    return rest, value


def compose_body(argv: list[str], key: str) -> list[str]:
    """Append the session marker as the body's last line and return new argv.

    ``--body-file`` is resolved here rather than edited: the caller's file is
    theirs, and writing a marker into it would make the same file carry a
    different body on the next call. The content is read, the marker appended,
    and the result passed as ``--body``.

    An empty body is refused **before** the marker is added. The
    implementation refuses one too ("评论正文为空"), but by the time the marker
    is appended the body is no longer empty, so that guard would never fire and
    a marker-only comment would be posted.
    """
    argv, body = take_option(argv, "--body")
    argv, body_file = take_option(argv, "--body-file")
    if body_file is not None:
        try:
            text = Path(body_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise BridgeError(f"cannot read --body-file {body_file}: {exc}") from None
    else:
        text = body or ""
    if not text.strip():
        raise BridgeError("the comment body is empty: --body / --body-file is "
                          "required, and a session marker is not a body")
    marker = SESSION_MARKER.format(key=key)
    composed = text.rstrip("\n") + "\n" + marker
    return argv + ["--body", composed]


def _warn_retired_env() -> None:
    """Say that the old variable no longer does anything, instead of ignoring it."""
    if (os.environ.get(RETIRED_ENV) or "").strip():
        print(f"[shell] {RETIRED_ENV} 已失效（见 #96）：实现固定取本检出内的 "
              f"{IMPL_FILE.as_posix()}，该变量不再改变加载目标。", file=sys.stderr)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    is_comment = COMMENT in argv
    argv, key = take_option(argv, "--session-key")
    key = (key or "").strip()
    if key and not is_comment:
        raise BridgeError(
            "--session-key applies to comment: a session key names who wrote a "
            "verdict, and only comment puts a body on the issue.")
    # Refused before the implementation is loaded and before anything is
    # printed: a permissive implementation must not be able to make an
    # unattributed comment look like a successful one. A missing implementation
    # is reported in the same breath - two faults, two facts, so that fixing
    # one does not hide the other.
    if is_comment and not key:
        expected = (checkout() / IMPL_FILE).resolve()
        missing = f" (also: {expected} does not exist)" if not expected.is_file() else ""
        raise BridgeError(
            "comment needs --session-key: without it the engine reads the "
            f"verdict as unattributed (#87). Pass --session-key <your key>.{missing}")
    _warn_retired_env()
    if is_comment:
        if "--state-dir" in argv:
            # The implementation strips --state-dir itself; taking it apart
            # here would have to know its value. Refuse instead of guessing.
            raise BridgeError(
                "--state-dir and --session-key together are not supported: "
                "pass --state-dir with the implementation's own entry point.")
        argv = compose_body(argv, key)
        # The body is what this shell is responsible for, so it prints the body
        # it is about to send - including under --dry-run, where the
        # implementation only prints the payload's key names.
        print("[shell] comment body:")
        for line in argv[argv.index("--body") + 1].splitlines():
            print("  " + line)
    impl = load_impl()
    return impl.main(argv)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BridgeError as exc:
        print(f"[shell] {exc}", file=sys.stderr)
        raise SystemExit(2)
