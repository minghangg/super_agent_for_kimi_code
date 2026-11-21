"""GitHub issue CLI: policy, prefixes, ledger and HTTP calls for one repository.

This is the implementation behind ``tools/gh_issue.py``. The shell there adds
the ``<!-- session: ... -->`` marker and refuses a ``comment`` without a key;
everything else -- who may do what, which prefix a role gets, which project
maps to which repository, the ledger row, the HTTP call -- lives here.

Four sources of deployment fact, and none of them is baked in:

* **project table** -- ``--projects-file`` > ``$SAFKC_PROJECTS_FILE`` >
  ``<checkout>/config/gh_projects.json``. The file is untracked; the shape is
  shown in ``config/sample.projects.json``. There is no built-in repository
  list: a default table of real repositories is a private fact, and one that
  cannot be published.
* **token** -- ``$GITHUB_TOKEN`` > ``--env-file`` >
  ``<checkout>/auth/github.env``. The value is never printed, not even in an
  error message.
* **roles** -- see :mod:`super_agent.role_config`. Public symbols only; the
  deployment's own spelling comes from ``config/roles.local.json``.
* **session state** -- see :mod:`super_agent.session_ledger`, which resolves
  the root with ``session_events`` so that a binding written here is read by
  the engine. A second resolution chain would write into a directory nobody
  reads, and the failure is silent.

Thirteen subcommands, all served here. Nothing is forwarded to another
checkout: two entry points for one command face is how a policy table starts
disagreeing with itself.

Usage::

    python -m super_agent.issue_cli verify
    python -m super_agent.issue_cli create --role supervisor --project demo \\
        --title "..." --body-file D:/temp/task.md
    python -m super_agent.issue_cli comment --role worker \\
        --worker-name wk-demo-1 --project demo --issue 42 --body "..."
    python -m super_agent.issue_cli close --role supervisor --project demo --issue 42

Every command takes ``--dry-run``: the request is printed and nothing is sent,
and no ledger row is written.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import agent_config
from . import allowlist as allowlist_mod
from . import config as config_mod
from . import flow as flow_mod
from . import markers
from . import role_config as roles_mod
from . import session_ledger as ledger_mod

API = "https://api.github.com"
CHECKOUT = Path(__file__).resolve().parents[1]
DEFAULT_PROJECTS = CHECKOUT / "config" / "gh_projects.json"
DEFAULT_ALLOWLIST = CHECKOUT / "config" / "allowlist.json"
DEFAULT_ROLES = CHECKOUT / "config" / "roles.local.json"

PROJECTS_ENV = "SAFKC_PROJECTS_FILE"
ALLOWLIST_ENV = "SAFKC_ALLOWLIST"
CONFIG_ENV = "SAFKC_CONFIG"

# issue three-state: open means "in flight"; a hold label means "suspended";
# closed means "done". The engine skips a held issue entirely -- not one
# request -- so a new issue starts held and is released by ``open``.
HOLD_LABEL = "stage:on-hold"

HOLD_LABEL_COLORS = {
    "stage:plan-review": "6f42c1", "stage:plan-revise": "d4c5f9",
    "stage:dev": "1d76db", "stage:qa": "fbca04",
    "stage:released": "0e8a16", HOLD_LABEL: "ededed",
    "flow:at": "6f42c1", "flow:standard": "1d76db",
    "sev:p0": "b60205", "sev:p1": "d93f0b", "sev:p2": "fbca04",
    "kind:bug": "e11d21", "kind:feat": "a2eeef", "kind:ops": "bfd4f2",
}

EXIT_DENIED = 3
EXIT_EVENT_LOST = 8

_LABEL_CACHE: dict[str, set] = {}


class Denied(Exception):
    """The call is not allowed. Maps to exit code 3, never a request."""


# ------------------------------------------------------------------- config
def _checkout() -> Path:
    return CHECKOUT


def _projects_path(arg: str | None = None) -> Path:
    raw = (arg or os.environ.get(PROJECTS_ENV) or "").strip()
    if not raw:
        raw = agent_config.projects_file()
    return Path(os.path.expanduser(raw)).resolve() if raw else DEFAULT_PROJECTS


def _allowlist_path(arg: str | None = None) -> Path:
    raw = (arg or os.environ.get(ALLOWLIST_ENV) or "").strip()
    if not raw:
        raw = agent_config.allowlist_file()
    return Path(os.path.expanduser(raw)).resolve() if raw else DEFAULT_ALLOWLIST


def _config_path(arg: str | None = None) -> Path | None:
    """``$SAFKC_CONFIG`` > an explicit ``--config`` > nothing.

    ``<checkout>/config/engine.json`` is **not** a fallback: a deployment file
    inside the public checkout is the wrong owner for a private fact (supervisor,
    cid 7003789336 §二). The session root now arrives through
    :mod:`super_agent.agent_config` instead, so this function returning ``None``
    no longer means "the ledger is silently skipped".
    """
    raw = (arg or os.environ.get(CONFIG_ENV) or "").strip()
    if raw:
        return Path(os.path.expanduser(raw)).resolve()
    return None


def _engine_cfg(arg: str | None = None) -> dict:
    """The engine block the session paths are resolved from.

    The private root comes from :mod:`super_agent.agent_config` (``paths.state_dir``
    -> ``engine.session_state_dir``), merged over the public defaults by
    ``config.load`` -- the one place that validates it. ``$SAFKC_CONFIG``, when
    set, is read as well, so an explicit deployment file can still win.

    A broken private file raises :class:`agent_config.AgentConfigError`, which
    the CLI entry turns into rc=2 **with the path printed**; it is never
    downgraded to "not configured" (that would write a ledger nobody reads).
    """
    return _full_cfg(arg).get("engine") or {}


def _full_cfg(arg: str | None = None) -> dict:
    """The whole config (defaults <- optional file <- agent_config overrides)."""
    path = _config_path(arg)
    overrides = agent_config.overrides()
    return config_mod.load(path, overrides=overrides) if overrides else config_mod.load(path)


def _load_projects(arg: str | None = None) -> dict:
    """The project table. Both layers empty -> rc=3 ``Denied`` (#98 反例 1).

    An inline table in the private file wins over ``projects_file``; when
    neither is configured the call is refused **with the three ways to supply
    one** -- running against a built-in empty table would turn every project
    into "unknown project" and hide the real cause.
    """
    inline = agent_config.projects()
    if inline:
        return inline
    path = _projects_path(arg)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise Denied(
            f"项目表未配置：{path}。三种给法：① {agent_config.config_path()} 里写 "
            f"\"github\": {{\"projects\": {{\"<短名>\": \"<owner>/<repo>\"}}}}；"
            f"② 写 \"github\": {{\"projects_file\": \"<路径>\"}}；"
            f"③ 给 --projects-file 或 ${PROJECTS_ENV}") from None
    except (OSError, ValueError) as exc:
        raise Denied(f"项目表读不出：{path}：{type(exc).__name__}: {exc}") from None
    projects = (data or {}).get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, dict):
        raise Denied(f"项目表格式不对：{path} 需要 {{\"projects\": {{\"<短名>\": \"<owner>/<repo>\"}}}}")
    return {str(k).lower(): str(v) for k, v in projects.items() if str(v).strip()}


def _repo(project: str, projects: dict) -> str:
    key = (project or "").strip().lower()
    if not projects:
        raise Denied("项目表为空：所有 --project 都会被拒")
    if key not in projects:
        raise Denied(f"未知项目 {project!r}（可选：{'/'.join(sorted(projects))}）")
    return projects[key]


def _token(env_file: str | None = None) -> str:
    """The token value, from the env layer or the configured file.

    The variable name and the file both come from ``agent_config``
    (``github.token_env`` / ``github.token_file``), so a deployment names its own
    credential source. Nothing here prints a value -- only the source name.
    """
    env_name, default_file = agent_config.token_source()
    tok = (os.environ.get(env_name) or "").strip()
    if tok:
        return tok
    raw = env_file or default_file
    if not raw:
        raise Denied(
            f"找不到 token：未配置 github.token_file，且 ${env_name} 为空"
            f"（在 {agent_config.config_path()} 里给 github.token_file）")
    path = Path(os.path.expanduser(raw)).resolve()
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith(f"{env_name}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    raise Denied(f"找不到 {env_name}（环境变量、--env-file 或 {path}）")


# ------------------------------------------------------------------- roles
def _roles(arg: str | None = None) -> roles_mod.RoleConfig:
    path = Path(os.path.expanduser(arg)).resolve() if arg else DEFAULT_ROLES
    return roles_mod.load(path)


def _role(arg: str | None, table: roles_mod.RoleConfig) -> str:
    raw = (arg or os.environ.get("AGENT_ROLE") or "").strip()
    if not raw:
        raise Denied(
            f"缺角色：给 --role 或设 AGENT_ROLE（可用：{'|'.join(table.available())}）")
    symbol = table.canonical(raw)
    if symbol is None:
        raise Denied(
            f"未知角色 {raw!r}；可用：{'|'.join(table.available())}。"
            f"私有别名要写在 {DEFAULT_ROLES}（未跟踪）里，"
            f"或用 ${roles_mod.ENV_ALIASES} 给")
    return symbol


def _policy(table: roles_mod.RoleConfig) -> dict:
    """Who may do what. Keys are role **symbols**, never spellings."""
    every = set(table.symbols())
    sup = roles_mod.SUPERVISOR
    return {
        "create": {sup},
        "close": {sup},
        "edit": {sup},
        "label": {sup, "pm"},
        "open": {sup},
        "hold": {sup},
        "bind": {sup, "pm"},
        "comment": every,
        "show": every,
        "list": every,
        "verify": every,
        "projects": every,
        "labels-init": {sup, "pm"},
    }


def _check(action: str, role: str, table: roles_mod.RoleConfig) -> None:
    allowed = _policy(table).get(action, set())
    if role not in allowed:
        raise Denied(
            f"角色 {role} 无权执行 {action}（允许：{'/'.join(sorted(allowed))}）")


def _delivery_name(name: str, table: roles_mod.RoleConfig) -> str:
    """Validate ``--worker-name``; the name goes straight into an engine regex.

    The engine matches a delivery to an assignment by exact string equality
    (``assignments.py`` compares ``session_key`` with the name in ``[WK:<name>]``),
    so one missing prefix is ``assignment.unmatched``: the delivery is not
    recorded, the QA pass is ``review.unmatched``, and nobody is reminded.
    A name that carries no role prefix at all gets a warning for that reason.
    """
    text = (name or "").strip()
    if not text or len(text) > 64 or "]" in text:
        raise Denied(
            f"--worker-name 必须是 1..64 个不含 ']' 的字符"
            f"（引擎正则是 '{{1,64}}'）：{name!r}")
    low = text.lower()
    if not any(low.startswith(p) for p in table.session_prefixes()):
        print(f"  ⚠ --worker-name={text!r} 看起来少了角色前缀。"
              f"引擎用精确相等把交付挂到 assignment，少前缀即 assignment.unmatched。",
              file=sys.stderr)
        print(f"  ⚠ 正确写法是完整会话键名，例如 wk-ISS-92-dev-20261118。",
              file=sys.stderr)
    return text


def _already_prefixed(body: str, prefix: str) -> bool:
    """Is the body's first line already carrying ``prefix``?

    Two shapes need care. A bare ``[SC]`` is not a delivery marker at all, so a
    body starting with ``[SC] 请求裁决`` must still get its name. And a verdict
    prefix is a subset of a dispatch prefix: ``[PM:TASK]`` already is a ``[PM]``
    line, so adding one more produces ``[PM] [PM:TASK] ...`` and the task book
    is then read as a verdict.
    """
    first = (body or "").lstrip()
    if prefix.lower() == "[pm]":
        return bool(re.match(r"\[PM(?::TASK)?\]", first, re.I))
    if ":" in prefix:
        bare = prefix.split(":", 1)[0].rstrip("]")
        return bool(re.match(re.escape(bare) + r"[:]", first, re.I))
    bare = prefix.rstrip("]")
    return bool(re.match(re.escape(bare) + r"[:\]]", first, re.I))


def _prefix(role: str, worker_name: str | None, table: roles_mod.RoleConfig,
            *, do: str = "", notify: str = "", project: str = "",
            cid: str = "", session_key: str = "") -> str:
    """The first-line prefix for this comment.

    Two shapes, chosen by whether ``--do`` was given (ISS-99):

    * **five-part** ``[who]-[do]-[notify]-[project]-[cid]`` -- the caller named
      an action, so the title says who wrote it, what it did, who to tell and
      which project. The action is checked against the whitelist here: a typo
      that reached GitHub would be read as "no notification" by the engine, and
      the writer would never learn why nobody answered;
    * **legacy** ``[WK:<name>]`` / ``[QA]`` -- unchanged, because 3,000+ comments
      already on GitHub carry that shape.

    ``cid`` is optional: not every action answers a comment (``[裁决]`` and
    ``[已归档]`` do not), so demanding it would make those titles impossible.
    """
    if do:
        action = do.strip()
        if action not in markers.NOTIFY_ACTIONS:
            raise Denied(
                f"--do {action!r} 不在白名单（{len(markers.NOTIFY_ACTIONS)} 个）："
                f"{'、'.join(markers.NOTIFY_ACTIONS)}")
        # The first segment is a **session key**, never a bare role marker. A
        # bare `[SC]` carries no name, so the engine cannot attribute the
        # comment and `parse_delivery` refuses it -- the comment looks correct
        # and is silently not a delivery. That is exactly the hole #99 closed,
        # and it reopened here because this branch preferred the role prefix.
        #
        # Order: --worker-name (delivery roles) → --session-key (every role) →
        # refuse. The last step is a refusal, not a fallback to the prefix:
        # a fallback produces the broken shape silently.
        who = (worker_name or session_key or "").strip().strip("[]")
        if not who:
            raise Denied(
                "五段式首段必须是会话键名：给 --worker-name（交付角色）或 --session-key。"
                f"裸角色标记 {table.prefix(role)!r} 不算键名——引擎无法归属，"
                "parse_delivery 会判它不是交付。")
        if not notify:
            raise Denied("--do 必须配 --notify（不通知写 'No Notify'）")
        if not project:
            raise Denied("--do 必须配 --project-key（gh_projects.json 的键）")
        head = f"[{who}]-[{action}]-[{notify}]-[{project}]"
        if cid:
            head += f"-[{cid}]"
        return head
    if worker_name is not None:
        delivery = table.delivery_prefix(role)
        if not delivery:
            raise Denied(
                f"角色 {role!r} 不能用 --worker-name 拼交付前缀"
                f"（可拼：{'/'.join(sorted(table.delivery))}）。"
                f"{role!r} 不产交付：用 {table.brackets(role)} ＋ verdict 标记")
        return f"[{delivery}:{_delivery_name(worker_name, table)}]"
    if role == "worker":
        raise Denied("worker 角色必须给 --worker-name（拼成 [WK:<任务名>]）")
    return f"[{table.prefix(role)}]"


# -------------------------------------------------------------------- HTTP
def _headers(env_file: str | None, table: roles_mod.RoleConfig) -> dict:
    return {
        "Authorization": f"Bearer {_token(env_file)}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": table.user_agent,
    }


def _call(method: str, path: str, payload: dict | None = None, *,
          dry_run: bool = False, env_file: str | None = None,
          table: roles_mod.RoleConfig | None = None) -> dict:
    """One API call. ``--dry-run`` prints it and sends nothing.

    The printed preview carries the body's **first and last line**, not just the
    payload's key names: the first line is the role prefix and the last one is
    the session marker, and those two are exactly what a caller has to see
    before sending a verdict.
    """
    url = f"{API}{path}"
    if dry_run:
        print(f"[dry-run] {method} {url}  body_keys={list((payload or {}))}")
        body = str((payload or {}).get("body") or "")
        lines = [line for line in body.splitlines() if line.strip()]
        if lines:
            print(f"  body[0]: {lines[0]}")
            if len(lines) > 1:
                print(f"  body[-1]: {lines[-1]}")
        return {"dry_run": True}
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in _headers(env_file, table or roles_mod.load(DEFAULT_ROLES)).items():
        req.add_header(key, value)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "replace")
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise SystemExit(f"GitHub API {exc.code} {method} {path}：{detail}")


def _call_raw(method: str, path: str, payload: dict | None = None, *,
              dry_run: bool = False, env_file: str | None = None,
              table: roles_mod.RoleConfig | None = None) -> dict:
    """Like :func:`_call`, but an HTTP error is returned instead of raised."""
    url = f"{API}{path}" if not str(path).startswith("http") else str(path)
    if dry_run:
        print(f"[dry-run] {method} {url}  body_keys={list((payload or {}))}")
        return {"dry_run": True}
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in _headers(env_file, table or roles_mod.load(DEFAULT_ROLES)).items():
        req.add_header(key, value)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "replace")
            out = json.loads(body) if body.strip() else {}
            if isinstance(out, dict):
                out.setdefault("_status", resp.status)
            return out
    except urllib.error.HTTPError as exc:
        return {"_status": exc.code,
                "_error": exc.read().decode("utf-8", "replace")[:400]}


def _file_get(url: str, *, env_file: str | None = None,
              table: roles_mod.RoleConfig | None = None) -> dict:
    full = url if str(url).startswith("http") else f"{API}{url}"
    req = urllib.request.Request(full, method="GET")
    for key, value in _headers(env_file, table or roles_mod.load(DEFAULT_ROLES)).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "replace")
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise SystemExit(f"GitHub API {exc.code} GET {url}：{detail}")


def _rows(rows) -> list:
    return rows if isinstance(rows, list) else []


def _label_exists(repo: str, name: str, **kw) -> bool:
    cached = _LABEL_CACHE.get(repo)
    if cached is not None and name in cached:
        return True
    out = _call_raw("GET", f"/repos/{repo}/labels/{urllib.parse.quote(name, safe='')}", **kw)
    if out.get("_status") == 200 and out.get("name"):
        _LABEL_CACHE.setdefault(repo, set()).add(name)
        return True
    return False


def _list_repo_labels(repo: str, **kw) -> list[dict]:
    out: list[dict] = []
    for page in range(1, 11):
        data = _file_get(f"/repos/{repo}/labels?per_page=100&page={page}", **kw)
        if not isinstance(data, list) or not data:
            break
        out.extend(x for x in data if isinstance(x, dict))
        if len(data) < 100:
            break
    for item in out:
        _LABEL_CACHE.setdefault(repo, set()).add(str(item.get("name")))
    return out


def _live_issue_numbers(repo: str, **kw) -> list[int]:
    try:
        rows = _call("GET", f"/repos/{repo}/issues?state=open&per_page=100", **kw)
    except Exception:      # noqa: BLE001  printing only; never blocks the command
        return []
    out = []
    for row in _rows(rows):
        if not isinstance(row, dict) or not row.get("number") or row.get("pull_request"):
            continue
        names = {str((x or {}).get("name") or "") for x in (row.get("labels") or [])}
        if HOLD_LABEL not in names:
            out.append(int(row["number"]))
    return sorted(out)


def _has_qa_comment(repo: str, issue: int, **kw) -> bool:
    rows = _call("GET", f"/repos/{repo}/issues/{issue}/comments?per_page=100", **kw)
    return any(str(r.get("body", "")).lstrip().startswith("[QA]") for r in _rows(rows))


# ------------------------------------------------------------------- ledger
def _ledger(role: str, act: str, repo: str, issue, cfg: dict | None,
            ok: bool = True, note: str = "") -> None:
    try:
        ledger_mod.ledger({"role": role, "act": act, "repo": repo, "issue": issue,
                           "ok": ok, "note": note}, cfg=cfg)
    except ledger_mod.SessionLedgerError as exc:
        print(f"[警告] 台账未落盘：{exc}", file=sys.stderr)


# ---------------------------------------------------------------- allowlist
def _register_in_allowlist(project: str, repo: str, issue: int,
                           path: Path) -> str:
    """Add the new issue number to the project's entry in the live allowlist.

    The engine decides "is this issue in flight" from the allowlist overlay it
    rewrites every round, so a number that is not there has every frame refused
    before dispatch, silently. Writing it at creation time is what stops a
    person having to remember.

    The allowlist names projects its own way, so the entry is found by
    repository, not by guessing that the two short names are equal. A failure
    is reported, never raised: the issue already exists on GitHub, and this
    must not read as "creation failed".
    """
    if not path.is_file():
        return (f"  allowlist 未找到（{path}），跳过自动入册——"
                f"新单的帧会投不出去，请手工补")
    try:
        table = allowlist_mod.load(path)
    except Exception as exc:      # noqa: BLE001  registering must not block
        return f"  allowlist 读不出（{type(exc).__name__}: {exc}），跳过自动入册"
    wanted = str(repo).strip().lower()
    key = None
    for name, entry in ((table.data or {}).get("projects") or {}).items():
        if str((entry or {}).get("repo") or "").strip().lower() == wanted:
            key = name
            break
    if key is None:
        known = "/".join(sorted((table.data or {}).get("projects") or {}))
        return (f"  allowlist 里没有认领 {repo} 的项目（现有键：{known}）"
                f"——新单的帧会投不出去，请手工补")
    current = list(((table.data or {})["projects"].get(key) or {}).get("issues") or [])
    if int(issue) in current:
        return f"  allowlist 已含 #{issue}（项目键 {key}），无需改动"
    out = table.sync_issues(key, sorted(set(current) | {int(issue)}))
    if out.get("error"):
        return f"  allowlist 自动入册失败：{out['error']}——新单的帧会投不出去，请手工补"
    if not out.get("changed"):
        return f"  allowlist 无需改动（项目键 {key}）"
    return (f"  allowlist 已写入 #{issue}（项目键 {key}）；生效表由引擎每轮 "
            f"sync_issues() 覆盖，本行只是登记事实")


# ------------------------------------------------------------------ archive
def _archive_session(sid: str, engine: dict) -> bool | None:
    """Archive one session through the configured command.

    ``True`` = done (including "archived but the local event was not written",
    which is success because archive is not reversible), ``False`` = failed (the
    registration is kept so a retry is possible), ``None`` = no command
    configured, so nothing was attempted and nothing is claimed.

    The command is deployment fact: this repository has no session console and
    must not name another checkout's script. The public default
    (``config.DEFAULTS["engine"]["session_archive_cmd"]``) therefore names
    **this** package -- ``{python} -m super_agent.cli recycle archive {sid}`` --
    so a fresh clone plus one private ``agent.json`` can archive without naming
    any other checkout (ISS-100 D5).
    """
    template = str(engine.get("session_archive_cmd") or "").strip()
    if not template:
        print("  未配置归档命令，登记保留（engine.session_archive_cmd）")
        return None
    # Both placeholders are replaced **after** the split: ``shlex.split`` would
    # eat the backslashes in ``sys.executable`` (``D:\x`` -> ``D:x``), and a
    # token-wise compare keeps the substitution from touching an argument that
    # merely contains the text ``{python}``. ``{python}`` is the interpreter
    # running the engine, so the recycle entry point is always importable.
    argv = [part.replace("{sid}", sid) for part in _split_command(template)]
    argv = [sys.executable if part == "{python}" else part for part in argv]
    if not argv:
        print("  archive 命令为空，登记保留")
        return None
    # The child must import **this** checkout, not another one. ``sys.executable``
    # only pins the interpreter: on a machine with this package installed
    # editable, ``import super_agent`` resolves to the installed checkout no
    # matter which directory the child runs in (measured, ISS-100 D5). The value
    # is overwritten, not setdefault-ed: a caller's ``PYTHONPATH`` may name a
    # different checkout, and that is exactly the case this line must remove.
    env = dict(os.environ)
    env["PYTHONPATH"] = str(CHECKOUT)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30,
                              env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  archive 失败（不阻断关单）：{type(exc).__name__}: {exc}")
        return False
    out = (proc.stdout or "").strip()
    print("  archive:", out or proc.returncode)
    if proc.returncode == EXIT_EVENT_LOST:
        print("  ⚠ 归档已生效但 session.archived 事件未落（rc=8）；"
              "该会话的归档闸旁证会退化")
        return True
    return proc.returncode == 0


def _split_command(template: str) -> list[str]:
    import shlex
    try:
        return shlex.split(template)
    except ValueError:
        return template.split()


# ------------------------------------------------------------------ commands
def cmd_verify(args, ctx) -> int:
    rows = _call("GET", "/user", **ctx.call)
    print("login =", (rows or {}).get("login"))
    for proj, repo in sorted(ctx.projects.items()):
        meta = _call("GET", f"/repos/{repo}", **ctx.call) or {}
        print(f"{proj:12} {repo:26} issues={meta.get('has_issues')} "
              f"private={meta.get('private')}")
    return 0


def cmd_create(args, ctx) -> int:
    role = _role(args.role, ctx.roles)
    _check("create", role, ctx.roles)
    repo = _repo(args.project, ctx.projects)
    body = (Path(args.body_file).read_text(encoding="utf-8")
            if args.body_file else (args.body or ""))
    pfx = _prefix(role, args.worker_name, ctx.roles)
    head = body if _already_prefixed(body, pfx) else f"{pfx} {body}"
    payload = {"title": args.title, "body": head.strip()}
    labels = [x.strip() for x in (args.labels or "").split(",") if x.strip()]
    if not getattr(args, "open", False) and HOLD_LABEL not in labels:
        labels.append(HOLD_LABEL)
    if labels:
        payload["labels"] = labels
    out = _call("POST", f"/repos/{repo}/issues", payload, dry_run=args.dry_run, **ctx.call)
    if out.get("dry_run"):
        return 0
    number = out.get("number")
    print(f"created #{number} {out.get('html_url')}")
    print(_register_in_allowlist(args.project, repo, number, ctx.allowlist))
    if HOLD_LABEL in labels:
        print(f"  已挂起（{HOLD_LABEL}）：引擎不会看它。开工时跑 "
              f"`gh_issue.py open --issue {number}` 摘除。")
    else:
        print("  ⚠ 未挂起（--open）：引擎下一轮就会检查它。")
    _ledger(role, "create", repo, number, ctx.cfg, note=(args.title or "")[:60])
    return 0


def cmd_comment(args, ctx) -> int:
    role = _role(args.role, ctx.roles)
    _check("comment", role, ctx.roles)
    repo = _repo(args.project, ctx.projects)
    body = (Path(args.body_file).read_text(encoding="utf-8")
            if args.body_file else (args.body or ""))
    if not body.strip():
        raise Denied("评论正文为空")
    # ISS-99: the five-part title's project key must exist. A misspelled key is
    # invisible downstream -- the engine resolves recipients from the project,
    # so nobody gets told and nothing reports that. `super_agent_for_kimi_code`
    # is the repository name; the key is `superagent`, which is exactly the
    # mistake this check catches.
    project_key = str(getattr(args, "project_key", "") or "").strip()
    if project_key and project_key.lower() not in (ctx.projects or {}):
        raise Denied(
            f"--project-key {project_key!r} 不在项目登记表（{len(ctx.projects or {})} 个键）。"
            f"可选：{'、'.join(sorted(ctx.projects or {}))}")
    pfx = _prefix(role, args.worker_name, ctx.roles,
                  do=getattr(args, "do", "") or "",
                  notify=getattr(args, "notify", "") or "",
                  project=project_key,
                  cid=str(getattr(args, "cid", "") or ""),
                  session_key=str(getattr(args, "session_key", "") or ""))
    head = body if _already_prefixed(body, pfx) else f"{pfx} {body}"
    head = head.strip()
    # ISS-99: the session key goes on its own line. Inline would put it inside
    # the verdict's anchored tail and hide the marker from the parser.
    session_key = str(getattr(args, "session_key", "") or "").strip()
    if session_key:
        head += f"\n<!-- session: {session_key} -->"
    elif role in ("worker", "qa", "at", "scout", "steward", "auditor",
                  "reviewer", "analyst", "planner"):
        # Not a refusal: plenty of historical callers omit it and the comment is
        # still useful. But an unattributed verdict is silently ignored by the
        # engine, so say so rather than let the writer believe it landed.
        print(f"  ⚠ 未给 --session-key：引擎无法归属这条 {role} 评论，"
              f"裁决会被判 unverified。", file=sys.stderr)
    payload = {"body": head}
    out = _call("POST", f"/repos/{repo}/issues/{args.issue}/comments", payload,
                dry_run=args.dry_run, **ctx.call)
    if out.get("dry_run"):
        return 0
    print(f"commented #{args.issue} id={out.get('id')}")
    print("  提示：评论不唤醒会话；需唤醒请 dispatch 或依赖收单哨")
    # ISS-99: the ledger carries the **title**, not just the cid. The delivery
    # watcher decides "is this a real delivery or a probe" from the title, and
    # until now the ledger had no title to read -- so it could not tell them
    # apart and every probe spent quota.
    first = head.splitlines()[0] if head else ""
    note = f"cid:{out.get('id')}" if out.get("id") else ""
    if first:
        note += f";title:{first[:200]}"
    _ledger(role, "comment", repo, args.issue, ctx.cfg, note=note)
    return 0


def _audit_line(note: str, role: str, table: roles_mod.RoleConfig) -> str:
    """The audit line appended to an edited body (local time, fixed shape)."""
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    what = (note or "").strip() or "正文更新"
    return (f"\n\n---\n> **{table.brackets(role)} 编辑于 {stamp}"
            f"（Asia/Shanghai）**：{what}（正文）\n")


def cmd_edit(args, ctx) -> int:
    """Change the title or the body of an issue (supervisor only).

    A title change does not occupy the body, so it leaves a one-line comment
    instead; a body change appends an audit line to the new body, and the rest
    of the text is proven byte-identical to what the caller passed.
    """
    role = _role(args.role, ctx.roles)
    _check("edit", role, ctx.roles)
    repo = _repo(args.project, ctx.projects)
    new_body = None
    if args.body_file:
        new_body = Path(args.body_file).read_text(encoding="utf-8")
    elif args.body is not None:
        new_body = args.body
    if args.title is None and new_body is None:
        print("edit: 至少要给 --title 或 --body-file/--body", file=sys.stderr)
        return 2

    cur = _file_get(f"/repos/{repo}/issues/{args.issue}", **ctx.fetch)
    old_title = str((cur or {}).get("title") or "")
    old_body = str((cur or {}).get("body") or "")

    if args.title is not None:
        if args.dry_run:
            _call("PATCH", f"/repos/{repo}/issues/{args.issue}",
                  {"title": args.title}, dry_run=True, **ctx.call)
        else:
            _call("PATCH", f"/repos/{repo}/issues/{args.issue}",
                  {"title": args.title}, **ctx.call)
            print(f"edited #{args.issue} title：{old_title!r} → {args.title!r}")
            _call("POST", f"/repos/{repo}/issues/{args.issue}/comments",
                  {"body": f"{ctx.roles.brackets(role)} 标题改为「{args.title}」"
                           f"（原「{old_title}」）"}, **ctx.call)

    if new_body is not None:
        audit = _audit_line(args.note, role, ctx.roles)
        final_body = new_body + audit
        if args.dry_run:
            _call("PATCH", f"/repos/{repo}/issues/{args.issue}",
                  {"body": final_body}, dry_run=True, **ctx.call)
            print(f"[dry-run] 正文 {len(new_body)} → {len(final_body)} 字节（含审计行）")
        else:
            _call("PATCH", f"/repos/{repo}/issues/{args.issue}",
                  {"body": final_body}, **ctx.call)
            unchanged = final_body == new_body + audit
            print(f"edited #{args.issue} body：{len(old_body)} → {len(final_body)} 字节"
                  f"（正文＝原文+审计行 精确相等：{unchanged}）")

    if not args.dry_run:
        parts = []
        if args.title is not None:
            parts.append(f"title: {old_title[:30]} → {args.title[:30]}")
        if new_body is not None:
            parts.append(f"body: {(args.note or '正文更新')[:40]}")
        if parts:
            _ledger(role, "edit", repo, args.issue, ctx.cfg, note="；".join(parts))
    if args.dry_run:
        print("[dry-run] 未产生远端写与 ledger 行")
    return 0


def cmd_label(args, ctx) -> int:
    role = _role(args.role, ctx.roles)
    _check("label", role, ctx.roles)
    repo = _repo(args.project, ctx.projects)
    created: list[str] = []
    existed: list[str] = []
    if args.add:
        names = [x.strip() for x in args.add.split(",") if x.strip()]
        have = {n for n in names if _label_exists(repo, n, **ctx.call)}
        if args.no_create:
            # GitHub creates a missing label by itself when one is attached.
            # "--no-create" can only stop *this* command from creating it, so
            # the ones GitHub will create are printed instead of pretended away.
            missing = [n for n in names if n not in have]
            existed = [n for n in names if n in have]
            if missing:
                print(f"[--no-create] 以下 label 仓库里不存在，本脚本不建；"
                      f"但 GitHub 挂载时会自动创建（实测行为）：{missing}")
            if existed:
                print(f"已存在 label：{existed}")
        else:
            for name in names:
                if name in have:
                    existed.append(name)
                    continue
                if args.dry_run:
                    print(f"[dry-run] 将新建 label {name!r}（color={args.color}）")
                    created.append(name)
                    continue
                out = _call_raw("POST", f"/repos/{repo}/labels",
                                {"name": name, "color": args.color}, **ctx.call)
                if str(out.get("_status", 0)).startswith("2") and out.get("name"):
                    created.append(name)
                    have.add(name)
                    print(f"新建 label {name!r}（color={args.color}）")
                else:
                    print(f"label {name!r} 新建失败：{out.get('_error') or out}",
                          file=sys.stderr)
            if existed:
                print(f"已存在 label：{existed}")
        _call("POST", f"/repos/{repo}/issues/{args.issue}/labels",
              {"labels": names}, dry_run=args.dry_run, **ctx.call)
        print(f"labeled #{args.issue} +{names}")
    if args.remove:
        for name in [x.strip() for x in args.remove.split(",") if x.strip()]:
            path = (f"/repos/{repo}/issues/{args.issue}/labels/"
                    f"{urllib.parse.quote(name)}")
            out = _call_raw("DELETE", path, dry_run=args.dry_run, **ctx.call)
            status = out.get("_status", 200) if isinstance(out, dict) else 200
            if status == 404:
                print(f"labeled #{args.issue} -{name}（本就不存在）")
                continue
            if not str(status).startswith("2"):
                raise SystemExit(f"GitHub API {status} DELETE {path}："
                                 f"{out.get('_error') or out}")
            print(f"labeled #{args.issue} -{name}")
    if args.dry_run:
        return 0
    note = f"+{args.add or ''} -{args.remove or ''}"
    if created:
        note += f" created={','.join(created)}"
    _ledger(role, "label", repo, args.issue, ctx.cfg, note=note)
    return 0


def cmd_flow(args, ctx) -> int:
    role = _role(args.role, ctx.roles)
    _check("label", role, ctx.roles)
    selected = str(args.flow).lower()
    if selected not in flow_mod.LABELS:
        raise Denied("--flow 只能是 at 或 standard")
    args.add = flow_mod.LABELS[selected]
    args.remove = flow_mod.LABELS[flow_mod.STANDARD if selected == flow_mod.AT
                                    else flow_mod.AT]
    args.no_create = False
    args.color = "6f42c1" if selected == flow_mod.AT else "1d76db"
    result = cmd_label(args, ctx)
    if result == 0:
        print(f"flow #{args.issue} = {selected}")
    return result


def cmd_close(args, ctx) -> int:
    role = _role(args.role, ctx.roles)
    _check("close", role, ctx.roles)
    repo = _repo(args.project, ctx.projects)
    if not _has_qa_comment(repo, args.issue, **ctx.call):
        if not args.retro:
            raise Denied(f"#{args.issue} 没有 [QA] 开头的结论评论，按政策不许关单"
                         f"（确需追溯关闭：--retro --reason \"…\"）")
        reason = (args.reason or "").strip()
        if len(reason) < 10:
            raise Denied("--retro 必须给 --reason（≥10 字），写清为什么没有 QA 结论、谁验的")
        _call("POST", f"/repos/{repo}/issues/{args.issue}/comments",
              {"body": f"{ctx.roles.brackets(role)} **追溯关闭（无 QA 结论）**：{reason}"},
              dry_run=args.dry_run, **ctx.call)
        _ledger(role, "retro-close-comment", repo, args.issue, ctx.cfg,
                note=reason[:60])
    _call("PATCH", f"/repos/{repo}/issues/{args.issue}", {"state": "closed"},
          dry_run=args.dry_run, **ctx.call)
    print(f"closed #{args.issue}")
    _ledger(role, "close", repo, args.issue, ctx.cfg)

    # One issue, one set of sessions: closing archives this issue's QA and
    # worker. Registered long-lived sessions are never archived -- that took
    # the whole team offline once, and archive is not reversible.
    #
    # Order matters: archive first, delete the registration only when every
    # archive is done. The old order dropped the registration first, so a
    # failed archive left no record that anything was still to be done.
    key = f"{args.project}#{args.issue}"
    db = ledger_mod.load_issue_sessions(cfg=ctx.cfg)
    rec = db.get(key)
    if rec and not args.dry_run:
        fixed = ledger_mod.fixed_session_ids(ctx.roles.fixed_session_key, cfg=ctx.cfg)
        failed: list[tuple[str, str, str]] = []
        skipped = 0
        for bind_role, item in sorted((rec or {}).items()):
            sid = str((item or {}).get("session") or "")
            if not sid:
                continue
            if sid in fixed:
                print(f"跳过在册固定会话 {fixed[sid]}（{bind_role} {sid[:12]}…，不归档）")
                continue
            print(f"归档本单 {bind_role}：{(item or {}).get('name') or sid[:12]}")
            done = _archive_session(sid, ctx.engine)
            if done is None:
                skipped += 1
                continue
            if not done:
                failed.append((bind_role, sid, str((item or {}).get("name") or "")))
        if failed:
            print("归档失败（登记**保留**，可重试）：", file=sys.stderr)
            for bind_role, sid, name in failed:
                print(f"  - {bind_role} {sid}（{name or '无名字'}）", file=sys.stderr)
            if getattr(args, "strict", False):
                raise SystemExit(f"close：{len(failed)} 个会话归档失败（--strict）")
            return 0
        if skipped:
            print(f"（{skipped} 个会话未归档：归档命令未配置，登记保留）")
            return 0
        db.pop(key, None)                # every archive succeeded
        ledger_mod.save_issue_sessions(db, cfg=ctx.cfg)
    elif rec and args.dry_run:
        print(f"（dry-run：本单登记 {key} 有 {len(rec)} 个会话，未归档、未删登记）")
    elif not rec:
        print("（未登记本单会话，跳过归档；应用 bind 命令登记）")
    return 0


def cmd_projects(args, ctx) -> int:
    """List registered projects; ``--discover`` also scans the account."""
    for proj, repo in sorted(ctx.projects.items()):
        print(f"{proj:14} {repo}")
    if not args.discover:
        return 0
    known = set(ctx.projects.values())
    print("\n=== 名下仓库（issues=True 且未登记）===")
    rows = _call("GET", "/user/repos?per_page=100&affiliation=owner&sort=updated",
                 **ctx.call)
    for row in _rows(rows):
        full = row.get("full_name")
        if not row.get("has_issues") or full in known:
            continue
        print(f'  "{row.get("name", "").lower()}": "{full}"   # private={row.get("private")}')
    return 0


def cmd_open(args, ctx) -> int:
    """Remove the hold label: the issue is in flight from the next round on.

    Holding and releasing are one step each because the two facts that matter
    -- "the issue is open" and "the engine looks at it" -- must never be done
    apart. The live list is printed afterwards so the caller can see that the
    issue really entered it.
    """
    role = _role(args.role, ctx.roles)
    _check("open", role, ctx.roles)
    repo = _repo(args.project, ctx.projects)
    _call("DELETE", f"/repos/{repo}/issues/{args.issue}/labels/"
                    f"{urllib.parse.quote(HOLD_LABEL, safe='')}",
          dry_run=args.dry_run, **ctx.call)
    if args.dry_run:
        return 0
    print(f"opened #{args.issue}（已摘 {HOLD_LABEL}）：引擎下一轮开始检查它")
    flying = _live_issue_numbers(repo, **ctx.call)
    print(f"  当前在飞单：{flying or '（无）'}")
    if int(args.issue) not in flying:
        print(f"  ⚠ #{args.issue} 不在在飞列表里——核对 label 是否真的摘掉了",
              file=sys.stderr)
    _ledger(role, "open", repo, args.issue, ctx.cfg, note=f"-{HOLD_LABEL}")
    return 0


def cmd_hold(args, ctx) -> int:
    """Add the hold label back: the issue is suspended, not closed."""
    role = _role(args.role, ctx.roles)
    _check("hold", role, ctx.roles)
    repo = _repo(args.project, ctx.projects)
    _call("POST", f"/repos/{repo}/issues/{args.issue}/labels",
          {"labels": [HOLD_LABEL]}, dry_run=args.dry_run, **ctx.call)
    if args.dry_run:
        return 0
    print(f"held #{args.issue}（已加 {HOLD_LABEL}）：引擎不再检查它")
    _ledger(role, "hold", repo, args.issue, ctx.cfg, note=f"+{HOLD_LABEL}")
    return 0


def cmd_labels_init(args, ctx) -> int:
    """Create the standard label set; idempotent."""
    role = _role(args.role, ctx.roles)
    _check("labels-init", role, ctx.roles)
    repo = _repo(args.project, ctx.projects)
    have = {str(x.get("name")) for x in _list_repo_labels(repo, **ctx.call)}
    for name, color in HOLD_LABEL_COLORS.items():
        if name in have:
            print(f"= {name}")
            continue
        if args.dry_run:
            print(f"[dry-run] + {name}")
            continue
        _call("POST", f"/repos/{repo}/labels", {"name": name, "color": color},
              **ctx.call)
        print(f"+ {name}")
    if not args.dry_run:
        _ledger(role, "labels-init", repo, None, ctx.cfg,
                note=f"{len(HOLD_LABEL_COLORS)} labels")
    return 0


def cmd_bind(args, ctx) -> int:
    """Record which session works on this issue, so closing can archive it.

    The record goes to ``issue_sessions.json`` beside ``sessions.json`` -- the
    file the engine reads. A one-shot key given as ``--name`` is also written
    to ``sessions.json``, because that is what the context tools look up.
    A truncated sid is refused: it can never be delivered to, and a record that
    cannot be used is worse than no record, because it reads as done.
    """
    role = _role(args.role, ctx.roles)
    _check("bind", role, ctx.roles)
    if args.bind_role not in {"qa", "worker", "at"}:
        raise Denied("--bind-role 只能是 qa、worker 或 at")
    problem = ledger_mod.sid_problem(args.session)
    if problem and not getattr(args, "allow_short_sid", False):
        raise Denied(f"--session 不是完整 sid 形态；{problem}。"
                     f"确要登记残号请加 --allow-short-sid（会打 WARN）")
    if problem:
        print(f"[警告] {args.bind_role} 登记的是非完整 sid（--allow-short-sid）",
              file=sys.stderr)
        print(f"       {problem}", file=sys.stderr)
    key = f"{args.project}#{args.issue}"
    db = ledger_mod.load_issue_sessions(cfg=ctx.cfg)
    rec = db.setdefault(key, {})
    rec[args.bind_role] = {
        "session": args.session,
        "name": args.name or "",
        "bound": dt.datetime.now().isoformat(timespec="seconds"),
    }
    ledger_mod.save_issue_sessions(db, cfg=ctx.cfg)
    try:
        ledger_mod.emit("session.bound", cfg=ctx.cfg, issue=int(args.issue),
                        role=args.bind_role, sid=args.session,
                        **({"key": args.name} if args.name else {}))
    except ledger_mod.SessionLedgerError as exc:
        print(f"[警告] session.bound 未落盘：{exc}", file=sys.stderr)
    print(f"bound {key} {args.bind_role} = {args.session[:16]}…")
    report = ledger_mod.register_session_key(args.name, args.session, cfg=ctx.cfg)
    if report:
        print(report)
    _ledger(role, "bind", _repo(args.project, ctx.projects), args.issue, ctx.cfg,
            note=f"{args.bind_role}:{args.session[:12]}")
    return 0


def cmd_show(args, ctx) -> int:
    repo = _repo(args.project, ctx.projects)
    out = _call("GET", f"/repos/{repo}/issues/{args.issue}", **ctx.call) or {}
    print(f"#{out.get('number')} [{out.get('state')}] {out.get('title')}")
    print("labels:", ", ".join(str(x.get("name")) for x in out.get("labels", [])) or "-")
    body = (out.get("body") or "").strip()
    print(body[: args.chars] + ("…" if len(body) > args.chars else ""))
    return 0


def cmd_list(args, ctx) -> int:
    repo = _repo(args.project, ctx.projects)
    rows = _call("GET", f"/repos/{repo}/issues?state={args.state}&per_page={args.limit}",
                 **ctx.call)
    for row in _rows(rows):
        if "pull_request" in row:
            continue
        labels = ",".join(str(x.get("name")) for x in row.get("labels", [])) or "-"
        print(f"#{row['number']:>4} [{labels}] {row['title'][:70]}")
    return 0


# -------------------------------------------------------------------- entry
class _Ctx:
    """Everything a command needs, resolved once.

    ``call`` and ``fetch`` differ only in whether a missing token is reported
    as a refusal: both carry the env file and the role table down to the HTTP
    layer, which is where the token is read.
    """

    def __init__(self, args):
        # config.CTX-HOLD: ``cfg`` is the **whole** config, because the session
        # helpers (session_ledger / session_events) read ``cfg["engine"]``. Handing
        # them the engine block silently resolved to "no stream configured", so the
        # ledger row was dropped while the comment succeeded (#98).
        self.cfg = _full_cfg(getattr(args, "config", None))
        self.engine = (self.cfg.get("engine") or {})
        self.roles = _roles(getattr(args, "roles_file", None))
        self.allowlist = _allowlist_path(getattr(args, "allowlist", None))
        self.projects = _load_projects(getattr(args, "projects_file", None))
        env_file = getattr(args, "env_file", None)
        self.call = {"env_file": env_file, "table": self.roles}
        self.fetch = {"env_file": env_file, "table": self.roles}
        for warning in self.roles.warnings():
            print(f"[警告] {warning}", file=sys.stderr)


def _apply_state_dir(argv: list[str]) -> tuple[list[str], str | None]:
    """Take ``--state-dir`` off the command line and set the env var it maps to.

    Keeping one resolution chain is the point: the session root is resolved by
    ``session_events`` for the engine and the CLI alike, so the flag can only
    become the top level of that chain, never a second code path.
    """
    rest = list(argv)
    state_dir = None
    while "--state-dir" in rest:
        i = rest.index("--state-dir")
        if i + 1 < len(rest):
            state_dir = rest[i + 1]
            del rest[i:i + 2]
        else:
            del rest[i:i + 1]
    if state_dir:
        os.environ["SESS_EVENTS_STATE_DIR"] = os.path.expanduser(state_dir)
    return rest, state_dir


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="issue_cli",
        description="GitHub issue channel: policy, prefixes, ledger, HTTP.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(parser):
        parser.add_argument("--role", help="角色名（默认取 AGENT_ROLE）")
        parser.add_argument("--project", help="项目短名（见项目表）")
        parser.add_argument("--dry-run", action="store_true", help="只打印请求，不发")
        parser.add_argument("--projects-file", help="项目表路径")
        parser.add_argument("--roles-file", help="角色覆盖表路径（未跟踪）")
        parser.add_argument("--env-file", help="含 GITHUB_TOKEN 的文件")
        parser.add_argument("--allowlist", help="项目策略表路径（create 入册用）")
        parser.add_argument("--config", help="引擎配置路径（读归档命令）")

    p = sub.add_parser("verify", help="核 token：身份与各仓可见性")
    common(p)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("create", help="建单（supervisor）；默认挂起 stage:on-hold")
    common(p)
    p.add_argument("--title", required=True)
    p.add_argument("--body", default="")
    p.add_argument("--body-file")
    p.add_argument("--labels", default="")
    p.add_argument("--worker-name")
    p.add_argument("--open", action="store_true",
                   help="建单即开工（不挂 stage:on-hold）")
    p.set_defaults(func=cmd_create)

    p = sub.add_parser("open", help="开工：摘除 stage:on-hold（supervisor）")
    common(p)
    p.add_argument("--issue", type=int, required=True)
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("hold", help="挂起：加回 stage:on-hold（supervisor）")
    common(p)
    p.add_argument("--issue", type=int, required=True)
    p.set_defaults(func=cmd_hold)

    p = sub.add_parser("comment", help="写评论（全部角色，自动加前缀）")
    common(p)
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--body", default="")
    p.add_argument("--body-file")
    p.add_argument("--worker-name")
    # ISS-99 five-part title. All four of --do/--notify/--project-key are
    # required together: a title missing one of them says less than the legacy
    # marker it replaces, and the engine would have to guess.
    p.add_argument("--do", default="",
                   help=f"动作（白名单 {len(markers.NOTIFY_ACTIONS)} 个）："
                        f"{'、'.join(markers.NOTIFY_ACTIONS)}")
    p.add_argument("--notify", default="",
                   help="通知谁：'No Notify' / 'All Active' / 会话键名 / 逗号分隔")
    p.add_argument("--project-key", dest="project_key", default="",
                   help="gh_projects.json 的项目键（如 lwa、superagent）")
    p.add_argument("--cid", default="",
                   help="本条回应的评论 id（可选；裁决/已归档通常没有）")
    p.add_argument("--session-key", dest="session_key", default="",
                   help="本会话键名，写成正文末行 <!-- session: … -->")
    p.set_defaults(func=cmd_comment)

    p = sub.add_parser("edit", help="改 issue 标题／正文（supervisor）")
    common(p)
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--title", default=None, help="新标题")
    p.add_argument("--body", default=None, help="新正文（与 --body-file 二选一）")
    p.add_argument("--body-file", default=None)
    p.add_argument("--note", default="", help="审计行里「改了什么」，缺省「正文更新」")
    p.set_defaults(func=cmd_edit)

    p = sub.add_parser("label", help="改阶段 label（supervisor/pm）；缺失 label 自动建")
    common(p)
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--add", default="")
    p.add_argument("--remove", default="")
    p.add_argument("--no-create", dest="no_create", action="store_true",
                   help="不自动创建缺失的 label（GitHub 挂载时仍会代建）")
    p.add_argument("--color", default="ededed", help="自动建 label 的颜色")
    p.set_defaults(func=cmd_label)

    p = sub.add_parser("flow", help="选择 Issue 的 AT/普通流程（supervisor）")
    common(p)
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--flow", required=True, choices=["at", "standard"])
    p.set_defaults(func=cmd_flow)

    p = sub.add_parser("close", help="关单（supervisor，且必须有 [QA] 评论）")
    common(p)
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--retro", action="store_true",
                   help="追溯关闭（无 QA 结论时用，必须配 --reason）")
    p.add_argument("--reason", default="", help="追溯理由（≥10 字）")
    p.add_argument("--strict", action="store_true",
                   help="归档失败时非零退出（缺省：打印警告并保留登记）")
    p.set_defaults(func=cmd_close)

    p = sub.add_parser("projects", help="列已登记项目；--discover 扫名下可开 issue 的仓")
    common(p)
    p.add_argument("--discover", action="store_true")
    p.set_defaults(func=cmd_projects)

    p = sub.add_parser("labels-init", help="建标准 label 集（幂等）")
    common(p)
    p.set_defaults(func=cmd_labels_init)

    p = sub.add_parser("bind", help="登记本单的 QA/worker 会话（关单时归档）")
    common(p)
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--bind-role", required=True, choices=["qa", "worker", "at"])
    p.add_argument("--session", required=True)
    p.add_argument("--name", default="")
    p.add_argument("--allow-short-sid", action="store_true", dest="allow_short_sid",
                   help="逃生门：明知是残号也照登记（会打 WARN）")
    p.set_defaults(func=cmd_bind)

    p = sub.add_parser("show", help="看单")
    common(p)
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--chars", type=int, default=1200)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("list", help="列单")
    common(p)
    p.add_argument("--state", default="open")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_list)
    return ap


def main(argv=None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    argv, _state_dir = _apply_state_dir(raw)
    ap = _parser()
    args = ap.parse_args(argv)
    if getattr(args, "project", None) is None and args.cmd not in {"verify", "projects"}:
        ap.error("--project 必填")
    try:
        if args.cmd == "projects":
            # Listing an undeployed table is a fact to report, not a refusal:
            # the command sends no request and every other command still says
            # which file is missing.
            try:
                projects = _load_projects(getattr(args, "projects_file", None))
            except Denied as exc:
                print(f"[拒绝] {exc}", file=sys.stderr)
                return 0
            ctx = _Ctx(args)
            ctx.projects = projects
            return args.func(args, ctx)
        ctx = _Ctx(args)
        return args.func(args, ctx)
    except Denied as exc:
        print(f"[拒绝] {exc}", file=sys.stderr)
        return EXIT_DENIED
    except ledger_mod.SessionLedgerError as exc:
        print(f"[拒绝] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
