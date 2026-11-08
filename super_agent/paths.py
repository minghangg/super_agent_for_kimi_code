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
  · `KIMI_SERVER_INSTANCES_DIR` —— 实例目录夹具（诊断/测试）
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit

ENV_HOME = "KIMI_CODE_HOME"
ENV_TOKEN_FILE = "KIMI_CODE_TOKEN_FILE"
ENV_CONFIG_FILE = "KIMI_CODE_CONFIG_FILE"
ENV_SERVER = "KIMI_SERVER"
ENV_INSTANCES_DIR = "KIMI_SERVER_INSTANCES_DIR"
DEFAULT_BASE = "http://127.0.0.1:58627"

DEFAULT_DIRNAME = ".kimi-code"

_WARN = logging.getLogger("super_agent.paths").warning


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


def _valid_url(raw: str) -> bool:
    try:
        parts = urlsplit(raw)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def _env_base() -> str | None:
    raw = (os.environ.get(ENV_SERVER) or "").strip()
    if not raw or not _valid_url(raw):
        return None
    return raw


def instances_dir() -> Path:
    raw = (os.environ.get(ENV_INSTANCES_DIR) or "").strip()
    if raw:
        return Path(os.path.expanduser(raw))
    return home() / "server" / "instances"


def _coerce_port(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        port = value
    elif isinstance(value, str) and value.strip().isdigit():
        port = int(value.strip())
    elif isinstance(value, float) and value.is_integer():
        port = int(value)
    else:
        return None
    return port if 1 <= port <= 65535 else None


def _coerce_host(value) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "127.0.0.1"


def _instance_files(root: Path) -> list[Path]:
    try:
        return sorted((p for p in root.glob("*.json") if p.is_file()),
                      key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    except OSError as exc:
        _WARN("instances 解析失败，回落 58627：%s: %s（目录=%s）",
              type(exc).__name__, exc, root)
        return []


def latest_instance(root: Path | None = None) -> dict | None:
    """mtime 最新且可解析的实例；返回 server_id/path/port/host/mtime，全坏返回 None。"""
    directory = root if root is not None else instances_dir()
    if not directory.is_dir():
        _WARN("instances 解析失败，回落 58627：FileNotFoundError: %s（目录=%s）",
              directory, directory)
        return None
    errors: list[str] = []
    for path in _instance_files(directory):
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError) as exc:
            errors.append(f"{type(exc).__name__}: {path.name}")
            continue
        if not isinstance(data, dict):
            errors.append(f"TypeError: {path.name}")
            continue
        port = _coerce_port(data.get("port"))
        if port is None:
            errors.append(f"ValueError: {path.name}")
            continue
        host = _coerce_host(data.get("host"))
        raw_id = data.get("server_id") or data.get("id") or path.stem
        return {"server_id": str(raw_id), "path": str(path),
                "port": port, "host": host,
                "mtime": path.stat().st_mtime}
    reason = ", ".join(errors) if errors else "ValueError: no valid instance"
    _WARN("instances 解析失败，回落 58627：%s（目录=%s）", reason, directory)
    return None


def server_id() -> str:
    """当前实例的 server_id；env 显式设置或无有效实例时返回空串。"""
    if _env_base() is not None:
        return ""
    rec = latest_instance()
    return str(rec["server_id"]) if rec else ""


def server_base() -> str:
    """现取服务基址：env > 最新有效 instances > 58627（失败必有 WARN）。"""
    env = _env_base()
    if env is not None:
        return env
    rec = latest_instance()
    if rec is None:
        return DEFAULT_BASE
    return f"http://{rec['host']}:{rec['port']}"


if __name__ == "__main__":
    print(describe())
    for name, p in (("home", home()), ("token", token_file()),
                    ("config", config_file()), ("sessions", sessions_dir())):
        print(f"  {name:9} {p}  exists={p.exists()}")
