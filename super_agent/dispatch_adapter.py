"""Structured dispatch interface: dicts in, dicts out.

Every function here returns a plain ``dict`` and never parses human CLI text.
The console it wraps is required to emit machine-readable output (``--json``);
if the payload is not JSON the call fails with ``code="adapter_not_json"``
rather than falling back to a regex over prose.

Common result keys:

``ok``          bool - did the call succeed
``code``        short machine code (``ok``, ``creation_mode_forbidden``, ...)
``session_id``  the sid when the call resolves/creates one, else ``None``
``error``       human-readable failure detail, else ``""``
``source``      which layer produced the answer (``console``, ``local``, ...)

``create_worker`` and ``create_qa`` are gated by ``creation_mode``: with
``pm_created`` (the only mode M0 accepts) they refuse with
``creation_mode_forbidden`` and prepare a draft only. They must never create a
session.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

FORBIDDEN_CREATION = "creation_mode_forbidden"
ALLOWED_CREATION_MODES = ("pm_created",)


def _result(ok: bool, code: str, *, session_id=None, error="", source="local",
            **extra) -> dict:
    out = {"ok": bool(ok), "code": code, "session_id": session_id,
           "error": str(error or ""), "source": source}
    out.update(extra)
    return out


class DispatchAdapter:
    """Thin subprocess wrapper that only ever reads JSON payloads.

    ``runner`` is a callable ``(argv: list[str]) -> tuple[int, str, str]``
    returning ``(returncode, stdout, stderr)``. It is injectable so tests can
    stub the console without a real session service.
    """

    def __init__(self, *, console: str | Path | None = None,
                 creation_mode: str = "pm_created", mode: str = "observe",
                 runner=None, timeout_s: float = 60.0):
        self.console = str(console) if console else ""
        self.creation_mode = str(creation_mode or "pm_created")
        self.mode = str(mode or "observe")
        self.timeout_s = float(timeout_s)
        self.runner = runner or self._subprocess_runner

    # -------------------------------------------------------------- transport
    def _subprocess_runner(self, argv: list[str]) -> tuple[int, str, str]:
        if not self.console:
            return 127, "", "no dispatch console configured"
        done = subprocess.run([sys.executable, self.console, *argv],
                              capture_output=True, text=True,
                              timeout=self.timeout_s)
        return done.returncode, done.stdout or "", done.stderr or ""

    def _call(self, argv: list[str], *, source: str = "console") -> dict:
        try:
            rc, out, err = self.runner(list(argv))
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            return _result(False, "adapter_spawn_failed", error=f"{type(exc).__name__}: {exc}",
                           source=source)
        if rc != 0:
            return _result(False, "adapter_rc_nonzero", error=(err or out or "").strip()[:300],
                           source=source, rc=rc)
        try:
            payload = json.loads(out)
        except ValueError:
            return _result(False, "adapter_not_json",
                           error="console did not emit JSON (human text is never parsed)",
                           source=source)
        if not isinstance(payload, dict):
            return _result(False, "adapter_not_object", error="JSON payload must be an object",
                           source=source)
        ok = bool(payload.get("ok", True))
        return _result(ok, str(payload.get("code") or ("ok" if ok else "failed")),
                       session_id=payload.get("session_id") or payload.get("sid"),
                       error=payload.get("error") or "",
                       source=str(payload.get("source") or source),
                       **{k: v for k, v in payload.items()
                          if k not in {"ok", "code", "session_id", "sid",
                                       "error", "source"}})

    # ------------------------------------------------------------------ gates
    def _creation_gate(self, kind: str, assignment: dict | None) -> dict | None:
        """Return a refusal dict, or None when creation is permitted."""
        if self.creation_mode not in ALLOWED_CREATION_MODES:
            return _result(False, "creation_mode_unknown",
                           error=f"creation_mode {self.creation_mode!r} is not supported")
        return _result(False, FORBIDDEN_CREATION,
                       error=(f"creation_mode={self.creation_mode}: the engine may only "
                              f"prepare a draft {kind}, never create a session"),
                       draft={"kind": kind, "assignment": assignment or {},
                              "status": "draft_only"})

    # -------------------------------------------------------------------- API
    def resolve_role_session(self, project: str, role: str) -> dict:
        if not project or not role:
            return _result(False, "bad_arguments", error="project and role are required")
        return self._call(["resolve", "--json", "--project", str(project),
                           "--role", str(role)])

    def create_worker(self, assignment: dict) -> dict:
        gate = self._creation_gate("worker", assignment)
        return gate if gate is not None else self._call(
            ["create-worker", "--json"])

    def create_qa(self, assignment: dict) -> dict:
        gate = self._creation_gate("qa", assignment)
        return gate if gate is not None else self._call(["create-qa", "--json"])

    def bind_assignment(self, assignment: dict) -> dict:
        return self._call(["bind", "--json",
                           "--assignment", str((assignment or {}).get("id") or "")])

    def send(self, assignment: dict, text_ref: str) -> dict:
        return self._call(["send", "--json",
                           "--assignment", str((assignment or {}).get("id") or ""),
                           "--text-ref", str(text_ref or "")])

    def fetch(self, assignment: dict) -> dict:
        return self._call(["fetch", "--json",
                           "--assignment", str((assignment or {}).get("id") or "")])

    def archive(self, assignment: dict, reason: str) -> dict:
        return self._call(["archive", "--json",
                           "--assignment", str((assignment or {}).get("id") or ""),
                           "--reason", str(reason or "")])

    def reconcile_close(self, thread: dict) -> dict:
        return self._call(["reconcile-close", "--json",
                           "--thread", str((thread or {}).get("id") or "")])


def build(cfg: dict, *, runner=None) -> DispatchAdapter:
    """Adapter from config: creation_mode and the console path (if any)."""
    engine_cfg = cfg.get("engine") or {}
    dispatch_cfg = engine_cfg.get("dispatch") or {}
    console = dispatch_cfg.get("console") or ""
    if console:
        console = str(Path(console).expanduser())
    return DispatchAdapter(console=console,
                           creation_mode=str(engine_cfg.get("creation_mode")
                                             or "pm_created"),
                           mode=str(cfg.get("mode") or "observe"),
                           runner=runner,
                           timeout_s=float(dispatch_cfg.get("timeout_s") or 60.0))
