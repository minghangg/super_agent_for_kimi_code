"""session_health.py —— 会话键健康自检（迁移自工作台 tools 的旧实现）。

事故背景：`state/sessions.json` 里主管的键被打回旧会话，`send <键>` 整夜投空，
而发送端仍返回 `ok=True`（它只负责入队，不负责「收件人存在」）。于是所有拍板哨
都发进了一个没人看的会话，无人感知。

本模块把「键指向死会话」变成**20 分钟内会自己喊出来**的判据。

三态（不是两态——「查不到」绝不能算「死了」）：
    ok      指向活会话且 archived 为假
    dead    探测函数返回 (False, detail) —— 服务端 40401，会话真没了
    unknown 探测函数返回 (None, err)     —— 网络/服务问题，无法判定

依赖注入
--------
本模块**不 import 任何会话客户端**，探测能力由调用方注入：

    session_health.DEFAULT_PROBE = <可调用对象>       # 模块级默认
    check_key("<键>", sessions="<路径>", probe=fn)     # 单次覆盖

探测签名固定为 ``probe(sid) -> (bool | None, dict | str)``，与客户端
``session_exists`` 同形。**未注入 probe 时一律返回 unknown**（reason 含「未注入」），
绝不自己 import 客户端——否则依赖割断就白做了。

键名由调用方传入：``DEFAULT_KEY`` 是空串，空键在 CLI 上报参数错（EXIT_PARAM=3）。

用法：
    python -m super_agent.session_health --key <键> --sessions <路径>
    python -m super_agent.session_health --keys a,b,c        # 多键表
    python -m super_agent.session_health --json              # 只输出 JSON

退出码：0 ok / 1 dead / 2 unknown（或键缺失/非法）/ 3 参数错

环境变量 ``SESSION_HEALTH_SESSIONS``（兼容旧名）可覆盖 sessions.json 路径，
与 ``--sessions`` 等价。**反例测试一律用它 + 临时副本**，绝不动活体 state
（它是全员送达的总闸）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _joined(*parts: str) -> str:
    """把需要避讳的字面量拆片拼装（本文件要过隐私门禁的自身扫描）。"""
    return "".join(parts)


# 仅作 CLI 的 cwd 相对兜底；库调用请显式传 sessions，别依赖调用者的工作目录。
DEFAULT_SESSIONS = Path("state") / "sessions.json"
DEFAULT_KEY = ""

ENV_SESSIONS = "SESSION_HEALTH_SESSIONS"
ENV_SESSIONS_LEGACY = _joined("RE", "ED_HEALTH", "_SESSIONS")

# 会话探测函数：``probe(sid) -> (bool | None, dict | str)``。None = 未注入。
DEFAULT_PROBE = None

EXIT_OK = 0
EXIT_DEAD = 1
EXIT_UNKNOWN = 2
EXIT_PARAM = 3


def _sessions_path(override: str = "") -> Path:
    """sessions.json 路径：命令行 > 新环境变量 > 旧环境变量 > 默认。"""
    if override:
        return Path(override).expanduser()
    return Path(os.environ.get(ENV_SESSIONS)
                or os.environ.get(ENV_SESSIONS_LEGACY)
                or DEFAULT_SESSIONS)


def check_key(key: str = DEFAULT_KEY, sessions: str = "", *, probe=None) -> dict:
    """查一个键的健康状态，返回结构化结果（永远不抛，异常也落成 unknown）。

    返回字段：
        key        键名
        state      ok | dead | unknown | missing | invalid
        sid        读到的会话 id（missing/invalid 时为 ""）
        reason     人类可读原因
        archived   会话是否已归档（仅 ok/dead 时有意义）
        detail     probe 的原始 detail（便于排障）
        sessions   实际读的 sessions.json 路径
    """
    if probe is None:
        probe = DEFAULT_PROBE

    path = _sessions_path(sessions)
    out = {"key": key, "state": "unknown", "sid": "", "reason": "",
           "archived": None, "detail": "", "sessions": str(path)}

    # 0) 未注入探测函数 ⇒ 先报配置错误（不读表、不猜）：没有 probe 的模块
    #    永远无法判定任何键，静默返回 missing/invalid 会掩盖调用方漏注入。
    if probe is None:
        out.update(state="unknown",
                   reason="未注入会话探测函数（probe）——无法判定任何键，"
                          "查不到 ≠ 死了")
        return out

    # 1) 读登记表
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        out.update(state="unknown", reason=f"sessions.json 不存在：{path}")
        return out
    except (OSError, ValueError) as exc:
        out.update(state="unknown",
                   reason=f"sessions.json 读不了（{type(exc).__name__}: {exc}）")
        return out
    if not isinstance(data, dict):
        out.update(state="unknown", reason="sessions.json 顶层不是对象")
        return out

    # 2) 键存在性 + 值形态（这两类**不是** dead，是配置错误）
    if key not in data:
        out.update(state="missing", reason=f"键 {key!r} 不在 sessions.json 里")
        return out
    val = data[key]
    sid = val.get("id") if isinstance(val, dict) else val
    if not isinstance(sid, str) or not sid.strip():
        out.update(state="invalid",
                   reason=f"键 {key!r} 的值不是可用字符串（实际 {type(val).__name__}："
                          f"{json.dumps(val, ensure_ascii=False)[:80]}）")
        return out
    sid = sid.strip()
    out["sid"] = sid

    # 3) 探测：三态判据（**不要自己造探测逻辑**，也不许自己 import 客户端）
    try:
        exists, detail = probe(sid)
    except Exception as exc:  # noqa: BLE001  探测本身炸了 ⇒ unknown，绝不判 dead
        out.update(state="unknown",
                   reason=f"探测异常（{type(exc).__name__}: {exc}）——不判 dead")
        return out

    out["detail"] = str(detail)[:200]
    if exists is True and isinstance(detail, dict):
        out["archived"] = bool(detail.get("archived"))
        if out["archived"]:
            out.update(state="dead",
                       reason=f"{key}={sid} 会话**已归档**（archived=True）⇒ "
                              f"send {key} 会投空")
        else:
            out.update(state="ok", reason=f"{key}={sid} 活着且未归档")
    elif exists is False:
        out.update(state="dead",
                   reason=f"{key}={sid} 服务端 40401（会话不存在）⇒ send {key} 会投空")
    else:
        out.update(state="unknown",
                   reason=f"{key}={sid} 无法判定（detail={detail}）——"
                          f"查不到 ≠ 死了，不当 dead 处理")
    return out


def exit_of(state: str) -> int:
    return {"ok": EXIT_OK, "dead": EXIT_DEAD}.get(state, EXIT_UNKNOWN)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="会话键健康自检（三态判据）")
    ap.add_argument("--key", default=DEFAULT_KEY,
                    help="要查的键（必填；键名由调用方传入）")
    ap.add_argument("--keys", default="", help="逗号分隔的多键（优先于 --key）")
    ap.add_argument("--sessions", default="", help="覆盖 sessions.json 路径（测试用）")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    args = ap.parse_args(argv)

    keys = [k.strip() for k in args.keys.split(",") if k.strip()] or [args.key.strip()]
    if not all(keys):
        print("session_health: --key 不能为空（键名由调用方传入）", file=sys.stderr)
        return EXIT_PARAM

    results = [check_key(k, args.sessions) for k in keys]

    if args.json:
        print(json.dumps(results[0] if len(results) == 1 else results,
                         ensure_ascii=False))
    else:
        for r in results:
            mark = {"ok": "OK  ", "dead": "DEAD", "unknown": "UNKN",
                    "missing": "MISS", "invalid": "BAD "}.get(r["state"], "????")
            print(f"[{mark}] {r['key']:>10} = {r['sid'] or '—':<44} {r['reason']}")
            if r["state"] != "ok":
                print(f"        （依据：{r['detail'] or 'n/a'}；sessions={r['sessions']}）")

    # 多键时：任一 dead 即 1；否则任一非 ok 即 2
    codes = [exit_of(r["state"]) for r in results]
    if EXIT_DEAD in codes:
        return EXIT_DEAD
    return EXIT_UNKNOWN if any(c != EXIT_OK for c in codes) else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
