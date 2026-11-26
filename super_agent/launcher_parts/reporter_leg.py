"""The resident Reporter page as a launcher component (ISS-132 F).

The page itself is unchanged - the ticket says 不重做页面 - so this module is only
the refresh: collect, render, write, then beat. Two decisions:

**The source is built from ``github.source``, as the CLI does it.** The first
delivery hard-coded ``NullSource``, which silently overrode the setting (QA P1-4
measured identical page lengths for ``source=none`` and ``source=live``) and made
the page blame ``--no-quotes`` for a decision the leg never made. Both halves are
fixed: the backend comes from :func:`github_source.build`, and when it cannot be
built the page is told **why** through ``source_error``, so a fixture typo is not
reported as "you asked for no quotes".

Offline is still the **default**, because ``github.source`` defaults to ``none``:
a resident page that reaches the network unasked would change what the page is.
That switch belongs to the operator, not to this module.

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

    ``github.source`` decides the data source, exactly as ``cli.cmd_reporter``
    does: ``none`` stays offline, ``live``/``fixture`` really reads. A broken
    source is reported through ``source_error`` instead of being folded into "no
    quotes", because blaming a setting the operator never chose is the mistake
    that parameter exists to prevent.
    """
    from .. import github_source
    from .. import reporter as reporter_mod
    from ..cli import _engine

    source_error = ""
    try:
        source = github_source.build(cfg)
    except Exception as exc:            # noqa: BLE001  a broken source is a field
        source_error = f"{type(exc).__name__}: {exc}"
        source = github_source.NullSource()
    if isinstance(source, github_source.BrokenSource):
        # The real reason survives: a fixture path typo must not be reported as
        # ``github.source=none``, which the operator never configured.
        source_error = str(source.error)
        source = github_source.NullSource()
    # ``quotes`` stays left at its default: rendering with ``quotes=False`` prints
    # ``--no-quotes 指定`` per row, and this leg has no such switch. Left on, the
    # page names the real source state instead.
    engine = _engine(cfg, store, None, source=github_source.NullSource())
    report, text = reporter_mod.collect_and_render(
        engine, source=source, source_error=source_error)
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
