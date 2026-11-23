"""Versioned AT requirement contracts, with GitHub as the source of truth.

A requirement is published in an issue comment as one ``safkc-requirement``
JSON fence. Approval is a separate ``safkc-requirement-approval`` fence written
by the supervisor. The local projection is disposable; replaying the comments
must produce the same approved revision and hash.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
PACKAGE_FENCE = "safkc-requirement"
APPROVAL_FENCE = "safkc-requirement-approval"
STATUSES = ("draft", "clarifying", "ready", "approved", "dispatched", "completed")
RISK_TIERS = ("T1", "T2")
WORKER_MODES = ("plan_and_implement", "separate")
TEST_LEVELS = ("L0", "L1", "L2", "L3")
SEMANTIC_FIELDS = (
    "schema_version", "requirement_id", "revision", "objective", "context_refs",
    "scope", "out_of_scope", "constraints", "acceptance", "negative_tests",
    "risk_tier", "review_policy", "open_questions",
)
STRING_LISTS = (
    "context_refs", "scope", "out_of_scope", "constraints", "acceptance",
    "negative_tests", "open_questions",
)
_FENCE_RE = re.compile(
    r"^```([A-Za-z0-9_-]+)\s*\n(.*?)^```\s*$", re.M | re.S)


class RequirementError(ValueError):
    """A contract or approval is not safe to dispatch."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _strings(value, field: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip()
                                         for x in value):
        raise RequirementError(f"{field} must be a list of non-empty strings")
    if nonempty and not value:
        raise RequirementError(f"{field} must not be empty")
    return [x.strip() for x in value]


def semantic(package: dict) -> dict:
    out = {key: copy.deepcopy(package.get(key)) for key in SEMANTIC_FIELDS}
    # Optional fields are omitted from legacy hashes, so existing Issue packages
    # keep their identity after the interactive questionnaire is introduced.
    if "interaction" in package:
        interaction = copy.deepcopy(package.get("interaction"))
        if isinstance(interaction, dict):
            interaction.setdefault("choice_mode", "single")
            interaction.setdefault("allow_custom", True)
            options = interaction.get("options") or []
            if isinstance(options, list):
                interaction["options"] = [
                    {"id": item.get("id"), "label": item.get("label"),
                     "description": str(item.get("description") or "").strip()}
                    if isinstance(item, dict) else copy.deepcopy(item)
                    for item in options
                ]
        out["interaction"] = interaction
    return out


def requirement_hash(package: dict) -> str:
    raw = json.dumps(semantic(package), ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def validate_package(raw: dict, *, approved: bool = False) -> dict:
    if not isinstance(raw, dict):
        raise RequirementError("requirement must be a JSON object")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise RequirementError(f"schema_version must be {SCHEMA_VERSION}")
    rid = str(raw.get("requirement_id") or "").strip()
    if not re.match(r"^[^/\s#]+/[^/\s#]+#[1-9][0-9]*$", rid):
        raise RequirementError("requirement_id must be owner/repo#issue")
    revision = raw.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise RequirementError("revision must be a positive integer")
    status = str(raw.get("status") or "").strip()
    if status not in STATUSES:
        raise RequirementError(f"status must be one of {STATUSES}")
    objective = str(raw.get("objective") or "").strip()
    if not objective:
        raise RequirementError("objective must not be empty")
    out = copy.deepcopy(raw)
    out.update(requirement_id=rid, revision=revision, status=status,
               objective=objective, updated_at=str(raw.get("updated_at") or ""))
    for field in STRING_LISTS:
        out[field] = _strings(raw.get(field), field,
                              nonempty=field in ("acceptance", "negative_tests"))
    risk = str(raw.get("risk_tier") or "")
    if risk not in RISK_TIERS:
        raise RequirementError(f"risk_tier must be T1 or T2")
    out["risk_tier"] = risk
    policy = raw.get("review_policy")
    if not isinstance(policy, dict):
        raise RequirementError("review_policy must be an object")
    plan_qa = policy.get("plan_qa")
    implementation_qa = policy.get("implementation_qa")
    if not isinstance(plan_qa, bool) or not isinstance(implementation_qa, bool):
        raise RequirementError("review_policy QA switches must be booleans")
    mode = str(policy.get("worker_mode") or "")
    level = str(policy.get("test_level") or "")
    if mode not in WORKER_MODES:
        raise RequirementError(f"worker_mode must be one of {WORKER_MODES}")
    if level not in TEST_LEVELS:
        raise RequirementError(f"test_level must be one of {TEST_LEVELS}")
    if risk == "T2" and (not plan_qa or mode != "separate"):
        raise RequirementError("T2 requires plan_qa=true and worker_mode=separate")
    out["review_policy"] = {
        "plan_qa": plan_qa, "implementation_qa": implementation_qa,
        "worker_mode": mode, "test_level": level,
    }
    if "interaction" in raw:
        interaction = raw.get("interaction")
        if not isinstance(interaction, dict):
            raise RequirementError("interaction must be an object")
        choice_mode = str(interaction.get("choice_mode") or "single")
        if choice_mode not in ("single", "multiple"):
            raise RequirementError("interaction.choice_mode must be single or multiple")
        allow_custom = interaction.get("allow_custom", True)
        if not isinstance(allow_custom, bool):
            raise RequirementError("interaction.allow_custom must be a boolean")
        raw_options = interaction.get("options") or []
        if not isinstance(raw_options, list) or len(raw_options) > 12:
            raise RequirementError("interaction.options must be a list of at most 12 items")
        options, ids = [], set()
        for item in raw_options:
            if not isinstance(item, dict):
                raise RequirementError("each interaction option must be an object")
            option_id = str(item.get("id") or "").strip()
            label = str(item.get("label") or "").strip()
            if not re.match(r"^[A-Za-z0-9_-]{1,32}$", option_id) or not label:
                raise RequirementError("option id/label is invalid")
            if option_id in ids:
                raise RequirementError("interaction option ids must be unique")
            ids.add(option_id)
            options.append({"id": option_id, "label": label,
                            "description": str(item.get("description") or "").strip()})
        out["interaction"] = {"choice_mode": choice_mode,
                              "allow_custom": allow_custom, "options": options}
    questions = out["open_questions"]
    if status == "ready" and questions:
        raise RequirementError("READY requires open_questions=[]")
    if status in ("ready", "approved", "dispatched", "completed") and questions:
        raise RequirementError("an approved requirement cannot have open questions")
    approval_ref = str(raw.get("approval_ref") or "").strip()
    out["approval_ref"] = approval_ref
    if approved and not re.match(r"^issuecomment-[1-9][0-9]*$", approval_ref):
        raise RequirementError("approved requirement requires a valid approval_ref")
    expected = requirement_hash(out)
    supplied = str(raw.get("requirement_hash") or "").strip()
    if supplied and supplied != expected:
        raise RequirementError(f"requirement_hash mismatch: expected {expected}")
    out["requirement_hash"] = expected
    return out


def validate_approval(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise RequirementError("approval must be a JSON object")
    rid = str(raw.get("requirement_id") or "").strip()
    revision = raw.get("revision")
    digest = str(raw.get("requirement_hash") or "").strip()
    package_ref = str(raw.get("package_ref") or "").strip()
    if not re.match(r"^[^/\s#]+/[^/\s#]+#[1-9][0-9]*$", rid):
        raise RequirementError("approval requirement_id must be owner/repo#issue")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise RequirementError("approval revision must be a positive integer")
    if not re.match(r"^sha256:[0-9a-f]{64}$", digest):
        raise RequirementError("approval requirement_hash is invalid")
    if not re.match(r"^issuecomment-[1-9][0-9]*$", package_ref):
        raise RequirementError("approval package_ref is invalid")
    out = copy.deepcopy(raw)
    out.update(requirement_id=rid, revision=revision, requirement_hash=digest,
               package_ref=package_ref,
               approved_at=str(raw.get("approved_at") or ""))
    return out


def fence(kind: str, value: dict) -> str:
    return f"```{kind}\n{json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)}\n```"


def blocks(body: str, kind: str) -> list[dict]:
    out = []
    for match in _FENCE_RE.finditer(str(body or "")):
        if match.group(1) != kind:
            continue
        try:
            value = json.loads(match.group(2))
        except ValueError as exc:
            raise RequirementError(f"invalid {kind} JSON: {exc}") from None
        if not isinstance(value, dict):
            raise RequirementError(f"{kind} must contain a JSON object")
        out.append(value)
    return out


def _entry(projection: dict, rid: str) -> dict:
    requirements = projection.setdefault("requirements", {})
    return requirements.setdefault(rid, {
        "requirement_id": rid, "current_revision": 0, "status": "draft",
        "revisions": {}, "approvals": [], "errors": [],
    })


def project_comments(comments: list[dict], previous: dict | None = None, *,
                     package_verifier=None, approval_verifier=None) -> dict:
    """Apply source-verified contracts and retry only unverified sources."""
    projection = copy.deepcopy(previous) if previous else {
        "schema_version": SCHEMA_VERSION, "requirements": {}, "errors": [],
    }
    projection.setdefault("schema_version", SCHEMA_VERSION)
    projection.setdefault("requirements", {})
    projection.setdefault("errors", [])
    seen = set(projection.setdefault("seen_comment_ids", []))
    retry = set()
    containers = [projection] + list((projection.get("requirements") or {}).values())
    for container in containers:
        for error in container.get("errors") or []:
            if not error.get("source_unverified"):
                continue
            try:
                retry.add(int(error.get("comment_id")))
            except (TypeError, ValueError):
                continue
    seen.difference_update(retry)

    def record_unverified(container: dict, error: dict) -> None:
        container["errors"] = [
            old for old in container.get("errors") or []
            if not (old.get("comment_id") == error.get("comment_id")
                    and old.get("error") == error.get("error")
                    and str(old.get("revision")) == str(error.get("revision")))]
        container.setdefault("errors", []).append(error)

    for comment in sorted(comments or [], key=lambda c: int(c.get("id") or 0)):
        try:
            cid = int(comment.get("id"))
        except (TypeError, ValueError):
            continue
        if cid in seen:
            continue
        unverified = False
        body = str(comment.get("body") or "")
        for kind in (PACKAGE_FENCE, APPROVAL_FENCE):
            try:
                values = blocks(body, kind)
            except RequirementError as exc:
                projection["errors"].append({"comment_id": cid, "error": str(exc)})
                continue
            for raw in values:
                rid = str(raw.get("requirement_id") or "")
                entry = _entry(projection, rid) if rid else None
                verifier = package_verifier if kind == PACKAGE_FENCE else approval_verifier
                if verifier is None or not verifier(comment):
                    error = {"comment_id": cid, "revision": raw.get("revision"),
                             "error": f"unverified {kind} source", "fatal": False,
                             "source_unverified": True}
                    record_unverified(entry if entry is not None else projection, error)
                    unverified = True
                    continue
                try:
                    if kind == PACKAGE_FENCE:
                        package = validate_package(raw)
                        revision = package["revision"]
                        existing = entry["revisions"].get(str(revision))
                        if existing and existing.get("requirement_hash") != package["requirement_hash"]:
                            raise RequirementError(
                                "same revision has different semantics; increment revision")
                        package["source_ref"] = f"issuecomment-{cid}"
                        package["source_comment_id"] = cid
                        package["source_verified"] = True
                        entry["revisions"][str(revision)] = package
                        if revision >= int(entry.get("current_revision") or 0):
                            entry["current_revision"] = revision
                        entry["errors"] = [e for e in entry.get("errors", [])
                                           if e.get("revision") != revision or e.get("fatal")]
                    else:
                        approval = validate_approval(raw)
                        approval["approval_ref"] = f"issuecomment-{cid}"
                        approval["comment_id"] = cid
                        approval["source_verified"] = True
                        entry["approvals"] = [a for a in entry.get("approvals", [])
                                              if a.get("comment_id") != cid]
                        entry["approvals"].append(approval)
                        entry["errors"] = [e for e in entry.get("errors", [])
                                           if e.get("comment_id") != cid or e.get("fatal")]
                except RequirementError as exc:
                    error = {"comment_id": cid, "error": str(exc),
                             "revision": raw.get("revision"),
                             "fatal": kind == PACKAGE_FENCE}
                    if entry is not None:
                        entry.setdefault("errors", []).append(error)
                        if kind == PACKAGE_FENCE:
                            try:
                                failed_revision = int(raw.get("revision"))
                            except (TypeError, ValueError):
                                failed_revision = 0
                            if failed_revision >= int(entry.get("current_revision") or 0):
                                entry["current_revision"] = failed_revision
                    else:
                        projection["errors"].append(error)
        if not unverified:
            seen.add(cid)
    for rid in list(projection.get("requirements") or {}):
        _refresh_entry(projection, rid)
    projection["seen_comment_ids"] = sorted(seen)
    projection["projected_at"] = now()
    return projection


def _refresh_entry(projection: dict, rid: str) -> None:
    entry = projection["requirements"][rid]
    revision = int(entry.get("current_revision") or 0)
    fatal = next((e for e in entry.get("errors") or []
                  if e.get("fatal") and str(e.get("revision")) == str(revision)), None)
    if fatal:
        package = copy.deepcopy((entry.get("revisions") or {}).get(str(revision)) or {
            "requirement_id": rid, "revision": revision, "requirement_hash": ""})
        package.update(status="invalid", approval_ref="")
        entry["status"] = "invalid"
        entry["current"] = package
        entry.pop("approval", None)
        return
    package = (entry.get("revisions") or {}).get(str(revision))
    if not package:
        entry["status"] = "invalid" if entry.get("errors") else "draft"
        return
    status = str(package.get("status") or "draft")
    approval = next((a for a in reversed(entry.get("approvals") or [])
                     if a.get("revision") == revision
                     and a.get("requirement_hash") == package.get("requirement_hash")
                     and a.get("package_ref") == package.get("source_ref")), None)
    if (approval is not None and status in ("ready", "approved")
            and package.get("source_verified") is True
            and approval.get("source_verified") is True):
        package = copy.deepcopy(package)
        package["status"] = "approved"
        package["approval_ref"] = approval["approval_ref"]
        package["approval_source_verified"] = True
        entry["revisions"][str(revision)] = package
        entry["approval"] = approval
        status = package["status"]
    else:
        entry.pop("approval", None)
        package = copy.deepcopy(package)
        package["approval_ref"] = ""
        package.pop("approval_source_verified", None)
        entry["revisions"][str(revision)] = package
    entry["status"] = status
    entry["current"] = package


def current(projection: dict, requirement_id: str) -> dict | None:
    entry = (projection.get("requirements") or {}).get(requirement_id) or {}
    package = entry.get("current")
    return copy.deepcopy(package) if isinstance(package, dict) else None


def status(projection: dict, requirement_id: str) -> str:
    entry = (projection.get("requirements") or {}).get(requirement_id) or {}
    return str(entry.get("status") or "draft")


def dispatch_packet(projection: dict, requirement_id: str, assignment: str,
                    *, role: str = "worker", profile: str = "worker") -> dict:
    package = current(projection, requirement_id)
    if package is None:
        raise RequirementError(f"requirement not found: {requirement_id}")
    if str(package.get("status")) not in ("approved", "dispatched"):
        raise RequirementError("requirement is READY but not approved")
    if (package.get("source_verified") is not True
            or package.get("approval_source_verified") is not True):
        raise RequirementError("requirement or approval source is unverified")
    validate_package(package, approved=True)
    return {
        "requirement_id": requirement_id,
        "requirement_ref": package["source_ref"],
        "revision": package["revision"],
        "requirement_hash": package["requirement_hash"],
        "approval_ref": package["approval_ref"],
        "assignment": str(assignment), "role": str(role), "profile": str(profile),
        "review_policy": copy.deepcopy(package["review_policy"]),
    }


_REFERENCE_RE = re.compile(
    r"<!--\s*requirement:\s*([^\s]+)\s+revision:\s*([1-9][0-9]*)\s+"
    r"hash:\s*(sha256:[0-9a-f]{64})\s*-->", re.I)


def reference_marker(package: dict) -> str:
    return (f"<!-- requirement: {package['requirement_id']} "
            f"revision: {int(package['revision'])} "
            f"hash: {package['requirement_hash']} -->")


def event_reference(body: str) -> dict | None:
    matches = list(_REFERENCE_RE.finditer(str(body or "")))
    if len(matches) != 1:
        return None
    match = matches[0]
    return {"requirement_id": match.group(1), "revision": int(match.group(2)),
            "requirement_hash": match.group(3).lower()}


def reference_matches(reference: dict | None, record: dict | None) -> bool:
    if not reference or not record:
        return False
    return (reference.get("requirement_id") == record.get("requirement_id")
            and reference.get("revision") == record.get("revision")
            and reference.get("requirement_hash") == record.get("requirement_hash"))


def version_matches(assignment: dict, package: dict | None) -> tuple[bool, str]:
    if not package:
        return False, "requirement package is missing"
    expected = (str(assignment.get("requirement_id") or ""),
                assignment.get("revision"), str(assignment.get("requirement_hash") or ""))
    actual = (str(package.get("requirement_id") or ""), package.get("revision"),
              str(package.get("requirement_hash") or ""))
    if expected != actual:
        return False, f"requirement version mismatch: assignment={expected}, current={actual}"
    return True, "same requirement revision"


def save_projection(root: str | Path, projection: dict) -> Path:
    path = Path(root) / "requirements.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(projection, ensure_ascii=False, indent=2,
                              sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def load_projection(root: str | Path) -> dict:
    path = Path(root) / "requirements.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema_version": SCHEMA_VERSION, "requirements": {},
                "seen_comment_ids": [], "errors": []}
    except (OSError, ValueError) as exc:
        raise RequirementError(f"cannot read requirement projection {path}: {exc}") from None
    if not isinstance(data, dict):
        raise RequirementError(f"requirement projection {path} must be an object")
    return data


def main(argv=None) -> int:
    from . import requirement_cli
    return requirement_cli.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
