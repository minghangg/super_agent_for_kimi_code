"""The three fixture gates: default-open, explicitly closed (ISS-132 C2).

Why they are a module instead of 45 lines inside ``decide_watch``: these four
functions decide **whether a message may leave the machine**, and the incident
they encode is the reason they must stay legible.

2026-11-07 14:17: ``decide_watch`` *claimed* three gates and had none - grep
found zero hits - so a "dry" run really delivered one 收单 notice into a live
supervisor session (wire ``turnId=206``). The lesson is not "add the gates" but
"a safety gate must be assertable on its own", which is why the block is small,
has no dependencies beyond ``os.environ``, and reads the environment **at call
time** - a value captured at import would make ``DELIVERY_FIXTURE=1`` set by a
test after import invisible.

Semantics (Q1): **default open, explicitly closed.**

* nothing set          -> production really sends (never silently dry because
  some variable happens to be absent);
* ``DELIVERY_FIXTURE=1`` -> recipients are rewritten to fake sids **and** a sink
  is mandatory, otherwise the run is refused (:func:`check`);
* ``DELIVERY_SEND_SINK=p`` -> every message that would be sent is written to
  ``p`` first;
* ``DELIVERY_ALLOW_SEND=1`` -> with a sink present, only this variable makes the
  send real; without it the message stops at the sink.
"""
from __future__ import annotations

import hashlib
import os

#: Every fake sid starts with this; ``gate_rewrite`` is idempotent because of it.
DELIVERY_FAKE_PREFIX = "session_5e55a000-"


def fixture() -> bool:
    return (os.environ.get("DELIVERY_FIXTURE") or "").strip() == "1"


def sink() -> str:
    return (os.environ.get("DELIVERY_SEND_SINK") or "").strip()


def allow_send() -> bool:
    return (os.environ.get("DELIVERY_ALLOW_SEND") or "").strip() == "1"


def check() -> str:
    """启动即校验；返回错误描述（空串＝可运行）。"""
    if fixture() and not sink():
        return ("DELIVERY_FIXTURE=1 但未设 DELIVERY_SEND_SINK —— 夹具而不落 sink 等于白测，"
                "且会退化成真发。拒绝运行。")
    return ""


def fake_sid(name: str) -> str:
    h = hashlib.md5(str(name).encode("utf-8")).hexdigest()[:12]
    return f"{DELIVERY_FAKE_PREFIX}0000-4000-8000-{h}"


def rewrite(sid: str, name: str = "") -> str:
    """夹具模式下把任何 sid 换成假 sid（**不只是 stub 判活**）。"""
    if not fixture():
        return sid
    if sid.startswith(DELIVERY_FAKE_PREFIX):
        return sid
    return fake_sid(name or sid)
