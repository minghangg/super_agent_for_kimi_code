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


# --------------------------------------------------------------------- 旧格式
#: Which role a delivery marker letter belongs to (§P0-2 归一表).
MARKER_ROLES = {"WK": "worker", "SC": "scout", "ST": "steward",
                "AU": "auditor", "RV": "reviewer", "PL": "analyst",
                "AT": "analyst"}

#: Which ``from_role`` a ``role_of`` prefix normalises to (§P0-2 末表).
PREFIX_ROLES = {"supervisor": "supervisor", "pm": "pm", "qa": "qa",
                "wk": "worker", "worker": "worker", "scout": "scout",
                "steward": "steward", "auditor": "auditor",
                "reviewer": "reviewer", "analyst": "analyst", "at": "analyst"}

#: Every legacy first line this module can read, by the shape it opens with.
LEGACY_MARKERS = ("WK", "SC", "ST", "AU", "RV", "AT", "PL", "QA", "PM:TASK",
                  "PM", "AT")


def first_line_sha1(body: str) -> str:
    """SHA-1 of the first non-empty line - a reference that leaks no text.

    An unknown marker is counted, not quoted: the line may carry a session key
    name, and a key name is a credential (RULES §37).
    """
    import hashlib as _hashlib

    from . import markers

    line = markers.first_line(body)
    return _hashlib.sha1(line.encode("utf-8", "replace")).hexdigest()


def from_role_of(claimed_key: str = "", marker: str = "",
                 verified: bool = False) -> str:
    """The ``from_role`` for a comment: **whitelist first, marker second**.

    Three answers, and the order is the whole point (§D.2):

    * ``verified`` and the key names a role → that role. The whitelist already
      decided the key belongs to this thread, so its prefix is the identity;
    * not verified → ``""``. The marker letter is **not** a fallback: a body
      that writes ``[WK:<someone else's key>]`` would otherwise certify itself,
      which is the forgery ``DELIVERY_RE``'s ``([^\\]\\n]{1,64})`` allows;
    * no claimed key at all → ``""`` as well. Unverified is a meaning, not a
      gap to be filled from the text.

    ``MARKER_ROLES`` and ``PREFIX_ROLES`` are consulted here and nowhere else,
    so the two tables cannot drift apart from what is actually used.
    """
    if not verified:
        return ""
    from . import sessionkeys

    role = PREFIX_ROLES.get(sessionkeys.role_of(claimed_key), "")
    if role:
        return role
    return MARKER_ROLES.get(str(marker or "").upper(), "")


def _task_prefix_re():
    """The engine's own ``[PM:TASK]`` prefix pattern, imported lazily.

    Reused rather than copied: two patterns for one prefix would drift, and the
    mapping must agree with ``engine._triage`` about what a task book is
    (§C.2 note 1).
    """
    from .engine import TASK_PREFIX_RE

    return TASK_PREFIX_RE


def looks_like_marker(line: str) -> bool:
    """Does the first line open with *any* bracketed marker?

    Bracketed, half- or full-width, case-insensitive: the shape a tool writes.
    Prose that merely starts with a word is not a marker. The engine uses this
    to tell "an unknown marker" from "ordinary discussion" (#92 §C.3).
    """
    if not line:
        return False
    head = line.lstrip()
    if head[:1] not in ("[", "［"):
        return False
    close = head.find("]")
    if close < 0:
        close = head.find("］")
    return close > 1


def _looks_like_marker(line: str) -> bool:
    """Backwards-compatible private alias of :func:`looks_like_marker`."""
    return looks_like_marker(line)


def legacy_map(body: str, *, repo: str, issue, cid, from_role: str = "",
               project: str = "", created_at: str = "", thread: str = "",
               thread_scope: str = "issue") -> dict:
    """Map one legacy comment onto an envelope. **Read-only, one way** (#92 §C).

    Five first lines, and one row of answers no parser produces:

    * ``[WK:…]`` and the other six delivery prefixes → ``type="delivery"``;
    * ``[QA]`` → ``type="review"``;
    * ``[PM:TASK]`` → ``type="pm_task"`` with ``result_ref`` always empty - it
      is a **prefix judgment**, not a parsed verdict (§C.2 note 1);
    * ``[PM]`` → ``type="pm_verdict"``; a **prose** line keeps ``result_ref``
      empty and records ``coords_ok`` so "has a coordinate" is never read as
      "has a conclusion" (§P1-1);
    * ``[AT]`` → ``type="at_verdict"``; a conclusion without a coordinate stays
      a conclusion with an unreadable state, never folded into ``blocked``.

    An unknown marker is **not** mapped: the answer is ``type=""`` plus an
    ``unknown_marker`` reference carrying the count-worthy fields and a
    ``first_line_sha1`` instead of the text (§C.3).

    Nothing here writes, rewrites or upgrades: the caller reads the envelope and
    the comment stays exactly as it was (§C.4).
    """
    from . import markers

    line = markers.first_line(body)
    if not thread:
        from . import issuekey

        thread = issuekey.thread_key(repo, issue)
    base = dict(repo=repo, issue=issue, cid=cid, from_role=from_role,
                project=project, created_at=created_at, thread=thread,
                thread_scope=thread_scope)
    delivery = markers.parse_delivery(body)
    if delivery:
        env = build(**base, type="delivery")
        env["marker"] = delivery["marker"]
        env["claimed_name"] = delivery["name"]
        return env
    review = markers.parse_review(body)
    if review:
        env = build(**base, type="review")
        env["result_ref"] = ({"kind": "issue_comment", "cid": int(cid),
                              "verdict": review["verdict"]}
                             if review.get("verdict") else {})
        return env
    task = _task_prefix_re().match(line) if line else None
    if task:
        # ``result_ref`` stays empty: a task book declares a prefix, it answers
        # nothing (§C.2 note 1).
        return build(**base, type="pm_task")
    pm = markers.parse_pm_verdict(body)
    if pm:
        env = build(**base, type="pm_verdict")
        env["result_ref"] = _verdict_ref(pm, cid)
        env["coords_ok"] = bool(pm.get("coords_ok"))
        env["source"] = pm.get("source") or ""
        return env
    at = markers.parse_at_verdict(body)
    if at:
        env = build(**base, type="at_verdict")
        env["result_ref"] = _verdict_ref(at, cid)
        env["coords_ok"] = bool(at.get("coords_ok"))
        env["source"] = at.get("source") or ""
        return env
    if _looks_like_marker(line):
        env = build(**base, type="")
        env["result_ref"] = {}
        # The count-and-reference record belongs to the envelope it describes,
        # so the caller does not have to re-open the body to build it (#92 P2-1).
        env["unknown_marker"] = {
            "kind": "unknown_marker",
            "first_line_sha1": first_line_sha1(body),
            "comment_id": int(cid),
        }
        return env
    return build(**base, type="note")


def _verdict_ref(parsed: dict, cid) -> dict:
    """Point to the actual comment that carried the structured verdict."""
    if not parsed.get("structured") or not parsed.get("state"):
        return {}
    return {"kind": "issue_comment", "cid": int(cid), "state": parsed["state"]}


def unknown_marker_event(body: str, *, repo: str, issue, cid, at: str = "") -> dict:
    """The event an unrecognised first-line marker leaves (§C.3).

    Three fields and no text: the count aggregates by ``type``, ``comment_id``
    points back at the comment, ``first_line_sha1`` lets an operator match
    duplicates without the line ever reaching a log.
    """
    return {"type": "envelope.unknown", "at": at or _now(), "repo": str(repo or ""),
            "issue": int(issue) if str(issue).isdigit() else issue,
            "comment_id": int(cid), "first_line_sha1": first_line_sha1(body)}
