"""Kimi Code 家目录的**单一解析源**。全仓任何工具/skill 都不许再自己拼路径。

为什么要有（2026-11-06，issue #7，用户明令）：
  用户要把 Kimi Code 从 C 盘迁到 E 盘。任何写死绝对路径的地方
  或直接 `Path.home()/".kimi-code"` 的地方，迁移后都会静默读不到文件
  （token 读不到 → 工具报「未授权」；sessions 读不到 → 归档/上下文全断）。

统一口径：
  · 环境变量 **`KIMI_CODE_HOME`** 指向家目录（如 `E:/kimi-code`）；
  · 未设则回落 `~/.kimi-code`（兼容迁移前的现状）；
  · 值支持 `~` 与 Windows 反斜杠，落盘前统一 `expanduser()`。

用法：
    from kimi_paths import home, token_file, sessions_dir, config_file
    token_file().read_text(encoding="utf-8")

    # 命令行自检（迁移演练用）
    python tools/kimi_paths.py

规矩（写新代码时照抄）：
  ✔ `import kimi_paths; kimi_paths.token_file()`
  ✘ `Path.home() / ".kimi-code" / "server.token"`
  ✘ `os.path.join(os.path.expanduser("~"), ".kimi-code", "server.token")`

env 覆盖（保留既有更细粒度的开关，优先级高于本模块）：
  · `KIMI_CODE_TOKEN_FILE`  —— 直接指定 token 文件
  · `KIMI_CODE_CONFIG_FILE` —— 直接指定 config.toml
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_HOME = "KIMI_CODE_HOME"
ENV_TOKEN_FILE = "KIMI_CODE_TOKEN_FILE"
ENV_CONFIG_FILE = "KIMI_CODE_CONFIG_FILE"

DEFAULT_DIRNAME = ".kimi-code"


def home() -> Path:
    """kimi 家目录。`KIMI_CODE_HOME` 优先，未设回落 `~/.kimi-code`。

    不检查存在性——迁移演练时要能在**尚无该目录**的情况下解析出正确路径
    （issue #7 验收：指向空目录 `E:/kimi-code` 时 helper 全部解析到新根）。
    """
    raw = (os.environ.get(ENV_HOME) or "").strip()
    if raw:
        # 允许 `~`、正反斜杠混用；expanduser 只处理 `~`，其余原样
        return Path(os.path.expanduser(raw)).expanduser()
    return Path(os.path.expanduser("~")) / DEFAULT_DIRNAME


def token_file() -> Path:
    """server.token 的路径。`KIMI_CODE_TOKEN_FILE` 优先。"""
    raw = (os.environ.get(ENV_TOKEN_FILE) or "").strip()
    if raw:
        return Path(os.path.expanduser(raw))
    return home() / "server.token"


def config_file() -> Path:
    """config.toml 的路径。`KIMI_CODE_CONFIG_FILE` 优先。"""
    raw = (os.environ.get(ENV_CONFIG_FILE) or "").strip()
    if raw:
        return Path(os.path.expanduser(raw))
    return home() / "config.toml"


def sessions_dir() -> Path:
    """会话目录（其下 `<workspace>/<session>/agents/main/wire.jsonl`）。"""
    return home() / "sessions"


def token() -> str:
    """读 token 原文（去空白）。读不到返回空串——调用方自己决定怎么报错。

    值**永不打印、永不进日志**（凭据纪律）。
    """
    try:
        return token_file().read_text(encoding="utf-8-sig", errors="replace").strip()
    except OSError:
        return ""


def describe() -> str:
    """一行人类可读的解析结果，供迁移演练与排障。"""
    src = f"${ENV_HOME}" if os.environ.get(ENV_HOME) else f"~/{DEFAULT_DIRNAME}(默认)"
    return (f"home={home()} [{src}]  token={token_file()}  "
            f"config={config_file()}  sessions={sessions_dir()}")


if __name__ == "__main__":
    print(describe())
    for name, p in (("home", home()), ("token", token_file()),
                    ("config", config_file()), ("sessions", sessions_dir())):
        print(f"  {name:9} {p}  exists={p.exists()}")
