"""The ``closed`` terminal-state memo (#134 step 5).

Only one of the three answers may be remembered, and the reason is asymmetric:
``unknown`` is a *failed* read, so caching it makes one outage permanent;
``open`` changes, so caching it hides a close forever; ``closed`` is terminal
for a given issue number, and a reopened issue re-enters ``issues()`` and is
treated as live again.

Kept here rather than inline in ``engine.py`` because ``engine.py`` is over the
file-size line: new state helpers belong in ``engine_parts/``.
"""
from __future__ import annotations


class ClosedStates:
    """Memo of issue numbers already proven closed."""

    def __init__(self) -> None:
        self._closed: set[int] = set()

    def known(self, number: int) -> bool:
        return int(number) in self._closed

    def remember(self, number: int) -> None:
        self._closed.add(int(number))

    def state(self, number: int, read) -> str:
        """``read()``'s answer, memoised only when it is ``closed``."""
        if self.known(number):
            return "closed"
        state = str(read() or "unknown")
        if state == "closed":
            self.remember(number)
        return state


def read_state(source, number: int, log, now) -> str:
    """``source.issue_state`` with a failed read reported and folded to unknown."""
    try:
        return str(source.issue_state(int(number)) or "unknown")
    except Exception as exc:  # noqa: BLE001  unknown, never closed
        log({"type": "issue.state_unreadable", "at": now(), "issue": int(number),
             "error": f"{type(exc).__name__}: {exc}"})
        return "unknown"
