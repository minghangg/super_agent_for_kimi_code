"""The launcher's non-host parts (ISS-132 F).

``launcher.py`` is a host: it takes the lock, builds the components and stops
them within a budget. Everything else this ticket adds is a **job** with its own
failure modes, so each lives here and the host only wires it:

* :mod:`.backends` - the two read-only backends (sessions, issues) and the one
  behaviour they must never grow: an implicit restore, or a rewritten key.
* :mod:`.components` - the three consumer legs (decide_watch, AT questionnaire,
  Reporter page) as runner components, each with the blocking work on a thread.
* :mod:`.readapi` - the whitelisted read-only HTTP surface, its Host guard and
  its redaction. Standard library only: ``aiohttp`` is a dependency of this
  project, but a health endpoint that only works when a test environment
  happens to carry it is exactly counter-example 7.

Nothing here sends a notification, dispatches a packet or archives a session -
those stay in the engine. The parts read, render and report.
"""
