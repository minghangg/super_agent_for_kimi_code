"""Kimi Code 服务端适配层（兼容薄壳）—— **实现全在 `tools/kimi_direct.py`**。

2026-11-06（issue #8 步 1 返工，QA P0-1/P0-2）改造说明：
  上一版本文件是「唯一的直连实现」，导致 `wss_bus.py` 大量 `import kimi_api`，
  形成「daemon 调自己的降级层」的递归依赖，QA 的 grep 硬条款直接判红。
  现在职责拆开：
    · `kimi_direct.py` —— 协议实现（传输、判代、WS 探针、读写动作），**不认 daemon**；
    · 本文件           —— 保留**原有函数名与 CLI**，内部转调 `kimi_direct`，
                          既有消费者（`sess.py`/`ctx.py`/`revive.py`/`auto_approve.py`/
                          看门狗）一行不用改。

保留本文件的理由：13 个工具与 3 个脚本按旧签名调用，改签名＝大面积返工，
而步 1 只该动 daemon。**新代码请直接 import `kimi_direct`**，本层只为兼容存在。

兼容契约（不得删改，除非全仓同步迁移）：
    _req(method, path, body)        → (status, payload)
    get / post / data_of / code_of
    version / is_new / source
    WSProbe / cached
    session_info / status / active_prompt / has_pending
    pending_interactions / tasks / norm_task
    abort_prompt / abort_prompt_session / cancel_task / answer_interaction
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.append(str(BASE / "tools"))      # 追加不插入（tools/queue.py 遮蔽标准库 queue）
from . import direct as _D  # noqa: E402

# ---- 常量透出（旧代码 from kimi_api import SERVER 之类）
SERVER = _D.SERVER
WS_PATH = _D.WS_PATH
DEFAULT_AGENT = _D.AGENT
TRANSCRIPT_GRADE = _D.TRANSCRIPT_GRADE
NEW_API_SINCE = _D.NEW_API_SINCE
CACHE_FILE = BASE / "state" / "kimi_ws_cache.json"
TOKEN_FILE = None          # 已废弃：路径解析走 kimi_paths，保留名字避免 ImportError

# ---- 传输
_request = _D._request
get = _D.get
post = _D.post
data_of = _D.data_of
code_of = _D.code_of

# ---- 判代
version = _D.version
is_new = _D.is_new
source = _D.source

# ---- WS / 归并
WSProbe = _D.WSProbe
_flat = _D.flat
cached = _D.cached

# ---- 读
session_info = _D.session_info
status = _D.status
active_prompt = _D.active_prompt
has_pending = _D.has_pending
pending_interactions = _D.pending_interactions
tasks = _D.tasks
norm_task = _D.norm_task

# ---- 写
abort_prompt = _D.abort_prompt


def abort_prompt_session(sid: str) -> tuple:
    """旧名 → `kimi_direct.abort_session`（停当前轮，不必先取 pid）。"""
    return _D.abort_session(sid)


cancel_task = _D.cancel_task
answer_interaction = _D.answer_interaction


def __getattr__(name: str):
    """未显式透出的名字，回落到 kimi_direct（含新增函数）。"""
    try:
        return getattr(_D, name)
    except AttributeError:
        raise AttributeError(f"module 'kimi_api' has no attribute {name!r}") from None


if __name__ == "__main__":
    import json

    print("version =", version() or "(unreachable)", "| is_new =", is_new(), "|", source())
    if len(sys.argv) > 1:
        sid = sys.argv[1]
        print("session_info =", json.dumps(session_info(sid), ensure_ascii=False)[:300])
        print("status       =", json.dumps(status(sid), ensure_ascii=False)[:400])
        print("active_prompt =", active_prompt(sid) or "(none)")
