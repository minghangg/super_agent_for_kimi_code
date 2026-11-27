"""One full-suite run per machine, and only from a role allowed to run it.

Why this exists (2026-11-25): a worker ran ``pytest tests/`` on the strength of
one stale line in a guide, while a scout was already running the same suite.
Five pytest processes competed for the same temp directories and runtime files;
none of them finished. The rule "worker runs L0/L1, full suite belongs to QA and
the supervisor" existed in the charters, but nothing executed it, so nothing
stopped the duplicate.

Two checks, both cheap and both fail loud:

1. **Role gate.** ``SAFKC_SESSION_KEY`` names the caller. A ``wk-`` key may not
   start the full suite. No key at all (a plain checkout, CI, a human shell) is
   allowed -- this gate is about the agent fleet, not about developers.
2. **Single-runner lock.** A cross-process file lock keyed on the suite, so two
   full runs on one machine queue or refuse instead of interleaving.

Both are opt-in from the caller: nothing here changes how pytest behaves on its
own. ``tests/conftest.py`` installs them for this repo; other repos call
``guard()`` from their own conftest.

The guard never decides that a run "passed" -- it only refuses to start a run
that would fight another one.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .session_map_store import FileLock

#: Roles that may start the full suite. Mirrors ``rules/charters/worker.md`` §3
#: (worker: L0/L1 only) and ``rules/charters/qa.md`` §3 (full run: phase 3).
ALLOWED_PREFIXES = ("qa-", "supervisor-", "steward", "auditor",
                    "reviewer", "scout", "planner", "analyst")

#: Roles that may never start it. Kept explicit so a new ``wk-``-like prefix
#: does not silently fall through the allow list.
DENIED_PREFIXES = ("wk-", "pm-")

#: How long to wait for a competing full run before refusing, in seconds.
DEFAULT_WAIT_S = 0.0

ENV_SESSION_KEY = "SAFKC_SESSION_KEY"
ENV_LOCK_PATH = "SAFKC_FULL_SUITE_LOCK"
ENV_SKIP = "SAFKC_SKIP_FULL_SUITE_GUARD"
#: Set by pytest-xdist in every worker process.
WORKER_ENV = "PYTEST_XDIST_WORKER"


class FullSuiteRefused(RuntimeError):
    """Raised when a run must not start. Message names the reason and the fix."""


def _session_key() -> str:
    return (os.environ.get(ENV_SESSION_KEY) or "").strip()


def _role_allowed(key: str) -> bool:
    """True when ``key`` may run the full suite; an empty key always may."""
    if not key:
        return True
    lowered = key.lower()
    for prefix in DENIED_PREFIXES:
        if lowered.startswith(prefix):
            return False
    for prefix in ALLOWED_PREFIXES:
        if lowered.startswith(prefix):
            return True
    # Unknown prefix: allow, and let the single-runner lock do the real work.
    # Denying unknown names would break humans and CI, which carry no key.
    return True


def default_lock_path() -> Path:
    """Lock beside the repo's runtime state, not inside a test temp dir."""
    override = (os.environ.get(ENV_LOCK_PATH) or "").strip()
    if override:
        return Path(override)
    root = Path(__file__).resolve().parent.parent
    return root / ".full_suite.lock"


def check_role(key: str | None = None) -> None:
    """Raise ``FullSuiteRefused`` when this role may not run the full suite."""
    key = _session_key() if key is None else key
    if _role_allowed(key):
        return
    raise FullSuiteRefused(
        "full suite refused: session key %r may not run it.\n"
        "  worker runs L0/L1 only (rules/charters/worker.md §3).\n"
        "  Full suite belongs to QA phase 3 and to the supervisor before a "
        "release (rules/charters/qa.md §3).\n"
        "  Run the targeted group instead, or ask the PM to dispatch QA."
        % key)


def guard(wait_s: float = DEFAULT_WAIT_S) -> "FullSuiteRun":
    """Check the role, then hold the single-runner lock for the run's duration.

    Returns a context manager. Use as::

        with guard():
            ...run the suite...
    """
    if (os.environ.get(ENV_SKIP) or "").strip() in ("1", "true", "yes"):
        return _NoopRun()
    check_role()
    return FullSuiteRun(default_lock_path(), wait_s=wait_s)


class FullSuiteRun:
    """Context manager holding the one-full-run-per-machine lock."""

    def __init__(self, lock_path: Path, wait_s: float = DEFAULT_WAIT_S):
        self.lock_path = Path(lock_path)
        self.wait_s = max(0.0, float(wait_s))
        self._lock: FileLock | None = None

    def __enter__(self) -> "FullSuiteRun":
        self._lock = FileLock(self.lock_path, timeout=self.wait_s)
        try:
            self._lock.__enter__()
        except Exception as exc:  # noqa: BLE001 - re-raised with the fix
            raise FullSuiteRefused(
                "full suite refused: another full run holds %s.\n"
                "  Two full runs on one machine compete for the same temp dirs "
                "and runtime files; measured 2026-11-25: five concurrent pytest "
                "processes, none finished.\n"
                "  Wait for the running one, or stop it first."
                % self.lock_path) from exc
        return self

    def __exit__(self, *exc_info):
        lock, self._lock = self._lock, None
        if lock is not None:
            lock.__exit__(*exc_info)
        return False


class _NoopRun:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def is_full_suite_run(args) -> bool:
    """True when the invocation runs the whole suite, not a chosen subset.

    ``pytest`` with no path, ``pytest tests/``, or ``pytest tests`` is a full
    run. ``pytest tests/test_x.py`` is targeted: it touches one file's runtime
    state, so it neither needs the lock nor is it a role violation. A worker
    running a targeted group is the normal case and must not be blocked.
    """
    paths = [str(a) for a in (args or []) if not str(a).startswith("-")]
    if not paths:
        return True
    for raw in paths:
        norm = raw.replace("\\", "/").rstrip("/")
        if norm in ("", ".", "tests", "./tests"):
            return True
        # ``tests/`` with a subdirectory still covers many files but is a
        # deliberate narrowing; treat only the bare suite root as full.
    return False


def install_pytest_hook() -> None:
    """Install hooks that enforce the guard, on full runs only.

    Called from ``tests/conftest.py``. A full run is refused before collection
    starts, so a refused run exits with a clear message instead of burning
    minutes and then failing on a lock collision deep in the suite. Targeted
    runs pass through untouched: they are what a worker is supposed to run.
    """
    try:
        import pytest  # noqa: F401
    except ImportError:  # pragma: no cover - pytest is a test dependency
        return

    state: dict = {}

    def pytest_configure(config):
        if not is_full_suite_run(config.args):
            return
        if os.environ.get(WORKER_ENV):
            # An xdist worker is a child of the process that already holds the
            # lock: asking for it again would deadlock the run it belongs to.
            # The parent's guard still makes "one full run per machine" true.
            state["run"] = _NoopRun()
            state["run"].__enter__()
            return
        state["run"] = guard(wait_s=0.0)
        state["run"].__enter__()

    def pytest_unconfigure(config):
        run = state.pop("run", None)
        if run is not None:
            run.__exit__(None, None, None)

    return pytest_configure, pytest_unconfigure
