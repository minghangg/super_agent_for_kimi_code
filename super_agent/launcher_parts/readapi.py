"""The whitelisted read-only HTTP surface of the launcher (ISS-132 E).

Three properties, each of which fails in a way that is easy to miss:

**Whitelist, not blacklist.** ``/healthz`` echoes a fixed set of fields built by
:func:`health_payload`. Nothing walks the health dict and redacts the bad keys,
because a redaction list is a promise about the fields that exist today; the next
field added anywhere would be published until someone remembered to add it to the
list. The whitelist fails the other way: a new field is invisible until it is
named here, and "invisible" is the safe direction for a status page.

**A host guard that refuses rather than rewrites.** The service binds loopback,
so a request whose ``Host`` is not loopback is either a proxy misconfiguration or
a DNS-rebinding attack - in both cases answering is worse than refusing, and
rewriting the header would turn an attack into a successful request.

**No ``aiohttp``.** The project depends on it (``wss_bus`` needs it), but the
ticket's counter-example 7 is "the test environment happened to have aiohttp":
a health endpoint whose test only runs when a third-party package is importable
proves nothing on a machine that lacks it. ``http.server`` is stdlib, so this
module has **no third-party import at all** - and it is verified by constructing
and querying a real server, not by reading a heartbeat file (counter-example 6).

Three endpoints, all GET, all read-only:

* ``GET /healthz`` -> ``{ok, ready, phase, components, failures, ...}``
* ``GET /sessions`` -> the read-only session list, ``capabilities`` included
* ``GET /issues`` -> canonical ``owner/repo#N`` keys beside their provider

The third is what gives :class:`~super_agent.launcher_parts.backends.IssueBackend`
a production caller (ISS-132 QA P1-3). It publishes the key **and** the provider
side by side, which is the shape the backend exists to enforce: the provider is
metadata, never part of the key. Without this route the backend was only reached
by tests, and a module reached only by tests is a module nobody can tell apart
from a counter-example prop.

No POST, no PUT, no DELETE: a launcher read API that can mutate is a second
writer, which is the split brain the single-instance lock exists to prevent.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from ..engine_parts.round import ROUND_FIELDS

#: Every field ``/healthz`` may publish, per component. Anything else stays home.
COMPONENT_FIELDS = ("enabled", "running", "kind", "started", "last_started_at",
                    "last_success_at", "last_error", "overrun", "stalled", "runs")

#: Published **only when the row carries it**. A recipient is a property of the
#: one leg that resolves one, so a whitelist-wide ``null`` would claim the other
#: components have a recipient field at all (ISS-136 §三 甲: the rescue override
#: must be visible on ``/healthz``).
OPTIONAL_COMPONENT_FIELDS = ("supervisor",)

#: Top-level fields. ``failures`` is derived, never a stored dict.
TOP_FIELDS = ("pid", "running", "phase", "started_at", "stalled")

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

#: Keys that must never appear in a rendered config, whatever their nesting.
SECRET_MARKERS = ("token", "secret", "password", "passwd", "credential",
                  "api_key", "apikey", "private_key", "auth")


class HostRefused(RuntimeError):
    """The request named a Host this service does not answer for."""


def host_allowed(host_header, *, allow=LOOPBACK_HOSTS) -> bool:
    """``Host`` -> bool. A port is allowed and ignored; a name must be loopback.

    An empty ``Host`` is refused as well: HTTP/1.1 requires the header, so its
    absence means a hand-written request, and "no header" must not be the way
    past a header check.
    """
    raw = str(host_header or "").strip().lower()
    if not raw:
        return False
    if raw.startswith("["):                       # [::1]:8080
        name = raw.split("]", 1)[0].lstrip("[")
    else:
        name = raw.rsplit(":", 1)[0] if raw.count(":") == 1 else raw
    return name in allow


def redact_config(value, *, depth: int = 0):
    """Return a copy of ``value`` with every secret-looking key **removed**.

    Removed, not masked: a masked value still tells an attacker the length and
    the shape, and a pointer (``env:GITHUB_TOKEN``) tells them where to look.
    The ticket names all three - token values, credential values and pointer
    values - so the replacement is absence.

    Lists are walked too: a token inside a list of providers is the same leak.
    """
    if depth > 12:
        return "…"
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            name = str(key).lower()
            if any(marker in name for marker in SECRET_MARKERS):
                continue
            out[key] = redact_config(item, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_config(item, depth=depth + 1) for item in value]
    return value


def health_payload(health: dict, *, failures: list | None = None) -> dict:
    """Project a runner health snapshot onto the whitelist.

    ``failures`` is built from the projected rows, not copied: it lists the
    components whose row says something is wrong, with the reason. A failed
    component therefore **shows up as failed** - a health endpoint that hides
    its own degradation is a green light nobody can trust.

    ``running=false`` is **not** a failure for a thread component: it is set
    between rounds, so every healthy engine would be listed as broken. The
    failure signals are the ones that mean something is wrong right now - a
    component switched off (`enabled=false`), a stalled one, a crash
    (`last_error`), or a service that started and returned.

    The four ``ROUND_FIELDS`` are published **as a group, only when the snapshot
    carries all of them** (#124 A2, #137). ``runner.health()`` fills them from the
    last round, so they are absent only before the first one - and then "no round
    yet" must not be spelled ``collection_complete=false``, which reads as a round
    that ran and failed. Once a round has run they stay, complete or not: the
    confirmation reading is a value, not an absence.
    """
    snap = health or {}
    out = {name: snap.get(name) for name in TOP_FIELDS}
    if all(name in snap for name in ROUND_FIELDS):
        out.update({name: snap[name] for name in ROUND_FIELDS})
    out["ok"] = not (snap.get("stalled") or False)
    components = {}
    bad: list[dict] = []
    for name, row in (snap.get("components") or {}).items():
        if not isinstance(row, dict):
            continue
        kept = {field: row.get(field) for field in COMPONENT_FIELDS}
        kept.update({field: row[field] for field in OPTIONAL_COMPONENT_FIELDS
                     if row.get(field)})
        components[str(name)] = kept
        reason = ""
        if not kept.get("enabled"):
            reason = str(kept.get("last_error") or "disabled")
        elif kept.get("stalled"):
            reason = "stalled"
        elif kept.get("last_error"):
            reason = str(kept["last_error"])
        elif kept.get("kind") == "service" and kept.get("started") and not kept.get("running"):
            # A service holds the loop while running; one that returned has died.
            # A thread is allowed to be idle between rounds, hence kind-gated.
            reason = "service returned"
        if reason:
            bad.append({"component": str(name), "reason": reason})
    out["components"] = components
    out["failures"] = list(failures if failures is not None else bad)
    out["ready"] = bool(out["running"]) and not out["failures"]
    return out


class _Handler(BaseHTTPRequestHandler):
    server_version = "super_agent.readapi"
    sys_version = ""

    def log_message(self, fmt, *args):        # keep the console quiet
        return

    def _refuse_host(self) -> bool:
        if host_allowed(self.headers.get("Host")):
            return False
        self._send(421, {"error": "host not allowed",
                         "detail": "read API answers loopback Host only"})
        return True

    def _send(self, code: int, payload) -> None:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):                          # noqa: N802  (http.server API)
        if self._refuse_host():
            return
        path = urlsplit(self.path).path
        if path == "/healthz":
            self._send(200, self.server.snapshot())
            return
        if path == "/sessions":
            self._send(200, self.server.sessions())
            return
        if path == "/issues":
            self._send(200, self.server.issues())
            return
        self._send(404, {"error": "not found",
                         "paths": ["/healthz", "/sessions", "/issues"]})

    def do_HEAD(self):                         # noqa: N802
        if self._refuse_host():
            return
        self._send(200, {})

    def _read_only(self):                      # pragma: no cover - see do_POST
        self._send(405, {"error": "read-only surface"})

    do_POST = _read_only                       # noqa: N815
    do_PUT = _read_only                        # noqa: N815
    do_DELETE = _read_only                     # noqa: N815
    do_PATCH = _read_only                      # noqa: N815


class ReadApiServer(ThreadingHTTPServer):
    """``ThreadingHTTPServer`` that refuses a port somebody else already holds.

    Same reasoning as ``at_serve._Server``: ``allow_reuse_address`` is 1 by
    default, and on Windows that lets a second process bind a port already
    listening - both exit 0 and one of them silently answers nothing.
    """

    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, address, *, snapshot, sessions=None, issues=None) -> None:
        self._snapshot = snapshot
        self._sessions = sessions
        self._issues = issues
        super().__init__(address, _Handler)

    def snapshot(self) -> dict:
        return health_payload(self._snapshot())

    def sessions(self) -> dict:
        if self._sessions is None:
            return {"provider": "", "sessions": [], "unsupported": ["list"]}
        try:
            refs = self._sessions()
        except Exception as exc:                # noqa: BLE001  a read failure is a field
            return {"provider": "", "sessions": [], "error": f"{type(exc).__name__}: {exc}"}
        return {"provider": "kimi",
                "capabilities": ["get", "list"],
                "restore": "unsupported",
                "sessions": [ref.as_dict() for ref in refs]}

    def issues(self) -> dict:
        """Canonical keys beside their provider - the ``IssueBackend`` contract.

        ``([] , "why")`` is how the caller says "no issue table in this root"; the
        route then publishes the reason instead of an empty list, because ``[]``
        reads as "no issues" and would hide a missing index.
        """
        if self._issues is None:
            return {"provider": "", "issues": [],
                    "unsupported": ["list"], "reason": "未注入 issue 读取器"}
        try:
            rows, reason = self._issues()
        except Exception as exc:                # noqa: BLE001  a read failure is a field
            return {"provider": "", "issues": [],
                    "error": f"{type(exc).__name__}: {exc}"}
        out = {"provider": "", "capabilities": ["key"],
               "read": "unsupported", "issues": []}
        for ref in rows:
            row = ref.as_dict()
            out["provider"] = out["provider"] or row["provider"]
            out["issues"].append(row)
        if reason:
            out["reason"] = str(reason)
        return out


def serve(*, host: str, port: int, snapshot, sessions=None, issues=None,
          ready: threading.Event | None = None):
    """Bind now, serve on a daemon thread, return ``(server, thread)``.

    Binding happens on the caller's thread so a port conflict raises here, where
    the launcher can roll the component back, instead of inside a thread nobody
    joins. The thread is a daemon: a stuck request must not keep the process
    alive past its stop budget.
    """
    server = ReadApiServer((host, int(port)), snapshot=snapshot, sessions=sessions,
                           issues=issues)
    thread = threading.Thread(target=server.serve_forever, name="readapi",
                              daemon=True)
    thread.start()
    if ready is not None:
        ready.set()
    return server, thread
