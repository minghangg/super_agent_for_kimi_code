"""Read-mostly client for the local event bus daemon.

Protocol (``127.0.0.1:58800`` by default):

* ``GET /healthz``                -> ``{ok, ready, boot_id, seq, connected, ...}``
* ``GET /events?since=&wait=``    -> long-poll, returns events with ``seq > since``
* ``POST /events/inject``         -> append one ``event.*`` frame

The client never raises on a dead daemon: ``healthz()`` returns a dict with
``ok=False`` and an error string instead. Injection is the only write path and
the engine only calls it when dispatch is enabled.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request


class BusError(RuntimeError):
    pass


class BusClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 58800,
                 timeout_s: float = 2.0):
        self.host = host
        self.port = int(port)
        self.timeout_s = float(timeout_s)

    @property
    def base(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _get(self, path: str, timeout_s: float | None = None):
        url = self.base + path
        with urllib.request.urlopen(url, timeout=timeout_s or self.timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def healthz(self) -> dict:
        """Never raises. Returns the daemon payload or ``{ok: False, error: ...}``."""
        try:
            data = self._get("/healthz")
        except urllib.error.URLError as exc:
            return {"ok": False, "error": f"unreachable: {getattr(exc, 'reason', exc)}"}
        except (OSError, ValueError) as exc:
            if isinstance(exc, ValueError):
                return {"ok": False, "error": f"unexpected payload: {exc}"}
            return {"ok": False, "error": f"unreachable: {type(exc).__name__}: {exc}"}
        if not isinstance(data, dict):
            return {"ok": False, "error": "unexpected payload"}
        return data

    def events(self, since: int = 0, wait: float = 0.0, sid: str | None = None,
               limit: int = 200) -> dict:
        """Long-poll one batch. Raises ``BusError`` if the daemon is unreachable."""
        query = f"/events?since={int(since)}&wait={float(wait):.1f}&limit={int(limit)}"
        if sid:
            query += f"&sid={sid}"
        timeout = max(self.timeout_s, float(wait) + self.timeout_s)
        try:
            data = self._get(query, timeout_s=timeout)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise BusError(f"{type(exc).__name__}: {exc}") from None
        if not isinstance(data, dict):
            raise BusError("unexpected payload")
        return data

    def inject(self, event_type: str, payload: dict) -> dict:
        """Append one file-class event. Callers must gate on ``dispatch_enabled``."""
        if not str(event_type).startswith("event."):
            raise BusError("event type must start with 'event.'")
        body = json.dumps({"event": event_type, "payload": payload or {}}).encode()
        req = urllib.request.Request(self.base + "/events/inject", data=body,
                                     method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise BusError(f"{type(exc).__name__}: {exc}") from None
