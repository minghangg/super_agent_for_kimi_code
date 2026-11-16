"""GitHub Issue comment entry point for this repository: a shell, not a copy.

Every role shares one GitHub token, so the comment's author login cannot say
who wrote a verdict. The fix (issue #87) is a marker the writer appends itself::

    [PM] 可关单 #42 <!-- verdict: ready -->
    <!-- session: pm-demo -->

The token goes on **its own line** because every verdict marker is anchored at
the end of its line; appending it inline hides the verdict from the parser.

**The implementation is not here.** The policy table, the role prefixes, the
project registry, the ledger and the HTTP calls all live in the workbench's
``tools/gh_issue.py``, and this file loads that module and calls its
``main()``. Nothing about *who may comment* or *what prefix a role gets* is
restated here - a second copy of that table is how the two entry points start
disagreeing about permissions.

Where the workbench is comes from ``SAFKC_BRIDGE_ROOT`` and nowhere else: this
repository is the open one, so a private checkout path must not be written into
it. A missing or wrong value is an error, never a guess - loading whatever
``gh_issue`` happens to sit on ``sys.path`` is how a shell silently runs the
wrong policy.

Usage::

    set SAFKC_BRIDGE_ROOT=<workbench checkout>
    python tools/gh_issue.py comment --role pm --project <project> \
        --issue 87 --session-key pm-demo --body "probe" --dry-run
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

# The environment variable that names the checkout holding ``tools/gh_issue.py``.
ROOT_ENV = "SAFKC_BRIDGE_ROOT"

# The marker the engine reads back out of the body (``markers.read_session_token``).
SESSION_MARKER = "<!-- session: {key} -->"

# The one subcommand this shell adds anything to. Every other subcommand is
# handed to the implementation untouched: adding ``--session-key`` there would
# restate a rule that belongs to the implementation.
COMMENT = "comment"


class BridgeError(RuntimeError):
    """The shell cannot reach the implementation, or was called wrongly."""


def impl_root() -> Path:
    """The workbench checkout, from ``SAFKC_BRIDGE_ROOT``.

    No default. A guess would be a private path written into an open
    repository, and a wrong guess would run another repository's policy.
    """
    raw = (os.environ.get(ROOT_ENV) or "").strip()
    if not raw:
        raise BridgeError(
            f"{ROOT_ENV} is not set. It must name the checkout that holds "
            "tools/gh_issue.py; this repository does not carry that path.")
    root = Path(raw).expanduser()
    if not (root / "tools" / "gh_issue.py").is_file():
        raise BridgeError(
            f"{ROOT_ENV}={root} has no tools/gh_issue.py")
    return root


def load_impl(root: Path):
    """Import ``<root>/tools/gh_issue.py`` as a module and return it.

    ``spec_from_file_location`` is used rather than ``sys.path`` plus a bare
    ``import gh_issue`` for two reasons:

    * the path is the one this shell resolved, so a ``gh_issue`` elsewhere on
      ``sys.path`` (or one already imported under that name) cannot answer;
    * the module keeps its own ``__file__``, so its ``WORKBENCH`` - which it
      derives from that path - still points at its own checkout. That derivation
      is what keeps the implementation single-sourced.

    The cost is that the module is registered under a private name and is not
    importable as ``gh_issue``; nothing needs it to be. ``spec_from_file_location``
    also yields ``__package__ == ""``, which is why the implementation must keep
    using absolute imports - it already inserts its own ``tools`` directory.
    """
    target = root / "tools" / "gh_issue.py"
    spec = importlib.util.spec_from_file_location("_safkc_gh_issue_impl", target)
    if spec is None or spec.loader is None:
        raise BridgeError(f"cannot load {target}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
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


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    is_comment = COMMENT in argv
    argv, key = take_option(argv, "--session-key")
    key = (key or "").strip()
    if key and not is_comment:
        raise BridgeError(
            "--session-key applies to comment: a session key names who wrote a "
            "verdict, and only comment puts a body on the issue.")
    # Resolved before anything is printed or composed: a shell that previews a
    # body and *then* says it cannot reach the implementation reads as success.
    root = impl_root()
    if is_comment and key:
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
    elif is_comment:
        print("[shell] no --session-key: the comment will not name its session "
              "and the engine reads it as unattributed.")
    impl = load_impl(root)
    return impl.main(argv)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BridgeError as exc:
        print(f"[shell] {exc}", file=sys.stderr)
        raise SystemExit(2)
