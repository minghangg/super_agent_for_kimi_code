"""Source attribution for requirement packages and supervisor approvals."""
from __future__ import annotations

from . import markers
from . import session_events
from . import session_ledger
from . import sessionkeys


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
    # No built-in supervisor name. A sid locates the session; it never exempts
    # the corresponding ledger key from the random-tail requirement.
    keys = {str(key) for key in engine.get("supervisor_keys") or []
            if sessionkeys.has_random_suffix(str(key))}
    sid = str(engine.get("supervisor_sid") or "")
    if sid:
        try:
            sessions = session_ledger.load_sessions(cfg)
        except Exception:  # noqa: BLE001 - absence is simply no additional key
            sessions = {}
        keys.update(str(name) for name, entry in sessions.items()
                    if isinstance(entry, dict)
                    and str(entry.get("id") or entry.get("session") or "") == sid
                    and sessionkeys.has_random_suffix(str(name)))
    return {key for key in keys if key}


def supervisor_verifier(cfg: dict):
    history = {"signature": None, "events": [], "verdicts": {}}

    def historical(comment: dict, claimed: str) -> bool:
        created = str(comment.get("created_at") or "").strip()
        if not created or session_events._parse_at(created) is None:
            return False
        if (sessionkeys.role_of(claimed) != "supervisor"
                or not sessionkeys.has_random_suffix(claimed)):
            return False
        sessions = session_ledger.load_sessions(cfg)
        entry = sessions.get(claimed) or {}
        sid = str(entry.get("id") or entry.get("session") or "")
        if not sid:
            return False
        try:
            path = session_events.resolve_events_path(cfg)
            signature = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size)
                              for p in session_events.segments(path))
            if signature != history["signature"]:
                history["events"] = session_events.load_events(path)
                history["signature"] = signature
                history["verdicts"] = {}
        except Exception:  # noqa: BLE001 - an unreadable history fails closed
            history["events"] = []
            history["verdicts"] = {}
            return False
        cache_key = (claimed, created)
        if cache_key in history["verdicts"]:
            return history["verdicts"][cache_key]
        state = session_events.replay(history["events"], created)
        rec = (state.get("sessions") or {}).get(sid) or {}
        if not rec or rec.get("archived"):
            result = False
        else:
            role = str(rec.get("role") or "")
            result = (role == "supervisor" if role else
                      sessionkeys.role_of(str(rec.get("created_key") or "")) == "supervisor")
        history["verdicts"][cache_key] = result
        return result

    def verify(comment: dict) -> bool:
        claimed = claimed_key(comment)
        if not claimed:
            return False
        if claimed in supervisor_keys(cfg):
            return True
        return historical(comment, claimed)
    return verify
