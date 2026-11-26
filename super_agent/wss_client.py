"""HTTP client for the WSS daemon: health, GET, POST (ISS-131).

Why moved out of ``wss_bus.py``: these four functions speak HTTP to the daemon
and know nothing about the event ring, the journal or the file watchers. Keeping
them beside that state made the daemon module the only place to look for "how do
I ask the bus something", and it had to change in the same commit as the lock
work. ``worker.md`` §3.1.1 #4 lets that happen only while the shed code becomes
a real module - this is it.

The port is resolved in one place (:func:`port_of`) because the three ways it
can be supplied - explicit argument, ``configure(port=...)``, environment - must
not disagree. Two of them disagreeing is how a caller ends up asking a daemon
that is not the one it started.
"""
from __future__ import annotations

import json
import os
import urllib.request

HOST = "127.0.0.1"
HEALTH_TIMEOUT = 2.0

#: Set by :func:`bind`; the daemon module owns ``configure()``.
_PORT = lambda: 0          # noqa: E731


def bind(port_getter) -> None:
    """Install the port accessor (``wss_bus.PORT``). Idempotent."""
    global _PORT
    _PORT = port_getter


def port_of(port: int | None = None) -> int:
    """Resolve the port: explicit > bound ``PORT`` > environment.

    Raises when all three are empty rather than defaulting: a client that
    silently picks 80 asks the wrong process and reports "daemon down".
    """
    value = port or int(_PORT() or 0)
    if not value:
        value = int(os.environ.get("WSS_BUS_PORT", "0") or 0)
    if not value:
        raise RuntimeError("未指定端口（configure(port=...) 或 WSS_BUS_PORT）")
    return value


def health(timeout: float = HEALTH_TIMEOUT, port: int | None = None) -> dict | None:
    """Is the daemon usable? None ⇒ **the caller should fall back to direct**."""
    try:
        value = port_of(port)
    except RuntimeError:
        return None
    try:
        with urllib.request.urlopen(f"http://{HOST}:{value}/healthz",
                                    timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return None


def get(path: str, timeout: float = 3.0, port: int | None = None):
    value = port_of(port)
    with urllib.request.urlopen(f"http://{HOST}:{value}{path}",
                                timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def post(path: str, body: dict, timeout: float = 20.0, port: int | None = None):
    value = port_of(port)
    req = urllib.request.Request(
        f"http://{HOST}:{value}{path}",
        data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))
