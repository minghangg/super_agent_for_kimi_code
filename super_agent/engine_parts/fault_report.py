"""What actually went wrong: message, type, and where it came from.

The rule (user 2026-11-28): **an error must state its cause.** "KeyError" on its
own is a symptom with no address - the person reading the event log cannot tell
whether the cause is a missing config key, a corrupt index or a bug three frames
up. Every catch site that records a fault calls :func:`describe` so the record
carries the type, the message and the last frames of the traceback.

Why not the whole traceback: events are bounded and rotated (``store`` keeps
``max_event_lines``), and a 60-frame traceback inside one JSON line makes the log
unreadable. The tail is where the raise happened; that is the address a person
needs. ``--verbose`` style deep dives read the stderr print, which keeps more.
"""
from __future__ import annotations

import traceback

#: Frames kept in a recorded fault. Enough to show "who called whom", short
#: enough that one event stays one readable line.
FRAMES = 6


def describe(exc: BaseException, *, frames: int = FRAMES) -> str:
    """``Type: message`` plus the last ``frames`` frames, as one string.

    Never raises: a failure inside the failure report would hide the original
    fault, which is exactly backwards.
    """
    try:
        head = f"{type(exc).__name__}: {_text(exc)}"
        stack = traceback.format_exc()
        lines = [line for line in stack.strip().splitlines() if line.strip()]
        if lines and lines[0].startswith("Traceback"):
            lines = lines[1:]
        return "\n".join([head.rstrip()] + lines[-max(1, int(frames)):])
    except Exception:  # noqa: BLE001  reporting never fails a caller
        return f"{type(exc).__name__}: <unreportable>"


def _text(exc: BaseException) -> str:
    """``str(exc)`` that cannot itself raise - a broken ``__str__`` is real."""
    try:
        return str(exc)
    except Exception:  # noqa: BLE001
        return "<unprintable>"


def one_line(exc: BaseException, *, frames: int = 3) -> str:
    """The same information collapsed to one line, for a stderr print."""
    return " | ".join(part.strip() for part in
                      describe(exc, frames=frames).splitlines() if part.strip())
