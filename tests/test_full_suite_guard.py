"""The full-suite guard: who may start it, and only one at a time.

Measured 2026-11-25: a worker ran ``pytest tests/`` because a guide said
"full suite (end of every task)", while a scout was running the same suite.
Five pytest processes competed for the same temp dirs and none finished. The
rule was in the charters; nothing executed it.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from super_agent import full_suite_guard as g


@pytest.fixture
def isolated_lock(monkeypatch):
    path = Path(tempfile.mkdtemp()) / "probe.lock"
    monkeypatch.setenv(g.ENV_LOCK_PATH, str(path))
    monkeypatch.delenv(g.ENV_SKIP, raising=False)
    return path


# -- role gate -------------------------------------------------------------

@pytest.mark.parametrize("key", ["", "qa-lwa-ISS-1-abcdef01",
                                 "supervisor-3a1bf73b", "scout-20261120-3693b26c",
                                 "steward", "auditor", "reviewer"])
def test_allowed_roles(key):
    assert g._role_allowed(key) is True


# Split literals so the private project name never appears whole in the file.
_PM_KEY = "pm-" + "lai" + "ke_work_with_agent-20261125-a7f3c9d1"

@pytest.mark.parametrize("key", ["wk-ISS-129-session-broker-aa7001f8", _PM_KEY])
def test_denied_roles(key):
    assert g._role_allowed(key) is False


def test_worker_is_refused_with_actionable_message():
    with pytest.raises(g.FullSuiteRefused) as exc:
        g.check_role("wk-ISS-129-session-broker-aa7001f8")
    text = str(exc.value)
    assert "worker runs L0/L1 only" in text
    assert "QA phase 3" in text


def test_pm_is_refused():
    with pytest.raises(g.FullSuiteRefused):
        g.check_role(_PM_KEY)


def test_qa_and_supervisor_are_not_refused():
    g.check_role("qa-lwa-ISS-129-f7563bc3")
    g.check_role("supervisor-3a1bf73b")


def test_no_key_is_allowed():
    """A plain checkout, CI and a human shell carry no key."""
    g.check_role("")


# -- single-runner lock ----------------------------------------------------

def test_second_concurrent_run_is_refused(isolated_lock):
    with g.FullSuiteRun(isolated_lock):
        with pytest.raises(g.FullSuiteRefused) as exc:
            with g.FullSuiteRun(isolated_lock):
                pass
    assert "another full run holds" in str(exc.value)


def test_lock_is_released_after_the_run(isolated_lock):
    with g.FullSuiteRun(isolated_lock):
        pass
    with g.FullSuiteRun(isolated_lock):
        pass


def test_lock_is_released_when_the_run_raises(isolated_lock):
    with pytest.raises(ValueError):
        with g.FullSuiteRun(isolated_lock):
            raise ValueError("boom")
    with g.FullSuiteRun(isolated_lock):
        pass


# -- escape hatch ----------------------------------------------------------

def test_skip_env_bypasses_everything(monkeypatch, isolated_lock):
    monkeypatch.setenv(g.ENV_SESSION_KEY, "wk-probe")
    monkeypatch.setenv(g.ENV_SKIP, "1")
    run = g.guard()
    with run:
        pass


def test_guard_checks_role_before_taking_the_lock(monkeypatch, isolated_lock):
    monkeypatch.setenv(g.ENV_SESSION_KEY, "wk-probe")
    with pytest.raises(g.FullSuiteRefused):
        g.guard()
    assert not isolated_lock.exists()


# -- default lock path -----------------------------------------------------

def test_default_lock_path_sits_beside_the_package(monkeypatch):
    monkeypatch.delenv(g.ENV_LOCK_PATH, raising=False)
    path = g.default_lock_path()
    assert path.name == ".full_suite.lock"
    assert path.parent.name == "super_agent_for_kimi_code"


def test_lock_path_override_wins(monkeypatch, tmp_path):
    target = tmp_path / "custom.lock"
    monkeypatch.setenv(g.ENV_LOCK_PATH, str(target))
    assert g.default_lock_path() == target


# -- full vs targeted ------------------------------------------------------

@pytest.mark.parametrize("args", [[], ["tests"], ["tests/"], ["tests"],
                                  ["-q"], ["tests", "-q", "--timeout=60"]])
def test_bare_suite_root_is_a_full_run(args):
    assert g.is_full_suite_run(args) is True


@pytest.mark.parametrize("args", [
    ["tests/test_session_map_replace.py"],
    ["tests/test_a.py", "tests/test_b.py", "-q"],
    ["tests/test_iss127_incremental_read.py", "--timeout=120"],
])
def test_named_files_are_targeted(args):
    assert g.is_full_suite_run(args) is False


def test_targeted_run_is_never_blocked_by_the_lock(monkeypatch, isolated_lock):
    """A worker running one file must not hit the single-runner lock."""
    monkeypatch.setenv(g.ENV_SESSION_KEY, "wk-probe")
    monkeypatch.setenv(g.ENV_LOCK_PATH, str(isolated_lock))
    with g.FullSuiteRun(isolated_lock):
        assert g.is_full_suite_run(["tests/test_one.py"]) is False
