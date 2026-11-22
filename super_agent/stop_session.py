#!/usr/bin/env python3
"""Stop the current turn of a kimi-code session using the 2.7 REST API.

Usage:
  python -m super_agent.stop_session <session_id> [--wait 60]

Exit codes: 0 = cancelled or already idle, 1 = cancellation rejected,
2 = still busy or an interaction needs a person.
"""

import sys

from . import direct
from . import dispatch_cli as D


def _status(base: str, token: str, sid: str) -> dict:
    return D.unwrap(D.http_get(f"{base}/api/v1/sessions/{sid}", token), "status")


def _is_idle(st: dict) -> bool:
    return st.get("activity") == "idle"


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

    ok, code = direct.cancel_session(sid, include_background=False)
    print(f"[stop] turn:cancel cancelled={ok} code={code}")
    if not ok:
        return 1
    try:
        result = D.wait_rounds(base, token, sid, wait, permission_mode="manual")
    except D.NeedsPerson as exc:
        print(f"[stop] interaction needs a person: {exc}")
        return 2
    except D.DispatchError:
        print(f"[stop] still busy after {wait:.0f}s")
        return 2
    print(f"[stop] turn finished (activity={result.get('activity')}, "
          f"outcome={result.get('outcome')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
