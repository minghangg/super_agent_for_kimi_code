#!/usr/bin/env python3
"""Wait for an existing session to finish, then print its final assistant answer.

Use when the dispatcher client dies (e.g. the wrapping shell hits its own
timeout) but the session keeps running server-side.

Usage: python -m super_agent.wait_session <session_id> [--timeout 5400] [--out path]

Exit codes: 0 = finished, 2 = usage error, 3 = still running at --timeout.
"""

import sys

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
    # 判据只用 `:wait` 的 `timed_out` ＋ `activity`（ISS-103 P0-1.1）。旧写法读
    # `busy`/`main_turn_active`/`last_turn_reason`，2.7 三个键都没有 ⇒ 恒不 break ⇒
    # 永远走到超时那行。这里不要另写一份判据，直接复用 `dispatch_cli.wait_rounds`。
    try:
        res = D.wait_rounds(base, token, sid, timeout)
    except D.DispatchError as exc:
        print(f"[wait] still running after {timeout:.0f}s; {exc}")
        return 3
    reason = res.get("outcome")
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

