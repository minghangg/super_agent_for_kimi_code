"""Write side of the session state the issue CLI shares with the engine.

``bind`` records which session works on which issue, ``close`` archives those
sessions, and ``comment`` keeps the ledger row. All three write into the **same
session root the engine reads**: ``issue_sessions.json`` sits beside
``sessions.json`` (``engine._issue_sessions_path``), and that root is resolved
by :func:`super_agent.session_events.resolve_session_root` -- the one function
the engine itself uses.

Writing through a second resolution chain is how a binding lands where the
engine never looks, and the symptom is silence: ``assignment.unmatched``, no
delivery recorded, no reminder to anyone (#92). So this module has no path
logic of its own; every function here calls ``session_events`` and nothing
else.

The event stream is written by ``emit`` with the same payload whitelist the
workbench writer enforces (scalars only, ``seq`` assigned under a file lock).
A second writer that formats the same file differently would make ``replay``
skip rows; the payload check here is what keeps the two writers compatible
without copying the whole reader.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
from pathlib import Path

from . import session_events

EVENTS_NAME = "events.jsonl"
ISSUE_SESSIONS_NAME = "issue_sessions.json"
LEDGER_NAME = "ledger.jsonl"
LOCK_NAME = "events.lock"
LOCK_TIMEOUT_S = 15.0
MAX_EVENT_LINES = 10_000

# The one shape a session id may have. Same pattern as
# ``engine.SID_RE`` and the workbench writer; a truncated id can never be
# delivered to, so it must not enter the ledger either.
FULL_SID_RE = re.compile(
    r"^session_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE)
SID_SHAPE_HINT = "session_<8>-<4>-<4>-<4>-<12>（小写十六进制 UUID，共 44 字符）"

PAYLOAD_KEYS = ("seq", "at", "type", "sid", "role", "issue", "assignment",
                "profile", "from_sid", "to_sid", "key")


class SessionLedgerError(RuntimeError):
    """The session root is not configured, or the write was refused."""


def sid_problem(sid) -> str:
    """``""`` when ``sid`` has the full shape, else why it does not."""
    text = str(sid or "")
    if FULL_SID_RE.match(text):
        return ""
    return (f"收到的值：{text!r}（{len(text)} 字符）；期望形态：{SID_SHAPE_HINT}")


def root(cfg: dict | None = None) -> Path:
    """The session root, resolved exactly as the engine resolves it."""
    try:
        return session_events.resolve_session_root(cfg)
    except session_events.SessionStreamNotConfigured as exc:
        raise SessionLedgerError(
            f"{exc}；CLI 侧四个入口：$SESS_EVENTS_STATE_DIR、$LWA_STATE_DIR、"
            f"配置 engine.session_state_dir、配置 engine.session_events_path") from None


def sessions_path(cfg: dict | None = None) -> Path:
    return session_events.resolve_sessions_path(cfg)


def issue_sessions_path(cfg: dict | None = None) -> Path:
    """``issue_sessions.json`` beside ``sessions.json`` (engine reads it there)."""
    return sessions_path(cfg).parent / ISSUE_SESSIONS_NAME


def ledger_path(cfg: dict | None = None) -> Path:
    return root(cfg) / LEDGER_NAME


def events_path(cfg: dict | None = None) -> Path:
    return root(cfg) / EVENTS_NAME


# ------------------------------------------------------------------- reads
def load_issue_sessions(cfg: dict | None = None) -> dict:
    try:
        data = json.loads(issue_sessions_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_issue_sessions(data: dict, cfg: dict | None = None) -> Path:
    path = issue_sessions_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
                    encoding="utf-8")
    return path


def load_sessions(cfg: dict | None = None) -> dict:
    try:
        data = json.loads(sessions_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def register_session_key(name: str, sid: str, cfg: dict | None = None) -> str:
    """Record ``name -> sid`` in ``sessions.json``; returns a one-line report.

    Equivalent to the workbench's ``sessions.py set`` without the CLI: the
    event has already been emitted by ``bind``. Idempotent, and a failure is
    reported instead of raised -- the registration is not a precondition of
    the binding itself.
    """
    key = str(name or "").strip()
    if not key:
        return ""
    path = sessions_path(cfg)
    data = load_sessions(cfg)
    old = data.get(key)
    old_sid = str((old or {}).get("id") or "") if isinstance(old, dict) else ""
    data[key] = {"id": sid,
                 "updated": dt.datetime.now().strftime("%Y-%m-%dT%H:%M"),
                 "note": (old.get("note") or "") if isinstance(old, dict) else ""}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    except OSError as exc:
        return f"[警告] 登记 {key} 失败：{type(exc).__name__}: {exc}"
    verb = "已改指" if old_sid and old_sid != sid else "已登记"
    return f"{verb}会话键 {key} → {sid[:12]}"


def fixed_session_ids(is_fixed, cfg: dict | None = None) -> dict:
    """``{sid: name}`` for registered long-lived sessions.

    ``is_fixed`` is the predicate from ``role_config``: closing an issue must
    archive one-shot workers only. Archiving a registered assistant took the
    whole team offline once (#9), and archive is not reversible.
    """
    out: dict[str, str] = {}
    for name, item in load_sessions(cfg).items():
        if not is_fixed(name):
            continue
        sid = (item or {}).get("id") if isinstance(item, dict) else None
        if sid:
            out[str(sid)] = str(name)
    return out


# ------------------------------------------------------------------ writes
def ledger(row: dict, cfg: dict | None = None) -> None:
    """Append one ledger row (``t/role/act/repo/issue/ok/note``)."""
    line = {"t": dt.datetime.now().isoformat(timespec="seconds")}
    line.update(row)
    path = ledger_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")


class _FileLock:
    """Cross-process lock around "read last seq, append one line"."""

    def __init__(self, path: Path, timeout: float = LOCK_TIMEOUT_S):
        self.path = Path(path)
        self.timeout = timeout
        self._fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        if os.path.getsize(self.path) == 0:
            fh.write(b"0")
            fh.flush()
        deadline = time.time() + self.timeout
        while True:
            try:
                fh.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() >= deadline:
                    fh.close()
                    raise SessionLedgerError(
                        f"events lock busy > {self.timeout}s: {self.path}") from None
                time.sleep(0.01)
        self._fh = fh
        return self

    def __exit__(self, *_exc):
        fh, self._fh = self._fh, None
        if fh is None:
            return False
        try:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            fh.close()
        return False


def _last_seq(path: Path) -> int:
    seq = 0
    for seg in (path.with_name(path.name + ".1"),
                path.with_name(path.name + ".2"), path):
        if not seg.is_file():
            continue
        try:
            with open(seg, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    try:
                        seq = max(seq, int(rec.get("seq") or 0))
                    except (TypeError, ValueError):
                        continue
        except OSError:
            continue
    return seq


def emit(event_type: str, *, cfg: dict | None = None, **fields) -> dict:
    """Append one lifecycle event; ``seq`` is assigned under the file lock.

    Only scalar payload keys are accepted, and only the whitelist above: a
    body or a token in the stream is a leak that no later cleanup can undo.
    """
    payload = {}
    for key, value in fields.items():
        if key not in PAYLOAD_KEYS or key in ("seq", "at", "type"):
            raise SessionLedgerError(f"emit: 字段 {key!r} 不在白名单内")
        if isinstance(value, (dict, list, tuple, set)):
            raise SessionLedgerError(f"emit: 字段 {key} 只接受标量")
        if value is not None:
            payload[key] = value
    path = events_path(cfg)
    with _FileLock(path.with_name(LOCK_NAME)):
        record = {"seq": _last_seq(path) + 1,
                  "at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                  "type": str(event_type)}
        record.update(payload)
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(path)
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return record


def _rotate_if_needed(path: Path) -> None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            count = sum(1 for _ in fh)
    except OSError:
        return
    if count < MAX_EVENT_LINES:
        return
    for level in (2, 1):
        src = path.with_name(path.name if level == 1 else path.name + ".1")
        dst = path.with_name(path.name + f".{level}")
        if src.is_file():
            os.replace(src, dst)
