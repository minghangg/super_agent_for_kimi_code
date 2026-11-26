"""The launcher's command line, split out so ``launcher.py`` stays a host.

Why a separate module: the flag surface is a contract (``cli``'s own options,
mirrored) and the rest of ``launcher.py`` is behaviour. Keeping them in one file
made the host cross the 300-line line the moment a new flag was added, and the
added lines would then sit next to the stop logic - exactly the "one more thing
in the big file" that ``worker.md`` §3.1 stops.

The flags are the ones ``cli._build`` reads. They are declared here rather than
``import``ed from ``cli`` because ``cli`` has one parser for every subcommand,
and reusing that one would register ``status``/``doctor``/``run`` a second time.
"""
from __future__ import annotations

import argparse


class Args:
    """The flags ``cli._build`` reads, so the config chain is reused verbatim.

    Reusing that function rather than re-reading the config here is deliberate:
    a second resolution order is how ``--state-dir`` starts meaning two things,
    and ``legacy_state_dir`` / ``choose_root`` would be skipped silently.
    """

    def __init__(self, *, config=None, state_dir=None, mode=None, role=None,
                 source=None, allowlist=None, session_api=None,
                 accept_empty_index=False, allow_empty_cursor=False):
        self.config = config
        self.state_dir = state_dir
        self.mode = mode
        self.role = role
        self.source = source
        self.allowlist = allowlist
        self.session_api = session_api
        self.accept_empty_index = accept_empty_index
        self.allow_empty_cursor = allow_empty_cursor


MODES = ("observe", "assist")
SOURCES = ("none", "fixture", "live")
SESSION_APIS = ("real", "none")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="super_agent.launcher",
                                 description="Single-process host for A/B/C")
    ap.add_argument("--config", default=None)
    ap.add_argument("--state-dir", default=None)
    ap.add_argument("--mode", default=None, choices=list(MODES))
    ap.add_argument("--role", default=None)
    ap.add_argument("--source", default=None, choices=list(SOURCES))
    ap.add_argument("--allowlist", default=None)
    ap.add_argument("--session-api", default=None, choices=list(SESSION_APIS))
    ap.add_argument("--accept-empty-index", action="store_true")
    ap.add_argument("--allow-empty-cursor", action="store_true")
    sub = ap.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run every component on one loop")
    run.add_argument("--wss", dest="wss", action="store_true", default=None,
                     help="force the WSS component on (default: on when injected)")
    run.add_argument("--no-wss", dest="wss", action="store_false",
                     help="force the WSS component off")
    run.add_argument("--supervisor", default=None,
                     help="rescue override for the recipient sid (default: the "
                          "config chain, i.e. engine.supervisor_sid -> "
                          "sessions.json); it reaches the supervisor probe, the "
                          "delivery and the liveness patrol")
    sub.add_parser("status", help="print the lock and the engine health snapshot")
    return ap
