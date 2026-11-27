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


def runtime_root(cfg: dict, override: str | None = None) -> tuple[Path, str]:
    """The engine's runtime root, and the note explaining the choice.

    One resolution chain, called by every process that reads what the engine
    wrote (#140): ``state_root`` -> the declared ``legacy_state_dir`` ->
    ``store.choose_root``. ``cli._build`` and ``at_serve`` used to each spell
    this out, and they drifted -- ``at_serve`` dropped the legacy branch and
    read an empty root while the engine wrote elsewhere, both silently.

    The legacy root is consulted **only when declared**. It used to default to
    the ``"state"`` sibling of the chosen root, a directory found by its name;
    that answered "no legacy data" for ever once a deployment moved its root to
    ``data``.

    Imports are deferred: ``store`` imports ``issuekey``, ``config`` is read by
    both, and this module is imported early by ``agent_config``.
    """
    from . import config as config_mod
    from . import store as store_mod

    root = config_mod.state_root(cfg, override)
    legacy = declared(cfg.get("legacy_state_dir"))
    if not legacy:
        return root, ""
    legacy_path = Path(legacy).resolve()
    if legacy_path == root:
        return root, ""
    return store_mod.choose_root(root, legacy=legacy_path)
