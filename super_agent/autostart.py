"""autostart.py —— 双后端登录自启（startup 默认 / scheduled 可选）。

Migrated from the supervisor workbench tools/autostart.py (ISS-25 item 1).
issue #18 前置门禁。用户裁定：**自启用启动文件夹后端（零提权），不等硬化批**。

为什么要两个后端
----------------
`startup`（**默认**）：往用户启动文件夹写一个 `.vbs`，用 `WScript.Shell.Run(..., 0, False)`
以隐藏窗口起 `pythonw.exe <脚本>`。**零提权**、无需 UAC、登录即生效，用户删掉文件即关闭。
`scheduled`：沿用 `decide_watch.py` 既有的 `Register-ScheduledTask` 路径（需 UAC），
保留不动，只作为可选项——已有注册的任务（`KimiDecideWatch`）继续用它。

单一来源
--------
本模块是**两端唯一实现**（`decide_watch.py` 与 `monitor.py` 都 import 它）。
`Spec` 描述「装什么」，两个后端各自把 `Spec` 翻成自己的落地形式；
回读校验也读同一份 `Spec`，所以注册与校验不会漂。

为什么 `.vbs` 用 UTF-16LE + BOM
-------------------------------
`WScript` 读 `.vbs` 时按**系统 ACP（ANSI 代码页）**解析；不吃 BOM 的 UTF-8 在中文
Windows 上通常按 GBK 解，中文路径会乱码。用 **UTF-16LE + BOM** 的结论不变，
但理由是「**不依赖系统 ACP 设置**」而不是「UTF-8 必然乱」——QA 实测本机
ACP=65001（UTF-8）时，无 BOM 的 UTF-8 也能跑通。UTF-16 在任何 ACP 下都成立。
带 BOM 的 UTF-16LE 是 VBScript 的标准编码，实测可正确处理中文路径。
（备选：ANSI/GBK 也能跑，但一旦路径含非 GBK 字符就崩；UTF-16 更稳。）

退出码（与调用方共用）
----------------------
0   成功（含幂等跳过）
10  startup 注册失败（写盘失败 / 回读不符）
11  startup 卸载失败（删了但回读仍在）

（8 是 `--send-mode` 的预留位，9 留给 salarm 的 uninstall —— 自启**不占**这两个，
 详见交付评论里的完整占用表。QA18-3 订正：本段曾误写 8/9。）
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

EXIT_OK = 0
# QA18-3：8 是 `--send-mode` 的预留位（`decide_watch.py:366` 注释明写「勿混」），
# 自启不得占用，故用 10 / 11。完整占用表见交付评论。
EXIT_STARTUP_INSTALL_FAILED = 10
EXIT_STARTUP_UNINSTALL_FAILED = 11

BACKENDS = ("startup", "scheduled")
DEFAULT_BACKEND = "startup"

VBS_ENCODING = "utf-16-le"
VBS_BOM = b"\xff\xfe"


@dataclass
class Spec:
    """「装什么」的唯一定义（两个后端共用）。"""

    name: str                  # KimiDecideWatch / KimiMonitor
    script: Path               # 被启动的 .py 绝对路径
    args: list[str] = field(default_factory=list)   # 传给脚本的参数
    workdir: Path | None = None                     # 工作目录（默认脚本所在仓根）
    execute_note: str = ""     # pythonw 降级说明（空＝正常）

    def __post_init__(self) -> None:
        self.script = Path(self.script).resolve()
        if self.workdir is None:
            self.workdir = self.script.parent.parent
        self.workdir = Path(self.workdir).resolve()

    @property
    def startup_file(self) -> Path:
        """`%APPDATA%\\...\\Startup\\<name>.vbs`（可在测试里用 APPDATA 覆盖）。"""
        return startup_dir() / f"{self.name}.vbs"

    def command_line(self) -> str:
        """`.vbs` 里实际执行的命令行（脚本路径与参数都加引号）。"""
        parts = [f'"{self.script}"'] + [f'"{a}"' if " " in a else a for a in self.args]
        return " ".join(parts)


def startup_dir() -> Path:
    """登录启动文件夹。`APPDATA` 可覆盖（测试用）；取不到就走已知默认路径。"""
    appdata = (os.environ.get("APPDATA") or "").strip()
    if appdata:
        return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    return (Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows"
            / "Start Menu" / "Programs" / "Startup")


def windowless_python() -> tuple[str, str]:
    """取同目录 `pythonw.exe`；拿不到就如实降级并给出说明（不假装）。"""
    exe = Path(sys.executable).resolve()
    cand = exe.with_name("pythonw.exe")
    if cand.is_file():
        return str(cand), ""
    return str(exe), (f"同目录未找到 pythonw.exe（{cand}），降级用 python.exe —— "
                      f"登录时会**弹控制台窗口**")


def render_vbs(spec: Spec, exe: str) -> str:
    """生成 `.vbs` 全文（**内容与时间无关**，保证幂等）。

    QA18-1：正文曾含「生成于 HH:MM」⇒ 跨分钟重装必判不一致并重写，
    幂等事实上不成立。生成时间只出现在 `--dry-run` 的打印里，**不进文件**。
    """
    cmd = f'"{exe}" {spec.command_line()}'
    # VBScript 里字符串中的双引号要写成两个；这里用 _vbs_str 统一处理，避免转义歧义。
    lines = [
        f"' {spec.name} —— 登录自启（issue #18，startup 后端，零提权）",
        "' 删除本文件即取消自启。",
        "' 用 pythonw.exe 隐藏窗口启动；工作目录固定为仓根。",
        "Option Explicit",
        "Dim sh, rc",
        "On Error Resume Next",
        "Set sh = CreateObject(\"WScript.Shell\")",
        "If Err.Number <> 0 Then WScript.Quit 1",
        f'sh.CurrentDirectory = "{spec.workdir}"',
        "If Err.Number <> 0 Then WScript.Quit 1",
        f"rc = sh.Run({_vbs_str(cmd)}, 0, False)",
        "If Err.Number <> 0 Then WScript.Quit 1",
        "WScript.Quit 0",
    ]
    return "\r\n".join(lines) + "\r\n"


def _vbs_str(s: str) -> str:
    """把任意字符串安全地放进 VBScript 的字符串字面量（引号加倍）。"""
    return '"' + s.replace('"', '""') + '"'


def read_vbs(path: Path) -> str:
    """按 `.vbs` 的实际编码读回（带 BOM 的 UTF-16LE 或 ANSI/GBK 兜底）。"""
    raw = path.read_bytes()
    if raw.startswith(VBS_BOM):
        return raw[2:].decode(VBS_ENCODING, "replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gbk", "replace")


def write_vbs(path: Path, text: str) -> None:
    """原子写（写 .tmp → fsync → replace）。

    QA18-2：上一版没有 try/finally，`os.replace` 失败（目标只读／同名目录）时
    会遗留 1KB 左右的 `.vbs.tmp`，与「失败不留半写文件」的承诺不符。
    现在失败路径**一定**清理 tmp；成功路径 tmp 已被 replace 移走（unlink 幂等）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".vbs.tmp")
    data = VBS_BOM + text.encode(VBS_ENCODING)
    try:
        with tmp.open("wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)      # 成功时已不存在；失败时清残file
        except OSError:
            pass


# --------------------------------------------------------------------- 回读校验
def verify_vbs(spec: Spec, exe: str) -> list[str]:
    """读回 `.vbs` 并逐项比对；返回失败项（空＝全过）。"""
    path = spec.startup_file
    if not path.is_file():
        return [f"启动文件不存在：{path}"]
    try:
        text = read_vbs(path)
    except OSError as exc:
        return [f"读不回：{type(exc).__name__}: {exc}"]
    fails: list[str] = []
    if str(spec.workdir) not in text:
        fails.append(f"工作目录不符：期望含 {spec.workdir!r}")
    if str(spec.script) not in text:
        fails.append(f"脚本路径不符：期望含 {spec.script!r}")
    if Path(exe).name not in text:
        fails.append(f"解释器不符：期望含 {Path(exe).name!r}")
    for a in spec.args:
        if a not in text:
            fails.append(f"参数缺失：{a!r}")
    return fails


def diff_summary(old: str, new: str) -> list[str]:
    """给「覆盖」时的变更点摘要（只报有差异的行，最多 6 条）。"""
    o = old.splitlines()
    n = new.splitlines()
    out: list[str] = []
    for i in range(max(len(o), len(n))):
        a = o[i] if i < len(o) else "<无>"
        b = n[i] if i < len(n) else "<无>"
        if a != b:
            out.append(f"行{i + 1}: - {a.strip()[:70]}  + {b.strip()[:70]}")
        if len(out) >= 6:
            break
    return out
