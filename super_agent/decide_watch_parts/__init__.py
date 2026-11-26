"""Pieces of ``decide_watch`` that are a job of their own (ISS-132 C2).

``decide_watch.py`` is the delivery/notice/liveness consumer and it is large for
a reason: three legs share one cursor, one lock and one heartbeat. What does not
share them is login autostart - it talks to the Windows scheduler and never
touches the event ring - so it lives in :mod:`.autostart_install` and the host
module keeps forwarders with the same names.

Same treatment as ``engine_parts`` / ``runner_parts``: the host module is the
entry point, the parts are the responsibilities.
"""
