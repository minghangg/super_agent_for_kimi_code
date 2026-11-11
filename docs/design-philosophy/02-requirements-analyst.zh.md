[English](02-requirements-analyst.en.md) | [中文](02-requirements-analyst.zh.md)

# 02 · Requirements Analyst：需求预审与澄清

> 系列：设计哲学（design-philosophy）｜[01 核心架构](01-core-architecture.zh.md) ｜ 02 需求分析员 ｜ [03 Agent 间通信](03-agent-communication.zh.md) ｜ [04 面向人的 Reporter](04-human-reporter.zh.md) ｜ [05 产物回收站](05-artifact-recycle-bin.zh.md)｜ [06 Token 经济学](06-token-economics.zh.md) ｜ [07 永续上下文](07-perpetual-context.zh.md)
> 版本：v3 补充 · 2026-11-07
> 约束：非官方、单用户、多项目、资源有限、无重型数据库。
> 性质：设计建议，不授权修改章程、创建角色会话、开启 Web 服务或实施开发。

## 一、要解决的问题

用户需要与 supervisor 反复对话，supervisor 还要为理解需求去调研仓库。结果是：

- 多轮澄清占掉 supervisor 的上下文；
- 项目事实、用户偏好和历史决定堆积在主会话里；
- supervisor 换血时，需求背景要重新交接一遍；
- 模糊需求可能过早进入 PM 派单，造成返工；
- 用户得读长对话，而不是集中回答少量关键问题。

因此建议在 supervisor 与项目 PM 之间增加一个**长线、高思考的 Requirements Analyst（需求分析员）**，专门负责需求预审、快速探查、模糊点识别和澄清材料生成。

## 二、角色定位与核心边界

### 1. 职责

1. 预审 supervisor 对用户意图的理解。
2. 快速、只读地探查相关仓库、Issue 和既有事实。
3. 挖掘真正影响执行的模糊地带。
4. 判断是否需要用户回答，避免为了「更完整」而过度提问。
5. 生成结构化澄清报告和本地 HTML 问答页。
6. 分析用户答案，形成更清晰、可建单的需求包。
7. 维护可回溯的长期事实，提升后续需求判断的准确性。

### 2. 不做

- 不直接联系用户，只与 supervisor 交互。
- 不建单、不关单、不派 worker/QA、不发布。
- 不掌握 supervisor 的生产权限。
- 不修改项目代码、规则或凭据。
- 不替项目 PM 拆分开发 Assignment。
- 不替 QA 判断实现质量。
- 不把未验证的推断写成长期事实。

### 3. 与既有角色的差异

```text
Analyst     真正要做什么，哪些模糊点必须澄清
supervisor  是否批准、如何授权、是否建单、跨项目裁决
项目 PM     已定需求怎样拆成开发和验收任务
scout       按需只读取证，不负责需求澄清结论
QA          方案或实现是否满足验收
```

Analyst 不是第二个 supervisor，也不是新的项目 PM。

## 三、修订后的角色图

```text
                         用户
                          ▲
                          │ 只回答少量关键问题
                          │ 通过 supervisor 转交 HTML 报告
                          │
                    supervisor
              需求理解、授权、裁决、建单
                     ▲          │
                     │          │ 稳定需求链接／短指令
                     │          ▼
                     │   Requirements Analyst
                     │   长线、高思考、只读探查
                     │   预审／澄清／需求定稿建议
                     │          │
                     │          ▼
                     │     本地需求记录与报告
                     │
                     │ READY ＋ 清晰需求包链接
                     │
              supervisor 审阅、建单
                          │
                          ▼
                      项目 PM
                          │
                          ▼
                      worker / QA
```

用户仍然只面对 supervisor；Analyst 不产生第二条用户沟通渠道。

## 四、完整处理流程

```text
用户提出需求
   ↓
supervisor 写最小需求记录
   ↓
只把内部链接发给 Analyst
   ↓
Analyst 读取记录 ＋ 快速只读探查
   ↓
判断是否存在影响执行的模糊地带
   ├─ 无：返回 READY ＋ 清晰需求包链接
   │        ↓
   │     supervisor 审阅、裁决、建单、交项目 PM
   │
   └─ 有：生成 HTML 澄清报告
             ↓
          supervisor 将报告交给用户
             ↓
          用户集中回答
             ↓
          答案写回同一需求记录
             ↓
          Analyst 复审并返回最终需求包链接
             ↓
          supervisor 审阅、裁决、建单、交项目 PM
```

这一流程把 supervisor 从逐项追问和仓库调研中释放出来。

## 五、supervisor 与 Analyst 的快速通信

### 1. supervisor → Analyst

只需一个稳定内部引用：

```text
req_id: REQ-20261107-003
record: state/requirements/REQ-20261107-003/request.json
action: review | recheck
project_hint: repo-a
```

「这次不做什么」也要一起给：范围外的东西写进记录，比事后解释便宜。

### 2. Analyst → supervisor

```text
req_id: REQ-20261107-003
status: READY | QUESTIONS | BLOCKED
package_ref: state/requirements/REQ-20261107-003/analysis.json
report_ref: state/requirements/REQ-20261107-003/report.html   # 仅 QUESTIONS 时
open_questions: 2
```

附一句「为什么需要问」，但不要在信封里复述正文。

### 3. 状态定义

```text
received      已接收，尚未完成预审
investigating 正在只读取证
questions     等待用户回答
ready         已形成可审阅需求包
blocked       无法完成，需要 supervisor 处置
closed        已建单或明确不再处理
```

Analyst 只改变需求记录状态，不改变 Issue 阶段。

## 六、长线角色与事实记忆

### 1. 为什么需要长线角色

需求判断依赖累积知识：

- 用户过去如何描述某类需求；
- 项目的既有约束和术语；
- 哪些模糊点已经澄清；
- 哪些决定不能反复询问；
- 哪些仓库、模块和边界与当前需求相关。

因此 Analyst 应有长期身份，但不能依赖永不轮换的会话：

> **长期事实落在外部记录，长线会话只负责当前理解和判断。**

### 2. 三层事实

| 层次 | 内容 | 使用方式 |
|---|---|---|
| 本次需求事实 | 仓库位置、相关模块、现有行为、用户原话 | 当前需求使用 |
| 已确认长期事实 | 用户拍板的术语、偏好、项目边界 | 同项目后续需求复用 |
| 临时假设 | 尚未确认的推断 | 必须验证或提问，不得直接建单 |

长期事实示例：

```json
{
  "fact": "该项目禁止自动发布",
  "source": "issuecomment-123",
  "confirmed_at": "2026-11-07T12:00:00+08:00",
  "scope": "repo-a"
}
```

规则：

- 每条事实必须有来源和适用范围。
- 新决定覆盖旧决定时保留历史引用。
- 按项目和需求 id 精确读取，不引入向量数据库。
- Analyst 换血时只读长期规则摘要＋当前需求，不读全部历史。
- 事实摘要和原文分离；原文仍在 Issue 或项目证据中。

## 七、存储设计：JSON 即可

每个需求一个目录：

```text
state/requirements/
├── index.json
└── REQ-20261107-003/
    ├── request.json
    ├── facts.json
    ├── analysis.json
    ├── questions.json
    ├── answers.json
    └── report.html
```

| 文件 | 内容 |
|---|---|
| `request.json` | 用户原话、supervisor 转述、项目提示、时间、状态 |
| `facts.json` | 已确认仓库事实及证据引用 |
| `analysis.json` | 清晰需求包、模糊点、建议与风险 |
| `questions.json` | 问题、选项、是否必须回答 |
| `answers.json` | 用户答案与提交时间 |
| `report.html` | 从结构化数据生成的静态报告 |
| `index.json` | id、状态、项目、更新时间、链接 |

写入要求：

- 单写者；
- 临时文件＋原子替换；
- 索引只保存短元数据；
- 不保存密钥、无关个人数据或完整日志；
- 可以追加有界 `events.jsonl` 记录状态变化，但不必为每次修改做复杂事件溯源。

### 1. `questions.json` 最小结构

```json
{
  "req_id": "REQ-20261107-003",
  "revision": 1,
  "questions": [
    {
      "id": "q1",
      "required": true,
      "question": "本次是否允许修改生产数据？",
      "kind": "single",
      "options": ["不允许", "仅限指定测试记录"],
      "recommended": "不允许",
      "why": "该答案决定权限边界"
    }
  ]
}
```

### 2. 清晰需求包最小结构

```json
{
  "req_id": "REQ-20261107-003",
  "status": "ready",
  "project": "repo-a",
  "objective": "一句话目标",
  "scope": ["要做的"],
  "out_of_scope": ["不做的"],
  "acceptance": ["可判定验收"],
  "constraints": ["权限、红线和资源限制"],
  "evidence_refs": ["issuecomment-123"],
  "open_questions": []
}
```

## 八、HTML 澄清报告与 Web 边界

### 1. 为什么值得一个页面

- 每题展示推荐选项和影响；
- 支持单选、多选、短文本补充；
- 标明必答与可采用默认值；
- 一次集中提交；
- 支持修订后重新生成；
- 桌面或手机浏览器都能用。

它首先是**本地需求工作台**，不是新的项目管理后台。

### 2. 首版不需要 WSS

```text
GET  /requirements/<req_id>
POST /requirements/<req_id>/answers
```

- 页面读取当前问题；
- 用户提交答案；
- 答案写回同一需求；
- supervisor 收到结果通知；
- 页面刷新显示最新状态。

只有确实需要「分析进度实时显示、报告完成自动刷新、多页面同步」时才增加 WSS。不要为了实时效果让首版背上额外协议和前端复杂度。

### 3. 安全边界

- 只监听 `127.0.0.1`。
- 使用随机、不可猜测的 req id 或短期访问令牌。
- 只允许写入需求答案。
- 网页不能执行 shell、修改规则、改变权限或读取凭据。
- 报告、答案、日志均不得含密钥。
- 本地服务启停与计划任务仍需实施期授权。
- HTML 由可信模板和结构化数据生成；用户文本必须转义，避免注入。

## 九、何时应该提问

Analyst 不能因为「还能问更多」就打扰用户。只有答案会实质改变以下内容时才问：

1. 改哪个项目或模块；
2. 做什么、不做什么；
3. 验收标准；
4. 数据、权限或安全边界；
5. 是否允许外部写操作；
6. 方案之间存在明显成本/风险取舍。

示例：

- 项目已有按钮颜色规范：直接采用，不问。
- 是否直接修改生产数据：必须问。
- 「希望优化体验」：须转成具体页面、行为和验收，否则不能直接建单。
- 有多个低风险等价实现：给出推荐，不必全部交给用户选择。

没有影响执行的模糊点时，直接 `READY`。目标是最少提问，而不是产生最多问题。

## 十、清晰需求包应包含什么

返回给 supervisor 的完整材料至少包含：

1. 用户原始目标；
2. supervisor 当前理解；
3. 已验证的仓库事实及引用；
4. 建议项目与影响范围；
5. 一句话目标；
6. 要做／不做；
7. 验收标准与必要反例；
8. 权限、资源和安全边界；
9. 用户已经回答的决定；
10. 未解决风险或阻塞项。

supervisor 审阅后才建单；项目 PM 收到的是已批准 Issue，而不是 Analyst 的探索过程。

## 十一、实施顺序

### 第一步：无 Web 的最小闭环

- JSON 需求记录；
- supervisor 与 Analyst 的 req id 通信；
- 静态 HTML 报告；
- 用户答案人工回写；
- `READY / QUESTIONS / BLOCKED` 状态。

验收：

- supervisor 不复制长需求正文；
- Analyst 只读探查，不改代码；
- 无模糊点直接 READY；
- 有模糊点只生成真正影响执行的问题；
- 需求全过程可从 req id 复原。

### 第二步：本地网页答题

- GET 页面、POST 答案；
- 同一 req id 回写；
- 原子保存；用 revision 防止旧页面覆盖新答案；
- 提交后通知 supervisor。

验收：

- 页面仅回环可访问；
- 只能写当前需求答案；
- 重复提交不产生相互覆盖的多个需求；
- 无密钥、无命令执行入口。

### 第三步：长期事实与换血恢复

- 按项目整理已确认事实；
- 事实带来源和 scope；
- 新决定覆盖旧决定；
- Analyst 换血只读长期摘要＋当前需求。

验收：

- 不重复询问已经确认的问题；
- 临时假设不进入长期事实；
- 新会话可准确恢复当前需求；
- 历史事实增长不导致每次需求都全量注入上下文。

### 第四步：按需增加实时能力

只有真实使用证明刷新/通知不够时才加 WSS。不能让实时功能先于需求闭环成为开发重点。

## 十二、与核心架构的衔接

- 本补充不改变 supervisor／PM／引擎的职责划分。
- Requirements Analyst 位于需求进入 PM 之前，是 supervisor 的需求助理。
- 复用核心架构的「链接＋短摘要＋按需引用」通信原则。
- 复用事件总线的通知/控制基础设施，但需求数据存储独立于会话事件缓存。
- 文档治理生效后，未建单需求的 JSON/HTML 属于明确的运行态例外；建单后长期事实以 Issue 和项目记录为准，避免过程文档重新堆积。
- 新角色上线需要另行修改章程和授权；本文不产生角色创建权限。

## 十三、主要风险与控制

| 风险 | 控制 |
|---|---|
| Analyst 变成第二个 supervisor | 只与 supervisor 交互，无建单/派单/发布权 |
| 过度提问增加用户负担 | 只问影响目标、验收、权限和重大取舍的问题 |
| 长线会话上下文膨胀 | 事实外置、按需读取、阈值换血 |
| 临时猜测污染长期记忆 | 事实带来源；假设未确认不入长期记录 |
| 本地网页成为控制入口 | 仅允许答案写入，不开放 shell/配置/权限 |
| 过程文件再次堆积 | 只保留当前需求与长期事实；建单后归 Issue |
| 与 scout/PM 重复工作 | Analyst 负责需求判断，scout 只取证，PM 负责任务拆分 |

## 结论

建议新增该角色，定位为 **supervisor 的长线需求预审与澄清助理**。

最合适的设计不是再增加一个聊天窗口，而是：

> **长线高思考 Analyst ＋ 稳定需求 id ＋ 轻量 JSON 事实库 ＋ 集中回答的本地 HTML 报告。**

它能同时减少 supervisor 的调研、多轮追问和上下文消耗，又不改变现有 PM、worker、QA 的职责。首版应先完成无实时 Web 的需求闭环，再逐步增加网页答题和长期事实能力。
