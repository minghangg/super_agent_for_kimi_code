"""The single HTTP exit for GitHub REST calls (#134 step 2).

Every GitHub request the engine makes goes through :func:`get`. Nothing else in
the package opens a socket, so "how many requests are in flight", "which loop
they run on" and "was this answered from cache" are decided in one place.

Four properties, each with a naive version that looks correct:

* **one resident loop.** ``asyncio.run`` per request creates and tears down a
  loop -- and with it the connection pool -- on every call: no keep-alive, and
  a second loop per process makes cancellation unreliable. The loop here is
  created once, runs in a daemon thread, and serves every call.
* **bounded concurrency.** ``asyncio.Semaphore(N)`` caps callers and
  ``TCPConnector(limit=N)`` caps sockets; they are deliberately equal, so a
  burst of threads waits instead of opening N connections.
* **``trust_env=True``.** The corporate proxy lives in the environment. A
  session that ignores it works on one laptop and hangs on another.
* **ETag revalidation.** A 200 carrying ``ETag`` is stored; the next request
  sends ``If-None-Match``, and a 304 returns the **cached body and headers**.
  The headers matter as much as the body: ``Link`` is how the pagination walk
  learns the page count, and a 304 carries no ``Link``.

HTTP only: this module parses JSON, never interprets it, and has no mutating
verb -- the source is read-only by design.
"""
from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import json
import threading
import time
from dataclasses import dataclass, field

import aiohttp

from .. import config

#: Detail text kept from an error body. The old ``urlopen`` path kept 200
#: characters; enough to name a cause, short enough to keep a log readable.
DETAIL_CHARS = 200


def _header(headers: dict, name: str) -> str:
    """Case-insensitive header read; "" when absent.

    ``aiohttp``'s ``CIMultiDict`` normalises the spelling it hands to
    ``dict(...)`` (``ETag`` arrives as ``Etag``), so an exact ``.get("ETag")``
    silently finds nothing and every response looks uncacheable.
    """
    wanted = name.lower()
    for key, value in (headers or {}).items():
        if str(key).lower() == wanted:
            return str(value)
    return ""


class HttpError(RuntimeError):
    """A failed request: transport, HTTP status, or an unparsable body.

    ``status`` is the HTTP status and ``0`` when none was received, so a caller
    can tell "GitHub said 404" from "we never reached GitHub" without parsing
    the message. ``kind`` names the exception class for the second case.
    """

    def __init__(self, detail: str, *, status: int = 0, kind: str = "",
                 url: str = ""):
        self.detail = str(detail)[:DETAIL_CHARS]
        self.status = int(status or 0)
        self.kind = kind or (f"HTTP {self.status}" if self.status else "HttpError")
        self.url = url
        if self.status:
            text = f"GitHub {self.status} on {url}: {self.detail}"
        else:
            text = f"{self.kind}: {self.detail}"
        super().__init__(text)


@dataclass(frozen=True)
class Response:
    """One parsed answer. ``from_cache`` is True only for a 304 revalidation."""

    body: object
    headers: dict
    status: int = 200
    from_cache: bool = False
    etag: str = ""


@dataclass
class _Cached:
    """A 200 worth revalidating: the raw text, its headers, and its ETag."""

    text: str
    headers: dict = field(default_factory=dict)
    etag: str = ""
    stored_at: float = 0.0


_POLICY: dict = {
    key: config.DEFAULTS["github"][key]
    for key in ("etag_cache", "local_cache_ttl_s", "max_concurrent")
}

_LOCK = threading.Lock()
_LOOP: asyncio.AbstractEventLoop | None = None
_THREAD: threading.Thread | None = None
_SESSION: aiohttp.ClientSession | None = None
_SEMAPHORE: asyncio.Semaphore | None = None
_SEMAPHORE_LIMIT = 0
#: Written and read on the loop thread only; ``reset`` clears it after joining.
_CACHE: dict[str, _Cached] = {}


def configure(github_cfg: dict | None = None) -> dict:
    """Adopt ``github`` config values; returns the policy actually in force.

    Values are normalised, not trusted: ``"max_concurrent": 0`` from a config
    file would otherwise build a session that can never connect. The next
    request rebuilds session and semaphore if the cap changed, so calling this
    while the client is in use is safe.
    """
    cfg = dict(github_cfg or {})
    defaults = config.DEFAULTS["github"]
    try:
        ttl = float(cfg.get("local_cache_ttl_s", _POLICY["local_cache_ttl_s"]))
    except (TypeError, ValueError):
        ttl = float(defaults["local_cache_ttl_s"])
    try:
        cap = int(cfg.get("max_concurrent", _POLICY["max_concurrent"]))
    except (TypeError, ValueError):
        cap = int(defaults["max_concurrent"])
    with _LOCK:
        _POLICY["etag_cache"] = bool(cfg.get("etag_cache", _POLICY["etag_cache"]))
        _POLICY["local_cache_ttl_s"] = max(0.0, ttl)
        _POLICY["max_concurrent"] = max(1, cap)
        return dict(_POLICY)


def policy() -> dict:
    with _LOCK:
        return dict(_POLICY)


def loop() -> asyncio.AbstractEventLoop:
    """The resident loop. Created once; never replaced while it runs."""
    global _THREAD
    with _LOCK:
        if _LOOP is not None and _LOOP.is_running():
            return _LOOP
        ready = threading.Event()

        def _run() -> None:
            global _LOOP
            running = asyncio.new_event_loop()
            asyncio.set_event_loop(running)
            _LOOP = running
            ready.set()
            try:
                running.run_forever()
            finally:
                running.close()

        _THREAD = threading.Thread(target=_run, name="github-http-loop",
                                   daemon=True)
        _THREAD.start()
        if not ready.wait(5.0):
            raise RuntimeError("github http loop did not start")
        assert _LOOP is not None
        return _LOOP


async def _state(limit: int) -> aiohttp.ClientSession:
    """The session and semaphore for ``limit``, built inside the loop.

    ``ClientSession`` binds to the running loop, so it can only be created from
    inside a coroutine -- building it in ``get`` would bind it to whichever loop
    happened to be current and fail on the next call.
    """
    global _SESSION, _SEMAPHORE, _SEMAPHORE_LIMIT
    if _SESSION is not None and not _SESSION.closed and _SEMAPHORE_LIMIT == limit:
        return _SESSION
    if _SESSION is not None and not _SESSION.closed:
        await _SESSION.close()
    _SESSION = aiohttp.ClientSession(
        trust_env=True, connector=aiohttp.TCPConnector(limit=limit))
    _SEMAPHORE = asyncio.Semaphore(limit)
    _SEMAPHORE_LIMIT = limit
    return _SESSION


async def _facts(limit: int) -> dict:
    session = await _state(limit)
    return {"trust_env": bool(session.trust_env),
            "connector_limit": int(session.connector.limit),
            "semaphore_limit": int(_SEMAPHORE_LIMIT),
            "closed": bool(session.closed)}


def session_facts() -> dict:
    """What the live session is actually configured with (checks and tests)."""
    cap = int(policy()["max_concurrent"])
    return asyncio.run_coroutine_threadsafe(_facts(cap), loop()).result(10.0)


def _fresh(url: str, ttl: float) -> _Cached | None:
    """The cached copy for ``url`` when it is younger than ``ttl``, else None."""
    entry = _CACHE.get(url)
    if entry is None:
        return None
    if not entry.etag or (time.monotonic() - entry.stored_at) > ttl:
        _CACHE.pop(url, None)
        return None
    return entry


def _parse(raw: str, url: str):
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise HttpError(str(exc), kind="ValueError", url=url) from None


async def _request(url, headers, timeout_s, pol) -> Response:
    session = await _state(int(pol["max_concurrent"]))
    cached = _fresh(url, float(pol["local_cache_ttl_s"])) if pol["etag_cache"] else None
    sent = dict(headers or {})
    if cached is not None:
        sent.setdefault("If-None-Match", cached.etag)
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    try:
        async with _SEMAPHORE, session.get(
                url, headers=sent, timeout=timeout) as resp:
            if resp.status == 304:
                if cached is None:
                    raise HttpError("304 without a cached copy", status=304,
                                    url=url)
                return Response(_parse(cached.text, url), dict(cached.headers),
                                304, True, cached.etag)
            raw = (await resp.read()).decode("utf-8", "replace")
            if resp.status >= 400:
                raise HttpError(raw, status=resp.status, url=url)
            body = _parse(raw, url)
            out_headers = dict(resp.headers)
            etag = _header(out_headers, "ETag")
            if pol["etag_cache"] and etag:
                _CACHE[url] = _Cached(raw, out_headers, etag, time.monotonic())
            return Response(body, out_headers, resp.status, False, etag)
    except HttpError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        raise HttpError(str(exc), kind=type(exc).__name__, url=url) from None


def get(url: str, *, headers: dict | None = None,
        timeout_s: float = 15.0) -> Response:
    """One GET. Blocks the calling thread until the request settles.

    The outer ``future.result`` deadline is a **dead-loop detector**, not the
    request deadline: ``aiohttp.ClientTimeout`` already bounds the request, and
    a call merely queued behind the semaphore has not timed out yet. Setting
    both to the same number would fail calls that were only waiting their turn.
    """
    pol = policy()
    future = asyncio.run_coroutine_threadsafe(
        _request(url, headers or {}, float(timeout_s), pol), loop())
    try:
        return future.result(float(timeout_s) * 3 + 5.0)
    except concurrent.futures.TimeoutError:
        future.cancel()
        raise HttpError(f"no response within {timeout_s}s",
                        kind="TimeoutError", url=url) from None


def reset() -> None:
    """Drop the cache and stop the loop; the next call starts a fresh one.

    Production never needs this -- it exists so a test can start from an empty
    cache and a stopped loop without depending on garbage collection order.
    """
    global _SESSION, _SEMAPHORE, _SEMAPHORE_LIMIT
    running = _LOOP is not None and _LOOP.is_running()
    if running and _SESSION is not None and not _SESSION.closed:
        try:
            asyncio.run_coroutine_threadsafe(
                _SESSION.close(), _LOOP).result(5.0)
        except (concurrent.futures.TimeoutError, RuntimeError):
            pass
    if running:
        _LOOP.call_soon_threadsafe(_LOOP.stop)
        if _THREAD is not None:
            _THREAD.join(5.0)
    _SESSION = None
    _SEMAPHORE = None
    _SEMAPHORE_LIMIT = 0
    _CACHE.clear()


atexit.register(reset)
