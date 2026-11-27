"""The consumer legs of the launcher: the C2 forwarder and the leg registry.

The C2 leg itself lives in :mod:`c2` - it carries the loop's four guards and the
state they need, which is a subject of its own. This module keeps the names
``host`` and the tests address, so moving it did not change any call site.
"""
from __future__ import annotations

from .c2 import DEFAULT_WAIT_S, C2Leg, config  # noqa: F401  re-exported
from .c2 import config as decide_watch_config
from .seams import session_problem  # noqa: F401  re-exported (#139 P0-1)


def decide_watch_tick(cfg: dict, store, *, bus=None, port: int = 0,
                      dry: bool = False, wait: float | None = None,
                      supervisor: str | None = None) -> dict:
    """One round, built fresh. Kept for callers that do not hold a ``C2Leg``.

    A launcher registers a **single** :class:`C2Leg` instance instead: the backoff
    and the liveness clock only mean anything across rounds, and a fresh object
    per call would reset both every time.
    """
    leg = C2Leg(cfg, store, bus=bus, port=port, dry=dry, wait=wait,
                supervisor=supervisor)
    return leg()


# The AT service and the Reporter refresh live in their own modules; the names
# are re-exported here because ``host`` and the tests address ``components.at``.
from .at_service import (AtService, build_at_service,  # noqa: E402,F401
                         _deployment_resolver)
from .reporter_leg import (REPORTER_PAGE_NAME, render_reporter_page,  # noqa: E402,F401
                           reporter_refresh)
