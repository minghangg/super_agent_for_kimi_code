[English](README.en.md) | [中文](README.md)

# super_agent_for_kimi_code

> **给「一个人同时跑好几个 AI 编码项目」的人：一个调度器 ＋ 一层外置记忆。**
> 你需要的不是更大的上下文窗口，而是让不该待在上下文里的东西搬出去。
> [Kimi Code CLI](https://github.com/MoonshotAI/kimi-code) 的周边工具，非官方，与 Moonshot AI 无关。

---

## 问题，一段话说完

你是一个人，手上好几个项目。每个项目都有 AI 会话在写代码、有会话在复核、有会话在决定下一步做什么。它能跑 —— 直到某天跑不动了。

决定下一步的那个会话，一天到晚在把结果从这儿搬到那儿。你自己最上层的那个会话，被项目细节塞满，不得不重启。而重启之后，除了你能重新打一遍的东西，它全忘了。

上限不在模型。**上限是一个人同一时间能装进脑子里的量。**

## 这个项目做什么

两件事，只有两件：

1. **程序搬纸。** worker 交付之后，一个确定性的状态机 —— 纯代码，不经过模型 —— 决定下一步通知谁。只有真的需要判断的地方才问模型。
2. **记忆住在 Issue 里，不住在会话里。** 需求、任务书、交付、复核结论、决定，都记在能搜到的地方。重启的会话读一份短简报加几个引用就能接着干。

仓库里其他一切，都是从这两条推出来的。

### 两个值得争一争的主张

**主张一 —— 你不需要更大的上下文窗口。** 窗口溢出不是因为太小，是因为**装了不该装的东西**。做大它，既推迟溢出，又让每一轮更贵 —— 窗口里的东西每一轮都要重读。把项目细节搬出去，只留意图、授权、未决事项。见 [07](docs/design-philosophy/07-perpetual-context.zh.md)。

**主张二 —— 单个 agent 不需要聪明。** 蚂蚁脑子极小、没有地图、没有计划，可蚁群能跨地形集体迁徙、能把觅食力量分配到多个食物源。进化优化的是**蚁群**，不是蚂蚁。让每个 agent 保持窄，把智慧放进互动规则和持久记录。见 [07 §3.2](docs/design-philosophy/07-perpetual-context.zh.md)。

## 60 秒看懂

| | |
|---|---|
| **角色要薄** | 项目 PM 领单、判两个开关、原样转述结论、五句话向上报告。它不做调研。 |
| **流转是代码** | 「这次交付现在该送复核」是一条规则，不是一次判断。状态机免费执行，而且不会累。 |
| **记忆是记录** | Issue 是长期真源。本地索引是可重建的缓存 —— 删掉它会自己长回来。 |
| **换血是常规操作** | 因为没有任何有价值的东西只活在会话里，扔掉一个会话很便宜。这才是长期工作能成立的原因。 |

```text
  you ──► supervisor  （长命，上下文很小：意图 · 授权 · 仲裁）
              │
     ┌────────┼────────┐
     ▼        ▼        ▼
   PM (A)   PM (B)   PM (C)      每个项目一个
     │        │        │
     ▼        ▼        ▼
 worker→QA worker→QA worker→QA   一次性、隔离、干完即归档
     │        │        │
     └────────┼────────┘
              ▼
        Issue ＝ 持久记录
        需求 · 任务书 · 交付 · 复核结论 · 决定

  引擎（纯代码）在这些框之间搬东西；
  它从不判断活干得好不好。
```

## 想试

Python 3.10+ 之外没有依赖。不用装包，不需要任何凭据。

```bash
git clone <this repo> && cd super_agent_for_kimi_code

python -m super_agent.cli doctor
#   体检：进程 · 事件总线 · 数据源 · 配置 · outbox · 游标
#   有问题就老实报 FAIL —— 这就是它的用处

python -m super_agent.cli once --mode observe
#   跑一轮，只计算。observe 按设计不执行任何外部动作。
#   预期：mode=observe issues=0 ... external_actions=0

python -m super_agent.cli reporter --out status.html
#   生成自包含状态页：「现在什么需要我」，浏览器打开即可

python -m unittest discover -s tests -v      # 30 个文件，每条反例都在里面
```

想看真数据，把数据源设成 `fixture`（本地 JSON）或 `live`（只读 REST，token 走环境变量）。默认是 `none`，什么都不会离开你的机器。

**前五分钟你会看到：** 一份诚实的体检报告、一轮什么都不碰的计算、一个只回答「什么需要人」的状态页。形状就这些，其余都是展开。

## 想懂思想，不想读代码

七篇短文，每篇论证一个设计决定。只有十分钟就读 01 和 07。

| # | 篇目 | 一句话 |
|---|---|---|
| [01](docs/design-philosophy/01-core-architecture.zh.md) | **核心架构** | 角色、记忆外包、事件、分阶段交付 —— 整个形状。 |
| [02](docs/design-philosophy/02-requirements-analyst.zh.md) | **需求分析员** | 模糊需求来得太早。预审先把它变成可判定的。 |
| [03](docs/design-philosophy/03-agent-communication.zh.md) | **Agent 间通信** | 短指令加引用胜过长解释，因为长解释每轮都被重读。 |
| [04](docs/design-philosophy/04-human-reporter.zh.md) | **面向人的 Reporter** | 状态视图应当只读、对未知诚实、只回答一个问题。 |
| [05](docs/design-philosophy/05-artifact-recycle-bin.zh.md) | **产物回收站** | 干活时可用、收尾后可回收、全程可追溯。 |
| [06](docs/design-philosophy/06-token-economics.zh.md) | **Token 经济学** | 贵的 token 是重复的那一份。四种开销，各自可消。 |
| [07](docs/design-philosophy/07-perpetual-context.zh.md) | **永续上下文** | 追更大的窗口是错的方向。蚂蚁早解决过。 |

每篇都有中英两份。[索引](docs/design-philosophy/README.md)。

## 给工程师：真正重要的那一页

```text
super_agent/
  cli.py               once · run · doctor · status · brief · replay · reporter
  engine.py            状态机：按固定规则分类，最多派发一个动作
  assignments.py       Thread × Assignment 生命周期（queued → working → waiting_review → …）
  store.py             index / events / outbox / health —— 除记录外全部可重建
  github_source.py     只读采集 Issue 评论
  dispatch_adapter.py  建、绑、归档 —— 与会话打交道的那层薄壳
  brief.py · ctx_gate.py · reporter.py    恢复包 · 上下文闸 · 状态页
tests/                 30 个文件，标准库 unittest，每条反例都写成测试
config/                白名单：允许碰哪个项目、哪张单、哪个动作
```

读代码前值得知道的三件事：

- **零依赖。** 只用标准库，`pyproject.toml` 里一个依赖都没有。
- **observe 是默认值。** `--mode observe` 不执行任何外部动作，计数器被断言恒为 0。你不明确放行，它什么都不会做。
- **两张白名单，两个不同的问题。** 一张说「这个引擎能碰哪些单」，另一张说「它能做哪些动作」。分开检查，按这个顺序。

设计与实际完成度：[docs/STATUS.md](docs/STATUS.md)。约定与隐私门禁：[AGENTS.md](AGENTS.md)。

## 关于数字

这个项目只定性描述效果，不承诺百分比。我们唯一用到的数字 —— **大约三次尝试有一次能不改就过** —— 是我们在自己任务构成上的观察，只作为量级参考，不是保证。换模型、换代码库它就会动。我们明确**不能**声称什么，见 [06 · Token 经济学](docs/design-philosophy/06-token-economics.zh.md) §5。

## 许可

MIT，见 [LICENSE](LICENSE)。
