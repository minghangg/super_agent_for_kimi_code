# Design Philosophy · 设计哲学

Read in your language: every document ships as a Chinese/English pair. / 按语言选择：每篇都有中英两份。

This directory records the reasoning behind `super_agent_for_kimi_code` — not the API surface, but why the system is shaped the way it is. Each paper stands alone; together they form one argument.

本目录记录 `super_agent_for_kimi_code` 的设计理由：不是接口说明，而是「为什么长成这样」。每篇可独立阅读，合起来是一条完整的论证。

---

## The five papers · 五篇

| # | Paper · 篇目 | Chinese | English |
|---|---|---|---|
| 01 | **Core architecture** — roles, memory outsourcing, events, staged delivery<br>**核心架构** — 角色、记忆外包、事件、分阶段交付 | [中文](01-core-architecture.zh.md) | [English](01-core-architecture.en.md) |
| 02 | **Requirements Analyst** — pre-review, clarification, long-lived factual memory<br>**需求分析员** — 预审、澄清、长线事实记忆 | [中文](02-requirements-analyst.zh.md) | [English](02-requirements-analyst.en.md) |
| 03 | **Simplified agent-to-agent communication** — short instructions plus references<br>**Agent 间简化通信** — 短指令加引用 | [中文](03-agent-communication.zh.md) | [English](03-agent-communication.en.md) |
| 04 | **Human-facing Reporter** — an honest, read-only status view<br>**面向人的 Reporter** — 诚实、只读的状态视图 | [中文](04-human-reporter.zh.md) | [English](04-human-reporter.en.md) |
| 05 | **Artifact recycle bin** — tiered retention and safe reclaim<br>**产物回收站** — 分级保留与安全回收 | [中文](05-artifact-recycle-bin.zh.md) | [English](05-artifact-recycle-bin.en.md) |

---

## The one-sentence version · 一句话版本

Four problems, four answers, one architecture:

- **The PM has become a courier.** Deterministic transitions belong to a program, not an LLM.
- **The supervisor burns attention watching progress.** Events advance Issues; the human handles exceptions and acceptance.
- **Rotating a session loses memory.** Long-term facts live in Issues; a rotation reads a short brief plus precise references.
- **Vague requirements reach the wrong place too early.** A pre-review role turns them into something decidable before any Issue is opened.

四个问题，四个答案，一套架构：

- **PM 变成了搬运工。** 确定性流转该由程序做，不该由 LLM 做。
- **supervisor 的注意力被盯进度消耗掉。** 事件推进 Issue，人只处理例外与验收。
- **换血即失忆。** 长期事实住在 Issue 里，换血只读短 brief 加精确引用。
- **模糊需求过早流到了不该去的地方。** 一个预审角色在建单之前把它变成可判定的东西。

---

## How to read them · 阅读顺序

If you only have five minutes, read 01 and stop. It contains the whole shape.

If you are evaluating whether this design is sound, read 01 → 03 → 05. Those three carry the load: the architecture, the communication discipline, and the retention boundary.

If you care about the human side — what the operator sees and asks — read 04 last. It is the least load-bearing and the easiest to change.

只有五分钟：读 01 就够，形全部在里面。

在评估这个设计站不站得住：按 01 → 03 → 05 读。这三篇承重：架构、通信纪律、保留边界。

关心人的一侧——操作者看到什么、问什么——把 04 放最后。它最不承重，也最容易改。

---

## A note on roles · 关于角色名

Throughout these papers, `supervisor`, `PM`, `worker` and `QA` are **role names, not fixed people or sessions**. The names are configurable; the default for the top-level role is `supervisor`. If you run this with different vocabulary, nothing in the design changes.

以上各篇里的 `supervisor`、`PM`、`worker`、`QA` 都是**角色名，不是固定的人或会话**。角色名可配置，顶层角色的默认名是 `supervisor`。换一套叫法，设计本身不变。

---

## Scope · 范围

These are **design papers**, not implementation contracts. They argue for a shape and record the reasoning that produced it. Where they name files or commands, those names describe intent; the authoritative behaviour lives in the code and its tests.

Status: the engine described in 01 is being built in stages (0 through 4). Sections 02–05 are designs ahead of their implementation, kept here so that the reasoning is available before the code exists.

这些是**设计文稿**，不是实现契约。它们论证形状、记录推理过程。文中出现的文件名与命令描述的是意图；权威行为以代码与其测试为准。

现状：01 描述的引擎正在分阶段（0–4）落地。02–05 是先于实现的设计，放在这里是为了让推理在代码存在之前就可查。

---

See also: the repository [README](../../README.md) for what exists today and how to run it.
另见：仓库根 [README](../../README.md)，说明当前有什么、怎么跑。
