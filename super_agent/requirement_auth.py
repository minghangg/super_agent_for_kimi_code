"""Source attribution for requirement packages and supervisor approvals."""
from __future__ import annotations

from . import markers
from . import session_ledger


def claimed_key(comment: dict) -> str:
    return str(comment.get("session_key")
               or markers.read_session_token(str(comment.get("body") or "")) or "").strip()


def at_verifier(cfg: dict, requirement_id: str, project: str):
    """Accept only the currently bound AT key for this exact Issue."""
    repo = requirement_id.rsplit("#", 1)[0]
    issue = requirement_id.rsplit("#", 1)[1]
    keys = [requirement_id, f"{project}#{issue}", f"{repo.rsplit('/', 1)[-1]}#{issue}"]
    database = session_ledger.load_issue_sessions(cfg)

    def verify(comment: dict) -> bool:
        claimed = claimed_key(comment)
        if not claimed:
            return False
        for key in keys:
            entry = database.get(key) or {}
            at = entry.get("at") if isinstance(entry, dict) else {}
            if isinstance(at, dict) and at.get("session") and claimed == str(at.get("name") or ""):
                return True
        return False
    return verify


def supervisor_keys(cfg: dict) -> set[str]:
    engine = cfg.get("engine") or {}
    keys = {str(key) for key in engine.get("supervisor_keys") or ["supervisor"]}
    sid = str(engine.get("supervisor_sid") or "")
    if sid:
        try:
            sessions = session_ledger.load_sessions(cfg)
        except Exception:  # noqa: BLE001 - absence is simply no additional key
            sessions = {}
        keys.update(str(name) for name, entry in sessions.items()
                    if isinstance(entry, dict)
                    and str(entry.get("id") or entry.get("session") or "") == sid)
    return {key for key in keys if key}


def supervisor_verifier(cfg: dict):
    keys = supervisor_keys(cfg)

    def verify(comment: dict) -> bool:
        claimed = claimed_key(comment)
        return bool(claimed) and claimed in keys
    return verify
