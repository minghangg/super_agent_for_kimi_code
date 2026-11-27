"""Issue-space key normalisation for the WSS daemon (moved out in #138).

Every key the daemon publishes or compares - ``owner/repo#N``, the short
``repo#N`` a legacy writer may have used, the ``…#evidence`` delivery key - is
built here, and nowhere else. Two repositories that share a name must never
collide, and a short key must never silently match the wrong owner; both rules
live in these pure functions, which is why they are worth a module of their own
rather than 157 lines in the middle of the daemon.

Split out under ``worker.md`` §3.1.1 (a change to ``wss_bus`` must leave it
smaller and extract real modules). The bodies are moved verbatim; the only two
host seams are injected through :func:`bind`, because the binding table's path
is the host's to resolve and a second resolution here could read production.
"""
from __future__ import annotations

import json
from pathlib import Path

_CFG = None
_STATE_ROOT = None


def bind(*, cfg, state_root) -> None:
    """Install the two host seams. Idempotent; the host calls this after import."""
    global _CFG, _STATE_ROOT
    _CFG = cfg
    _STATE_ROOT = state_root


def _cfg():
    if _CFG is None:
        raise RuntimeError("wss_bus_parts.issue_keys: 未 bind 宿主，拒绝猜配置")
    return _CFG()


def current_state_root() -> Path:
    if _STATE_ROOT is None:
        raise RuntimeError("wss_bus_parts.issue_keys: 未 bind 宿主，拒绝猜状态根")
    return _STATE_ROOT()


def repo_full_of(repo: str) -> str:
    """Normalise ``owner/repo`` for key building: lowercase, slash-preserved."""
    raw = str(repo or "").strip()
    if "/" not in raw:
        return ""
    owner, _, name = raw.rpartition("/")
    owner = owner.strip().lower()
    name = name.strip().lower()
    if not owner or not name:
        return ""
    return f"{owner}/{name}"


def project_of_repo(repo: str) -> str:
    """``owner/repo`` → ``repo`` (lowercased). Used only for read-only compat."""
    full = repo_full_of(repo)
    return full.rsplit("/", 1)[-1] if full else ""


def issue_key(repo: str, issue) -> str:
    """Canonical, cross-repository-safe key: ``owner/repo#issue``."""
    full = repo_full_of(repo)
    num = str(issue if issue is not None else "").strip()
    if not full or not num:
        return ""
    return f"{full}#{num}"


def legacy_issue_keys(repo: str, issue) -> list[str]:
    """Short keys a pre-migration writer may have used — **read-only** compat."""
    num = str(issue if issue is not None else "").strip()
    out: list[str] = []
    full = repo_full_of(repo)
    proj = project_of_repo(repo)
    for short in (full, proj):
        if short and num:
            out.append(f"{short}#{num}")
    return out


def _record_repo_full(key: str, rec) -> str:
    """Best-effort ``owner/repo`` of a stored record (key or payload)."""
    if isinstance(rec, dict):
        val = rec.get("repo_full") or rec.get("repo") or ""
        if val:
            return repo_full_of(str(val))
    if isinstance(key, str) and "/" in key and "#" in key:
        return repo_full_of(key.split("#", 1)[0])
    return ""


def _normalised_bindings(db: dict) -> dict:
    """Index a binding table under case-folded keys (qualified and short alike)."""
    out: dict = {}
    for k, v in db.items():
        if not isinstance(k, str) or "#" not in k:
            continue
        head, _, num = k.rpartition("#")
        full = repo_full_of(head) if "/" in head else ""
        norm = f"{full}#{num.strip()}" if full else f"{head.strip().lower()}#{num.strip()}"
        out[norm] = v
    return out


def lookup_issue_record(db: dict | None, repo: str, issue) -> dict | None:
    """Binding lookup: fully-qualified key first, short keys only when unambiguous.

    A short key such as ``demo#12`` cannot tell two owners apart. It is therefore
    read **only** when no other repository could own it; a short key that is
    ambiguous, or that points at a different owner, returns ``None`` instead of
    silently matching the wrong repository. That is the anti-crosstalk rule.
    """
    if not isinstance(db, dict):
        return None
    norm = _normalised_bindings(db)
    full_key = issue_key(repo, issue)
    if full_key and isinstance(norm.get(full_key), dict):
        return norm[full_key]
    proj = project_of_repo(repo)
    num = str(issue if issue is not None else "").strip()
    if not proj or not num:
        return None
    short = f"{proj}#{num}"
    rec = norm.get(short)
    if not isinstance(rec, dict):
        return None
    # Every record that could own this short key: the bare spelling plus any
    # qualified spelling of the same repository name and issue number.
    owners: set[str] = set()
    suffix = f"/{short}"
    for k in norm:
        if k == short:
            continue
        if k.endswith(suffix):
            owners.add(k[: -len(suffix)])
    want = repo_full_of(repo)
    if owners:
        # Another owner claims the same repo name and issue number ⇒ ambiguous.
        return None
    return rec


def bind_worker(repo: str, issue, bind_path: Path | None = None) -> tuple[str, str]:
    """``(worker name, session)`` from the binding table, or ``("unknown", "")``."""
    cfg = _cfg()
    if cfg.bind_lookup is not None:
        try:
            res = cfg.bind_lookup(repo, issue)
        except Exception:                       # noqa: BLE001  injected, best effort
            return "unknown", ""
        if isinstance(res, tuple) and len(res) == 2:
            return str(res[0] or "unknown"), str(res[1] or "")
        return "unknown", ""
    path = Path(bind_path) if bind_path else current_state_root() / cfg.bind_file
    try:
        db = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unknown", ""
    rec = lookup_issue_record(db, repo, issue) or {}
    worker = rec.get("worker") if isinstance(rec, dict) else None
    if not isinstance(worker, dict):
        return "unknown", ""
    return str(worker.get("name") or "unknown"), str(worker.get("session") or "")


def delivery_key(repo: str, issue, evidence: str = "") -> str:
    """Key used by delivery bookkeeping (nudge / undelivered / pending)."""
    key = issue_key(repo, issue)
    ev = str(evidence or "").strip()
    return f"{key}#{ev}" if key and ev else key


def qualify_event_payload(payload: dict, repo: str = "", issue=None) -> dict:
    """Return a payload whose issue-space keys are fully qualified.

    The original ``project`` field is preserved; ``repo_full`` and ``key`` are
    added so two repositories that share a name can never collide.
    """
    if not isinstance(payload, dict):
        return {}
    out = dict(payload)
    raw_repo = str(repo or out.get("repo_full") or out.get("repo") or "")
    num = issue if issue is not None else out.get("issue")
    full = repo_full_of(raw_repo)
    if not full:
        proj = str(out.get("project") or "")
        mapper = _cfg().repo_full_of_project
        if proj and callable(mapper):
            try:
                full = repo_full_of(str(mapper(proj) or ""))
            except Exception:                   # noqa: BLE001  injected, best effort
                full = ""
    out.setdefault("project", project_of_repo(raw_repo) or str(out.get("project") or ""))
    out["repo_full"] = full
    if full and num not in (None, ""):
        out["key"] = issue_key(full, num)
    return out
