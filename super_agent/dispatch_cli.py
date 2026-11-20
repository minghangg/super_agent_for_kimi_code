#!/usr/bin/env python3
"""Dispatch a task to a local kimi-code session over the local REST API.

Reads the bearer token from $KIMI_CODE_HOME/server.token (default ~/.kimi-code;
never printed), creates a
session in the target cwd, configures its agent profile (model / system_prompt /
permission_mode), submits the prompt, polls until the turn ends, and prints the
final assistant answer.

The caller may override the provider, the model and the thinking effort
(--provider / --model / --effort). Without them the house default applies:
provider `codebuddy`, model `cb-deepseek-v4.1-flash`, effort = that model's
declared default_effort (user ruling 2026-10-15). Whatever is resolved is
validated against $KIMI_CODE_HOME/config.toml before anything is dispatched, and an
unknown value prints the available choices and exits non-zero.

The dispatcher also applies a local development-compute quota gate (issue #40)
before creating or resuming a session. The gate reads config/dev_quota.json via
super_agent/dev_quota.py, counts active state/quiet_cores/core-N.lock files, and refuses
the dispatch when the current quota is full. Refusals print the config path and
current values on stderr and exit non-zero; no session or network request is made.

API contract verified against a live server on 2026-10-15 (server_version 2.1.1).
See README.md for the full contract and the provenance of every step.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from . import agent_config as agentcfg
from . import charters

DEFAULT_PORTS = (58627, 58628)
TOKEN_ENV = "KIMI_CODE_SERVER_TOKEN"
CONFIG_ENV = "KIMI_CODE_CONFIG_FILE"
# 章程/手册根由 agent_config 的 charter.root 提供（#98）。本文件不拼检出路径；
# `_charter_root()` 是唯一读者，未配置时返回空串（`charter_for()` 据此返回 ""）。
# Which model names the session when --name is omitted. This is a housekeeping
# choice, not the work model, so it stays cheap and fixed. Set the env var to ""
# to fall back to the deterministic slug.
DEFAULT_NAMING_MODEL = os.environ.get("KIMI_CODE_NAMING_MODEL", "DeepSeek-new/deepseek-flash")

# Warm-up ping for a freshly created session. Its job is to complete one trivial
# turn so the session is "activated" before the real task is submitted. The
# wording must stay inert: an open-ended phrase (e.g. "/warmup") is treated as a
# real instruction and can make the agent read AGENTS.md and run project sync
# work for ~50s, polluting the session. This phrasing answers in ~1s with no
# tool calls. See README "激活".
WARMUP_PROMPT = (
    "Reply with exactly: ping. Do not call any tools. "
    "Do not read files. Do not run commands."
)


# 会话三契约（用户 2026-11-10）：新建会话的第一条消息改为「先读契约文件」，
# 内容＝思考用英文（ASD-STE100）／输出精准（≤30字一句）／写码前七级检查。
#
# **本包自带一份**（`rules/charters/session_contract.md`），所以 safkc 不依赖
# 任何外部仓就能建会话——换机后只要配置对，装好本包即可工作。部署若要换一版
# 契约，配 `charter.contract_file` 指过去；未配置时用自带的。
CONTRACT_REL = os.path.join("rules", "charters", "session_contract.md")


def _charter_root() -> str:
    """章程/手册根：`agent_config` 的 `charter.root`（未配置＝空串）。"""
    return agentcfg.charter_root()


def contract_file() -> str:
    """The session contract a fresh session must read.

    Order: ``charter.contract_file`` (explicit) → the copy shipped with this
    package. Never the workbench's: a public package that cannot create a
    session without a private checkout is not usable on another machine, and
    that is the whole point of the split (ISS-95).

    Returns ``""`` when neither exists, which the caller treats as "warm up
    with a bare ping" -- a degraded but working state, not a refusal. The
    contract shapes how a session writes, not whether it can start; a missing
    charter is the fatal one.
    """
    explicit = str(agentcfg.get("charter.contract_file") or "").strip()
    if explicit:
        path = os.path.expanduser(explicit)
        return path if os.path.isfile(path) else ""
    shipped = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), CONTRACT_REL)
    return shipped if os.path.isfile(shipped) else ""


# 会话名前缀 → 章程文件名。**内置表在 `agent_config.DEFAULT_CHARTER_MAP`**
# （ISS-95）。本文件此前自带一份 `GENERIC_CHARTER_MAP`，与 agent_config 的那份
# 是两条真源——改一处另一处不生效，且两处都含 `_analyst_or_at.md` 这种需要
# 双候选探测的特殊叶子名。现在只有一份，且叶子名就是真实文件名。
#
# 项目专属前缀（`charter.project_prefixes`）与私有章程文件名（`charter.map`）
# 只在私有配置里给；公开默认是空表，运行期由 `charter_map()` 合并到内置表之前。
_PROJECT_PREFIXES: tuple = ()
_EXTRA_CHARTER_MAP: tuple = ()


def set_charter_sources(project_prefixes=None, charter_map=None) -> None:
    """由 `agent_config` 注入项目专属前缀与私有章程文件名（#98 步 4）。"""
    global _PROJECT_PREFIXES, _EXTRA_CHARTER_MAP
    _PROJECT_PREFIXES = tuple((str(k), str(v)) for k, v in (project_prefixes or ()))
    _EXTRA_CHARTER_MAP = tuple((str(k), str(v)) for k, v in (charter_map or ()))


def charter_map() -> tuple:
    """生效的前缀表：项目专属前缀 → 私有章程 → 内置表。

    内置表在 ``agent_config.DEFAULT_CHARTER_MAP``（ISS-95）。此前本文件自带一份
    ``GENERIC_CHARTER_MAP``，与 ``agent_config`` 的那份是两条真源——改一处另一处
    不生效。现在只有一份。
    """
    return _PROJECT_PREFIXES + _EXTRA_CHARTER_MAP + agentcfg.charter_map()


def charter_for(name: str) -> str:
    """Baseline charter for a session name; ``""`` when the map has no hit.

    Kept for callers that only need the general charter. New code should use
    :func:`charter_files`, which also returns the deployment's supplement and
    raises when the baseline is missing.

    **This function still returns "" rather than raising.** That is deliberate
    for one caller: ``--fetch`` and other read-only paths must not fail because
    a session name was unusual. Session creation goes through
    :func:`charter_files` instead, where a missing baseline is fatal (ISS-95).
    """
    leaf = charters.leaf_for(name)
    if not leaf:
        return ""
    path = charters.baseline_root() / leaf
    return str(path) if path.is_file() else ""


def charter_files(name: str) -> tuple[str, str]:
    """``(baseline, supplement)`` for a session that is about to be created.

    Raises :class:`charters.CharterMissing` when the baseline is absent. The
    caller must turn that into a refusal, not a session with no charter.
    """
    return charters.charter_files(name)


def warmup_prompt(charter: str = "", name: str = "",
                  supplement: str = "") -> str:
    """First message of a freshly created session: read contract (+ charters).

    ``charter`` 非空时一并读——用户 2026-11-16 令：``wk-*`` 读 ``worker.md``，
    ``qa-*`` 读 ``qa.md``。两处都必须真读到，否则默认测试级别等规则不生效。

    ``supplement``（ISS-95）是本机私有补充，**有则一并读，无则不出现**。它与
    ``charter`` 是叠加关系，不是二选一：基线永远读，补充按部署情况读。

    ``name`` 非空时，把**本会话的键名**一并告知：引擎用正文末行
    ``<!-- session: <键名> -->`` 判定裁决归属，而键名是凭据，只有本会话知道
    自己是哪把。此前无处可查 ⇒ 裁决一律判 unverified ⇒ release 铃不响
    （#76／#78／#79／#91 四条 PM verdict 实证）。
    """
    try:
        files = []
        contract = contract_file()
        if contract and os.path.isfile(contract):
            files.append(contract)
        if charter and os.path.isfile(charter):
            files.append(charter)
        if supplement and os.path.isfile(supplement):
            files.append(supplement)
        if files:
            posix = [f.replace(os.sep, "/") for f in files]
            # 权限限定到「那一次 Read」（2026-11-11 修正：原句"Only read X"与
            # "Do not call any tools"互相矛盾——read 本身就是 tool 调用）。
            if len(posix) == 1:
                what = posix[0]
            elif len(posix) == 2:
                what = f"{posix[0]} and {posix[1]}"
            else:
                what = ", ".join(posix[:-1]) + " and " + posix[-1]
            text = (f"Read {what} with the Read tool. "
                    "Reply with exactly: Read done. "
                    "Do not use any other tool. Do not run any command.")
            if name:
                text += f" 你的会话键名：{name}（发 comment 时用 --session-key 带上）"
            return text
    except OSError:
        pass
    if name:
        return f"{WARMUP_PROMPT} 你的会话键名：{name}"
    return WARMUP_PROMPT


# 停手守卫（2026-10-27）：当初以为派活端无法中止服务端轮次（杀客户端不停、
# REST 无 abort）—— **2026-10-31 实测已推翻**：`POST /sessions/{id}/prompts/{pid}:abort`
# 23ms 返回 `{"aborted":true}`（HTTP 恒 200，要看响应体 code）。见
# `state/evidence/session_control_api_20261031.md`；日常用 `python -m super_agent.console abort <sid>`。
# 本守卫仍保留：它是**协作式**停手，能保住已读上下文并留下进度档，与硬 abort 互补。
# 2026-11-01 精简（用户要求）：supervisor 已能强中止（`sess.py abort`），细则不必每份任务书都塞，
# 一句话即可；细则原文仍在 .kimi/rules/worker_conduct.md §七。
# 任务书措辞闸（2026-11-01 用户重申：派单一律完全自动，不许开放式问询）。
# 命中即在派单时打警告：这类措辞会让 worker 中途停下来等人，比 bug 更贵。
ASK_PATTERNS = ("必要时询问", "必要时问询", "先问我", "等我确认", "等你确认",
                "授权后再", "需要你批准", "不确定就停")
# 允许出现的正当用法：明确上升项／生产不可逆写的授权边界（判据式写法）。


def project_env_pointer(cwd: str) -> str:
    """One pointer line for the project whose directory ``cwd`` names (#101).

    The project key is the **directory basename**, lower-cased -- the same
    shape as the ``gh_projects.json`` key for every directory that has a
    matching document. No project table is read: safkc has none, and the one in
    the workbench is a cross-repo dependency this package does not take.

    The directory itself comes from the one declared source,
    :func:`agent_config.project_env_dir`, the same value ``doctor --project``
    uses. This function only answers "is there a document for this key"; it
    does not build a second path rule. No document (or no configured
    directory) is a normal state -- a worker dispatched outside a registered
    project must not be blocked by a missing optional pointer.
    """
    try:
        key = Path(os.path.abspath(cwd)).name.strip().lower()
    except (OSError, ValueError):
        return ""
    if not key:
        return ""
    raw = str(agentcfg.project_env_dir() or "").strip()
    if not raw:
        return ""
    base = Path(raw)
    if not base.is_dir():
        return ""
    # Exact stem match, like ``doctor --project``: a near-miss is not a hit.
    exact = [p for p in base.glob("*.md") if p.stem == key]
    if len(exact) != 1:
        return ""
    return f"项目环境：{key} → {exact[0]}"


def lint_task_text(text: str) -> list[str]:
    return [p for p in ASK_PATTERNS if p in (text or "")]


# 已退役（2026-11-09 用户令）：dispatch 不再调用 stop_guard() 追加守卫句；
# 保留常量与函数仅供外部工具脚本自愿使用。细则退役说明见 worker_conduct.md §七。
STOP_GUARD = (
    "\n\n【停手】随时可自查 `test -f {marker} && echo STOP`；命中即停，把进度写进 {progress}，"
    "并在答复首段说明。细则见 worker_conduct.md §七。"
)


def stop_guard(marker: str, progress_dir: str) -> str:
    """Render the cooperative stop guard with absolute paths."""
    return STOP_GUARD.format(marker=marker, progress=progress_dir.rstrip("/\\") + "/PROGRESS_<你的名字>.md")

class DispatchError(RuntimeError):
    """Raised for any unrecoverable failure in the dispatch pipeline."""


#: A session was refused because its baseline charter is missing (ISS-95).
#:
#: Separate from the parameter-error code on purpose: the two point at different
#: repairs. A parameter error says "change your arguments"; this one says "the
#: charters are not installed", and a caller that sees 7 would go looking in the
#: wrong place. Same reasoning as #104's ctx-gate refusal, opposite direction.
EXIT_CHARTER_MISSING = 9


class NeedsPerson(Exception):
    """派单停在「必须有人答」的交互上（2.7 的 `question` 交互）。

    为什么不是 `DispatchError` 的子类：两者的处置方向相反。超时/失败是「这单
    没成」，人看一眼就能重派；`needs_person` 是「这单在等人答，答案会污染任务
    书」。混成一个异常，上层就只能把「在等人」当失败处理，于是又回到「超时即
    失败」的老病（ISS-103 P0-1.4）。

    退出码 3，与 `wait_session` 的既有 3 同一档语义（「结论不确定，别看 rc 判
    成败」）。
    """

    def __init__(self, detail: str, interaction_id: str = "", kind: str = "question") -> None:
        super().__init__(detail)
        self.detail = detail
        self.interaction_id = interaction_id
        self.kind = kind


def _dev_quota():
    """派单资源配额模块（本包复制件），延迟加载以免拖长 import 链。

    两处私有路径（配置与核锁目录）在加载后按 `agent_config` 的
    `quota.config_file` / `quota.cores_dir` 注入，未配置则保留模块内的仓内默认。
    """
    from . import dev_quota as module
    cfg_file = agentcfg.quota_config()
    if cfg_file:
        os.environ.setdefault(module.CONFIG_PATH_ENV, cfg_file)
    cores = agentcfg.quota_cores_dir()
    if cores:
        os.environ.setdefault(module.STATE_CORES_ENV, cores)
    return module


def quota_gate() -> None:
    """派单前配额闸（ISS-40）：在跑 heavy 数达到当前时段配额即拒单。

    配额与在跑数都来自 super_agent/dev_quota.py；拒绝时报错带配置路径与当前值，
    且在读 token／连 server／建会话之前发生，所以不会留下远端副作用。
    """
    dq = _dev_quota()
    config = dq.read_config()
    quota = dq.current_quota(config, write_back=False)
    running = dq.running_heavy()
    limit = int(quota["limit"])
    if running >= limit:
        raise DispatchError(dq.quota_error_text(quota, running))
    print(f"[dispatch] 配额检查通过：在跑 heavy={running}/{limit} "
          f"配置={quota['config_rel']}（{quota['config_path']}） pinned={quota['pinned_cores']} "
          f"offpeak={quota['offpeak_extra_cores']} window={quota['window']} "
          f"窗口内={quota['window_active']} heavy_concurrency={quota['heavy_concurrency']}",
          flush=True)


def _kimi_paths():
    """家目录解析源（本包 `paths`）；不假设 sys.path，也不改它。"""
    from . import paths as kimi_paths
    return kimi_paths


def _sess_events():
    """延迟导入本包 `sess_events`（同上：不假设 sys.path，也不拖 import 链）。"""
    from . import sess_events
    return sess_events


def _retired_sids():
    """延迟导入本包 `retired_sids`（ISS-29 退役清单）。"""
    from . import retired_sids
    return retired_sids


def record_created(sid: str, name: str, *, role: str = "") -> None:
    """新建会话后记一条 `session.created`（ISS-27）。

    事件写哪由 `sess_events.resolve_root()` 决定（`--state-dir` > `SESS_EVENTS_STATE_DIR`
    > 声明式根），本文件不另拼路径——否则自测会写穿生产事件流。

    会话名落在 `key` 字段：白名单里没有 `name`（只有 `key`），而「这个 sid 叫什么」
    正是回放时最需要的一条，所以复用 `key` 而不是为了它扩白名单。

    事件是**旁路记载**：写失败只告警，绝不回滚一个已经建好的远端会话。
    """
    fields = {"sid": sid}
    if name:
        fields["key"] = name
    if role:
        fields["role"] = role
    try:
        _sess_events().emit("session.created", **fields)
    except Exception as exc:  # noqa: BLE001  会话已建好，事件失败不该让派单失败
        print(f"[dispatch] WARNING: session.created 未落盘（{type(exc).__name__}: {exc}）",
              file=sys.stderr)


def token_path() -> str:
    """server.token 路径。解析统一走本包 `paths`（issue #7）。

    迁移到 E 盘只需设 `KIMI_CODE_HOME`，本文件不再自己拼 `~/.kimi-code`。
    """
    return str(_kimi_paths().token_file())


def read_token() -> str:
    """Read the bearer token from disk. The value must never be logged."""
    path = token_path()
    try:
        with open(path, encoding="utf-8-sig") as fh:
            token = fh.read().strip()
    except OSError as exc:
        if os.environ.get(TOKEN_ENV):
            return os.environ[TOKEN_ENV].strip()
        raise DispatchError(f"cannot read token file {path}: {exc}") from exc
    if not token:
        raise DispatchError(f"token file {path} is empty")
    return token


def _instance_record() -> dict | None:
    """Live kimi-code server instance record (``port`` + ``host``), or None.

    Kimi Code Desktop binds a **random** port on each start (38370, then 4869,
    observed 2026-11-17) and records it in
    ``~/.kimi-code/server/instances/<id>.json``. ``DEFAULT_PORTS`` alone only
    works for the CLI server. ``super_agent.paths.latest_instance`` already parses
    that file; reuse it instead of duplicating the logic.

    The ``host`` from the file is kept, not replaced by ``127.0.0.1``: the same
    process tree must agree with ``paths.server_base()``, which honours it.
    """
    try:
        from . import paths as kimi_paths
        inst = kimi_paths.latest_instance()
    except Exception:                       # noqa: BLE001 - absence is normal
        return None
    if not isinstance(inst, dict):
        return None
    port = inst.get("port")
    if not (isinstance(port, int) and 1 <= port <= 65535):
        return None
    host = inst.get("host")
    return {"host": host if isinstance(host, str) and host.strip() else "127.0.0.1",
            "port": port}


def _parse_env_port(raw: str) -> tuple[str, int] | None:
    """``KIMI_CODE_SERVER_PORT`` → ``(host, port)``; unparsable returns None.

    Two accepted shapes: a bare port number (``15430``) and a full base URL
    (``http://127.0.0.1:15430``). The URL form keeps its host, so a non-local
    server can be named here. A URL without a port has nothing to connect to and
    is rejected the same way as garbage.
    """
    text = (raw or "").strip()
    if not text:
        return None
    if text.isdigit():
        port = int(text)
        return ("127.0.0.1", port) if 1 <= port <= 65535 else None
    parts = urlsplit(text)
    if parts.scheme in ("http", "https") and parts.hostname:
        port = parts.port
        if port is not None and 1 <= port <= 65535:
            return (parts.hostname, port)
    return None


def _candidates(explicit: int | None):
    """Yield ``(base, warn)`` in the frozen order (ISS-100 D1).

    Priority is fixed and must not be reshuffled:

    1. ``--port`` (local only; a remote host goes through the env URL form)
    2. ``KIMI_CODE_SERVER_PORT`` — bare port or full base URL
    3. ``KIMI_SERVER`` — parsed by ``paths.env_base()``, never re-implemented here
    4. ``latest_instance()`` — host taken from the instance file
    5. ``DEFAULT_PORTS`` — with a stderr warning on **first use**

    When both env vars are set, ``KIMI_CODE_SERVER_PORT`` wins: it is the
    dispatch-specific knob, ``KIMI_SERVER`` is the shared client base.

    ``warn`` is a message string for the default-port fallback and ``None``
    otherwise. It is **not** printed here: the caller prints it when it actually
    probes a default port, so listing the candidates never produces a warning.
    """
    seen: set[str] = set()

    def _fresh(base: str) -> bool:
        if base in seen:
            return False
        seen.add(base)
        return True

    if explicit:
        base = f"http://127.0.0.1:{explicit}"
        if _fresh(base):
            yield base, None
        return

    env_port = (os.environ.get("KIMI_CODE_SERVER_PORT") or "").strip()
    if env_port:
        parsed = _parse_env_port(env_port)
        if parsed is None:
            print(f"[dispatch] 警告：KIMI_CODE_SERVER_PORT 无法解析"
                  f"（{env_port[:60]}），已忽略", file=sys.stderr)
        else:
            base = f"http://{parsed[0]}:{parsed[1]}"
            if _fresh(base):
                yield base, None

    try:
        from . import paths as kimi_paths
        env_base = kimi_paths.env_base()
    except Exception:                       # noqa: BLE001 - absence is normal
        env_base = None
    if env_base:
        base = env_base.rstrip("/")
        if _fresh(base):
            yield base, None

    inst = _instance_record()               # Desktop: random port, discover it
    inst_base = ""
    if inst is not None:
        inst_base = f"http://{inst['host']}:{inst['port']}"
        if _fresh(inst_base):
            yield inst_base, None

    defaults = [f"http://127.0.0.1:{p}" for p in DEFAULT_PORTS]
    if not any(b not in seen for b in defaults):
        return
    ports = "/".join(str(p) for p in DEFAULT_PORTS)
    if inst_base:
        warn = f"[dispatch] 警告：实例端口 {inst['port']} 未响应，回落探测 {ports}"
    else:
        warn = f"[dispatch] 警告：实例文件不可用，回落探测 {ports}"
    for base in defaults:
        if _fresh(base):
            yield base, warn


def candidate_bases(explicit: int | None) -> list[str]:
    """Base URLs to try, in the frozen priority order (see ``_candidates``)."""
    return [base for base, _warn in _candidates(explicit)]


def discover_base(token: str, explicit_port: int | None) -> str:
    """Find the live kimi-code server.

    A candidate is accepted only when GET /api/v1/meta returns code 0 under our
    bearer token. That distinguishes the kimi server from an unrelated service
    that happens to hold the port (e.g. 8787 answers 302 to anything).
    """
    tried: list[str] = []
    warned = False
    for base, warn in _candidates(explicit_port):
        if warn and not warned:
            print(warn, file=sys.stderr)
            warned = True
        parts = urlsplit(base)
        host = parts.hostname or "127.0.0.1"
        port = parts.port
        if port is None or not port_open(port, host):
            tried.append(f"{base} (closed)")
            continue
        try:
            payload = http_get(f"{base}/api/v1/meta", token, timeout=5)
        except Exception as exc:  # noqa: BLE001 - any failure means "not our server"
            tried.append(f"{base} ({type(exc).__name__})")
            continue
        if isinstance(payload, dict) and payload.get("code") == 0:
            data = payload.get("data") or {}
            print(f"[dispatch] server {base} version={data.get('server_version')}")
            return base
        tried.append(f"{base} (no meta envelope)")
    raise DispatchError(
        "实例文件不可用且默认端口均 closed，kimi-code server 未运行？\n"
        "no live kimi-code server found. Start one with `kimi web`.\n  tried: "
        + ", ".join(tried)
    )


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex((host, port)) == 0


def _request(method: str, url: str, token: str, body: dict | None, timeout: float) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw)
        except ValueError:
            raise DispatchError(f"{method} {url} -> HTTP {exc.code}: {raw[:300]!r}") from exc
        raise DispatchError(
            f"{method} {url} -> HTTP {exc.code}: {payload.get('msg')} "
            f"{json.dumps(payload.get('details'), ensure_ascii=False)}"
        ) from exc
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise DispatchError(f"{method} {url} -> non-JSON response: {raw[:300]!r}") from exc


def http_get(url: str, token: str, timeout: float = 30) -> dict:
    return _request("GET", url, token, None, timeout)


def http_post(url: str, token: str, body: dict, timeout: float = 60) -> dict:
    return _request("POST", url, token, body, timeout)


def unwrap(payload: dict, what: str) -> dict:
    if payload.get("code") != 0:
        raise DispatchError(f"{what} failed: {payload.get('msg')} {payload.get('details')}")
    return payload.get("data") or {}


def slugify(text: str, limit: int = 40) -> str:
    text = text.strip().splitlines()[0] if text.strip() else "task"
    text = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", text).strip("-")
    return (text[:limit] or "task").lower()


def create_session(base: str, token: str, cwd: str, title: str) -> str:
    body = {"metadata": {"cwd": cwd}}
    if title:
        body["title"] = title
    data = unwrap(http_post(f"{base}/api/v1/sessions", token, body), "create session")
    return data["id"]


def record_restored(sid: str) -> None:
    """隐式 restore 之后补记 `session.restored`（ISS-76 P1-2）。

    事件写哪同 `record_created`（`sess_events.resolve_root()` 三级解析）。

    为什么必须写：`reuse_session` 的 restore 是**隐式的**——调用方没有执行过
    `sess.py restore`。此前这里只 `print` 一行，事件流里看不到它，于是服务端
    随后 40401 时，本地旁证 `_archived_from_events` 仍读到最后一条
    `session.archived`，把一个正在使用的会话判成已归档并拒投。这条事件让事件流
    与真实状态对齐。

    事件仍是**旁路记载**：写失败只告警，不回滚已经恢复好的远端会话。
    """
    try:
        _sess_events().emit("session.restored", sid=sid)
    except Exception as exc:  # noqa: BLE001  会话已恢复，事件失败不该让派单失败
        print(f"[dispatch] WARNING: session.restored 未落盘（{type(exc).__name__}: {exc}）",
              file=sys.stderr)


def _sess_verdict(st: int, payload: dict) -> tuple[bool, str]:
    """受理判据走 `console._request_verdict` 的**同一份实现**（ISS-76 P1-2）。

    为什么不在这里抄一遍：v2 批量端点外层 `code` 恒为 0，逐条结果在
    `data.results`。这套判据已经踩过一次坑（console 曾据此写出假的
    `session.restored`），两个副本迟早分叉，分叉的方向就是「一边记假事件」。
    复用＝只有一处需要维护。

    `http_post` 在 4xx/5xx 抛 `DispatchError`，所以走到这里的状态恒为 200；
    仍把它传进去，是因为 HTTP 层判据（`st >= 400` 一律拒）属于同一份契约。
    """
    from .console import _request_verdict   # noqa: PLC0415  只在该分支需要
    return _request_verdict(st, payload)


def reuse_session(base: str, token: str, sid: str) -> str:
    """Point the dispatch at an existing session and return its title.

    An archived session is restored first: the prompts endpoint rejects a turn on
    an archived session, and the failure surfaces only as a missing answer, so the
    restore is done here rather than left to the caller.

    ISS-76 P1-2: the restore is implicit, so it must also write the same
    ``session.restored`` event an explicit ``sess.py restore`` writes. Without it
    the local side-evidence keeps reading the old ``session.archived`` row and
    refuses a session that is actually in use. A rejected restore writes nothing.
    """
    info = unwrap(http_get(f"{base}/api/v1/sessions/{sid}", token), "get session")
    if info.get("archived"):
        payload = http_post(f"{base}/api/v1/sessions:restore", token, {"ids": [sid]})
        ok, why = _sess_verdict(200, payload)
        print(f"[dispatch] restored archived session {sid}（{why}）")
        if ok:
            record_restored(sid)
        else:
            print(f"[dispatch] WARNING: restore 未受理，未记 session.restored（{why}）",
                  file=sys.stderr)
    # 2026-10-31：会话在忙时派第二个 turn 会排队/串味。这里**只警告不阻断**
    # （有些调用方确实要排队），要硬打断用 `python -m super_agent.console abort <sid>`。
    # 2.7 没有 `busy` 键（读完恒 None ⇒ 告警永不触发），真源是 `activity` 三态：
    # `turn` 才会与本单排队；`background` 只是后台任务在跑，主轮空着。
    act = info.get("activity")
    if act == "turn":
        print(f"[dispatch] WARNING: session {sid} 正有 turn 在跑（activity=turn）；"
              f"本单会排队。要打断请先 python -m super_agent.console abort {sid}")
    elif act == "background":
        print(f"[dispatch] WARNING: session {sid} 有后台任务在跑（activity=background）；"
              f"主轮空闲，本单可直接开始")
    return str(info.get("title") or sid)


# --- config catalog: read $KIMI_CODE_HOME/config.toml (never the live server) ----
#
# The server's own GET /api/v1/config is NOT used for validation: it caches the
# config it booted with, so a provider added afterwards (e.g. cdy2Api) is live
# for new sessions but absent from that response. The on-disk file is the source
# of truth the server itself reads. api_key values are dropped on load.


def config_path() -> str:
    """config.toml 路径。同上，统一走本包 `paths`。"""
    return str(_kimi_paths().config_file())


def _toml_load(path: str) -> dict:
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        import tomllib  # Python >= 3.11
    except ModuleNotFoundError:  # pragma: no cover - 3.10 fallback
        try:
            import tomli as parser  # type: ignore[no-redef]
        except ModuleNotFoundError:
            try:
                import toml as parser  # type: ignore[no-redef]
            except ModuleNotFoundError as exc:
                raise DispatchError(
                    f"cannot parse {path}: no TOML reader available. Use Python >= 3.11, "
                    f"`pip install tomli`, or point {CONFIG_ENV} at a JSON export."
                ) from exc
        try:
            return parser.loads(raw.decode("utf-8-sig"))  # type: ignore[no-any-return]
        except Exception as exc:  # noqa: BLE001
            raise DispatchError(f"cannot parse {path}: {exc}") from exc
    return tomllib.loads(raw.decode("utf-8-sig"))


class Catalog:
    """Providers and their models, as declared in the kimi-code config file."""

    def __init__(self, providers: dict[str, list[str]], models: dict[str, dict]) -> None:
        self.providers = providers  # provider name -> [model alias, ...]
        self.models = models  # alias -> {provider, support_efforts, default_effort}

    @property
    def aliases(self) -> list[str]:
        return sorted(self.models)

    def provider_of(self, alias: str) -> str:
        return self.models.get(alias, {}).get("provider", "")

    def efforts_of(self, alias: str) -> list[str]:
        return self.models.get(alias, {}).get("support_efforts", [])

    def default_effort_of(self, alias: str) -> str:
        return self.models.get(alias, {}).get("default_effort", "")

    def render(self) -> str:
        lines: list[str] = []
        for name in sorted(self.providers):
            lines.append(f"  {name}")
            for alias in self.providers[name]:
                efforts = self.efforts_of(alias)
                if efforts:
                    shown = " / ".join(efforts)
                    default = self.default_effort_of(alias)
                    if default:
                        shown += f"   (default: {default})"
                    lines.append(f"    --model {alias}    --effort {shown}")
                else:
                    lines.append(
                        f"    --model {alias}    (no support_efforts declared: "
                        "no thinking effort can be requested)"
                    )
            lines.append("")
        if lines:
            lines.pop()
        return "\n".join(lines)


def _ctx_gate(sid: str, *, force: bool = False, operator: str = "") -> int:
    """派单前的 ctx 门禁（issue #12）——子进程跑本包 `ctx_gate`。

    为什么用子进程而不是 import：`ctx_gate` 依赖 `ctx`→`bus`→`direct`，
    那条链会 import websockets；而本模块是**最底层的派单器**，不该被它拖一起
    （且同名遮蔽标准库 queue 的坑只在某些 import 顺序下炸）。
    子进程还顺带保证「门禁挂了不等于派单挂了」——见下面的 except 分支。

    退出码语义：0 放行 / 4 拒-60 / 5 拒-75 / 6 硬顶 / 7 参数错。
    **门禁自身异常 ⇒ 放行（返回 0）但打印告警**：不能因为门禁坏了就掐断所有派单。
    """
    import subprocess
    cmd = [sys.executable, "-m", "super_agent.ctx_gate", "--id", sid, "--role", "pm"]
    if force:
        cmd += ["--force", "--operator", operator or "unknown"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception as exc:  # noqa: BLE001
        print(f"[dispatch] ⚠ ctx 门禁调用失败（{type(exc).__name__}: {exc}），本次放行未检查",
              file=sys.stderr)
        return 0
    err = (r.stderr or "").strip()
    if err:
        print(err, file=sys.stderr)
    return r.returncode


def load_catalog(path: str | None = None) -> Catalog:
    path = path or config_path()
    if not os.path.isfile(path):
        raise DispatchError(
            f"config file not found: {path}. Set {CONFIG_ENV} to point at another one."
        )
    doc = _toml_load(path)
    raw_providers = doc.get("providers") or {}
    raw_models = doc.get("models") or {}
    models: dict[str, dict] = {}
    for alias, entry in raw_models.items():
        if not isinstance(entry, dict):
            continue
        models[alias] = {
            "provider": str(entry.get("provider") or alias.split("/")[0]),
            "support_efforts": [str(e) for e in (entry.get("support_efforts") or [])],
            "default_effort": str(entry.get("default_effort") or ""),
        }
    providers: dict[str, list[str]] = {name: [] for name in raw_providers}
    for alias in sorted(models):
        entry = models[alias]
        provider = entry["provider"]
        # Prefix-only aliases (e.g. `codebuddy/deepseek-v4.1-flash`, whose
        # provider is `cdy2Api`) are filed under their declared provider, which
        # is also the only provider field the server actually persists.
        providers.setdefault(provider, []).append(alias)
    return Catalog(providers, models)


def catalog_choices(catalog: Catalog) -> str:
    return (
        f"providers ({len(catalog.providers)}):\n  "
        + "\n  ".join(sorted(catalog.providers))
        + f"\n\nprovider -> models ({len(catalog.models)}):\n"
        + catalog.render()
    )


def provider_error(catalog: Catalog, provider: str) -> DispatchError:
    known = sorted(catalog.providers)
    near = [p for p in known if p.lower() == provider.lower()]
    hint = f" Did you mean `{near[0]}` (case matters)?" if near else ""
    return DispatchError(
        f"unknown --provider `{provider}`.{hint}\n\n{catalog_choices(catalog)}"
    )


def model_error(catalog: Catalog, provider: str, model: str) -> DispatchError:
    tail = f"/{model}"
    near = [a for a in catalog.aliases if a.lower() == model.lower()]
    near += [a for a in catalog.aliases if a.lower().endswith(tail.lower()) and a not in near]
    hint = f" Did you mean `{near[0]}`?" if near else ""
    return DispatchError(
        f"unknown --model `{model}` for provider `{provider}`.{hint}\n\n"
        f"models declared for `{provider}`:\n  "
        + "\n  ".join(catalog.providers.get(provider) or ["(none declared)"])
        + f"\n\nall choices:\n{catalog_choices(catalog)}"
    )


def effort_error(catalog: Catalog, model: str, effort: str) -> DispatchError:
    efforts = catalog.efforts_of(model)
    if not efforts:
        detail = (
            f"model `{model}` declares no support_efforts in the config, so no thinking "
            "effort can be requested for it. Pick a model that declares some."
        )
    else:
        detail = (
            f"`{effort}` is not in `{model}`'s support_efforts.\n  --effort "
            + " / ".join(efforts)
            + (f"   (default: {catalog.default_effort_of(model)})" if catalog.default_effort_of(model) else "")
        )
    return DispatchError(f"unsupported --effort.{chr(10)}  {detail}")


def resolve_choice(
    args: argparse.Namespace, parser: argparse.ArgumentParser, catalog: Catalog
) -> tuple[str, str, str]:
    """Resolve --provider / --model / --effort against the config catalog.

    House default (user ruling 2026-10-15): provider `codebuddy`, model
    `cb-deepseek-v4.1-flash`, effort = that model's declared default_effort. Any
    of the three can be overridden on the command line; the resolved triple is
    always printed, so the caller can see which model actually ran.

    A `--model` that does not exist, or that belongs to another provider, still
    fails loudly and prints the choices — a wrong pair must never run silently on
    whichever provider the alias happens to map to.
    """
    provider, model, effort = args.provider, args.model, args.effort
    if not provider:
        provider = "codebuddy"
    if not model:
        model = "cb-deepseek-v4.1-flash"
    if provider not in catalog.providers:
        raise provider_error(catalog, provider)
    if model not in catalog.models:
        raise model_error(catalog, provider, model)
    if catalog.provider_of(model) != provider:
        raise DispatchError(
            f"--model `{model}` belongs to provider `{catalog.provider_of(model)}`, "
            f"not `{provider}`.\n\nmodels declared for `{provider}`:\n  "
            + "\n  ".join(catalog.providers.get(provider) or ["(none declared)"])
        )
    if not effort:
        effort = catalog.default_effort_of(model)
    if effort not in catalog.efforts_of(model):
        raise effort_error(catalog, model, effort)
    return provider, model, effort


def apply_profile(base: str, token: str, sid: str, agent_config: dict) -> dict:
    """Model / system_prompt / permission_mode only take effect here.

    Passing agent_config to POST /api/v1/sessions is accepted but silently
    ignored (verified 2026-10-15): the turn then dies with
    [model.not_configured] Model not set.
    """
    data = unwrap(
        http_post(f"{base}/api/v1/sessions/{sid}/profile", token, {"agent_config": agent_config}),
        "update profile",
    )
    return data.get("agent_config", {})


def submit_prompt(base: str, token: str, sid: str, text: str,
                  prompt_id: str = "") -> tuple[str, str]:
    """投一轮消息。返回 `(prompt_id, status)`；status ∈ running|queued|blocked|failed。

    `prompt_id` 是本单的幂等键（KAP §3.C.2）：同一 `prompt_id` 重投，服务端回
    `40927 conflict` 而不产生第二轮。`40927` 说明「这一轮已经投过了」，调用方
    应当直接进 `:wait`，**不许**当成失败重投——重投正是重复跑一轮的来源。

    `status=queued` 是**合法**回执（会话正忙时新轮排队），它不是「已开始」，
    调用方必须走 `await_turn_start` 等到 `ordinal` 增长（ISS-103 P0-5）。
    """
    body: dict = {"content": [{"type": "text", "text": text}]}
    if prompt_id:
        body["prompt_id"] = prompt_id
    payload = http_post(f"{base}/api/v1/sessions/{sid}/prompts", token, body)
    if payload.get("code") == 40927:
        return prompt_id, "running"          # 已投过：幂等命中，按「在跑」处理
    data = unwrap(payload, "submit prompt")
    st = str(data.get("status") or "")
    return str(data.get("prompt_id") or ""), st


def prompt_ref(issue: str, round_no: int, text: str) -> str:
    """幂等键 `{单号}-{轮次}-{内容哈希前8}`（KAP §3.C.2 的 `prompt_id`）。"""
    digest = hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:8]
    return f"{issue or 'adhoc'}-{int(round_no)}-{digest}"


def current_ordinal(base: str, token: str, sid: str) -> int:
    """提交**之前**取基线序号。空会话（还没有 turn）返回 -1。

    必须在 `submit_prompt` 之前调用：提交后再取，很可能已经拿到新轮的序号，
    栅栏于是立刻放行，等于没有栅栏。
    """
    d = unwrap(http_get(f"{base}/api/v1/sessions/{sid}/agents/main/turns?page_size=1",
                        token), "read turns")
    turns = d.get("turns") or []
    if not turns:
        return -1
    return int(turns[0].get("ordinal") or -1)


def await_turn_start(base: str, token: str, sid: str, prev_ordinal: int,
                     expect_prompt_id: str = "", deadline_s: float = 60.0) -> int:
    """提交后确认**新**一轮真的开始，返回新 turn 的 ordinal。

    唯一判据是 `ordinal > prev_ordinal`（ISS-103 P0-5）。**不许**加旁路：

      · `turn.status == "running"` —— 对正在跑的会话派单时，最新 turn 本就是
        上一轮且 `status=running`，第一条就放行，于是 `:wait` 在上一轮结束时
        收尾，返回上一轮的答案（看着像成功，比超时更坏）；
      · `session.activity == "turn"` —— 同理，与「新轮是否排上队」无关。

    `status=queued` 时本函数会一直等到该轮真正落地，这正是它存在的意义。
    """
    until = time.time() + deadline_s
    while time.time() < until:
        d = unwrap(http_get(f"{base}/api/v1/sessions/{sid}/agents/main/turns?page_size=2",
                            token), "read turns")
        for t in (d.get("turns") or []):
            if int(t.get("ordinal") or -1) > prev_ordinal:
                return int(t["ordinal"])
        time.sleep(0.5)
    raise DispatchError(
        f"round did not start within {deadline_s:.0f}s (session {sid}); "
        f"prev_ordinal={prev_ordinal} prompt_id={expect_prompt_id!r}"
        f" —— 派单未生效（不是超时），请查服务端 status/queued 状态")


def _decision_for(permission_mode: str) -> str:
    """`permission-mode` → approval 决策。**未知取值一律 rejected**（fail closed）。

    全仓没有 `PERMISSION_MODE` 这个全局符号，实有的是 `main()` 的
    `args.permission_mode`；在这里读全局会在实现时直接 `NameError`，异常被兜底
    吃掉后每条 approval 都落 `rejected` —— 静默错答比卡住更难查。
    """
    m = (permission_mode or "").strip().lower()
    if m in ("auto", "yolo"):
        return "approved"
    return "rejected"


def _auto_answer(base: str, token: str, sid: str, item: dict,
                 permission_mode: str) -> dict:
    """按 kind 应答一条待批交互。返回决策，供调用方记账。

    `question` 不猜：记 `NeedsPerson` 并停。猜测会污染任务书，而 dismiss 会让
    会话带着错的假设继续产出交付物，两者都比停下更贵。
    """
    iid = str(item.get("interaction_id") or "")
    kind = str(item.get("kind") or "approval")
    if kind != "approval":
        raise NeedsPerson(
            f"pending {kind} {iid}: {(item.get('request') or {}).get('questions') or []}",
            interaction_id=iid, kind=kind)
    decision = _decision_for(permission_mode)
    body = {"kind": "approval", "decision": decision}
    payload = http_post(f"{base}/api/v1/sessions/{sid}/interactions/{iid}",
                        token, body, timeout=30)
    code = payload.get("code")
    if code in (40404, 41001):
        # 交互已被别处处理／已过期：**不算派单失败**，回到 `:wait` 即可。
        return {"interaction_id": iid, "kind": kind, "decision": decision,
                "outcome": None, "code": code, "note": "interaction gone"}
    data = unwrap(payload, "answer interaction")
    return {"interaction_id": iid, "kind": kind, "decision": decision,
            "outcome": data.get("outcome"), "code": code}


def wait_rounds(base: str, token: str, sid: str, timeout: float, prompt_id: str = "",
                permission_mode: str = "", on_interaction=None) -> dict:
    """KAP §3.C.3 的 `:wait` 循环。返回 `{outcome, timed_out, interactions_seen}`。

    判据只有两条：`timed_out is False` 且 `activity != "turn"`。用
    `activity != "idle"` 会让 background 任务永远拦着收尾（KAP 逐字要求 `!== "turn"`）。

    `pending_interactions` 非空时**当场应答**（ISS-103 P0-1.4）：这是「修好 busy
    读法也仍然超时」的另一半——遇 approval/question 无人应答，`activity` 会一直
    停在 `turn`，`timed_out` 恒真，一路卡到 timeout。
    """
    deadline = time.time() + timeout
    seen: list = []
    while True:
        budget = max(0, min(15000, int((deadline - time.time()) * 1000)))
        if budget <= 0:
            raise DispatchError(f"timed out after {timeout:.0f}s waiting for session {sid}"
                                f"（仍在 turn，非交互阻塞）")
        data = unwrap(http_post(f"{base}/api/v1/sessions/{sid}:wait", token,
                                {"timeout_ms": budget}, timeout=budget / 1000 + 20),
                      "session wait")
        ses = data.get("session") or {}
        pending = data.get("pending_interactions") or []
        if pending:
            seen.extend(pending)
            for it in pending:
                if on_interaction is not None:
                    on_interaction(it)
                else:
                    _auto_answer(base, token, sid, it, permission_mode)
            continue                          # 应答完立刻回 `:wait`，不干等
        if bool(data.get("timed_out")) is False and ses.get("activity") != "turn":
            return {"outcome": ses.get("last_turn_outcome"), "timed_out": False,
                    "activity": ses.get("activity"), "interactions_seen": seen}


def run_turn(base: str, token: str, sid: str, text: str, timeout: float,
             permission_mode: str = "", quiet: bool = False,
             prompt_id: str = "") -> str:
    """投一轮并等到它结束，返回该轮的 assistant 回答。

    顺序是 `ordinal 基线 → 提交 → 栅栏 → :wait 循环 → 读 turns`（方案 §5.1）。
    基线必须在提交前取，否则栅栏形同虚设——所以这里**不提供**覆盖基线的参数：
    能传一个假基线，就等于能把 P0-5 的栅栏整个绕过。
    """
    prev = current_ordinal(base, token, sid)
    pid, status = submit_prompt(base, token, sid, text, prompt_id)
    if status in ("blocked", "failed"):
        raise DispatchError(f"prompt not accepted: status={status!r} (session {sid})")
    new_ord = await_turn_start(base, token, sid, prev, expect_prompt_id=pid,
                               deadline_s=min(120.0, max(30.0, timeout)))
    res = wait_rounds(base, token, sid, timeout, prompt_id=pid,
                      permission_mode=permission_mode)
    if not quiet:
        print(f"[dispatch] turn ended ordinal={new_ord} "
              f"outcome={res.get('outcome') or 'unknown'} "
              f"activity={res.get('activity') or '?'} "
              f"interactions={len(res.get('interactions_seen') or [])}")
    if res.get("outcome") == "failed":
        raise DispatchError(f"turn ended with outcome=failed (session {sid})")
    # `interrupted` 是本轮被 cancel/steer 收尾，属正常可交付（旧代的 cancelled 同理）。
    return fetch_final_answer(base, token, sid)


def fetch_final_answer(base: str, token: str, sid: str) -> str:
    """最后一轮里最后一条 `assistant_message` 的正文。取不到返回空串（不是 404）。

    两层顺序（2026-11-20 对着现网 2.7.0 实测复核，见下）：
      · `turns` 是**旧→新**（openapi 原文「oldest first: the latest page…」；
        实测 5 个会话 × page_size 1/2/3/5，ordinal 全升序：page_size=5 给
        `[40,41,42,43,44]`、=1 给 `[44]`）⇒ 要 `reversed`，第一条命中的才是
        最新一轮。按原序取会返回**最早**一轮（实测 ordinal 39 vs 最新 43），
        `--fetch` 于是恒读旧答案——比超时更坏，因为它看起来成功。
      · `items` 也是**旧→新**（同一轮里 assistant_message 按发生顺序排列）⇒
        同样要 `reversed`，取到的是该轮里最后一条。

    ⚠ 本函数上一版曾按「`turns` 新→旧」把外层 `reversed` 去掉，那是错的
    （ISS-103 P0-A：QA 实测 + 本轮复核）。别再依据方案 §1.3 那句「默认新→旧」
    改回去——那句记的是**页**是最新一页（`page_size=1` 只给最新一条），不是
    页内顺序。
    """
    d = unwrap(http_get(f"{base}/api/v1/sessions/{sid}/agents/main/turns?page_size=5",
                        token), "fetch turns")
    for turn in reversed(d.get("turns") or []):          # 旧→新 ⇒ 反着取，最新一轮在前
        for item in reversed(turn.get("items") or []):   # 旧→新 ⇒ 反着取，最后一条在前
            if not isinstance(item, dict) or item.get("type") != "assistant_message":
                continue
            text = str(item.get("text") or "").strip()
            if text:
                return text
    return ""


def pending_interactions(base: str, token: str, sid: str) -> list[dict]:
    """待批交互（2.7 的 `/interactions` 一个端点，旧代 `/approvals`+`/questions` 已合并）。"""
    d = unwrap(http_get(f"{base}/api/v1/sessions/{sid}/interactions", token),
               "list interactions")
    return [{"interaction_id": it.get("interaction_id"), "kind": it.get("kind"),
             "request": it.get("request") or {}}
            for it in (d.get("items") or []) if isinstance(it, dict)]


def check_pending_interactions(base: str, token: str, sid: str) -> list[str]:
    """还剩哪些待批交互（只报 kind，供收尾时提示）。空列表表示已清。"""
    return [str(it.get("kind") or "unknown")
            for it in pending_interactions(base, token, sid)]


def model_self_name(base: str, token: str, model: str, task: str, cwd: str) -> str:
    """Ask a (cheap) model to name the session. Falls back to a slug on any error."""
    instruction = (
        "Name the following agent task session. Reply with ONLY the name: "
        "2-4 lowercase words joined by hyphens, ASCII letters and digits only, "
        "no quotes, no punctuation, at most 40 characters.\n\n"
        f"Task: {task}"
    )
    try:
        sid = create_session(base, token, cwd, title="")
        apply_profile(base, token, sid, {"model": model, "permission_mode": "yolo"})
        # 这个会话是用 `permission_mode="yolo"` 建的（上一行），所以应答走 yolo：
        # 不许在这里读 argv，命名会话与调用方的 permission-mode 无关。
        text = run_turn(base, token, sid, instruction, timeout=90, quiet=True,
                        permission_mode="yolo")
        name = slugify(text.splitlines()[0] if text else "", limit=40)
        if name and re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name):
            print(f"[dispatch] named by {model}: {name}")
            return name
    except (DispatchError, NeedsPerson) as exc:
        print(f"[dispatch] naming model unavailable ({exc}); using slug")
    fallback = slugify(task)
    print(f"[dispatch] named by slug: {fallback}")
    return fallback


def activate_session(base: str, token: str, sid: str, timeout: float,
                     charter: str = "", name: str = "",
                     supplement: str = "") -> bool:
    """Run one trivial turn so a fresh session is activated before real work.

    Harmless when the session is already warm: it is just another no-op turn, and
    the real task's answer is read as the newest assistant message, so the ping
    reply can never be mistaken for the result.

    ``name`` is passed so the first message can tell the session its own key
    name: a verdict carries that key, and nothing else in the conversation
    knows which key this session holds.

    ``supplement`` (ISS-95) is the deployment's private charter addition; it is
    read **in addition to** ``charter``, never instead of it.
    """
    run_turn(base, token, sid, warmup_prompt(charter, name, supplement),
             timeout=min(timeout, 120), quiet=True, permission_mode="yolo")
    print("[dispatch] activated (warm-up turn completed)")
    return True


def guard_token(token: str, text: str) -> None:
    if token and token in text:
        raise DispatchError("refusing to emit output: the bearer token value appeared in it")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Dispatch a task to a local kimi-code session and print its answer. "
            "Defaults to provider `codebuddy` with model `cb-deepseek-v4.1-flash` at "
            "that model's default_effort; override any of the three on the command line. "
            "The resolved choice is always printed."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python dispatch.py --task \"1+1=?\"            # house default: codebuddy + cb-deepseek-v4.1-flash\n"
            "  python dispatch.py --task \"1+1=?\" --provider DeepSeek-new \\\n"
            "      --model DeepSeek-new/deepseek-flash --effort high        # explicit override\n"
            "  python dispatch.py --task \"rebuild it\" --session session_15707049-…   # resume a session\n"
        ),
    )
    parser.add_argument("--task", required=False, default="", help="the task text to send")
    parser.add_argument("--cwd", default=os.getcwd(), help="workspace dir for the session")
    parser.add_argument(
        "--state-dir", default=agentcfg.state_dir(),
        help=("运行态根目录（事件流/台账）。缺省取 paths.state_dir（agent.json），"
              "再缺省看 SESS_EVENTS_STATE_DIR。自测必须给临时目录，否则写穿生产事件流"),
    )
    parser.add_argument(
        "--stop-marker", default="",
        help="absolute path of the cooperative STOP marker (default: <cwd>/.kimi/tools/STOP)",
    )
    parser.add_argument(
        "--no-stop-guard", action="store_true",
        help="do not append the cooperative STOP-marker guard to the task text",
    )
    parser.add_argument(
        "--session", default="",
        help=(
            "resume an existing session id instead of creating one; an archived "
            "session is restored first and the warm-up turn is skipped"
        ),
    )
    parser.add_argument("--persona", default="", help="system_prompt (persona) for the session")
    parser.add_argument("--name", default="", help="session name; omit to let a model name it")
    parser.add_argument(
        "--permission-mode", default="auto", choices=["manual", "yolo", "auto"],
        help=("permission mode (default: auto = Never Ask). 注意语义：yolo 是 Ask When Needed"
              "（风险动作仍会问），auto 才是完全自动"),
    )
    parser.add_argument(
        "--provider", default="",
        help=(
            "provider name from the kimi-code config (house default for a NEW "
            "session: codebuddy). e.g. codebuddy / DeepSeek-new / managed:kimi-code. "
            "An unknown name prints the choices and exits. "
            "With --session, omit it to keep the session's current provider."
        ),
    )
    parser.add_argument(
        "--model", default="",
        help=(
            "model alias from the kimi-code config (house default for a NEW "
            "session: cb-deepseek-v4.1-flash). An unknown alias, or one belonging "
            "to another provider, prints the choices and exits. "
            "With --session, omit it to keep the session's current model "
            "(user ruling 2026-10-31: dispatching must never overwrite the model "
            "somebody picked by hand)."
        ),
    )
    parser.add_argument(
        "--effort", default="",
        help=(
            "thinking effort (default: the chosen model's declared default_effort). "
            "Must be one of that model's support_efforts."
        ),
    )
    parser.add_argument("--port", type=int, default=None, help="server port (default: auto-discover)")
    parser.add_argument("--timeout", type=float, default=600, help="seconds to wait (default 600)")
    parser.add_argument(
        "--no-activate", action="store_true",
        help="skip the warm-up turn that activates a fresh session",
    )
    parser.add_argument("--json", action="store_true", help="also print a JSON result block")
    parser.add_argument(
        "--ctx-force", action="store_true",
        help="越过 ctx 门禁（≥90%% 仍拒；issue #12 救场用，会在 ledger 留 forced=true）",
    )
    parser.add_argument(
        "--allow-retired", action="store_true",
        help="越过退役 sid 清单（ISS-29；默认拒绝给已退役 sid 派单）",
    )
    parser.add_argument("--role", default=os.environ.get("AGENT_ROLE", "pm"),
                        help="记录 ctx-gate 用的角色（默认取 AGENT_ROLE/pm）")
    parser.add_argument(
        "--detach", action="store_true",
        help="submit the prompt and return the session id immediately (do not wait for the answer)",
    )
    parser.add_argument(
        "--fetch", action="store_true",
        help="only print the session's latest assistant message (no new prompt)",
    )
    parser.add_argument(
        "--round-no", type=int, default=1,
        help="本单第几轮派单（幂等键 prompt_id 的一段，默认 1）",
    )
    parser.add_argument(
        "--issue", default="",
        help="单号，仅用于拼幂等键 prompt_id（默认 adhoc）",
    )
    parser.add_argument(
        "--skip-quota-gate", action="store_true",
        help="ISS-40 诊断用：跳过派单前算力配额闸（正常派单不用）",
    )
    args = parser.parse_args(argv)
    hits = lint_task_text(getattr(args, "task", "") or "")
    if hits:
        print(f"[dispatch] 警告：任务书含开放式问询措辞 {hits} —— 按规矩应改写成判据或上升项"
              f"（worker/QA 一律完全自动，不许中途等人）。", file=sys.stderr)

    # --fetch：只取该会话最后一条 assistant 消息，不提交新 prompt（配合 --detach 用）
    if args.fetch:
        if not args.session:
            parser.error("--fetch 需要 --session <id>")
        token = read_token()
        base = discover_base(token, args.port)
        print(fetch_final_answer(base, token, args.session) or "(该会话没有 assistant 消息)")
        return 0
    if not args.task:
        parser.error("--task is required (unless --fetch)")
    if args.state_dir:
        _sess_events().apply_state_dir(["--state-dir", args.state_dir])

    # ISS-40：配额闸在 token/server/session 之前；满额直接拒单并带配置路径与当前值。
    if not getattr(args, "skip_quota_gate", False):
        quota_gate()

    # Validate the three required choices BEFORE touching the token or the
    # network, so a missing/invalid choice never creates a session as a side
    # effect and never depends on a live server being up.
    # Reusing a session: leave its model/provider/effort alone unless the caller
    # named one explicitly (user ruling 2026-10-31).
    catalog = load_catalog()
    keep_session_model = bool(args.session) and not (args.provider or args.model or args.effort)
    if keep_session_model:
        provider, model, effort = "", "", ""
    else:
        provider, model, effort = resolve_choice(args, parser, catalog)

    token = read_token()
    base = discover_base(token, args.port)
    cwd = os.path.abspath(args.cwd)

    if args.session:
        sid = args.session
        # ISS-29：退役 sid 不再接活。归档只摘台账、不拦投喂，这里是投出去前
        # 的最后一道闸；`--allow-retired` 明确绕过（打印告警，不静默）。
        retired_reason = _retired_sids().check(sid)
        if retired_reason and not getattr(args, "allow_retired", False):
            raise DispatchError(f"拒绝派单：{retired_reason}（确认要派加 --allow-retired）")
        if retired_reason:
            print(f"[dispatch] ⚠ 绕过退役清单：{retired_reason}", file=sys.stderr)
        # issue #12：复用会话派单前先过 ctx 门禁（≥60% 拒、≥75% 强制交接、≥90% 连 --force 也拒）
        gate_rc = _ctx_gate(sid, force=args.ctx_force, operator=args.role)
        if gate_rc != 0:
            raise DispatchError(
                f"ctx 门禁拒绝派单（rc={gate_rc}）；确认要救场请加 --ctx-force")
        name = reuse_session(base, token, sid)
    else:
        if not os.path.isdir(cwd):
            raise DispatchError(f"--cwd is not a directory: {cwd}")
        if args.name:
            name = args.name
        else:
            name = model_self_name(base, token, DEFAULT_NAMING_MODEL, args.task, cwd)
        # ISS-95：章程在建会话**之前**检查。基线缺失时不许建——一个没有章程
        # 的会话会带着默认测试级别跑完一整轮，而没人看得见（2026-11-16 实证）。
        # 这一步刻意放在 create_session 之前：先失败，不留孤儿会话。
        try:
            _baseline, _supplement = charter_files(name)
        except charters.CharterMissing as exc:
            print(f"[dispatch] 拒绝建会话：{exc}", file=sys.stderr)
            print(f"[dispatch] 章程目录：{charters.baseline_root()}", file=sys.stderr)
            print("[dispatch] 这是环境错（装规则），不是参数错（改参数）",
                  file=sys.stderr)
            return EXIT_CHARTER_MISSING
        sid = create_session(base, token, cwd, title=name)
        record_created(sid, name)
    # What lands: POST …/profile persists ONLY `model` as the alias. `provider`
    # and `effort` are not in the agent_config schema — they are derived server
    # side from the alias (see README "provider / model / effort 的落地真相").
    # `provider` is sent anyway because the alias prefix is a naming convention,
    # not a binding, so the caller's choice is preserved in the request itself.
    agent_config: dict = {"permission_mode": args.permission_mode}
    if model:
        agent_config.update(model=model, provider=provider, thinking=effort)
    if args.persona:
        agent_config["system_prompt"] = args.persona
    applied = apply_profile(base, token, sid, agent_config)

    activated = False
    if not args.no_activate and not args.session:
        activated = activate_session(base, token, sid, args.timeout,
                                     charter=_baseline, name=name,
                                     supplement=_supplement)

    marker = args.stop_marker or os.path.join(os.path.abspath(cwd), ".kimi", "tools", "STOP")
    progress_dir = os.path.join(os.path.abspath(cwd), ".kimi", "tools")
    # 2026-11-09 用户令：STOP 守卫句退役——steer 已能打断当轮（abort/steer 实测成熟），
    # 守卫句成了每份任务书的纯噪音。不再追加；--stop-marker/--no-stop-guard 保留为兼容 no-op，
    # STOP 文件机制仍在（工具脚本可用），但不再是流程要求，也不自动写进任务书。
    task_text = args.task
    # #101 关单前必核：本次派单的项目若登记了环境档案，打一行指针到 stderr。
    # 只走 stderr：--json 的 stdout 是机器读的结果，不能混入一行说明。
    # 推不出项目键／未登记 ⇒ project_env_pointer 返回 ""，静默放行。
    pointer = project_env_pointer(cwd)
    if pointer:
        print(f"[dispatch] {pointer}", file=sys.stderr)
    pidemp = prompt_ref(args.issue, args.round_no, task_text)
    if args.detach:
        submit_prompt(base, token, sid, task_text, pidemp)
        print(f"[dispatch] detached: session id {sid} (name={name})，未等待回答")
        print(f"[dispatch] 收答案：dispatch.py --session {sid} --task \"只回结论\"")
        if args.json:
            print(json.dumps({"session_id": sid, "name": name, "detached": True,
                              "model": model, "effort": effort,
                              "prompt_id": pidemp}, ensure_ascii=False))
        return 0
    answer = run_turn(base, token, sid, task_text, timeout=args.timeout,
                      permission_mode=args.permission_mode, prompt_id=pidemp)

    pending = check_pending_interactions(base, token, sid)
    if pending:
        print(f"[dispatch] WARNING: still pending -> {', '.join(pending)} (session {sid})")

    # 2026-11-14：`--session` 走恢复路径，不跑 warm-up（activated 恒 False），
    # 但恢复本身会触发服务端加载该会话。原写法谎称 "no (resumed session)"，
    # 让人看不出这条为什么能成（PM 曾据此误信「须换工具」）。
    if activated:
        _activated = "yes (warm-up)"
    elif args.session:
        _activated = "no (resumed session; restore still triggers load)"
    else:
        _activated = "no (--no-activate)"

    lines = [
        "",
        "=" * 60,
        f"session name : {name}",
        f"session id   : {sid}",
        f"cwd          : {cwd}",
        f"provider     : {provider or '(kept: session unchanged)'} (choice; not echoed by the API)",
        f"model        : {applied.get('model') or model or '(kept: session unchanged)'}",
        f"effort       : {effort or '(kept)'} (choice; not echoed by the API)",
        f"permission   : {applied.get('permission_mode', args.permission_mode)}",
        f"activated    : {_activated}",
        "=" * 60,
        answer,
        "=" * 60,
    ]
    report = "\n".join(lines)

    if args.json:
        report += "\n" + json.dumps(
            {
                "name": name,
                "session_id": sid,
                "cwd": cwd,
                "provider": provider,
                "model": model,
                "effort": effort,
                "model_applied": applied.get("model") or "",
                "activated": activated,
                "answer": answer,
            },
            ensure_ascii=False,
        )

    guard_token(token, report)
    guard_token(token, " ".join(sys.argv))
    print(report)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except NeedsPerson as err:
        # 退出码 3＝「在等人答，结论不确定」，别当失败重派（`wait_session` 同档）。
        # 打印 `needs_person` ＋ interaction_id，供引擎直接记 `index.needs_person`。
        print(f"[dispatch] needs_person kind={err.kind} "
              f"interaction_id={err.interaction_id or '(unknown)'}", file=sys.stderr)
        print(f"[dispatch] {err.detail}", file=sys.stderr)
        sys.exit(3)
    except DispatchError as err:
        print(f"[dispatch] ERROR: {err}", file=sys.stderr)
        sys.exit(1)
