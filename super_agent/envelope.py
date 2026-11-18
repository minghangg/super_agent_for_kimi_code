"""One message's complete snapshot: 14 fields, closed value domains (#92 §A).

An **envelope** answers "what is this comment" once, so every consumer stops
re-deriving the answer from the body. It is a **runtime snapshot**: it is never
written to disk, never a second source of truth, and never rewrites the legacy
first-line format (#92 §C.4).

Two hard rules live here and nowhere else:

* ``from_role`` is an **input**, already decided by the session-key whitelist.
  ``build()`` never reads a body, so ``[WK:<someone else's key>]`` cannot name
  its own author (#92 §D.2). A caller with no verified key passes ``""``, which
  means "unverified", not "unknown but trusting".
* ``thread_scope`` is **explicit**. A message with no thread and no scope is
  refused instead of guessed: the 23 coordinate-free rows of the #89 table are
  `project` or `global` on purpose, and inferring `none` would erase that
  (#92 §附).

``validate()`` returns ``(ok, reason)`` and never raises, so a caller can log
``envelope.rejected{reason}`` and keep going. Refusal is fail-closed: a message
that does not validate buys no action.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

#: The schema this build speaks. A frame from any other version is refused -
#: forward compatibility is a decision, never a silent guess (#92 §A.5 反例 3).
SCHEMA_VERSION = 1

#: The closed type set. Matches ``engine._triage``'s returns plus ``pm_task``
#: and ``escalation``; ``""`` is what an unrecognised marker maps to (§C.3).
TYPES = ("delivery", "review", "pm_verdict", "pm_task", "at_verdict",
         "escalation", "note")

#: The roles a message may claim. `worker` covers the ``wk-`` prefix; `analyst`
#: covers both ``analyst-`` and the renamed ``at-`` (#92 §P0-2 归一表).
FROM_ROLES = ("supervisor", "pm", "qa", "worker", "scout", "steward",
              "analyst", "auditor", "reviewer")

#: Where a message belongs when it names no single thread (#92 §附).
THREAD_SCOPES = ("issue", "project", "global", "none")

#: The 14 fields, in the order a reader wants them.
FIELDS = ("schema_version", "message_id", "type", "thread", "thread_scope",
          "project", "assignment_id", "from_role", "to_role", "payload_ref",
          "result_ref", "created_at", "receipt_required", "steer_required")

_REF_KINDS = ("issue_comment", "file")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def message_id(repo: str, issue, cid) -> str:
    """A message's stable identity: ``sha256([repo, issue, cid])[:32]``.

    The separator is JSON's, not a ``#`` or ``:``: a repo name may contain
    either, and a human-readable join then reads ambiguously in logs. ``int()``
    is called explicitly so a caller passing ``"92"`` still gets a numeric
    segment with no separator inside it (#92 §P2-2a).
    """
    blob = json.dumps([str(repo), int(issue), int(cid)],
                      separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def build(*, repo: str, issue, cid, type: str, thread: str = "",
          thread_scope: str | None = None, project: str = "",
          assignment_id: str = "", from_role: str = "", to_role=None,
          payload_ref: dict | None = None, result_ref: dict | None = None,
          created_at: str = "", receipt_required: bool = False,
          steer_required: bool = False,
          schema_version: int = SCHEMA_VERSION) -> dict:
    """Assemble one envelope. **Reads no body** - every identity is an input.

    ``thread_scope`` may be omitted; :func:`normalize` then infers ``issue``
    when a thread is present and leaves it empty otherwise, which
    :func:`validate` refuses (rule 4).

    ``payload_ref`` defaults to *this comment*, which is what it means for every
    message a comment can carry: the message says what this comment says.
    """
    env = {
        "schema_version": int(schema_version),
        "message_id": message_id(repo, issue, cid),
        "type": str(type or ""),
        "thread": str(thread or ""),
        "thread_scope": "" if thread_scope is None else str(thread_scope),
        "project": str(project or ""),
        "assignment_id": str(assignment_id or ""),
        "from_role": str(from_role or ""),
        "to_role": [str(r) for r in (to_role or [])],
        "payload_ref": dict(payload_ref) if payload_ref is not None else
                       {"kind": "issue_comment", "cid": int(cid)},
        "result_ref": dict(result_ref or {}),
        "created_at": str(created_at or _now()),
        "receipt_required": bool(receipt_required),
        "steer_required": bool(steer_required),
    }
    return normalize(env)


def normalize(env: dict) -> dict:
    """Fill the fields whose absence has one safe reading (in place).

    Only ``thread_scope`` is inferred, and only from ``thread``: a message that
    names a thread belongs to it. Everything else is left alone, because
    ``""`` is a meaning here (unverified author, no result, no assignment).
    """
    env = dict(env or {})
    if not str(env.get("thread_scope") or ""):
        if str(env.get("thread") or "").strip():
            env["thread_scope"] = "issue"
    return env


def ref_ok(ref) -> bool:
    """Is ``ref`` a usable ``{"kind", …}`` reference? Empty is *absence*."""
    if not ref:
        return True                      # result_ref: "this message produces nothing"
    return _shape_ok(ref)


def _shape_ok(ref) -> bool:
    """The shape rules alone, with no allowance for an empty ref."""
    if not isinstance(ref, dict):
        return False
    kind = str(ref.get("kind") or "")
    if kind not in _REF_KINDS:
        return False
    if kind == "issue_comment":
        try:
            int(ref.get("cid"))
        except (TypeError, ValueError):
            return False
        return True
    return bool(str(ref.get("path") or ""))


def validate(env: dict) -> tuple[bool, str]:
    """``(ok, reason)``. Never raises; the caller logs the reason and refuses.

    The four ``thread_scope`` rules of #92 §附, in order:

    1. ``issue`` requires a thread;
    2. ``project`` / ``global`` / ``none`` require an empty thread;
    3. ``type`` is **not** restricted by scope - a delivery may be a project's,
       a dispatch may be global;
    4. a missing scope with an empty thread is refused, not guessed to ``none``.

    ``project`` is required only under ``thread_scope == "project"``: a global
    row has no project yet (that is what ``global`` means), and demanding one
    would make the first intake line impossible to write.
    """
    if not isinstance(env, dict):
        return False, "not_an_envelope"
    try:
        version = int(env.get("schema_version"))
    except (TypeError, ValueError):
        return False, "missing_schema_version"
    if version != SCHEMA_VERSION:
        return False, "unknown_schema"
    if not str(env.get("message_id") or ""):
        return False, "missing_message_id"
    kind = str(env.get("type") or "")
    if not kind:
        return False, "missing_type"
    if kind not in TYPES:
        return False, "unknown_type"
    if not _shape_ok(env.get("payload_ref")):
        return False, "missing_payload_ref"
    if not ref_ok(env.get("result_ref")):
        return False, "bad_result_ref"
    if not str(env.get("created_at") or ""):
        return False, "missing_created_at"
    thread = str(env.get("thread") or "").strip()
    scope = str(env.get("thread_scope") or "").strip()
    if not scope:
        return False, "missing_thread_scope"
    if scope not in THREAD_SCOPES:
        return False, "unknown_thread_scope"
    if scope == "issue" and not thread:
        return False, "thread_required"
    if scope != "issue" and thread:
        return False, "thread_not_allowed"
    if scope == "project" and not str(env.get("project") or "").strip():
        return False, "project_required"
    role = str(env.get("from_role") or "")
    if role and role not in FROM_ROLES:
        return False, "unknown_from_role"
    for target in env.get("to_role") or []:
        if str(target) not in FROM_ROLES:
            return False, "unknown_to_role"
    return True, ""


def rejected_event(env: dict, reason: str, *, at: str = "") -> dict:
    """The one event a refused envelope leaves: a count by reason, no body.

    ``comment_id`` is kept so the frame can be clicked back to; the text is
    not, because a body may carry a key name (#92 §C.3, RULES §37).
    """
    env = env if isinstance(env, dict) else {}
    cid = (env.get("payload_ref") or {}).get("cid") if isinstance(
        env.get("payload_ref"), dict) else None
    return {"type": "envelope.rejected", "at": at or _now(), "reason": reason,
            "envelope_type": env.get("type"), "comment_id": cid,
            "message_id": env.get("message_id")}
