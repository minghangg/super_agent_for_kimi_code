"""The resident Reporter page as a launcher component (ISS-132 F).

The page itself is unchanged - the ticket says 不重做页面 - so this module is only
the refresh: collect, render, write, then beat. Two decisions:

**Offline.** The engine is built with ``NullSource``, exactly as the CLI's
``--no-quotes`` path does. A resident page that reached the network every refresh
would change what the page is; quotes stay the CLI's job.

**The beat follows the page.** A heartbeat written before the write reports
"fresh" for a page that never landed - the same mistake as a health file saying
ok while the loop is stuck. The runner's ``last_success_at`` is the authoritative
signal; the heartbeat is for whoever registers the name for patrolling.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import seams

#: The page file under the state root. A file, not a table: the page is a
#: document, and a document that lives in JSON is one nobody can open.
REPORTER_PAGE_NAME = "reporter.html"



# ---------------------------------------------------------------- Reporter page
def render_reporter_page(cfg: dict, store) -> Path:
    """Collect + render + write the page. Returns its path.

    The engine is built with a ``NullSource`` exactly as ``cli.cmd_reporter`` does
    for ``--no-quotes``: a resident page that reaches the network on every refresh
    would change what the page *is*, and the ticket says not to redo it. Quotes
    stay the CLI's job; this leg is the resident copy that always renders.
    """
    from .. import github_source
    from .. import reporter as reporter_mod
    from ..cli import _engine

    source = github_source.NullSource()
    engine = _engine(cfg, store, None, source=github_source.NullSource())
    report, text = reporter_mod.collect_and_render(
        engine, source=source, quotes=False)
    path = Path(store.root) / REPORTER_PAGE_NAME
    tmp = path.with_suffix(".html.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return path


def reporter_refresh(cfg: dict, store) -> dict:
    """One refresh: render, write, then beat. The beat follows the page on purpose.

    A heartbeat written before the write would report "fresh" for a page that
    never landed - the same mistake as a health file that says ok while the loop
    is stuck.

    The name is not a built-in guardian: ``wss_bus.GUARDIAN_THRESHOLDS`` holds the
    two consumer legs, and the reporter is registered by whoever wants it patrolled
    (``state/guardian_extra.json``). Without a registration ``write_heartbeat`` is
    a documented no-op, so the authoritative signal is the runner's health row on
    ``/healthz`` - ``last_success_at`` moves only when the page actually landed.
    """
    from .. import wss_heartbeat

    path = render_reporter_page(cfg, store)
    try:
        wss_heartbeat.bind(thresholds=seams.thresholds, state_root=lambda: Path(store.root),
                           boot_id=seams.boot_id, log=lambda _msg: None)
        wss_heartbeat.write_heartbeat("reporter", {"page": str(path)})
    except Exception:                   # noqa: BLE001  the page landing is the news
        pass
    return {"page": str(path)}
