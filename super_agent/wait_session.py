#!/usr/bin/env python3
"""Wait for an existing session to finish, then print its final assistant answer.

Use when the dispatcher client dies (e.g. the wrapping shell hits its own
timeout) but the session keeps running server-side.

Usage: python -m super_agent.wait_session <session_id> [--timeout 5400] [--out path]
"""

import sys
import time

from . import dispatch_cli as D


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: wait_session.py <session_id> [--timeout N] [--out path]")
        return 2
    sid = sys.argv[1]
    timeout = 5400.0
    out = None
    args = sys.argv[2:]
    while args:
        key = args.pop(0)
        if key == "--timeout":
            timeout = float(args.pop(0))
        elif key == "--out":
            out = args.pop(0)
    token = D.read_token()
    base = D.discover_base(token, None)
    deadline = time.time() + timeout
    status = {}
    while time.time() < deadline:
        status = D.unwrap(D.http_get(f"{base}/api/v1/sessions/{sid}", token), "session status")
        idle = not status.get("main_turn_active") and not status.get("busy")
        if idle and status.get("last_turn_reason"):
            break
        time.sleep(20)
    else:
        print(f"[wait] still running after {timeout:.0f}s; last status={status.get('title', '')}")
        return 3
    reason = status.get("last_turn_reason")
    text = D.fetch_final_answer(base, token, sid)
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"[wait] reason={reason} chars={len(text)} out={out}")
    else:
        print(f"[wait] reason={reason}")
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
