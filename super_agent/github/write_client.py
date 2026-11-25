"""HTTP verbs for GitHub CLI writes (#134 step 4).

:mod:`super_agent.github.client` is the GET exit used by the engine. The CLI
also needs POST/PATCH/DELETE for create/comment/label/close. Those verbs live
here and deliberately reuse ``client.loop()``: a second loop would mean a
second connection pool and a second place where concurrency is decided.

The module imports aiohttp through :mod:`super_agent.github.client`'s dependency
but never re-implements session or semaphore policy. It keeps ``issue_cli``'s
two historical error shapes: :func:`request` returns status and parsed body for
``_call_raw``, while :func:`request_or_exit` raises ``SystemExit`` with the
original ``"GitHub API <status> <method> <path>"`` text for ``_call``/``_file_get``.
"""
from __future__ import annotations

import asyncio
import concurrent.futures

import aiohttp

from . import client

DETAIL_CHARS = 400


async def _once(url: str, method: str, headers: dict, payload: dict | None,
                timeout_s: float) -> tuple[int, dict | list | None, str]:
    session = await client._state(int(client.policy()["max_concurrent"]))
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    data = None if payload is None else aiohttp.JsonPayload(payload)
    async with client._SEMAPHORE, session.request(
            method, url, headers=headers, data=data, timeout=timeout) as resp:
        raw = (await resp.read()).decode("utf-8", "replace")
        body = None
        if raw.strip():
            try:
                body = client._parse(raw, url)
            except client.HttpError:
                body = None
        return int(resp.status), body, raw[:DETAIL_CHARS]


def request(url: str, *, method: str = "GET", headers: dict | None = None,
            payload: dict | None = None, timeout_s: float = 30.0) -> dict:
    """``{"_status": int, "body": ..., "error": str}`` — never raises on status."""
    future = asyncio.run_coroutine_threadsafe(
        _once(url, method, dict(headers or {}), payload, float(timeout_s)),
        client.loop())
    try:
        status, body, detail = future.result(float(timeout_s) * 3 + 5.0)
    except concurrent.futures.TimeoutError:
        future.cancel()
        return {"_status": 0, "body": None, "error": "timeout"}
    except Exception as exc:  # noqa: BLE001  CLI reports transport failures
        return {"_status": 0, "body": None,
                "error": f"{type(exc).__name__}: {exc}"[:DETAIL_CHARS]}
    return {"_status": status, "body": body,
            "error": detail if status >= 400 else ""}


def request_or_exit(url: str, *, method: str = "GET", path: str = "",
                    headers: dict | None = None, payload: dict | None = None,
                    timeout_s: float = 30.0):
    """Return the parsed body, or raise the CLI's historical ``SystemExit``.

    A **transport** failure (``_status == 0``: proxy down, timeout, DNS) raises
    exactly like an HTTP error does. Returning ``{}`` there was the P0-2 fault:
    the caller's success branch ran, printed ``commented … id=None`` and wrote a
    ledger row for a comment that was never sent. ``urllib`` raised ``URLError``
    before this rewrite, and the behaviour is restored rather than relaxed.
    """
    out = request(url, method=method, headers=headers, payload=payload,
                  timeout_s=timeout_s)
    if out["_status"] == 0:
        raise SystemExit(f"GitHub API 请求未完成（{method} {path or url}）："
                         f"{out['error']}")
    if out["_status"] >= 400:
        raise SystemExit(f"GitHub API {out['_status']} {method} {path or url}"
                         f"：{out['error']}")
    return out["body"] if out["body"] is not None else {}
