"""The two path rules every declared path in ``agent_config`` shares.

Split out of ``agent_config`` (#140) because the same two rules were spelled
out twelve times under twelve docstrings, and the modules that grew latest were
the ones that got the rule wrong. One definition, one place to change.

Both helpers are pure: they take the raw string a config layer answered and
return the path a caller may use. Reading which layer answered is
``agent_config.get``'s job, and it stays there.

The two shapes:

* :func:`declared` -- "``""`` means not configured; a value is passed through
  after ``expanduser``". Used for paths that are used verbatim (a file, a
  command, a table).
* :func:`anchored` -- the same, plus "a relative value resolves against **this
  checkout**, never the process working directory". Used for directories the
  process writes into: a relative value resolved against the cwd means the same
  command run from two places writes two roots (P0-1 #98).
"""
from __future__ import annotations

import os
from pathlib import Path


def declared(raw) -> str:
    """``""`` when nothing was configured, else the path with ``~`` expanded."""
    text = str(raw or "").strip()
    return os.path.expanduser(text) if text else ""


def anchored(raw, checkout: str | Path) -> str:
    """``""`` when nothing was configured, else an absolute path.

    An absolute value passes through unchanged. A relative one resolves against
    ``checkout`` -- the anchor is chosen by **value shape**, not by which layer
    answered: "a relative path means relative to this checkout" is a path rule,
    so ``tmp/x`` from the environment and ``tmp/x`` from the file must name one
    directory. A caller that wants working-directory semantics passes an
    absolute path.
    """
    text = declared(raw)
    if not text:
        return ""
    path = Path(text)
    if not path.is_absolute():
        path = Path(checkout) / path
    return str(path.resolve())
