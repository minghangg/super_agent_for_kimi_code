# 当前状态（事实页）

> 本页只记「现在到哪一步」，一句话一条，不写历史。方向性叙述见 `README.md` §现状与路线图；两者冲突时**以本页为准**。
> Issue 真源是本仓 `origin` 指向的 GitHub Issue；跨仓引用必须写全 `owner/repo#N`，裸 `#N` 只在本仓语境解释。里程碑写作 `M0` 与 `milestone 1`／`milestone 2`，避免隐私门禁把机器代号误判为私有机名。

## 已完成

- **M0 已完成**：引擎骨架与 observe 模式，`external_actions` 恒为 0（#19）。
- **milestone 1 已完成**：单 Issue 闭环，Thread × Assignment 状态机＋确定性快通道（#20）。
- **milestone 2 已完成**：QA 直通、`waiting_release`、关单对账四条件两轮，引擎只核对不关单（#21）。
- **Reporter 已完成**：只读首屏、三类动作桶、unknown 不读成 none（#49）。
- **#52 已实施**：`auth/` `rules/` `data/` 三层骨架落地；运行态默认根为 `data/`，旧根 `state/` 由 `choose_root()` 回退读取并 WARN（见 `AGENTS.md` §3）。
- **AT 模式已实现（#110）**：Issue 版本化需求包、精确批准、Assignment revision/hash 门禁、T1/T2 PM 路由、旧 outbox 失效、本地选项问卷和答案文档。操作见 `docs/at_mode.md`；公开默认 `at_mode=false`。

## 在办与待开工

- **#22 待开工**：M3 看门狗退役清单＋逐项回退手册；退役对象为 decide_watch 哨、monitor 巡查、pmw 轮询、reporter 心跳监护。

## 挂起

- **#36 挂起**：跨平台适配（launchd／systemd、POSIX pidlock、CI matrix），未开工。
- **#30 挂起**：Requirements Analyst 最小闭环（无 Web、不建会话），与引擎解耦可并行，等开工令。
- **#26 已关闭、内容并入 #22**：PM 总线唤醒协议的四类消息已被 milestone 1／milestone 2 吃掉，消息类型表转为 #22 的退役验收清单。
- **#23 已关闭、内容并入 #43**：M4 三档分级不存在、assist 零外部动作数据；「恒不自动清单」搬进 #43 当硬规则。
- **#43 挂起**：长期提醒单，不开发不验收，在所有在飞单关闭前保持 open。

## 已知缺腿清单

- **`dispatch_adapter` 的通用派单动词未全部实现**：`resolve`／`create-worker`／`create-qa`／`bind`／`send`／`fetch`／`archive`／`reconcile-close` 在通用 console 适配面仍有缺口，报 `unsupported_verb` 并非静默成功。与此同时，引擎 `_archive_one()` 已有直接 session API 回退，实际 outbox 也存在 archive 动作；不得再把「通用适配面缺动词」写成「引擎 archive 不可达」。
- **`reedkit/*.sh` 9 个脚本未迁移**：`atroute`／`rclose`／`rhandoff`／`rhouse`／`rissue`／`rpm`／`rpmlife`／`rrotate`／`rwatch`。其叙述性文本（`echo` 提示、`usage`、注释）需重写才过隐私门禁，属独立扩容，归后续单。本仓只复制了 `pmc.sh`／`pmd.sh` 两条链。

## 判据

- 回归：`pytest tests -q` 全绿；`python tools/privacy_gate.py` 必须 clean（rc=0）。
- 提交作者恒为 `safkc-bot <safkc-bot@users.noreply.github.com>`。
- 运行态永不入库：`state/` 与根目录回退产物均在 `.gitignore` 内。
