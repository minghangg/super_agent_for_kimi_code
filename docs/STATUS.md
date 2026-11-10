# 当前状态（事实页）

> 本页只记「现在到哪一步」，一句话一条，不写历史。方向性叙述见 `README.md` §现状与路线图；两者冲突时**以本页为准**。
> 两处措辞约定：①单号（`#N`）指私有工作台仓的 issue，本仓自身只有 `#1` 一张打穿测试靶；②里程碑写作 `M0` 与 `milestone 1`／`milestone 2`，因为隐私门禁的 `\bM[12]\b` 会把后两者当成私有机器码（backlog A5 已记该误报）。

## 已完成

- **M0 已完成**：引擎骨架与 observe 模式，`external_actions` 恒为 0（#19）。
- **milestone 1 已完成**：单 Issue 闭环，Thread × Assignment 状态机＋确定性快通道（#20）。
- **milestone 2 已完成**：QA 直通、`waiting_release`、关单对账四条件两轮，引擎只核对不关单（#21）。
- **Reporter 已完成**：只读首屏、三类动作桶、unknown 不读成 none（#49）。

## 在办与待开工

- **#22 待开工**：M3 看门狗退役清单＋逐项回退手册；退役对象为 decide_watch 哨、monitor 巡查、pmw 轮询、reporter 心跳监护。
- **#52 迁移期占位**：`auth/` `rules/` `data/` 三层骨架近期不实施；`data/` 就是运行态，当前由 `state/` 承担（见 `AGENTS.md` §3）。

## 挂起

- **#36 挂起**：跨平台适配（launchd／systemd、POSIX pidlock、CI matrix），未开工。
- **#30 挂起**：Requirements Analyst 最小闭环（无 Web、不建会话），与引擎解耦可并行，等开工令。
- **#26 已关闭、内容并入 #22**：PM 总线唤醒协议的四类消息已被 milestone 1／milestone 2 吃掉，消息类型表转为 #22 的退役验收清单。
- **#23 已关闭、内容并入 #43**：M4 三档分级不存在、assist 零外部动作数据；「恒不自动清单」搬进 #43 当硬规则。
- **#43 挂起**：长期提醒单，不开发不验收，在所有在飞单关闭前保持 open。

## 判据

- 回归：`pytest tests -q` 全绿；`python tools/privacy_gate.py` 必须 clean（rc=0）。
- 提交作者恒为 `safkc-bot <safkc-bot@users.noreply.github.com>`。
- 运行态永不入库：`state/` 与根目录回退产物均在 `.gitignore` 内。
