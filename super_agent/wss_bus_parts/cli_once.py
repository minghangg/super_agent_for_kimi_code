"""Operator one-shots: ``--status`` and ``--once`` (moved out in #138).

Neither touches the ring, the lock or the guardian table: ``--status`` asks a
running daemon for its health and ``--once`` opens one throw-away subscription to
watch frames arrive. They are diagnostics a human runs, so they belong beside the
daemon rather than inside it - and moving them is part of the §3.1.1 cleanup this
change owes ``wss_bus``.

Bodies are moved verbatim. The three host seams are injected through
:func:`bind`: the daemon owns the port, the ring state and the ACK bookkeeping,
and a copy of either here would let the one-shot disagree with the daemon it is
diagnosing.
"""
from __future__ import annotations

import asyncio
import json
import time

_PORT = None
_STATE = None
_HANDSHAKE = None
_MARK_ACK = None
_BUS_HEALTH = None
_CLIENT = None
_PATHS = None


def bind(*, port, state, handshake, mark_ack, bus_health, client, paths) -> None:
    """Install the host seams. Idempotent; the host calls this after import."""
    global _PORT, _STATE, _HANDSHAKE, _MARK_ACK, _BUS_HEALTH, _CLIENT, _PATHS
    _PORT, _STATE, _HANDSHAKE, _MARK_ACK = port, state, handshake, mark_ack
    _BUS_HEALTH, _CLIENT, _PATHS = bus_health, client, paths


def _port():
    return _PORT() if callable(_PORT) else _PORT


def status() -> int:
    """``--status``: UP/DOWN plus the daemon's own health document."""
    port = _port()
    h = _BUS_HEALTH(port=port)
    if not h:
        print(f"daemon: DOWN（localhost:{port} 不通；工具会降级直连）")
        return 1
    print("daemon: UP")
    print(json.dumps(h, ensure_ascii=False, indent=1))
    return 0


async def once(wait: float) -> int:
    """``--once``: subscribe for ``wait`` seconds and report what arrived."""
    import aiohttp

    state = _STATE
    token = _PATHS.token()
    if not token:
        print("读不到 token")
        return 1
    n, types = 0, {}
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(_CLIENT._ws_url(),
                                       headers={"Authorization": f"Bearer {token}"},
                                       heartbeat=20) as ws:
                await _HANDSHAKE(ws)
                deadline = time.time() + wait
                while time.time() < deadline:
                    try:
                        msg = await ws.receive(timeout=max(0.5, deadline - time.time()))
                    except asyncio.TimeoutError:
                        break
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        break
                    try:
                        frame = json.loads(msg.data)
                    except ValueError:
                        continue
                    t = frame.get("type") or ""
                    if t == "ping":
                        await ws.send_json({"type": "pong",
                                            "payload": {"nonce": (frame.get("payload")
                                                                  or {}).get("nonce", "")}})
                        continue
                    if t in ("pong", "server_hello"):
                        continue
                    if t in ("ack", "error", "response"):
                        _MARK_ACK(frame)
                        continue
                    types[t] = types.get(t, 0) + 1
                    n += 1
    except Exception as exc:  # noqa: BLE001
        print(f"--once 失败：{type(exc).__name__}: {exc}")
        return 1
    ok = sum(1 for r in state.subscribed.values() if r.get("ok"))
    print(f"--once 完成：请求订阅 {len(state.subscribed)}，ack 成功 {ok}，收到 {n} 帧")
    print("  类型分布：" + (json.dumps(types, ensure_ascii=False) if types else "（无事件）"))
    return 0 if ok else 1
