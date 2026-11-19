#!/usr/bin/env python
"""会话生命周期事件源：`emit()` 是 `state/events.jsonl` 的**唯一写者**。

写侧。读侧是 `session_events.py`（本包已有模块）；两者职责相反——本模块写事件，
`session_events` 只读事件流做 replay。**不合并、不双写**，且本模块的路径解析
一律委托 `session_events`，不自建第二套链。

为什么要有（ISS-27）：会话的上线／退出／更迭此前散落在 sessions.py、sess.py、
dispatch.py、gh_issue.py、rrotate.sh 里，各写各的本地台账，谁也说不清「某一时刻
谁是 qa、谁占着 pm-<项目>」。本模块把这类事实收成一条**可回放的单调事件流**：

    {"seq": 7, "at": "2026-11-08T03:40:11+08:00", "type": "session.key_changed",
     "key": "qa", "sid": "session_ab…"}


三条硬约定（改动前先读）：

  1. **seq 是单写者递增整数**，不用时间戳。启动时读事件流末条的 seq 续号（+1）；
     轮转（`.1`/`.2`）后仍续号，不回绕。`读末号 → 写` 整段在文件锁内完成。
  2. **payload 白名单**：只允许 `seq/at/type/sid/role/issue/assignment/profile/
     from_sid/to_sid/key`，`profile` 只允许 `model/effort/permission`。
     出现 `body`/`prompt`/`message`/`token`/`api_key` 或任何别的键 ⇒ **抛错拒绝写入**
     （不是忽略、不是丢字段）——正文/凭据永不进事件流。
  3. **路径解析委托**：`resolve_root()` 不再自己排优先级，而是交给
     `session_events.resolve_session_root`（声明式根）与
     `session_events.resolve_events_path(cfg, explicit=…)`（显式路径）。
     没有这条，任何自测都会写穿到生产 `state/sessions.json`（#18 的 G-1/H-1）。

与引擎事件流的关系（**两条流，不合并、不双写**）：

  · 本文件写的 `state/events.jsonl` = 会话生命周期的**真相源**；
  · safkc 仓 `store/events.jsonl` = 引擎自己的运行事件（`run.started` 等）。
  · 引擎用配置 `engine.session_events_path` **只读**本流来重建会话真相。

用法（CLI 只给排障与验收用，正路是 `import super_agent.sess_events; …emit(...)`）：

    python -m super_agent.sess_events root
    python -m super_agent.sess_events tail -n 5
    python -m super_agent.sess_events emit session.created --sid session_x --state-dir C:/tmp/s
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from . import config as config_mod
from . import session_events
from .session_ledger import FULL_SID_RE, SID_SHAPE_HINT, sid_problem

ENV_STATE_DIR = "SESS_EVENTS_STATE_DIR"
EVENTS_NAME = "events.jsonl"
MAX_EVENT_LINES = 10_000
LOCK_TIMEOUT_S = 15.0

# ISS-74 B：**完整 sid 形态的唯一一份正则**在 `session_ledger.FULL_SID_RE`。
# 本模块不再自带副本（D2 白名单 B：兄弟引用改相对），`check_sid` 包装函数名不变。


PAYLOAD_KEYS = ("seq", "at", "type", "sid", "role", "issue", "assignment",
                "profile", "from_sid", "to_sid", "key")
PROFILE_KEYS = ("model", "effort", "permission")
FORBIDDEN_KEYS = ("body", "prompt", "message", "token", "api_key")


def check_sid(sid) -> bool:
    """入参是不是**完整** sid 形态。空值、残号、非 `session_` 前缀一律 False。

    调用方负责决定「拒写」还是「写但告警」；本函数只回答形态问题。
    """
    return bool(FULL_SID_RE.match(str(sid or "")))


# ------------------------------------------------------------------ 路径（委托 session_events）
def _cfg(state_dir=None) -> dict:
    """The one config, built the one way: ``agent_config`` overrides + ``config.load``.

    The four levels of :func:`session_events.resolve_events_path` stay the only
    resolution order; this module only supplies the ``cfg`` they read. The
    explicit ``--state-dir`` is not folded in here -- it goes to
    ``resolve_events_path(explicit=...)``, which is what that parameter is for.
    """
    from . import agent_config
    return config_mod.load(overrides=agent_config.overrides())


def resolve_root(state_dir=None) -> Path:
    """state 根：显式参数优先，否则交给 `session_events.resolve_session_root`。

    没有第三级「猜工作台 state/」——公开仓不带私有路径。九处调用点（包内六个、
    包外三个）签名不变，因此全部沿用本函数。
    """
    cfg = _cfg(state_dir)
    if state_dir:
        explicit = Path(os.path.expanduser(str(state_dir))).resolve() / EVENTS_NAME
        return session_events.resolve_events_path(cfg, explicit=str(explicit)).parent
    try:
        return session_events.resolve_session_root(cfg)
    except session_events.SessionStreamNotConfigured as exc:
        raise session_events.SessionStreamNotConfigured(
            f"{exc}；四个入口：$SESS_EVENTS_STATE_DIR、$LWA_STATE_DIR、"
            f"配置 engine.session_state_dir、配置 paths.state_dir") from None


def events_path(state_dir=None) -> Path:
    return resolve_root(state_dir) / EVENTS_NAME


def sessions_path(state_dir=None) -> Path:
    """`state/sessions.json`（固定角色名 → sid 的台账）。"""
    return resolve_root(state_dir) / "sessions.json"


def issue_sessions_path(state_dir=None) -> Path:
    """`state/issue_sessions.json`（本单绑定的 qa/worker 会话）。"""
    return resolve_root(state_dir) / "issue_sessions.json"


def ledger_path(state_dir=None) -> Path:
    return resolve_root(state_dir) / "ledger.jsonl"


def lock_path(state_dir=None) -> Path:
    return resolve_root(state_dir) / "events.lock"


def apply_state_dir(argv, env: str = ENV_STATE_DIR) -> list:
    """把命令行上的 `--state-dir <p>` 抽走并写回环境变量，返回剩下的参数。

    CLI 都走这一条：参数优先（它覆盖已有 env），解析仍只有 `resolve_root` 一处，
    于是「事件写哪、台账写哪」永远同一根，不会出现第二处硬编码。
    """
    argv = list(argv)
    if "--state-dir" not in argv:
        return argv
    i = argv.index("--state-dir")
    if i + 1 < len(argv):
        os.environ[env] = os.path.expanduser(argv[i + 1])
        del argv[i:i + 2]
    else:
        del argv[i:i + 1]
    return argv


# ------------------------------------------------------------------ 文件锁
class _FileLock:
    """跨进程互斥（Windows `msvcrt`／POSIX `flock`，都不阻塞在别人手里）。

    `读末号 → 写一行` 必须整段在锁内，否则两个并发入口会拿到同一个 seq。
    """

    def __init__(self, path: Path, timeout: float = LOCK_TIMEOUT_S):
        self.path = Path(path)
        self.timeout = timeout
        self._fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        if fh.tell() == 0 and os.path.getsize(self.path) == 0:
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
                    raise TimeoutError(f"events lock busy > {self.timeout}s: {self.path}")
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


# ------------------------------------------------------------------ 读
def _read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
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
                    out.append(rec)
    except OSError:
        return []
    return out


def segments(state_dir=None) -> list[Path]:
    """事件流的所有分段：当前、`.1`、`.2`（只看存在的）。"""
    base = events_path(state_dir)
    return [p for p in (base, base.with_name(base.name + ".1"),
                        base.with_name(base.name + ".2")) if p.is_file()]


def read_events(state_dir=None) -> list[dict]:
    """全部分段按 seq 升序合并（无 seq 的旧行排在前面）。"""
    rows: list[dict] = []
    for path in segments(state_dir):
        rows.extend(_read_jsonl(path))
    rows.sort(key=lambda r: (int(r.get("seq") or 0), str(r.get("at") or "")))
    return rows


def last_seq(state_dir=None) -> int:
    """当前事件流的末号；无任何事件 ⇒ 0。"""
    best = 0
    for path in segments(state_dir):
        for rec in reversed(_read_jsonl(path)):
            seq = rec.get("seq")
            if isinstance(seq, int):
                return max(best, seq)
    return best


def _count_lines(path: Path) -> int:
    try:
        with open(path, "rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def _rotate(base: Path) -> None:
    second = base.with_name(base.name + ".2")
    first = base.with_name(base.name + ".1")
    if second.exists():
        second.unlink()
    if first.exists():
        os.replace(first, second)
    if base.exists():
        os.replace(base, first)


# ------------------------------------------------------------------ 校验
def validate(event_type: str, fields: dict) -> dict:
    """白名单 + profile 子白名单。不合规一律抛错，**不静默丢字段**。"""
    if not isinstance(event_type, str) or not event_type.strip():
        raise ValueError("emit: event_type 必须是非空字符串")
    bad = [k for k in fields if k not in PAYLOAD_KEYS]
    if bad:
        raise ValueError(
            f"emit: payload 出现白名单外的键 {bad}（允许：{list(PAYLOAD_KEYS)}）。"
            "正文/prompt/token 永不进事件流。")
    if not fields.get("at"):
        # microseconds: `--at` cutoffs must be able to separate two events that
        # land in the same second (a create and its bind often do).
        fields["at"] = datetime.now().astimezone().isoformat(timespec="microseconds")
    profile = fields.get("profile")
    if profile is not None:
        if not isinstance(profile, dict):
            raise ValueError("emit: profile 必须是对象")
        extra = [k for k in profile if k not in PROFILE_KEYS]
        if extra:
            raise ValueError(
                f"emit: profile 只允许 {list(PROFILE_KEYS)}，收到 {extra}")
        for key, val in profile.items():
            if val is not None and not isinstance(val, (str, int)):
                raise ValueError(f"emit: profile.{key} 必须是标量，收到 {type(val).__name__}")
    for key, val in fields.items():
        if key == "profile":
            continue
        if isinstance(val, (dict, list, tuple, set)):
            raise ValueError(f"emit: 字段 {key} 只接受标量，收到 {type(val).__name__}")
    return fields


# ------------------------------------------------------------------ 写
def emit(event_type: str, *, root=None, **fields) -> dict:
    """校验 → 分配 seq → 追加 → 返回落盘记录。

    `root` 是控制参数（等价命令行 `--state-dir`），不是 payload 字段。
    """
    payload = validate(event_type, dict(fields))
    path = events_path(root)
    with _FileLock(lock_path(root)):
        if _count_lines(path) >= MAX_EVENT_LINES:
            _rotate(path)
        record = {"seq": last_seq(root) + 1, "type": event_type}
        record.update(payload)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return record


# ------------------------------------------------------------------ CLI
def _main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.strip())
        return 2
    state_dir = None
    if "--state-dir" in argv:
        i = argv.index("--state-dir")
        if i + 1 >= len(argv):
            print("--state-dir 需要一个路径", file=sys.stderr)
            return 2
        state_dir = argv[i + 1]
        del argv[i:i + 2]
    cmd, rest = argv[0], argv[1:]
    if cmd == "root":
        print(f"root={resolve_root(state_dir)}")
        print(f"events={events_path(state_dir)}  exists={events_path(state_dir).is_file()}")
        print(f"sessions={sessions_path(state_dir)}")
        print(f"last_seq={last_seq(state_dir)}")
        return 0
    if cmd == "tail":
        n = 5
        if rest and rest[0] == "-n" and len(rest) > 1:
            n = int(rest[1])
        rows = read_events(state_dir)[-n:]
        for rec in rows:
            print(json.dumps(rec, ensure_ascii=False, sort_keys=True))
        print(f"# {len(rows)} 行；last_seq={last_seq(state_dir)}", file=sys.stderr)
        return 0
    if cmd == "emit":
        if not rest:
            print("emit 需要 event_type", file=sys.stderr)
            return 2
        fields = {}
        args = rest[1:]
        i = 0
        while i < len(args):
            key = args[i].lstrip("-").replace("-", "_")
            if i + 1 >= len(args):
                print(f"字段 {args[i]} 缺值", file=sys.stderr)
                return 2
            fields[key] = args[i + 1]
            i += 2
        try:
            rec = emit(rest[0], root=state_dir, **fields)
        except ValueError as exc:
            print(f"拒绝写入: {exc}", file=sys.stderr)
            return 3
        print(json.dumps(rec, ensure_ascii=False, sort_keys=True))
        return 0
    print(f"未知子命令：{cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
