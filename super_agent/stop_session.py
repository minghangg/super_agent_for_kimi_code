#!/usr/bin/env python3
"""Stop (abort) the running prompt of a kimi-code session.

Why this exists: killing the local `dispatch.py` client does NOT stop the
server-side turn. Cancellation is only exposed on the daemon WebSocket
(`/api/v1/ws`): `client_hello` → `abort {session_id, prompt_id}`.
The REST API has no abort endpoint (verified 2026-10-27: `/prompts/{tail}`
answers `unsupported action: abort|cancel|stop|interrupt`).

Usage:
  python -m super_agent.stop_session <session_id> [--wait 60]

Exit codes: 0 = aborted (or nothing was running), 1 = abort rejected,
2 = still busy after the wait window.

Version check for 2.5.x (2026-11-06, issue #6) -- read before "upgrading" this file:
  This script turns out to be **unaffected** by the 2.5.0 removals, because it uses
  only three things:
    · GET /sessions/{id}            -- NOT in the removal list (2.4 and 2.5 both have it)
    · GET /prompts?page_size=20     -- NOT in the removal list either
    · WS client_hello + abort frame -- `abort` still exists; only `subscribe` /
                                       `unsubscribe` were removed, which we never sent
  The `/status`, `/snapshot` and REST `/tasks` endpoints it never touches.
  The one thing that must be re-verified after upgrading is whether
  `client_hello.payload.subscriptions` is still honoured -- we deliberately do NOT
  send `subscriptions` here (it is only needed for receiving session events, and
  abort acks come back regardless). If abort ever stops working on 2.5.x, the
  fallback is `python -m super_agent.console stop <sid>` (WS abort + task.stop).
"""

import asyncio
import json
import sys
import time
import uuid

from . import dispatch_cli as D


def _status(base: str, token: str, sid: str) -> dict:
    return D.unwrap(D.http_get(f"{base}/api/v1/sessions/{sid}", token), "status")


def _is_idle(st: dict) -> bool:
    """会话是否真的空着。判据是 `activity` 三态（ISS-103 P0-1.1）。

    旧写法查 `busy`／`main_turn_active`，2.7 两者都没有 ⇒ 恒 `None` ⇒ `not None`
    为真 ⇒ **一个正在跑的会话会被判成「已经空了」**，于是 `stop` 直接返回 0 而
    什么都没停。`activity` 缺失时**不判空**（宁可多发一次 abort，不可漏停）。
    """
    return st.get("activity") == "idle"


def _running_prompt_id(base: str, token: str, sid: str) -> str:
    data = D.unwrap(
        D.http_get(f"{base}/api/v1/sessions/{sid}/prompts?page_size=20", token), "prompts")
    active = data.get("active") or {}
    pid = str(active.get("prompt_id") or active.get("id") or "")
    if pid:
        return pid
    queued = data.get("queued") or []
    if queued:
        head = queued[0] or {}
        return str(head.get("prompt_id") or head.get("id") or "")
    return ""


async def _abort_via_ws(base: str, token: str, sid: str, prompt_id: str) -> dict:
    import websockets

    ws_url = base.replace("http://", "ws://").replace("https://", "wss://") + "/api/v1/ws"
    hello_id = uuid.uuid4().hex
    abort_id = uuid.uuid4().hex
    hello = {"type": "client_hello", "id": hello_id,
             "payload": {"client_id": "supervisor-stop", "subscriptions": [sid]}}
    abort = {"type": "abort", "id": abort_id,
             "payload": {"session_id": sid, "prompt_id": prompt_id}}
    async with websockets.connect(ws_url, open_timeout=10, close_timeout=5,
                                  ping_interval=20,
                                  additional_headers={"Authorization": f"Bearer {token}"}) as ws:
        await ws.send(json.dumps(hello))
        await _wait_ack(ws, hello_id)
        await ws.send(json.dumps(abort))
        return await _wait_ack(ws, abort_id)


async def _wait_ack(ws, msg_id: str, timeout: float = 12.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(1.0, deadline - time.time()))
        except asyncio.TimeoutError:
            break
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if data.get("id") == msg_id and data.get("type") in ("ack", "error"):
            return data
        if data.get("type") == "server_hello":
            continue
    return {}


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: stop_session.py <session_id> [--wait 60]")
        return 2
    sid = sys.argv[1]
    wait = 60.0
    args = sys.argv[2:]
    while args:
        key = args.pop(0)
        if key == "--wait":
            wait = float(args.pop(0))

    token = D.read_token()
    base = D.discover_base(token, None)
    st = _status(base, token, sid)
    if _is_idle(st):
        print(f"[stop] already idle (activity={st.get('activity')}, "
              f"last_turn_outcome={st.get('last_turn_outcome')})")
        return 0

    prompt_id = _running_prompt_id(base, token, sid)
    if not prompt_id:
        print("[stop] no running prompt id; nothing to abort")
        return 1
    ack = asyncio.run(_abort_via_ws(base, token, sid, prompt_id))
    print(f"[stop] abort ack: {json.dumps(ack, ensure_ascii=False)[:240]}")

    deadline = time.time() + wait
    while time.time() < deadline:
        st = _status(base, token, sid)
        if _is_idle(st):
            print(f"[stop] idle now, activity={st.get('activity')} "
                  f"last_turn_outcome={st.get('last_turn_outcome')}")
            return 0
        time.sleep(2)
    print(f"[stop] still busy after {wait:.0f}s")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
