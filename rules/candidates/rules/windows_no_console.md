# Windows 后台服务「黑窗一闪」硬口径（模板源）

> 适用：Windows 上以**无控制台**方式长期运行的服务（`pythonw.exe` / `wscript` / 计划任务 / NSSM 服务），
> 以及由它派生的子进程。**Linux 不适用**（无控制台窗口概念）。
> 首次定稿：2026-10-28（cdy2Api 网关实测：改动前 15 次可见控制台 → 改动后 0 次）。

## 一、根因（一句话）

无控制台的父进程起**控制台子系统**子进程（`git`／`ssh`／`schtasks`／`python`／`cmd`／`pip`）时，
Windows 会给子进程分配一个新控制台并**显示窗口** ⇒ 用户看到黑框一闪。

`DETACHED_PROCESS|CREATE_NO_WINDOW` 只作用于**该进程自身**，**不传递给**它后续起的子进程。

## 二、四条硬规则

| # | 规则 | 判据 |
|---|---|---|
| R1 | `app/`／业务包内**禁止**裸调 `subprocess.run/Popen/call/check_output/system`、`os.system/os.popen/os.spawn*` | AST 静态门禁扫出即红 |
| R2 | 统一走一个薄封装（如 `winproc.run()`），内部加 `creationflags=CREATE_NO_WINDOW \| 已有标志` 与 `STARTUPINFO(SW_HIDE)`；非 Windows 退化为普通调用 | 单测断言 `creationflags == 0x08000000` |
| R3 | 启动器只用两种：`wscript` 跑 VBS 且 `sh.Run(..., 0, False)`；或 `pythonw.exe` + `DETACHED_PROCESS\|CREATE_NO_WINDOW` | 启动器里出现 `cmd /c` 直起 `.bat`／`python.exe` 即违规 |
| R4 | 计划任务 Action **不得**是 `cmd.exe`／`.bat`／`.py`；只能是 `wscript.exe //nologo <vbs>`（隐藏窗口参数 0） | `Get-ScheduledTask` 列出 Action 逐条核 |

## 三、验收方法（关键：测法不对会假绿）

- **必须**在「父进程无控制台」条件下测。从 Git Bash／Windows Terminal 直接起 `pythonw.exe`，
  子进程会继承终端的伪控制台 ⇒ 测出来恒为 0，**无效**。
- 正确姿势：用 `DETACHED_PROCESS|CREATE_NO_WINDOW` 拉 `pythonw.exe` 作被测父进程。
- 判据：每 50 ms 枚举子进程名下 `ConsoleWindowClass`／`PseudoConsoleWindow`／`CASCADIA_HOSTING_WINDOW_CLASS`
  窗口的 `IsWindowVisible`，只统计**本次新出现**的句柄。
- 合格线：**改动前 > 0、改动后 == 0**；两条都要有数字，只报「看起来不闪」不算。
- 参考实现：`<私有仓>` 的 `.kimi/tools/console_probe.py` 与 `.kimi/report/no_flash_window_20261028-1109.md`。

## 四、升级／新增功能时的自检清单（3 分钟）

1. `grep -rn "subprocess\.\|os\.system\|os\.popen\|os\.spawn" app/ <业务包>/` —— 命中即违规（测试目录除外）。
2. `pytest tests/test_winproc.py -q`（或等价 AST 门禁）—— 必须绿。
3. 新增计划任务后：`Get-ScheduledTask` 看 Action 是否仍为 `wscript … vbs`。
4. 动了子进程逻辑（超时／参数／重试）就重跑上面的「无控制台 + 可见窗口计数」，给前后数字。

## 五、常见误判

- **「子进程用了 `capture_output=True` 就不会弹窗」** —— 错。重定向管道不影响控制台分配。
- **「`start /b` 就不弹」** —— 错。`/b` 只是不新建窗口组，父进程无控制台时仍会分配。
- **`creationflags` 直接赋值** —— 会覆盖调用方已有的 `CREATE_NEW_PROCESS_GROUP` 等标志；必须**按位或**。
