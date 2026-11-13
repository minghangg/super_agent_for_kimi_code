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

# Built-in default profile (ISS-32): used whenever the model profile table is
# absent or malformed, so the engine behaves exactly as it did before the table
# existed.
DEFAULT_MODEL_PROFILE = {"model": "cb-deepseek-v4.1-flash", "effort": "high",
                         "thinking": "high"}

# Inert warm-up ping for a freshly created session (ISS-57). One trivial turn
# activates the session before the real task; the wording must stay inert so the
# agent calls no tools and reads no files. Copied verbatim from the workbench
# dispatcher (``dispatch.py:57-60``) so both sides behave identically.
WARMUP_PROMPT = ("Reply with exactly: ping. Do not call any tools. "
                 "Do not read files. Do not run commands.")


def contract_warmup_prompt(contract_path: str | Path | None) -> str:
    """First message for a newly created session, or the inert fallback.

    The workbench routes every new session through its session contract (think in
    English / precise output / seven-step pre-code check). The message therefore
    asks the agent to read that file once, then confirm. Wording matches the
    workbench dispatcher verbatim (``dispatch.py:70-82``); only the path differs,
    because this side never hardcodes a private workbench root.

    Pure: it tests for existence, reads no content, creates nothing, logs
    nothing, and never returns ``None`` -- any problem falls back to
    ``WARMUP_PROMPT``.
    """
    if not contract_path:
        return WARMUP_PROMPT
    try:
        path = Path(str(contract_path))
        if path.is_file():
            # The message is read by an agent, so hand it a POSIX path.
            return (f"Read {path.as_posix()} with the Read tool. "
                    "Reply with exactly: Read done. "
                    "Do not use any other tool. Do not run any command.")
    except (OSError, ValueError):
        pass
    return WARMUP_PROMPT


def _profile_ok(entry) -> bool:
    return isinstance(entry, dict) and all(
        isinstance(entry.get(k), str) and entry.get(k) for k in ("model", "effort"))


def load_model_profiles(path: str | Path | None) -> dict:
    """Read the model profile table; ``{}`` means "not usable".

    Validation mirrors the workbench side: ``schema_version`` must be present,
    ``rotation`` must be a non-empty list, and every profile must carry a string
    ``model`` and ``effort``. Never raises -- a broken table must not be able to
    change engine behaviour.
    """
    if not path:
        return {}
    try:
        with open(str(path), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"[WARN] model_profiles: cannot read {path} ({exc}); using default",
              file=sys.stderr)
        return {}
    if not isinstance(data, dict):
        print(f"[WARN] model_profiles: {path} is not a JSON object; using default",
              file=sys.stderr)
        return {}
    if "schema_version" not in data:
        print(f"[WARN] model_profiles: {path} has no schema_version; using default",
              file=sys.stderr)
        return {}
    rotation = data.get("rotation")
    if not isinstance(rotation, list) or not rotation or not all(
            _profile_ok(c) for c in rotation):
        print(f"[WARN] model_profiles: {path} rotation must be a non-empty list of "
              "{model, effort}; using default", file=sys.stderr)
        return {}
    profiles = data.get("profiles")
    if not isinstance(profiles, dict) or not all(_profile_ok(p)
                                                 for p in profiles.values()):
        print(f"[WARN] model_profiles: {path} profiles need model+effort each; "
              "using default", file=sys.stderr)
        return {}
    return data


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
                 runner=None, timeout_s: float = 60.0,
                 model_profiles_path: str | Path | None = None,
                 contract_path: str | Path | None = None):
        self.console = str(console) if console else ""
        self.creation_mode = str(creation_mode or "pm_created")
        self.mode = str(mode or "observe")
        self.timeout_s = float(timeout_s)
        self.runner = runner or self._subprocess_runner
        self.model_profiles_path = str(model_profiles_path) if model_profiles_path else ""
        self.contract_path = str(contract_path) if contract_path else ""

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

    # ------------------------------------------------------------------ API
    def choose_profile(self, assignment: dict) -> dict:
        """Return the model profile an assignment should run with (ISS-32).

        Read-only: it never creates or binds a session and does not consult
        ``creation_mode``. The role comes from ``assignment["role"]`` (falling
        back to ``assignment["profile"]``, then ``assignment["kind"]``). The
        answer is ``{"profile", "model", "effort", "thinking", "source"}`` where
        ``source`` is ``config`` when the table supplied it and ``fallback``
        when the built-in default did.
        """
        assignment = assignment if isinstance(assignment, dict) else {}
        role = ""
        for key in ("role", "profile", "kind"):
            val = assignment.get(key)
            if isinstance(val, str) and val.strip():
                role = val.strip()
                break
        table = load_model_profiles(self.model_profiles_path)
        entry = (table.get("profiles") or {}).get(role) if role else None
        if _profile_ok(entry):
            source = "config"
        else:
            entry, source = DEFAULT_MODEL_PROFILE, "fallback"
        return {"profile": role,
                "model": str(entry["model"]),
                "effort": str(entry["effort"]),
                "thinking": str(entry.get("thinking") or entry["effort"]),
                "source": source}

    def resolve_role_session(self, project: str, role: str) -> dict:
        if not project or not role:
            return _result(False, "bad_arguments", error="project and role are required")
        return self._call(["resolve", "--json", "--project", str(project),
                           "--role", str(role)])

    def session_bootstrap(self) -> dict:
        """First message the engine would send to a newly created session (ISS-57).

        Answers ``{"ok", "code", "text", "source", "contract_path"}``. ``source``
        is ``contract`` when the configured contract file is readable and
        ``fallback`` when the inert ping is returned instead.

        It creates nothing: no console spawn, no session, no state write.
        ``session_id`` is always ``None`` by contract -- this method produces a
        message, never a session, so the value must not be handed to ``send``.
        M0 keeps session creation outside the engine, so this method only fixes
        *what* the first message must be; it never delivers it. The first message
        of a new session must come from this ``text`` and not from ``send()`` --
        ``send`` carries follow-up messages for a session that already exists.
        """
        text = contract_warmup_prompt(self.contract_path)
        if text == WARMUP_PROMPT:
            return _result(True, "ok", source="fallback", text=text,
                           contract_path="")
        return _result(True, "ok", source="contract", text=text,
                       contract_path=Path(self.contract_path).as_posix())

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
                           timeout_s=float(dispatch_cfg.get("timeout_s") or 60.0),
                           model_profiles_path=str(
                               engine_cfg.get("model_profiles_path") or ""),
                           contract_path=str(engine_cfg.get("contract_path") or ""))
