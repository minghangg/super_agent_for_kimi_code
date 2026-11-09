"""Short-form recovery brief, and the single-issue context card.

``brief`` answers one question: can a fresh session resume from this page
alone? It is a *pointer* document on purpose. It names the paths, session ids,
action ids and comment ids the next session needs, and it never copies prose:
that is what keeps it inside the line/byte caps and what keeps a 2000-character
comment from landing in a transcript.

Six sections, fixed order, always printed. An empty section prints its empty
state instead of disappearing, because a missing section is indistinguishable
from "nothing to do" once the output is a screenshot in a handoff.

1. role/authorisation entry points (paths only)
2. projects and their PM session ids (keys and ids only)
3. items waiting on the supervisor (outbox ``waiting_person``/``uncertain``,
   index ``needs_person``)
4. items waiting on the user (the same sources, marked as a user question)
5. recent cross-project decisions (issue number and comment id only)
6. system health (``health.json``, cross-checked against ``index.json``)

Over budget, items are dropped lowest priority first (user, then
supervisor, then release/urgent, then normal) and every drop becomes a
``还有 N 项；取回：<command>`` line whose command really does fetch the items
back (``brief --full``). Silently dropping an item is the one behaviour this
module does not have.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import health, session_events

PRIORITY_SKELETON = -1
PRIORITY_USER = 0
PRIORITY_DECISION = 1
PRIORITY_URGENT = 2
PRIORITY_NORMAL = 3

SECTIONS = (
    ("role", "1. 角色/授权恢复入口"),
    ("projects", "2. 当前项目及 PM 引用"),
    ("decision", "3. 等 supervisor 裁决的事项"),
    ("user", "4. 等用户的事项"),
    ("decisions", "5. 最近跨项目决定的引用"),
    ("health", "6. 系统健康"),
)

USER_MARKERS = ("[user]", "等用户", "用户拍板")
URGENT_MARKERS = ("[release]", "[urgent]", "发版", "紧急")


def _markers(cfg: dict) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(user, urgent)`` marker sets; config may extend the defaults."""
    engine_cfg = cfg.get("engine") or {}
    user = tuple(str(m).lower() for m in
                 (engine_cfg.get("user_markers") or USER_MARKERS))
    urgent = tuple(str(m).lower() for m in
                   (engine_cfg.get("urgent_markers") or URGENT_MARKERS))
    return user, urgent


@dataclass(frozen=True)
class Limits:
    """Caps plus the per-section item counts that are not budget driven."""

    lines: int = 60
    bytes: int = 8 * 1024
    decisions: int = 5
    projects: int = 20
    comment_preview: int = 120


UNLIMITED = Limits(lines=10 ** 7, bytes=10 ** 9)


@dataclass
class Item:
    section: str
    priority: int
    text: str


@dataclass
class Problem:
    source: str
    detail: str


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_REDACT = (
    ("github pat", "ghp_"), ("github pat", "gho_"), ("github pat", "ghs_"),
    ("github pat", "ghu_"), ("github pat", "ghr_"),
    ("fine-grained pat", "github_pat_"),
    ("api key", "sk-"), ("aws key", "AKIA"),
)


def redact(text: str) -> str:
    """Mask credential-shaped runs. A brief is pasted into transcripts and issues.

    Short forms stay: the mask only fires on the prefix plus a long token body,
    so prose mentioning a prefix and a hyphenated word survives untouched.
    """
    out = str(text or "")
    for _label, prefix in _REDACT:
        idx = 0
        while True:
            at = out.find(prefix, idx)
            if at < 0:
                break
            end = at + len(prefix)
            while end < len(out) and (out[end].isalnum() or out[end] in "_-"):
                end += 1
            if end - (at + len(prefix)) >= 8:
                out = out[:at] + prefix + "<redacted>" + out[end:]
                idx = at + len(prefix) + len("<redacted>")
            else:
                idx = end
    return out


def _clip(text, limit: int) -> str:
    raw = " ".join(redact(str(text or "")).split())
    if len(raw) <= limit:
        return raw
    return raw[: max(0, limit - 1)] + "…"


def _strict_json(path: Path):
    """``(data, error)``; a corrupt file is reported, never read as empty."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, ""          # absent is a fresh state dir, not corruption
    except OSError as exc:
        return None, f"{path.name} 不可读: {exc.__class__.__name__}"
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return None, f"{path.name} 不是合法 JSON: {str(exc)[:80]}"
    if not isinstance(data, dict):
        return None, f"{path.name} 根节点不是对象"
    return data, ""


def _lwa_root(cfg: dict, override: str | None) -> Path | None:
    """Workbench checkout root, derived from where the session stream lives."""
    if override:
        return Path(override).expanduser().resolve()
    events = session_events.resolve_events_path(cfg)
    if events.parent.name == "state":
        return events.parent.parent
    return None


def _classify(reason: str, markers=None) -> int:
    user, urgent = markers or (USER_MARKERS, URGENT_MARKERS)
    text = str(reason or "").lower()
    if any(marker in text for marker in user):
        return PRIORITY_USER
    if any(marker in text for marker in urgent):
        return PRIORITY_URGENT
    return PRIORITY_DECISION


# ------------------------------------------------------------------ sections
def _role_items(root: Path | None, role: str) -> list[Item]:
    if root is None:
        return [Item("role", PRIORITY_SKELETON,
                     "未找到工作台根目录（设 --lwa 或 LWA_STATE_DIR）")]
    wanted = [
        f"assistants/{role}.md",
        "playbooks/workflow_12steps.md",
        "playbooks/closeout_checklist.md",
        "playbooks/review_checklist.md",
        "rules/shared/rules/worker_conduct.md",
    ]
    items = []
    for rel in wanted:
        path = root / rel
        if path.is_file():
            items.append(Item("role", PRIORITY_SKELETON, str(path)))
            continue
        fallback = ("AGENTS.md §1（身份与边界）"
                    if rel.startswith("assistants/") else "该文件缺失，先报告")
        items.append(Item("role", PRIORITY_SKELETON,
                          f"{path}（未找到 ⇒ {fallback}）"))
    return items


def _projects_items(cfg: dict, *, target: str, project: str | None,
                    limit: int, wb_root: Path | None) -> tuple[list[Item], list[Problem]]:
    """PM keys and sids only. The ledger is read from ``wb_root`` when given.

    Resolving the ledger from the engine's state dir would make an isolated
    brief read the production roster, which is exactly the cross-contamination
    this command exists to avoid: a pm brief would then leak every other
    project's session id.
    """
    if wb_root is not None:
        path = wb_root / "state" / "sessions.json"
    else:
        path = session_events.resolve_sessions_path(cfg)
    data, err = _strict_json(path)
    items: list[Item] = []
    problems: list[Problem] = []
    if data is None:
        problems.append(Problem("sessions.json", err))
        return [Item("projects", PRIORITY_SKELETON, f"未知（{err}）")], problems
    keys = sorted(k for k in data if isinstance(k, str) and k.startswith("pm-"))
    if target == "pm" and project:
        keys = [k for k in keys if k == f"pm-{project}"]
    for key in keys[:limit]:
        entry = data.get(key) or {}
        sid = str((entry or {}).get("id") or "") if isinstance(entry, dict) else ""
        items.append(Item("projects", PRIORITY_SKELETON,
                          f"{key} -> {sid or '(未记账)'}"))
    if len(keys) > limit:
        items.append(Item("projects", PRIORITY_SKELETON,
                          f"…另有 {len(keys) - limit} 个项目未列出"))
    return items, problems


def _outbox_items(store, *, markers=None) -> list[Item]:
    items: list[Item] = []
    for aid, rec in sorted(store.unfinished_outbox().items()):
        status = str(rec.get("status") or "")
        if status not in ("waiting_person", "uncertain"):
            continue
        reason = str(rec.get("reason") or "")
        payload = rec.get("payload") or {}
        awaiting = str((payload or {}).get("awaiting") or "")
        bucket = PRIORITY_USER if awaiting == "user" else _classify(reason, markers)
        issue = rec.get("issue")
        where = f"issue#{issue} " if issue is not None else ""
        section = "user" if bucket == PRIORITY_USER else "decision"
        items.append(Item(
            section, bucket,
            f"{where}{aid} ({status}) {_clip(reason, 80)}".strip() +
            "  [取回: python -m super_agent.cli status --outbox]"))
    return items


def _index_items(index: dict | None, *, markers=None) -> list[Item]:
    items: list[Item] = []
    issues = (index or {}).get("issues") or {}
    for key in sorted(issues, key=lambda k: int(k) if str(k).isdigit() else 0):
        rec = issues.get(key) or {}
        if not isinstance(rec, dict):
            continue
        for entry in rec.get("needs_person") or []:
            if not isinstance(entry, dict):
                continue
            reason = str(entry.get("reason") or "")
            bucket = _classify(reason, markers)
            section = "user" if bucket == PRIORITY_USER else "decision"
            cid = entry.get("comment_id")
            ref = f"issue#{key} comment {cid}" if cid is not None else f"issue#{key}"
            retrieve = f"python -m super_agent.cli context --issue #{key}"
            items.append(Item(section, bucket,
                              f"{ref} ({_clip(reason, 80)})  [取回: {retrieve}]"))
    return items


def _recent_decisions(index: dict | None, *, project_issues: set[int] | None,
                      limit: int) -> list[Item]:
    if limit <= 0:
        return []
    issues = (index or {}).get("issues") or {}
    rows = []
    for key, rec in issues.items():
        if not isinstance(rec, dict):
            continue
        try:
            number = int(key)
        except (TypeError, ValueError):
            continue
        if project_issues is not None and number not in project_issues:
            continue
        stamp = str(rec.get("last_event_ts") or rec.get("rebuilt_at") or "")
        rows.append((stamp, number, rec.get("last_comment_id")))
    rows.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [Item("decisions", PRIORITY_NORMAL,
                 f"issue#{number} comment {cid if cid is not None else '-'} "
                 f"at {stamp or '-'}")
            for stamp, number, cid in rows[:limit]]


def _health_items(store, *, index: dict | None, index_error: str) -> list[Item]:
    path = store.health_path
    if not path.is_file():
        return [Item("health", PRIORITY_SKELETON,
                     f"未知（{path.name} 不存在 ⇒ 引擎从未写过健康快照）")]
    raw, err = _strict_json(path)
    if raw is None:
        return [Item("health", PRIORITY_SKELETON, f"未知（{err}）")]
    if not raw:
        return [Item("health", PRIORITY_SKELETON,
                     f"未知（{path.name} 是空对象，没有任何字段）")]
    if index is None:
        return [Item("health", PRIORITY_SKELETON,
                     f"未知（index.json 不可读，assignments 不可信：{index_error}）")]
    snap = health.read(path)
    text = (f"pid={snap.get('pid')} mode={snap.get('mode')} "
            f"last_loop_at={snap.get('last_loop_at') or '-'} "
            f"degraded={snap.get('degraded')} "
            f"last_error={_clip(snap.get('last_error'), 80) or '-'} "
            f"active_assignments={snap.get('active_assignments')}")
    items = [Item("health", PRIORITY_SKELETON, text)]
    if snap.get("degraded") or snap.get("last_error"):
        items.insert(0, Item("health", PRIORITY_URGENT, "健康状态为降级，见下一条"))
    return items


# ------------------------------------------------------------------- render
def _compose(header: list[str], sections: dict, dropped: dict,
             retrieve: str) -> str:
    out = list(header)
    for key, title in SECTIONS:
        items = sections.get(key) or []
        out.append(title)
        if items:
            for item in items:
                out.append("  - " + item.text)
        elif not dropped.get(key):
            out.append("  无")
        if dropped.get(key):
            out.append(f"  还有 {dropped[key]} 项；取回：{retrieve}")
    return "\n".join(out) + "\n"


def _fits(text: str, limits: Limits) -> bool:
    return (text.count("\n") <= limits.lines
            and len(text.encode("utf-8")) <= limits.bytes)


def render(brief: dict, *, limits: Limits = Limits()) -> str:
    """Compose the brief, shedding the lowest-priority items until it fits.

    Skeleton items (section 1 paths, section 2 keys, section 6 health) are never
    shed: a brief without them is not a recovery brief, it is a list. When only
    skeleton items are left and the text still does not fit, the budget loses -
    printing an over-long but complete page beats printing a page that omits the
    entry points.
    """
    sections = {key: list(brief.get("sections", {}).get(key) or [])
                for key, _title in SECTIONS}
    dropped = {key: 0 for key, _title in SECTIONS}
    retrieve = brief.get("retrieve") or "python -m super_agent.cli brief --full"
    header = list(brief.get("header") or [])
    while True:
        text = _compose(header, sections, dropped, retrieve)
        if _fits(text, limits):
            return text
        worst = None
        for key, _title in SECTIONS:
            for idx, item in enumerate(sections[key]):
                if item.priority <= PRIORITY_SKELETON:
                    continue
                if worst is None or item.priority > worst[2].priority:
                    worst = (key, idx, item)
        if worst is None:
            return text
        key, idx, _item = worst
        sections[key].pop(idx)
        dropped[key] += 1


# -------------------------------------------------------------- entry point
def build_brief(cfg: dict, store, *, target: str = "supervisor",
                project: str | None = None,
                limits: Limits = Limits(),
                lwa_root: str | None = None) -> dict:
    """Collect the six sections. Read-only: nothing here writes a file."""
    root = _lwa_root(cfg, lwa_root)
    problems: list[Problem] = []

    index_path = store.index_path
    index, index_error = _strict_json(index_path)
    if index is None:
        problems.append(Problem("index.json", index_error))

    project_issues: set[int] | None = None
    if target == "pm":
        project_issues = _project_issues(cfg, project, problems)
        if project is None:
            problems.append(Problem("project", "pm brief 必须给 --project"))

    sections: dict[str, list[Item]] = {key: [] for key, _t in SECTIONS}
    sections["role"] = _role_items(root, str(cfg.get("role") or "supervisor"))

    project_items, project_problems = _projects_items(
        cfg, target=target, project=project, limit=limits.projects,
        wb_root=root)
    sections["projects"] = project_items
    problems.extend(project_problems)

    outbox = _outbox_items(store, markers=_markers(cfg))
    from_index = _index_items(index, markers=_markers(cfg))
    if project_issues is not None:
        outbox = [it for it in outbox if _item_in_project(it, project_issues)]
        from_index = [it for it in from_index if _item_in_project(it, project_issues)]
    for item in outbox + from_index:
        sections[item.section].append(item)

    sections["decisions"] = _recent_decisions(
        index, project_issues=project_issues, limit=limits.decisions)
    sections["health"] = _health_items(store, index=index,
                                       index_error=index_error)

    header = [f"# brief target={target}"
              + (f" project={project}" if project else "")
              + f" at={_now()}",
              f"# state={store.root}  limits={limits.lines}行/{limits.bytes}B"]
    if target == "pm":
        header.append(f"# 本项目：{project or '(未指定)'}；其他项目的条目已过滤")
    for problem in problems:
        header.append(f"# WARN {problem.source}: {problem.detail}")
    return {"header": header, "sections": sections, "problems": problems,
            "retrieve": _retrieve_command(target, project), "target": target,
            "project": project}


def _retrieve_command(target: str, project: str | None) -> str:
    cmd = f"python -m super_agent.cli brief --for {target}"
    if target == "pm" and project:
        cmd += f" --project {project}"
    return cmd + " --full"


def _item_in_project(item: Item, issues: set[int]) -> bool:
    for token in str(item.text).split():
        if token.startswith("issue#"):
            try:
                return int(token[len("issue#"):]) in issues
            except ValueError:
                return False
    return False


def _project_issues(cfg: dict, project: str | None,
                    problems: list[Problem]) -> set[int]:
    """Issue numbers the allowlist binds to ``project`` (empty = nothing shown)."""
    from .allowlist import ProjectAllowlist
    path = str((cfg.get("engine") or {}).get("project_allowlist") or "").strip()
    if not project:
        return set()
    if not path:
        problems.append(Problem("allowlist", "engine.project_allowlist 未配置"))
        return set()
    try:
        data = ProjectAllowlist(path).data or {}
    except Exception as exc:  # noqa: BLE001
        problems.append(Problem("allowlist", f"{exc.__class__.__name__}: {exc}"))
        return set()
    entry = (data.get("projects") or {}).get(project)
    if not isinstance(entry, dict):
        problems.append(Problem("allowlist", f"项目 {project!r} 未登记"))
        return set()
    return {int(n) for n in (entry.get("issues") or [])}


# ------------------------------------------------------------------ context
def parse_issue_ref(text: str, default_repo: str = "") -> tuple[str, int]:
    """``owner/name#12``, ``#12`` or ``12`` -> ``(repo, 12)``."""
    raw = str(text or "").strip()
    if "#" in raw:
        repo, _, number = raw.partition("#")
        repo = repo.strip() or default_repo
    else:
        repo, number = default_repo, raw
    number = number.strip()
    if not number.isdigit():
        raise ValueError(f"issue 引用里没有编号: {text!r}")
    if not repo or repo.count("/") != 1:
        raise ValueError("issue 引用需要 repo（owner/name），配置里也没有 github.repo")
    return repo, int(number)


def build_context(source, ref: str, *, default_repo: str = "",
                  limits: Limits = Limits(), max_comments: int = 8) -> dict:
    """One issue card: goal, last decision, acceptance, comment ids.

    Comment bodies are never returned in full: every one becomes a single
    collapsed line clipped to ``limits.comment_preview`` characters.
    """
    repo, number = parse_issue_ref(ref, default_repo)
    title = source.title(number)
    comments = source.comments(number)
    preview = limits.comment_preview
    tail = comments[-max_comments:]
    last = comments[-1] if comments else None
    acceptance = []
    for comment in reversed(comments):
        for line in str(comment.get("body") or "").splitlines():
            if not ("验收" in line or "acceptance" in line.lower()):
                continue
            text = _clip(line, preview)
            if text and text not in acceptance:
                acceptance.append(text)
            if len(acceptance) >= 3:
                break
        if len(acceptance) >= 3:
            break
    return {
        "repo": repo,
        "issue": number,
        "title": _clip(title, preview),
        "last_decision": (
            {"id": last.get("id"), "summary": _clip(
                (str(last.get("body") or "").lstrip().splitlines() or [""])[0],
                preview)}
            if last else None),
        "acceptance": acceptance,
        "comments": [{"id": c.get("id"),
                      "summary": _clip(
                          (str(c.get("body") or "").lstrip().splitlines() or [""])[0],
                          preview)}
                     for c in tail],
        "total_comments": len(comments),
    }


def render_context(card: dict) -> str:
    out = [f"# context {card['repo']}#{card['issue']}",
           f"目标: {card['title'] or '(无标题)'}"]
    last = card.get("last_decision")
    out.append(f"最近决定: comment {last['id']} {last['summary']}"
               if last else "最近决定: 无（该单还没有评论）")
    if card.get("acceptance"):
        out.append("验收要求:")
        out.extend("  - " + line for line in card["acceptance"])
    else:
        out.append("验收要求: 未在评论里找到（关键字：验收／acceptance）")
    out.append(f"相关评论 id（共 {card['total_comments']} 条，取最近 "
               f"{len(card['comments'])} 条）:")
    if not card["comments"]:
        out.append("  无")
    for row in card["comments"]:
        out.append(f"  - {row['id']} {row['summary']}")
    return "\n".join(out) + "\n"
