"""Where the GitHub token comes from: environment first, then the configured file.

Why this module exists (#134 regression, 2026-11-25): the new ``github`` package
read ``os.environ["GITHUB_TOKEN"]`` and nothing else. The engine is started by a
scheduled task, whose environment has no such variable, so every authenticated
read became an anonymous one -- and GitHub answers an anonymous request for a
private repository with **404, not 403**. The engine read that as "this issue
cannot be read", paused the thread, and never processed another comment on it:
QA passes and PM escalations sat unread for hours while the engine reported
itself healthy.

``issue_cli`` already had the full chain (env, then the file named by
``agent_config``). This module is that chain, in one place, so the read path and
the write path cannot drift apart again.

The value is never printed. Callers that want to log which source won ask
:func:`token_origin`, which returns a name like ``"env"`` or a path, never the
secret.
"""
from __future__ import annotations

import os
from pathlib import Path

from .. import agent_config


class TokenMissing(RuntimeError):
    """No token in the environment and none readable from the configured file."""


def read_token(env_file: str | None = None) -> str:
    """The token value: ``$<env_name>`` first, then ``env_file`` or the configured file.

    Raises :class:`TokenMissing` rather than returning ``""``: an empty token
    silently downgrades every request to anonymous, which is the failure this
    module exists to prevent. A caller that genuinely wants anonymous access
    passes ``allow_empty=True``.
    """
    return _read(env_file, allow_empty=False)


def read_token_or_empty(env_file: str | None = None) -> str:
    """Like :func:`read_token`, but returns ``""`` when nothing is configured.

    For callers whose repository may legitimately be public: an anonymous read
    works there, so a missing token is not an error.
    """
    return _read(env_file, allow_empty=True)


def _read(env_file: str | None, *, allow_empty: bool) -> str:
    env_name, default_file = agent_config.token_source()
    value = (os.environ.get(env_name) or "").strip()
    if value:
        return value
    path = env_file or default_file
    if path:
        found = _from_file(path, env_name)
        if found:
            return found
    if allow_empty:
        return ""
    raise TokenMissing(
        f"找不到 {env_name}：环境变量为空，且配置文件里没有这一行"
        f"（配置项 github.token_file，当前 {path or '未配置'}）")


def token_origin(env_file: str | None = None) -> str:
    """``"env"``, the file path, or ``""`` -- never the value itself."""
    env_name, default_file = agent_config.token_source()
    if (os.environ.get(env_name) or "").strip():
        return "env"
    path = env_file or default_file
    if path and _from_file(path, env_name):
        return str(path)
    return ""


def _from_file(path: str, env_name: str) -> str:
    try:
        raw = Path(os.path.expanduser(path)).read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in raw.splitlines():
        if line.startswith(f"{env_name}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""
