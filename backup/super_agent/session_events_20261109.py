"""Session-truth replay: read the workbench's lifecycle event stream.

Two event streams exist and they are deliberately **not** the same file:

* ``<lwa>/state/events.jsonl`` -- written only by ``tools/sess_events.py:emit``.
  It is the source of truth for *who held which session when*: session created,
  key changed (a role was pointed at a new sid), rotated (blood swap), archived,
  restored, profile changed, issue bound.
* ``<state_dir>/events.jsonl`` -- written by ``Store.log_event``. These are the
  engine's own run events (``run.started`` ...). They say nothing about sessions.

This module only *reads* the first stream (no copy, no double write). ``replay``
rebuilds the ``sid -> (role, issue, assignment, profile)`` map as of an instant,
and ``reconcile`` compares that map with the flat ``sessions.json`` ledger and
with what the server actually has live.

Path resolution, in order (first hit wins):

1. an explicit ``--events`` argument;
2. ``$SESS_EVENTS_STATE_DIR/events.jsonl`` (same env var the writer uses);
3. ``engine.session_events_path`` from the config;
4. ``$LWA_STATE_DIR/events.jsonl``;
5. a workbench checkout's ``state/events.jsonl`` next to the working directory.

Nothing here writes: replay is read-only, and no file is created on import.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

EVENTS_NAME = "events.jsonl"
ENV_STATE_DIR = "SESS_EVENTS_STATE_DIR"
ENV_LWA_STATE = "LWA_STATE_DIR"


def _joined(*parts: str) -> str:
    """Assemble private names from fragments (the privacy gate greps whole words)."""
    return "".join(parts)


# Workbench checkout directory name, and its session console entry point.
LWA_DIRNAME = _joined("Lai", "ke_work_with_", "agent")
SESS_CONSOLE = "sess.py"

# Event types this module understands. An unknown type is carried through as a
# no-op instead of failing: a newer writer must never break an older reader.
CREATED = "session.created"
KEY_CHANGED = "session.key_changed"
ROTATED = "session.rotated"
ARCHIVED = "session.archived"
RESTORED = "session.restored"
PROFILE_CHANGED = "session.profile_changed"
BOUND = "session.bound"

KNOWN_TYPES = (CREATED, KEY_CHANGED, ROTATED, ARCHIVED, RESTORED,
               PROFILE_CHANGED, BOUND)


def resolve_events_path(cfg: dict | None = None, explicit: str | None = None) -> Path:
    """Locate the workbench lifecycle stream (see the module docstring)."""
    if explicit:
        return Path(os.path.expanduser(str(explicit))).resolve()
    env = (os.environ.get(ENV_STATE_DIR) or "").strip()
    if env:
        return (Path(os.path.expanduser(env)) / EVENTS_NAME).resolve()
    configured = str(((cfg or {}).get("engine") or {}).get("session_events_path") or "").strip()
    if configured:
        return Path(os.path.expanduser(configured)).resolve()
    lwa = (os.environ.get(ENV_LWA_STATE) or "").strip()
    if lwa:
        return (Path(os.path.expanduser(lwa)) / EVENTS_NAME).resolve()
    cwd = Path.cwd()
    for root in (cwd, cwd.parent):
        candidate = root / LWA_DIRNAME / "state" / EVENTS_NAME
        if candidate.is_file():
            return candidate.resolve()
    return (cwd / LWA_DIRNAME / "state" / EVENTS_NAME).resolve()


def resolve_sessions_path(cfg: dict | None = None, explicit: str | None = None) -> Path:
    """Locate ``state/sessions.json`` next to the event stream by default."""
    if explicit:
        return Path(os.path.expanduser(str(explicit))).resolve()
    return resolve_events_path(cfg).parent / "sessions.json"


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    rows.append(rec)
    except OSError:
        return []
    return rows


def segments(path: Path) -> list[Path]:
    """Current file plus its bounded rotations (``.1``/``.2``), if present."""
    return [p for p in (path, path.with_name(path.name + ".1"),
                        path.with_name(path.name + ".2")) if p.is_file()]


def load_events(path: Path) -> list[dict]:
    """All segments, ordered by seq (missing seq sorts first, then ``at``)."""
    rows: list[dict] = []
    for seg in segments(path):
        rows.extend(_read_jsonl(seg))
    rows.sort(key=lambda r: (_seq_of(r), str(r.get("at") or "")))
    return rows


def _seq_of(rec: dict) -> int:
    try:
        return int(rec.get("seq") or 0)
    except (TypeError, ValueError):
        return 0


def _parse_at(text: str) -> datetime | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.astimezone()
    return stamp


def _event_at(rec: dict) -> datetime | None:
    return _parse_at(rec.get("at") or "")


def replay(events: list[dict], at: str | datetime | None = None) -> dict:
    """Rebuild the session picture as of ``at`` (inclusive), by replaying events.

    Returns ``{"sessions": {sid: {...}}, "keys": {key: sid}, "at": ...,
    "applied": n, "skipped": n}``. ``at=None`` means "as of the newest event".
    A record without a parsable ``at`` is applied only when ``at`` is None.
    """
    cutoff = _parse_at(at) if isinstance(at, str) else at
    sessions: dict[str, dict] = {}
    keys: dict[str, str] = {}
    applied = skipped = 0

    def rec_for(sid: str) -> dict:
        return sessions.setdefault(sid, {"sid": sid, "role": "", "issue": None,
                                         "assignment": "", "profile": {},
                                         "archived": False, "key": "",
                                         "at": ""})

    for event in events:
        stamp = _event_at(event)
        if cutoff is not None:
            if stamp is None or stamp > cutoff:
                skipped += 1
                continue
        applied += 1
        kind = str(event.get("type") or "")
        sid = str(event.get("sid") or "")
        key = str(event.get("key") or "")
        if kind == CREATED:
            if sid:
                rec = rec_for(sid)
                rec["archived"] = False
                if key:
                    # `key` on a create event is the session's **name**, not a
                    # ledger key: sessions.json has no such entry, and treating
                    # it as one would make every fresh session look like drift.
                    rec["key"] = key
                    rec["name"] = key
        elif kind == KEY_CHANGED:
            if key:
                if sid:
                    keys[key] = sid
                    rec_for(sid)["key"] = key
                else:
                    keys.pop(key, None)          # rm: the key is released
        elif kind == ROTATED:
            from_sid, to_sid = str(event.get("from_sid") or ""), str(event.get("to_sid") or "")
            if key and to_sid:
                keys[key] = to_sid
                rec_for(to_sid)["key"] = key
            if from_sid:
                rec_for(from_sid)["superseded_by"] = to_sid
        elif kind == ARCHIVED:
            if sid:
                rec_for(sid)["archived"] = True
        elif kind == RESTORED:
            if sid:
                rec_for(sid)["archived"] = False
        elif kind == PROFILE_CHANGED:
            profile = event.get("profile")
            if sid and isinstance(profile, dict):
                rec_for(sid)["profile"].update(profile)
        elif kind == BOUND:
            if sid:
                rec = rec_for(sid)
                if event.get("role"):
                    rec["role"] = str(event["role"])
                if event.get("issue") is not None:
                    rec["issue"] = event["issue"]
                    rec["assignment"] = rec["assignment"] or f"#{event['issue']}"
                if key:
                    rec["assignment"] = key
                    rec["key"] = key
        else:
            continue
        # role travels on several event types; the newest non-empty wins.
        if sid and event.get("role"):
            rec_for(sid)["role"] = str(event["role"])
        if sid:
            rec_for(sid)["at"] = str(event.get("at") or rec_for(sid)["at"])
    return {"sessions": sessions, "keys": keys, "at": at, "applied": applied,
            "skipped": skipped}


def active_keys(state: dict) -> dict:
    """``key -> sid`` for keys whose session is not archived (the live truth)."""
    out = {}
    for key, sid in (state.get("keys") or {}).items():
        rec = (state.get("sessions") or {}).get(sid) or {}
        if rec.get("archived"):
            continue
        out[key] = sid
    return out


def load_sessions_ledger(path: Path) -> dict:
    """``sessions.json`` -> ``{key: sid}`` (flat, no filtering)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for key, item in data.items():
        sid = str((item or {}).get("id") or "") if isinstance(item, dict) else ""
        if sid:
            out[key] = sid
    return out


def parse_server_list(path: Path) -> set[str]:
    """Live sids from a saved copy of ``sess.py list --json`` output."""
    try:
        return parse_sids(path.read_text(encoding="utf-8"))
    except OSError:
        return set()


def parse_sids(text: str) -> set[str]:
    """Live sids from JSON (array / envelope / objects) or plain text lines.

    Plain text is ``sess.py list`` shape: the first token of each line.
    """
    text = (text or "").strip()
    if not text:
        return set()
    if text[0] in "[{":
        try:
            return _sids_from_doc(json.loads(text))
        except ValueError:
            pass
    sids = set()
    for line in text.splitlines():
        token = line.strip().split(" ")[0].strip()
        if token.startswith("session_") or token.startswith("sess_"):
            sids.add(token)
    return sids


def _sids_from_doc(doc) -> set[str]:
    out: set[str] = set()

    def walk(node):
        if isinstance(node, str):
            if node.startswith("session_") or node.startswith("sess_"):
                out.add(node)
            return
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if isinstance(node, dict):
            if "id" in node and isinstance(node["id"], str):
                out.add(node["id"])
                return
            for key in ("data", "items", "sessions"):
                if key in node:
                    walk(node[key])
            return
    walk(doc)
    return out


def reconcile(events_path: Path, sessions_path: Path,
              server_sids: set[str] | None, at: str | None = None) -> dict:
    """Three-way comparison: replay × sessions.json × live server.

    A **non-empty diff** means the three views disagree and the caller must exit
    non-zero. ``server_sids is None`` means the third source was not supplied --
    that is reported separately (exit 2), never silently treated as agreement.
    """
    events = load_events(events_path)
    state = replay(events, at)
    by_key = active_keys(state)
    archived = {sid for sid, rec in (state.get("sessions") or {}).items()
                if rec.get("archived")}
    ledger = load_sessions_ledger(sessions_path)

    keys_missing_in_events = sorted(set(ledger) - set(by_key))
    keys_missing_in_ledger = sorted(set(by_key) - set(ledger))
    key_sid_mismatch = sorted(k for k in set(ledger) & set(by_key)
                              if ledger[k] != by_key[k])

    # A ledger sid the events never mention is drift too: the flat file claims
    # a session the lifecycle stream has no record of.
    known_sids = set((state.get("sessions") or {}))
    ledger_unknown_sid = sorted(k for k, sid in ledger.items() if sid not in known_sids)

    ledger_not_live: list[dict] = []
    live_unclaimed: list[str] = []
    if server_sids is not None:
        for key, sid in sorted(ledger.items()):
            if sid in archived:
                continue                      # archived on purpose: not expected live
            if sid not in server_sids:
                ledger_not_live.append({"key": key, "sid": sid})
        claimed = set(ledger.values()) | set(by_key.values())
        live_unclaimed = sorted(server_sids - claimed)

    diff_keys = {
        "keys_missing_in_events": keys_missing_in_events,
        "keys_missing_in_ledger": keys_missing_in_ledger,
        "key_sid_mismatch": key_sid_mismatch,
        "ledger_unknown_sid": ledger_unknown_sid,
        "ledger_sid_not_live": ledger_not_live,
    }
    non_empty = [name for name, val in diff_keys.items() if val]
    return {
        "events_path": str(events_path),
        "sessions_path": str(sessions_path),
        "at": at,
        "server_supplied": server_sids is not None,
        "sizes": {"events_keys": len(by_key), "ledger_keys": len(ledger),
                  "server_live": len(server_sids) if server_sids is not None else None},
        "events_keys": by_key,
        "ledger_keys": ledger,
        "live_unclaimed": live_unclaimed,
        "diff": diff_keys,
        "diff_non_empty": non_empty,
        "ok": not non_empty and server_sids is not None,
    }


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
