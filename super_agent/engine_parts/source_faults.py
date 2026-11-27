"""Telling a network blip from a fault that must stop the engine.

The rule the engine follows (user ruling 2026-11-28): **an error stops the
engine; a network blip is the one exception**. The opposite - keep running and
keep failing - is worse than stopping, because the log fills with refusals while
the work quietly does not happen (#140: six timeouts in one afternoon, each one
skipping assignment sync, delivery matching and the cursor for a whole round).

Two decisions, and neither guesses:

* **transient** - a transport-level failure. Sockets time out, connections are
  refused, TLS handshakes fail, the peer disconnects. Nothing in the
  configuration is wrong, so a retry can succeed; the loop tolerates a few in a
  row and then treats the run as failed anyway (a "blip" that lasts ten rounds
  is a broken link).
* **fatal** - everything else, and **unknown is fatal**. A 401, a 404 repo, an
  unexpected payload, a malformed listing, a bug: none of those are fixed by
  waiting, and carrying on turns one loud failure into hours of silence.

Classification reads the message because ``SourceError`` stringifies the cause
(``github/source.py`` raises ``SourceError(f"{exc.kind}: {exc.detail}")``). That
is a deliberate trade: a regex over a message is weaker than an exception type,
so the default is the safe one - anything not recognised stops the engine.
"""
from __future__ import annotations

#: Transport symptoms. Matched case-insensitively as substrings.
TRANSIENT_MARKERS = (
    "timeout", "timed out",
    "clientconnectorerror", "clientoserror", "clientproxyconnectionerror",
    "serverdisconnectederror", "connectionreset", "connection refused",
    "connectionreseterror", "cannot connect to host",
    "ssl", "temporary failure in name resolution",
    "network is unreachable", "no route to host",
    "incompleteread", "remoteprotocolerror",
)


def classify(exc: object) -> str:
    """``"transient"`` or ``"fatal"`` for one source failure.

    ``exc`` may be an exception or an already-formatted message; both are read
    as text, because that is all ``SourceError`` preserves.
    """
    text = str(exc or "").strip().lower()
    for marker in TRANSIENT_MARKERS:
        if marker in text:
            return "transient"
    return "fatal"


def is_fatal(exc: object) -> bool:
    return classify(exc) == "fatal"
