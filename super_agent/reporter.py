"""Read-only Reporter: first screen "what needs me", second screen statistics.

One rule decides the whole page: **an unknown source is shown as unknown, never
as "no action needed"**. The store's readers cannot carry that weight -
``load_index`` / ``load_outbox_state`` silently return an empty skeleton for a
damaged file, and ``health.read`` returns ``{}`` for both "missing" and
"corrupt". So :func:`probe_source_health` looks at the files themselves
(``is_file`` plus a strict parse plus an explicit structure check) and only a
positive answer from it lets the page print ``无需行动``.

Dismissal is not re-implemented here: the index list comes from
``engine.status(all=False)``, whose filter is ``needs.is_open``. One narrow gap
is closed on top of it, additively, in :func:`_index_items`.

``render`` is a pure function of the report (no I/O, no clock); everything that
is read is read in :func:`collect`. Nothing in this module writes: no
assignment, no issue, no index/outbox/assignment edit, no LLM call, and no
network beyond the comment source the caller injects. The only file that can
appear in the state dir is the allowlist last-good cache that
``Engine.__init__`` already maintains; this module never writes it itself.
"""
from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path

from . import assignments as asg_mod
from . import brief as brief_mod
from . import issuekey
from . import needs as needs_mod
from .github_source import NullSource

CATEGORY_USER = "user"
CATEGORY_ROLE = "role"
CATEGORY_BLOCKED = "blocked"
CATEGORIES = (CATEGORY_USER, CATEGORY_ROLE, CATEGORY_BLOCKED)

CATEGORY_TITLES = {
    CATEGORY_USER: "待你拍板",
    CATEGORY_ROLE: "待 {role} 裁决",
    CATEGORY_BLOCKED: "卡人工",
}

#: An item older than this is shown as 过期. Freshness is display only: it never
#: decides whether an item exists, so a wrong clock cannot hide a person's work.
ITEM_STALE_S = 24 * 3600

#: Assignment states that mean "a person has to step in". Both are members of
#: ``assignments.ACTIVE_STATES`` - a test pins that, so this list cannot drift
#: into claiming a state the engine would consider finished.
BLOCKED_STATES = ("blocked", "waiting_person")

#: Outbox states that ask the configured role to reconcile the remote side.
#: ``waiting_person`` is the person's, ``prepared`` is the engine's (replay).
ROLE_OUTBOX_STATES = ("in_flight", "uncertain", "reconcile")

_EXPIRY_TEXT = {"fresh": "新鲜", "stale": "过期", "unknown": "时间未知"}
_EXPIRY_MARK = {"fresh": "OK", "stale": "OLD", "unknown": "?"}

# Longer than this after the prefix and the run is treated as a credential.
_SECRET_PREFIXES = ("github_pat_", "ghp_", "gho_", "ghs_", "ghu_", "ghr_",
                    "sk-", "AKIA")
_SECRET_MIN_BODY = 8


# --------------------------------------------------------------- source probe
def _probe(path: Path, validate) -> dict:
    """``{path, exists, valid, error, data}`` for one JSON file.

    ``exists`` comes from ``is_file()``, never from a reader's empty return:
    "absent" and "present but empty" are different answers here, and only this
    one function is allowed to produce either.
    """
    out = {"path": str(path), "exists": False, "valid": False, "error": "",
           "data": None}
    try:
        out["exists"] = path.is_file()
    except OSError:
        out["exists"] = False
    if not out["exists"]:
        out["error"] = "不存在"
        return out
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        out["error"] = f"不可读: {type(exc).__name__}"
        return out
    try:
        data = json.loads(raw)
    except ValueError as exc:
        out["error"] = f"不是合法 JSON: {str(exc)[:80]}"
        return out
    if not isinstance(data, dict):
        out["error"] = "根节点不是对象"
        return out
    out["data"] = data
    problem = validate(data)
    if problem:
        out["error"] = problem
        return out
    out["valid"] = True
    return out


def _index_problem(data: dict) -> str:
    """Structural problems that make ``engine.status()`` unusable."""
    issues = data.get("issues", {})
    if not isinstance(issues, dict):
        return "结构非法: issues 不是对象"
    for key, rec in issues.items():
        if not isinstance(rec, dict):
            return f"结构非法: issues[{key!r}] 不是对象"
        rows = rec.get("needs_person")
        if rows is not None and not isinstance(rows, list):
            return f"结构非法: issues[{key!r}].needs_person 不是数组"
    assignments = data.get("assignments", {})
    if not isinstance(assignments, dict):
        return "结构非法: assignments 不是对象"
    for aid, rec in assignments.items():
        if not isinstance(rec, dict):
            return f"结构非法: assignments[{aid!r}] 不是对象"
    table = data.get(needs_mod.MARKS)
    if table is not None and not isinstance(table, dict):
        return "结构非法: 解除旁表不是对象"
    return ""


def _outbox_problem(data: dict) -> str:
    actions = data.get("actions", {})
    if not isinstance(actions, dict):
        return "结构非法: actions 不是对象"
    for aid, rec in actions.items():
        if not isinstance(rec, dict):
            return f"结构非法: actions[{aid!r}] 不是对象"
    attempts = data.get("attempts", {})
    if not isinstance(attempts, dict):
        return "结构非法: attempts 不是对象"
    return ""


def _health_problem(data: dict) -> str:
    # ``health.read`` would answer ``{}`` here too; an empty snapshot carries no
    # field at all, so it says nothing about the engine and reads as unknown.
    if not data:
        return "空对象（没有任何字段）"
    return ""


def probe_source_health(store) -> dict:
    """``{"index"|"outbox"|"health": {exists, valid, error, path, data}}``.

    Called before any reader runs. ``valid`` is only True for a file that is
    present, parses as a JSON object and passes its own structure check.
    """
    return {
        "index": _probe(store.index_path, _index_problem),
        "outbox": _probe(store.outbox_state_path, _outbox_problem),
        "health": _probe(store.health_path, _health_problem),
    }


def _index_ok(probe: dict) -> bool:
    """存在 ∧ 结构合法. A missing index is unknown, not "empty"."""
    return bool(probe.get("exists") and probe.get("valid"))


def _outbox_ok(probe: dict) -> bool:
    """合法. A missing ``outbox_state.json`` is an empty outbox, not unknown.

    That asymmetry is deliberate: no file means nothing was ever dispatched, so
    there is provably nothing unfinished. A *present* file that does not parse
    is the opposite - it may hold a waiting action - and only a strict parse
    can tell the two apart. The report says which case it took.
    """
    return (not probe.get("exists")) or bool(probe.get("valid"))


# ------------------------------------------------------------------- time/mark
def _timestamp(value):
    """Epoch seconds or ISO-8601 -> aware UTC datetime; None when unusable."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def _expiry(value, now: datetime) -> dict:
    """Three states, never two: fresh / stale / unknown."""
    stamp = _timestamp(value)
    if stamp is None:
        return {"at": "", "state": "unknown", "age_s": None}
    age = max(0.0, (now - stamp).total_seconds())
    return {"at": stamp.isoformat(timespec="seconds"),
            "state": "stale" if age > ITEM_STALE_S else "fresh",
            "age_s": round(age, 1)}


def _mask(text) -> str:
    """Drop credential-shaped runs, prefix included.

    ``brief.redact`` keeps the prefix (``sk-<redacted>``); a page handed to a
    person should not carry the prefix of the secret that leaked either.
    """
    out = str(text or "")
    for prefix in _SECRET_PREFIXES:
        idx = 0
        while True:
            at = out.find(prefix, idx)
            if at < 0:
                break
            end = at + len(prefix)
            while end < len(out) and (out[end].isalnum() or out[end] in "_-"):
                end += 1
            if end - (at + len(prefix)) >= _SECRET_MIN_BODY:
                out = out[:at] + "<redacted>" + out[end:]
                idx = at + len("<redacted>")
            else:
                idx = end
    return out


def _clip(text, limit: int = 120) -> str:
    raw = " ".join(_mask(text).split())
    return raw if len(raw) <= limit else raw[: max(0, limit - 1)] + "…"


def _first_line(body) -> str:
    """The first non-empty line, same take as ``brief`` does for a card."""
    text = str(body or "").lstrip()
    lines = text.splitlines()
    return lines[0].strip() if lines else ""


# -------------------------------------------------------------------- quotes
def _comment_id(row):
    """A comment id, whichever key the backend used.

    Index rows key it ``comment_id`` (``needs.cid_of`` reads that); a GitHub or
    fixture comment carries ``id``. Both are read here, and nothing else: an
    unreadable value is None and the caller reports the quote as unavailable
    instead of matching by position.
    """
    if not isinstance(row, dict):
        return None
    for key in ("comment_id", "id"):
        if key in row:
            try:
                return int(row[key])
            except (TypeError, ValueError):
                return None
    return None


class _Quotes:
    """Comment first lines, fetched once per issue. Missing is never silent."""

    def __init__(self, source, *, enabled: bool, source_error: str = ""):
        self.source = source
        self.enabled = enabled
        self.source_error = str(source_error or "")
        self._cache: dict[int, list] = {}

    def _reason_unusable(self) -> str:
        if not self.enabled:
            return "--no-quotes 指定，本轮不读引用"
        # A source the caller could not build explains itself. Saying
        # "github.source=none" here would blame a setting the user never chose.
        if self.source_error:
            return f"数据源不可用: {self.source_error}"
        if self.source is None:
            return "未提供数据源"
        if isinstance(self.source, NullSource):
            return "数据源不可用（github.source=none）"
        return ""

    def first_line(self, number, cid):
        """``(text, why)``; ``why`` is non-empty exactly when text is empty."""
        blocked = self._reason_unusable()
        if blocked:
            return "", blocked
        if cid is None:
            return "", "无评论 id"
        if number is None:
            return "", "定位串里没有单号"
        number = int(number)
        if number not in self._cache:
            try:
                rows = self.source.comments(number)
            except Exception as exc:  # noqa: BLE001  a quote must not kill the page
                return "", f"数据源读取失败: {type(exc).__name__}"
            self._cache[number] = list(rows or [])
        for row in self._cache[number]:
            if _comment_id(row) == int(cid):
                line = _first_line((row or {}).get("body"))
                return (line, "") if line else ("", "评论正文为空")
        return "", f"评论 {int(cid)} 不在数据源返回的评论里"


# --------------------------------------------------------------------- items
def _item(category: str, source: str, *, key, cid, at, reason, locator,
          extra: dict | None = None) -> dict:
    number = issuekey.issue_number(key)
    if number is None and isinstance(key, int) and not isinstance(key, bool):
        number = key
    return {"category": category, "source": source, "key": key,
            "number": number, "cid": None if cid is None else int(cid),
            "at": at, "reason": str(reason or ""), "locator": str(locator),
            "extra": dict(extra or {}), "also_from": [], "repeats": 0,
            "quote": "", "quote_error": "", "expiry": {"at": "", "state": "unknown",
                                                       "age_s": None}}


def _bucket_for_reason(reason, cfg: dict | None = None) -> str:
    """Map ``brief._classify`` onto the three first-screen buckets.

    ``_classify`` is the project's one classification rule (user markers first,
    then urgent markers, else a decision). Its private name is used on purpose:
    duplicating the marker tables here is how the two would drift apart. A
    change there must fail the drift test, not silently re-bucket rows.

    ``cfg`` is the engine's config, so markers a project adds under
    ``engine.user_markers``/``engine.urgent_markers`` apply here exactly as they
    do in ``brief``. Passing ``{}`` would silently fall back to the defaults and
    re-bucket every custom marker.
    """
    user, urgent = brief_mod._markers(cfg or {})
    bucket = brief_mod._classify(reason, (user, urgent))
    return CATEGORY_USER if bucket == brief_mod.PRIORITY_USER else CATEGORY_ROLE


def _from_status(row: dict, cfg: dict | None = None) -> dict:
    key = row.get("issue")
    cid = row.get("comment_id")
    locator = str(key)
    if needs_mod.cid_of(cid) is not None:
        locator = f"{locator} comment {int(cid)}"
    return _item(_bucket_for_reason(row.get("reason"), cfg), "index", key=key,
                 cid=cid, at=row.get("at"), reason=row.get("reason"),
                 locator=locator)


def _index_items(engine) -> tuple[list[dict], str]:
    """``engine.status()`` items, plus the damaged-row gap.

    ``needs.is_open`` treats any dict-shaped side-table value as a ruling, so an
    empty ``{}`` or ``{"state": "bogus"}`` would hide a real row (QA P1-3).
    ``needs.py`` owns the fix - the correct rule is "a ruling is a dict whose
    ``state`` is one of ``needs.STATES``" - but this ticket may not touch it.
    So the second call (``all=True``) supplies the rows the filter dropped, and
    only those whose stored ``state`` is *not* a real state are added back. The
    supplement can only ever add: a real ``resolved``/``superseded`` row is
    never resurrected. Backlog: make ``needs.is_open`` validate ``state``.

    The membership test below is a plain string compare, exactly like
    ``needs._state`` (``needs.py:190``) - so ``"Resolved"`` or ``" resolved "``
    is treated as an unusable row on both sides. That is the same latent
    case/whitespace defect, shared on purpose so the two cannot disagree; it is
    not a deliberate design of this ticket, and fixing it means fixing
    ``needs._state`` for everyone.

    ``engine.status()`` also raises on a structurally broken index; the caller
    only gets here after the structure check passed, and any remaining failure
    is reported as "unknown" instead of a traceback.
    """
    try:
        primary = engine.status(all=False)
    except Exception as exc:  # noqa: BLE001  a broken index must not kill the page
        return [], f"engine.status() 失败: {type(exc).__name__}: {exc}"
    cfg = getattr(engine, "cfg", {}) or {}
    items, seen = [], set()
    for row in primary.get("items") or []:
        if not isinstance(row, dict) or row.get("source") != "index":
            continue
        found = _from_status(row, cfg)
        items.append(found)
        seen.add((found["locator"], found["cid"]))
    try:
        broad = engine.status(all=True)
    except Exception:  # noqa: BLE001  the narrow call already answered
        broad = {"items": []}
    for row in broad.get("items") or []:
        if not isinstance(row, dict) or row.get("source") != "index":
            continue
        if "state" not in row:
            continue                      # no stored row: already open via is_open
        if str(row.get("state")) in needs_mod.STATES:
            continue                      # a real ruling: stay suppressed
        found = _from_status(row, cfg)
        if (found["locator"], found["cid"]) in seen:
            continue
        items.append(found)
        seen.add((found["locator"], found["cid"]))
    return items, ""


def _assignment_items(index: dict | None, cfg: dict | None = None) -> list[dict]:
    if not isinstance(index, dict):
        return []
    out = []
    assignments = index.get("assignments") or {}
    for aid, rec in sorted(assignments.items(), key=lambda kv: str(kv[0])):
        if not isinstance(rec, dict):
            continue
        status = str(rec.get("status") or "")
        if status not in BLOCKED_STATES:
            continue
        tomb = rec.get("tombstone") if isinstance(rec.get("tombstone"), dict) else {}
        key = str(rec.get("thread") or rec.get("issue") or "")
        # ``mark_blocked`` records the delivery that failed, so the blocked item
        # has a real comment id whenever one was consumed. Passing it makes the
        # row locatable by comment; a block that never reached a delivery keeps
        # ``cid=None`` and then the action id is its identity.
        cid = tomb.get("cid")
        out.append(_item(CATEGORY_BLOCKED, "assignment", key=key, cid=cid,
                         at=rec.get("updated_at") or tomb.get("at"),
                         reason=tomb.get("reason") or status, locator=aid,
                         extra={"status": status, "delivery": tomb.get("cid"),
                                "attempt": tomb.get("attempt")}))
    return out


def _outbox_items(probe: dict) -> list[dict]:
    if not _outbox_ok(probe):
        return []
    actions = (probe.get("data") or {}).get("actions") or {}
    out = []
    for aid, rec in sorted(actions.items(), key=lambda kv: str(kv[0])):
        if not isinstance(rec, dict):
            continue
        status = str(rec.get("status") or "")
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        if status == "waiting_person":
            category = CATEGORY_BLOCKED
            if str((payload or {}).get("awaiting") or "") == "user":
                category = CATEGORY_USER
        elif status in ROLE_OUTBOX_STATES:
            category = CATEGORY_ROLE
        else:
            continue                     # prepared: the engine replays it
        issue = rec.get("issue")
        key = "" if issue is None else str(issue)
        out.append(_item(category, "outbox", key=key,
                         cid=(rec.get("comment_id")
                              or _cid_from_input_ref(rec.get("input_ref"))),
                         at=rec.get("updated_at"),
                         reason=rec.get("reason") or status, locator=aid,
                         extra={"status": status, "verb": rec.get("verb"),
                                "input_ref": rec.get("input_ref")}))
    return out


def _cid_from_input_ref(input_ref):
    """The delivery id an action was run for, when ``input_ref`` is just a number.

    ``engine`` writes ``input_ref=str(delivery)`` for an archive, but
    ``Store._transition`` does not persist ``comment_id`` for
    ``mark_uncertain``/``mark_reconciled``, so on the failure path the outbox
    record has the id only here. Parsing it is what lets the outbox row and the
    assignment row (which *does* carry ``tombstone.cid``) be recognised as one
    item instead of being reported twice.

    Anything that is not a bare number returns ``None`` and the row keeps its
    own identity: ``"inject:3"`` and friends must never be coerced.
    """
    number = needs_mod.cid_of(input_ref)
    return number if number is not None and number >= 0 else None


def _signature(item: dict, repos_for: dict | None = None):
    """Dedupe identity of one item.

    Four rules, and the order matters:

    1. **No comment id -> its own row, keyed by locator AND content.** A bare
       locator is not enough: two ``needs_person`` rows under one issue key with
       no comment id share it, and keying on the locator alone dropped the
       second one silently. ``reason``/``at`` separate them, so a genuine
       re-read of the same row still merges.
    2. **A comment id -> ``(thread, cid)``.** The thread is the repository *and*
       the number (``issuekey`` exists because the same number in two repos is
       two threads); merging across repos would be a data loss, not a
       normalisation.
    3. **A bare number matches at most one repository.** ``"7"`` and
       ``owner/demo#7`` are one row only when ``7`` resolves to a single repo in
       this page. With two repos holding issue 7 the bare key is ambiguous and
       stays separate - guessing would silently attach a row to the wrong thread.
    """
    locator = str(item.get("locator") or "")
    cid = item.get("cid")
    if cid is None:
        return ("id", locator, str(item.get("reason") or ""),
                str(item.get("at") or ""))
    repo, number = issuekey.split_key(item.get("key"))
    if number is None and isinstance(item.get("number"), int):
        number = item.get("number")
    if not repo and number is not None and repos_for:
        owner = repos_for.get(number) or set()
        if len(owner) == 1:
            repo = next(iter(owner))
    return ("issue", repo or "", number, int(cid))


def _dedupe(items: list[dict]) -> list[dict]:
    """Drop exact repeats; remember every row that was folded away.

    A merge is only ever allowed for the same identity (see :func:`_signature`),
    and a dropped row is never just forgotten: another source lands in
    ``also_from``, and a repeat of the same source bumps ``repeats``. A row that
    disappears without a trace is the failure mode this whole function exists to
    prevent.
    """
    repos_for: dict = {}
    for item in items:
        repo, number = issuekey.split_key(item.get("key"))
        if repo and number is not None:
            repos_for.setdefault(number, set()).add(repo)
    out, seen = [], {}
    for item in items:
        sig = _signature(item, repos_for)
        first = seen.get(sig)
        if first is None:
            seen[sig] = item
            out.append(item)
            continue
        if item["source"] != first["source"]:
            if item["source"] not in first["also_from"]:
                first["also_from"].append(item["source"])
        else:
            first["repeats"] = int(first.get("repeats") or 0) + 1
    return out


def _detail(item: dict) -> str:
    sources = [item["source"]] + list(item.get("also_from") or [])
    if "index" in sources:
        key = str(item.get("key") or "")
        if issuekey.split_key(key)[0]:
            return f"python -m super_agent.cli context --issue {key}"
        return "python -m super_agent.cli status --all"
    if "outbox" in sources:
        return "python -m super_agent.cli status --outbox"
    return "python -m super_agent.cli status --all"


# --------------------------------------------------------------- second screen
def _projects(engine, index: dict | None) -> list[dict]:
    """Counts and project names only - never an issue number (the brief rule)."""
    assignments = (index or {}).get("assignments") or {}
    projects = getattr(engine, "projects", None)
    data = getattr(projects, "data", None) if projects is not None else None
    entries = data.get("projects") if isinstance(data, dict) else None
    rows = []
    if isinstance(entries, dict) and entries:
        for name in sorted(entries, key=str):
            entry = entries.get(name) if isinstance(entries.get(name), dict) else {}
            numbers = set()
            for value in entry.get("issues") or []:
                try:
                    numbers.add(int(value))
                except (TypeError, ValueError):
                    continue
            active = 0
            for rec in assignments.values():
                if not isinstance(rec, dict):
                    continue
                try:
                    issue = int(rec.get("issue"))
                except (TypeError, ValueError):
                    continue
                if issue in numbers and str(rec.get("status") or "") in asg_mod.ACTIVE_STATES:
                    active += 1
            rows.append({"name": str(name), "issues": len(numbers), "active": active})
        return rows
    counts: dict[str, int] = {}
    for key in (index or {}).get("issues") or {}:
        repo, number = issuekey.split_key(key)
        if number is None:
            continue
        name = repo or "(未限定)"
        counts[name] = counts.get(name, 0) + 1
    return [{"name": name, "issues": n, "active": 0}
            for name, n in sorted(counts.items())]


def _engine_state(engine, health: dict) -> dict:
    store = engine.store
    out = {"pid_lock": False, "health": {}, "heartbeat": {},
           "external_actions": None, "outbox": None, "stuck": None}
    try:
        out["pid_lock"] = store.pid_path.is_file()
    except OSError:
        out["pid_lock"] = False
    probe = health["health"]
    data = probe.get("data") or {}
    if probe["valid"]:
        out["health"] = {"state": "ok", "phase": data.get("phase"),
                         "running": data.get("running"),
                         "last_loop_at": data.get("last_loop_at"),
                         "degraded": data.get("degraded"),
                         "last_error": data.get("last_error")}
    elif not probe["exists"]:
        out["health"] = {"state": "missing", "detail": "health.json 不存在"}
    else:
        out["health"] = {"state": "bad", "detail": probe["error"]}
    out["heartbeat"] = store.load_heartbeat()
    try:
        out["external_actions"] = int(store.load_counters().get("external_actions") or 0)
    except Exception:  # noqa: BLE001  a counter is display only
        out["external_actions"] = None
    if _outbox_ok(health["outbox"]):
        counts: dict[str, int] = {}
        for rec in ((health["outbox"].get("data") or {}).get("actions") or {}).values():
            if isinstance(rec, dict):
                status = str(rec.get("status") or "?")
                counts[status] = counts.get(status, 0) + 1
        out["outbox"] = counts
        try:
            out["stuck"] = len(store.stuck_outbox())
        except Exception:  # noqa: BLE001
            out["stuck"] = None
    return out


def _count_lines(path: Path) -> int:
    try:
        with open(path, "rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def _file_size(path: Path):
    try:
        return path.stat().st_size
    except OSError:
        return None


def _storage(store) -> dict:
    return {
        "state_dir": str(store.root),
        "index_bytes": _file_size(store.index_path),
        "outbox_state_bytes": _file_size(store.outbox_state_path),
        "events_lines": _count_lines(store.events_path),
        "outbox_lines": _count_lines(store.outbox_path),
        "cursor_issues": len((store.load_cursor().get("issues") or {})),
        "allowlist_cache": (store.root / "allowlist.last-good").is_file(),
    }


# -------------------------------------------------------------------- collect
def collect(engine, *, source=None, quotes: bool = True, now=None,
            source_error: str = "") -> dict:
    """Read the state dir and build the report. Never writes, never raises on data.

    ``source`` supplies comment first lines (fixture/live); ``None`` means "no
    quote is available" and every line says why. ``quotes=False`` is the
    ``--no-quotes`` path: it must not touch a source at all - the caller is
    expected to pass ``NullSource`` and to have skipped constructing a live one.

    ``source_error`` is why the caller had to fall back to ``NullSource`` (a
    fixture that does not exist, an unreadable repo config). It is shown
    verbatim instead of the generic "github.source=none": blaming a setting the
    user did not choose is the same mistake as showing "unknown" as "none".
    """
    store = getattr(engine, "store", None)
    if store is None:
        raise ValueError("collect() needs an engine that carries .store")
    cfg = getattr(engine, "cfg", {}) or {}
    role = str(getattr(engine, "role", "") or cfg.get("role") or "") or "supervisor"
    now_dt = _timestamp(now) or datetime.now(timezone.utc)

    health = probe_source_health(store)
    index_ok = _index_ok(health["index"])
    outbox_ok = _outbox_ok(health["outbox"])

    warnings = []
    if not health["outbox"]["exists"]:
        warnings.append("outbox_state.json 不存在：按「没有未完成动作」处理")

    items: list[dict] = []
    status_error = ""
    if index_ok:
        got, status_error = _index_items(engine)
        items.extend(got)
        items.extend(_assignment_items(health["index"].get("data"), cfg))
    items.extend(_outbox_items(health["outbox"]))
    items = _dedupe(items)

    quote_reader = _Quotes(source, enabled=quotes, source_error=source_error)
    for item in items:
        text, why = quote_reader.first_line(item["number"], item["cid"])
        item["quote"], item["quote_error"] = text, why
        item["expiry"] = _expiry(item["at"], now_dt)
        item["detail"] = _detail(item)

    order = {name: idx for idx, name in enumerate(CATEGORIES)}
    items.sort(key=lambda it: (order.get(it["category"], 9),
                               it["number"] if it["number"] is not None else 10 ** 9,
                               it["cid"] if it["cid"] is not None else -1,
                               it["locator"]))

    known = bool(index_ok and outbox_ok and not status_error)
    reasons = []
    if not index_ok:
        reasons.append(f"index.json {health['index']['error']}")
    if not outbox_ok:
        reasons.append(f"outbox_state.json {health['outbox']['error']}")
    if status_error:
        reasons.append(status_error)

    if known:
        counts = {name: sum(1 for it in items if it["category"] == name)
                  for name in CATEGORIES}
        status = "none" if not any(counts.values()) else "action"
    else:
        counts = {name: None for name in CATEGORIES}
        status = "unknown"

    return {
        "at": now_dt.isoformat(timespec="seconds"),
        "role": role,
        "state_dir": str(store.root),
        "sources": {name: {k: v for k, v in probe.items() if k != "data"}
                    for name, probe in health.items()},
        "index_ok": index_ok,
        "outbox_ok": outbox_ok,
        "status": status,
        "counts": counts,
        "items": items,
        "unknown_reasons": reasons,
        "source_error": str(source_error or ""),
        "quotes_missing": sum(1 for it in items if not it["quote"]),
        "warnings": warnings,
        "projects": _projects(engine, health["index"].get("data")),
        "engine": _engine_state(engine, health),
        "storage": _storage(store),
    }


# ---------------------------------------------------------------------- render
def _esc(text) -> str:
    return html.escape(str(text if text is not None else ""), quote=True)


def _expiry_label(expiry: dict) -> str:
    """Text + symbol first; colour is decoration on top, never the message."""
    state = expiry.get("state") or "unknown"
    label = _EXPIRY_TEXT.get(state, "时间未知")
    mark = _EXPIRY_MARK.get(state, "?")
    if expiry.get("age_s") is None:
        return f"{mark} {label}"
    return f"{mark} {label}（{float(expiry['age_s']) / 3600.0:.1f}h）"


def _render_item(item: dict) -> list[str]:
    src = item["source"] + "".join(f"+{x}" for x in item.get("also_from") or [])
    expiry = item.get("expiry") or {}
    head = [f'<span class="chip">来源 {_esc(src)}</span>',
            f'<span class="locator">{_esc(item.get("locator"))}</span>']
    if item.get("cid") is not None:
        head.append(f'<span class="chip">comment {_esc(item["cid"])}</span>')
    if expiry.get("at"):
        head.append(f'<span class="stamp">{_esc(expiry["at"])}</span>')
    head.append(f'<span class="expiry expiry--{_esc(expiry.get("state") or "unknown")}">'
                f'{_esc(_expiry_label(expiry))}</span>')
    facts = ["<dl class=\"facts\">"]
    if item.get("quote"):
        facts.append(f"<dt>原文</dt><dd>{_esc(_clip(item['quote'], 160))}</dd>")
    else:
        facts.append("<dt>原文</dt><dd>引用不可用（"
                     f"{_esc(item.get('quote_error') or '原因未知')}）</dd>")
    if item.get("reason"):
        facts.append(f"<dt>原因</dt><dd>{_esc(_clip(item['reason'], 120))}</dd>")
    if item["source"] == "assignment":
        extra = item.get("extra") or {}
        detail = f"状态 {extra.get('status') or '-'}"
        if extra.get("delivery") is not None:
            detail += f" · 投递 {extra['delivery']}"
        if extra.get("attempt") is not None:
            detail += f" · 第 {extra['attempt']} 次"
        facts.append(f"<dt>记录</dt><dd>{_esc(detail)}</dd>")
    facts.append(f"<dt>详情</dt><dd><code>{_esc(item.get('detail') or '-')}</code></dd>")
    facts.append("</dl>")
    return ['<li class="item">',
            '<p class="item-head">' + "".join(head) + "</p>"] + facts + ["</li>"]


def _health_line(engine_state: dict) -> str:
    snap = engine_state.get("health") or {}
    state = snap.get("state")
    if state == "ok":
        text = (f"ok（phase={snap.get('phase')} · running={snap.get('running')} · "
                f"last_loop={snap.get('last_loop_at') or '-'}")
        if snap.get("degraded"):
            text += f" · 降级: {snap.get('last_error') or '未说明'}"
        return text + "）"
    if state == "missing":
        return "缺失（health.json 不存在 ⇒ 引擎从未写过健康快照）"
    return f"坏（{snap.get('detail') or 'health.json 不可读'}）"


def _render_details(report: dict) -> list[str]:
    out = ['<details class="card details">',
           "<summary>二级：统计与守护</summary>",
           '<h3 id="projects-title">项目概览（只给计数与项目名）</h3>',
           '<ul class="plain" aria-labelledby="projects-title">']
    projects = report.get("projects") or []
    if not projects:
        out.append("  <li>未登记项目</li>")
    for row in projects:
        out.append(f'  <li>{_esc(row["name"])} · 工单 {int(row["issues"])} · '
                   f'活跃 {int(row["active"])}</li>')
    out.append("</ul>")
    out.append('<h3 id="engine-title">引擎与守护</h3>')
    out.append('<dl class="facts" aria-labelledby="engine-title">')
    engine = report.get("engine") or {}
    out.append(f"<dt>健康快照</dt><dd>{_esc(_health_line(engine))}</dd>")
    out.append(f"<dt>pid 锁</dt><dd>{'存在' if engine.get('pid_lock') else '缺失'}</dd>")
    beat = engine.get("heartbeat") or {}
    if beat:
        out.append(f"<dt>心跳</dt><dd>round={_esc(beat.get('round'))} · "
                   f"ok={_esc(beat.get('ok'))} · at={_esc(beat.get('ts') or '-')}</dd>")
    else:
        out.append("<dt>心跳</dt><dd>无</dd>")
    actions = engine.get("external_actions")
    out.append(f"<dt>外部动作累计</dt>"
               f"<dd>{'未知' if actions is None else int(actions)}</dd>")
    counts = engine.get("outbox")
    if counts is None:
        out.append("<dt>outbox 未完成动作</dt>"
                   "<dd>未知（outbox_state.json 不可读）</dd>")
    else:
        text = " · ".join(f"{_esc(k)}={int(v)}" for k, v in sorted(counts.items()))
        out.append(f"<dt>outbox 未完成动作</dt><dd>{text or '0'}</dd>")
    stuck = engine.get("stuck")
    out.append(f"<dt>超时未完成</dt>"
               f"<dd>{'未知' if stuck is None else int(stuck)}</dd>")
    out.append("</dl>")
    out.append('<h3 id="storage-title">存储</h3>')
    storage = report.get("storage") or {}

    def size(value):
        return "缺失" if value is None else f"{int(value)} 字节"

    out.append('<dl class="facts" aria-labelledby="storage-title">')
    out.append(f"<dt>状态目录</dt><dd><code>{_esc(storage.get('state_dir'))}</code></dd>")
    out.append(f"<dt>index.json</dt><dd>{size(storage.get('index_bytes'))}</dd>")
    out.append("<dt>outbox_state.json</dt>"
               f"<dd>{size(storage.get('outbox_state_bytes'))}</dd>")
    out.append(f"<dt>events.jsonl</dt>"
               f"<dd>{int(storage.get('events_lines') or 0)} 行</dd>")
    out.append(f"<dt>outbox.jsonl</dt>"
               f"<dd>{int(storage.get('outbox_lines') or 0)} 行</dd>")
    out.append(f"<dt>cursor.json</dt>"
               f"<dd>{int(storage.get('cursor_issues') or 0)} 个游标</dd>")
    out.append("<dt>allowlist.last-good</dt>"
               f"<dd>{'存在' if storage.get('allowlist_cache') else '缺失'}</dd>")
    out.append("</dl>")
    for warning in report.get("warnings") or []:
        out.append(f'<p class="warn">WARN {_esc(warning)}</p>')
    out.append("</details>")
    return out


#: One stylesheet, three token tiers, five cascade layers. Literal values exist
#: only in tier 1 and in the reset; semantic tokens build on them and component
#: tokens build on semantic ones. No universal selector is used (a ``*`` reset
#: cannot be overridden by a lower layer), and every geometry property is
#: logical so the page does not silently change meaning under a vertical script.
STYLE = """
@layer reset, base, theme, components, utilities;

@layer reset {
  html, body, h1, h2, h3, p, ul, ol, dl, dd, figure { margin: 0; }
  html, body, header, main, footer, nav, section, li, dl, div, details {
    box-sizing: border-box;
  }
  ul, ol { padding-inline-start: 0; list-style: none; }
  dd { margin-inline-start: 0; }
  body { min-block-size: 100vh; min-block-size: 100dvh; }
  a { color: inherit; }
}

@layer theme {
  :root {
    /* tier 1 — literals */
    --hue: 205;
    --hue-ok: 152;
    --hue-warn: 35;
    --font-sans: system-ui, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    --font-mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    --size-1: 0.75rem;
    --size-2: 0.875rem;
    --size-3: 1rem;
    --size-4: 1.375rem;
    --size-5: 1.875rem;
    --space-1: 0.25rem;
    --space-2: 0.5rem;
    --space-3: 0.75rem;
    --space-4: 1rem;
    --space-5: 1.5rem;
    --space-6: 2rem;
    --radius-1: 8px;
    --radius-2: 14px;
    --measure-1: 60rem;
    --neutral-0: hsl(var(--hue) 40% 100%);

    /* tier 2 — semantic */
    --color-page: hsl(var(--hue) 32% 97%);
    --color-surface: hsl(var(--hue) 40% 99.5%);
    --color-text: hsl(var(--hue) 22% 18%);
    --color-muted: hsl(var(--hue) 12% 42%);
    --color-line: hsl(var(--hue) 20% 86%);
    --color-accent: hsl(var(--hue) 42% 38%);
    --color-on-accent: var(--neutral-0);
    --color-ok: hsl(var(--hue-ok) 42% 28%);
    --color-ok-bg: hsl(var(--hue-ok) 42% 93%);
    --color-warn: hsl(var(--hue-warn) 72% 30%);
    --color-warn-bg: hsl(var(--hue-warn) 82% 93%);
    --color-unknown: hsl(var(--hue) 10% 34%);
    --color-unknown-bg: hsl(var(--hue) 12% 92%);
    --font-body: var(--font-sans);
    --font-code: var(--font-mono);
    --measure-page: var(--measure-1);
    --space-gutter: var(--space-4);

    /* tier 3 — component */
    --card-pad: var(--space-4);
    --card-gap: var(--space-3);
    --card-radius: var(--radius-2);
    --card-surface: var(--color-surface);
    --card-line: var(--color-line);
    --shell-gap: var(--space-5);
    --chip-pad-inline: var(--space-2);
    --chip-pad-block: var(--space-1);
    --chip-radius: var(--radius-1);
    --chip-font: var(--size-1);
    --badge-pad-inline: var(--space-3);
    --badge-pad-block: var(--space-2);
    --badge-radius: var(--radius-1);
    --badge-font: var(--size-3);
  }
}

@layer base {
  body {
    font-family: var(--font-body);
    font-size: var(--size-3);
    line-height: 1.6;
    color: var(--color-text);
    background: var(--color-page);
  }
  code { font-family: var(--font-code); font-size: var(--size-2); }
  summary { cursor: pointer; font-size: var(--size-4); font-weight: 650; }
  h2 { font-size: var(--size-4); letter-spacing: -0.01em; }
  h3 { font-size: var(--size-2); color: var(--color-muted); text-transform: uppercase; }
  :focus-visible { outline: 3px solid var(--color-accent); outline-offset: 3px; }
}

@layer components {
  .shell {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: var(--shell-gap);
    max-inline-size: var(--measure-page);
    margin-inline: auto;
    padding-block: var(--space-6);
    padding-inline: var(--space-gutter);
  }
  .page-header { display: flex; flex-wrap: wrap; gap: var(--card-gap);
    align-items: baseline; }
  .page-header h1 { font-size: var(--size-5); letter-spacing: -0.02em;
    flex: 1 1 18rem; min-inline-size: 0; }
  .meta { color: var(--color-muted); font-size: var(--size-2); }
  .toc ul { display: flex; flex-wrap: wrap; gap: var(--space-2); }
  .toc a { color: var(--color-accent); text-underline-offset: 0.2em; }
  .card { background: var(--card-surface); border: 1px solid var(--card-line);
    border-radius: var(--card-radius); padding: var(--card-pad);
    display: grid; grid-template-columns: minmax(0, 1fr); gap: var(--card-gap); }
  .badge { display: inline-flex; flex-wrap: wrap; gap: var(--space-2);
    align-items: center; padding-inline: var(--badge-pad-inline);
    padding-block: var(--badge-pad-block); border-radius: var(--badge-radius);
    font-size: var(--badge-font); font-weight: 650; }
  .badge--none { background: var(--color-ok-bg); color: var(--color-ok); }
  .badge--action { background: var(--color-warn-bg); color: var(--color-warn); }
  .badge--unknown { background: var(--color-unknown-bg);
    color: var(--color-unknown); }
  .shell-stack { display: grid; grid-template-columns: minmax(0, 1fr);
    gap: var(--shell-gap); }
  .items { display: grid; grid-template-columns: minmax(0, 1fr);
    gap: var(--card-gap); }
  .item { border-inline-start: 3px solid var(--card-line);
    padding-inline-start: var(--space-3); display: grid;
    grid-template-columns: minmax(0, 1fr); gap: var(--space-2);
    min-inline-size: 0; }
  .item-head { display: flex; flex-wrap: wrap; gap: var(--space-2);
    align-items: baseline; }
  .chip { background: var(--color-page); border-radius: var(--chip-radius);
    padding-inline: var(--chip-pad-inline);
    padding-block: var(--chip-pad-block); font-size: var(--chip-font);
    color: var(--color-muted); }
  .locator { font-family: var(--font-code); font-size: var(--size-2);
    min-inline-size: 0; overflow-wrap: anywhere; }
  .stamp { font-size: var(--size-1); color: var(--color-muted); }
  .expiry { font-size: var(--size-1); border-radius: var(--chip-radius);
    padding-inline: var(--chip-pad-inline);
    padding-block: var(--chip-pad-block); }
  .expiry--fresh { background: var(--color-ok-bg); color: var(--color-ok); }
  .expiry--stale { background: var(--color-warn-bg); color: var(--color-warn); }
  .expiry--unknown { background: var(--color-unknown-bg);
    color: var(--color-unknown); }
  .facts { display: grid; grid-template-columns: minmax(6rem, max-content)
    minmax(0, 1fr); gap: var(--space-1) var(--space-3); font-size: var(--size-2); }
  .facts dt { color: var(--color-muted); }
  .facts dd { min-inline-size: 0; overflow-wrap: anywhere; }
  .plain { display: grid; grid-template-columns: minmax(0, 1fr);
    gap: var(--space-1); font-size: var(--size-2); }
  .warn { color: var(--color-warn); font-size: var(--size-2); }
  .details { border-style: dashed; }
}

@layer utilities {
  .muted { color: var(--color-muted); }
}
"""


def _render_status(report: dict) -> list[str]:
    status = report.get("status")
    counts = report.get("counts") or {}
    label = {"none": ("none", "○", "无需行动"),
             "action": ("action", "!", "需要行动"),
             "unknown": ("unknown", "?", "未知")}[status]
    out = ['<section class="card" aria-labelledby="status-title">',
           '<h2 id="status-title">当前状态</h2>',
           f'<p class="badge badge--{label[0]}"><span aria-hidden="true">'
           f'{label[1]}</span><span>{label[2]}</span></p>']
    if status == "action":
        total = sum(v for v in counts.values() if v)
        out.append(f'<p class="muted">共有 {int(total)} 项待处理，见下列三类。</p>')
    if status == "unknown":
        out.append('<p class="muted">数据源不可读，无法据此判断有无待办；'
                   '下列条数为未知，不是 0。</p>')
    sources = report.get("sources") or {}
    out.append('<dl class="facts">')
    bits = []
    for name in ("index", "outbox", "health"):
        probe = sources.get(name) or {}
        if probe.get("valid"):
            mark = "ok"
        elif probe.get("exists"):
            mark = "坏"
        else:
            mark = "缺失"
        bits.append(f"{name}={mark}")
    out.append(f'<dt>数据源</dt><dd>{_esc(" · ".join(bits))}</dd>')
    out.append(f'<dt>health</dt><dd>{_esc(_health_line(report.get("engine") or {}))}</dd>')
    out.append("</dl>")
    reasons = report.get("unknown_reasons") or []
    if reasons:
        out.append('<ul class="plain">')
        for reason in reasons:
            out.append(f'<li>未知原因：{_esc(reason)}</li>')
        out.append("</ul>")
    if report.get("source_error"):
        out.append(f'<p class="warn">数据源构建失败：{_esc(report["source_error"])}</p>')
    if report.get("quotes_missing"):
        out.append(f'<p class="muted">引用不可用：{int(report["quotes_missing"])} '
                   '条（每行注明原因）</p>')
    out.append("</section>")
    return out


def render(report: dict) -> str:
    """Compose the page. Pure: every fact it prints is already in ``report``.

    One HTML document, no script tag and therefore no event binding at all -
    there is nothing here that a ``Content-Security-Policy`` would have to
    allow. Status is always carried by words (``无需行动``/``未知``/``过期``);
    the colour classes only reinforce it.
    """
    status = report.get("status")
    counts = report.get("counts") or {}
    items = list(report.get("items") or [])
    role = report.get("role")

    out = ["<!DOCTYPE html>",
           '<html lang="zh-CN">',
           "<head>",
           '<meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width, initial-scale=1.0">',
           '<meta name="color-scheme" content="light">',
           "<title>需要我做什么 · super_agent reporter</title>",
           "<style>" + STYLE + "</style>",
           "</head>",
           "<body>",
           '<div class="shell">',
           '<header class="page-header">',
           "<h1>需要我做什么</h1>",
           f'<p class="meta">role={_esc(role)} · at={_esc(report.get("at"))} · '
           f'state={_esc(report.get("state_dir"))}</p>',
           "</header>",
           '<nav class="toc" aria-label="页面分区">',
           "<ul>",
           '<li><a href="#status-title">当前状态</a></li>']
    for index, category in enumerate(CATEGORIES, start=1):
        title = CATEGORY_TITLES[category].format(role=role)
        out.append(f'<li><a href="#cat-{category}">{_esc(title)}</a></li>')
    out.append("</ul>")
    out.append("</nav>")
    out.append("<main>")
    out.append('<div class="shell-stack">')
    out.extend(_render_status(report))

    for index, category in enumerate(CATEGORIES, start=1):
        title = CATEGORY_TITLES[category].format(role=_esc(role))
        rows = [it for it in items if it["category"] == category]
        count = counts.get(category)
        if count is None:
            head = (f'{index}. {title}（未知；已知 {len(rows)} 项）' if rows
                    else f"{index}. {title}（未知）")
        else:
            head = f"{index}. {title}（{int(count)}）"
        out.append('<section class="card" '
                   f'aria-labelledby="cat-{category}">')
        out.append(f'<h2 id="cat-{category}">{_esc(head)}</h2>')
        if count is None:
            out.append('<p class="muted">未知（数据源不可读，见「当前状态」）</p>')
        if rows:
            out.append('<ul class="items">')
            for row in rows:
                out.extend(_render_item(row))
            out.append("</ul>")
        elif count == 0:
            out.append('<p class="muted">无</p>')
        out.append("</section>")

    out.extend(_render_details(report))
    out.append("</div>")
    out.append("</main>")
    out.append("<footer>")
    out.append('<p class="muted">只读视图：不派单、不催办、不改状态；'
               '无脚本，故无事件绑定。</p>')
    out.append("</footer>")
    out.append("</div>")
    out.append("</body>")
    out.append("</html>")
    return "\n".join(out) + "\n"


def collect_and_render(engine, *, source=None, quotes: bool = True,
                       now=None, source_error: str = "") -> tuple[dict, str]:
    """``collect`` + ``render`` in one call, for callers that print the page."""
    report = collect(engine, source=source, quotes=quotes, now=now,
                     source_error=source_error)
    return report, render(report)
