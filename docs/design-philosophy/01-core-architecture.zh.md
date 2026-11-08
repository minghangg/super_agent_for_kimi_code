[English](01-core-architecture.en.md) | [中文](01-core-architecture.zh.md)

# 01 · 核心架构

> 系列：设计哲学（design-philosophy）｜01 核心架构 ｜ [02 需求分析员](02-requirements-analyst.zh.md) ｜ [03 Agent 间通信](03-agent-communication.zh.md) ｜ [04 面向人的 Reporter](04-human-reporter.zh.md) ｜ [05 产物回收站](05-artifact-recycle-bin.zh.md)
> 版本：v3 · 2026-11-07
> 约束：非官方项目、单人维护多项目、资源有限、不使用复杂数据库。
> 性质：设计基线，不授权创建仓库、修改规则或直接实施开发。

## 执行摘要

本项目不是「把某个官方 Router 搬进个人电脑」，而是为**单人多项目团队**补上两件事：

1. **任务自动化**：让事件推进 Issue，减少 PM 的搬运工作和 supervisor 盯进度的消耗。
2. **记忆外包**：把项目过程移出 supervisor 会话；换血时只恢复最小全局事实，避免核心上下文交接失忆。

角色必须分开：

- **supervisor**（角色名可配置，默认叫 `supervisor`）：用户意图、授权、跨项目裁决、发布关单。
- **项目 PM**：一个项目一个专职 PM，负责项目内拆需求、派 worker/QA、日常路由。
- **无 LLM 引擎**：事件、状态机、回收、轻量索引、恢复包；不做语义决策。
- **worker / QA**：隔离执行与独立验证，原有质量纪律不变。

存储坚持：

```text
GitHub Issue：长期过程事实
index.json：跨项目运行索引与待办引用
events.jsonl：有界事件记录
outbox.jsonl：外部动作状态机
health.json：健康状态
```

不上数据库、不上向量检索、不引消息队列、不设云端 Router、不做多租户，也不造「超级 PM」人格。

## 一、角色与信息流

### 1. supervisor：跨项目授权与裁决中心

通常使用高思考/高能力模型，是用户对齐点和跨项目唯一的 supervisor。

职责：

1. 把用户语言翻译为 Agent 可执行的项目目标、边界、优先级和验收口径。
2. 决定项目归属，按既有政策建 GitHub Issue。
3. 跨项目资源和优先级仲裁。
4. PM↔QA、需求冲突、两轮不过、异常升级的裁决。
5. 建单/关单、发布、生产写、授权、紧急事故等指定动作。
6. 掌握「哪些凭据可用于哪个动作」的规则，通过受控工具使用凭据。

不做：

- 不逐条盯 worker，不读项目全量日志，不替 PM 写任务书。
- 不把项目历史、接口细节、长任务书常驻自己的上下文。
- 不把密钥值写进 prompt、索引、Issue、outbox 或交接材料。
- 不因为权限高就收回项目 PM 的日常执行权。

**supervisor ≠ PM；supervisor 不是另一个项目 Router。**

### 2. `pm-<project>`：项目内日常 Router

职责：

1. 接收 supervisor 的一句需求＋边界。
2. 读本项目 Issue 与规则，拆任务、写任务书、派 worker/QA。
3. 决定方案复核和实现验收两个开关。
4. 跟踪本项目 Assignment、收口、退回、续派。
5. 维护项目紧凑运行摘要。
6. 只把跨项目、授权、冲突、发布等必要事项上升 supervisor。

不做：

- 不掌握 supervisor 的全局授权。
- 不替 supervisor 建单/关单、发版、生产写。
- 不处理其他项目任务，不读无关项目上下文。
- 不把 worker 原始日志和完整评论链原样转给 supervisor。
- 不写实现、不替 QA 判质量。

### 3. 无 LLM 执行引擎

职责：

- Trigger 收集、去重、映射 Issue。
- 确定性 Fast Path、状态机、冷却、重试、outbox。
- 调派单/bind/归档的薄适配接口。
- 维护可重建的跨项目索引。
- 生成 supervisor 全局 brief、PM 项目 brief、Issue 恢复包。
- 只把符合升级条件的事项通知当前登记的 PM/supervisor。

它不是第三个 PM，不是 supervisor，不直接持有或使用生产凭据，不做需求拆解、语义裁决或质量验收。

### 4. 信息流

```text
                           用户
                            │
                            ▼
                ┌───────────────────────┐
                │ supervisor            │
                │ 需求翻译/授权/建单/裁决 │
                │ 发布/关单/跨项目优先级 │
                └───────────┬───────────┘
                            │ 一句需求＋边界
             ┌──────────────┼──────────────┐
             ▼              ▼              ▼
       pm-project-A    pm-project-B    pm-super_agent...
       项目内 Router    项目内 Router    本仓专职 PM
             │              │              │
             └──────────────┼──────────────┘
                            ▼
                  无 LLM 执行引擎
        ┌─────────────────────────────────┐
        │ Trigger / Fast Path / 状态机     │
        │ outbox / 冷却 / bind / 回收      │
        │ 全局索引 / 摘要与恢复包          │
        └─────────────────────────────────┘
                     │                 │
                     ▼                 ▼
               worker / QA         GitHub Issue
               独立执行会话         全文过程档案
```

- 用户意图：用户 → supervisor → 对应项目 PM → worker/QA。
- 日常执行：事件 → 引擎 → 项目 PM。
- 需要裁决：PM → supervisor；不跨 PM 传递，也不直接惊动用户。
- worker/QA 写 Issue；PM 只向 supervisor 送必要结论。

## 二、与 Super-agent 架构的对应与差异

| Super-agent 概念 | 本项目对应 | 必须保留的差异 |
|---|---|---|
| Router | 项目 PM；跨项目语义决策由 supervisor | 不让一个长上下文 Router 管所有项目细节 |
| Worker | 隔离 worker/QA 会话 | QA 独立、分级验证不变 |
| Trigger | WSS、GitHub、定时器、配置事件 | 外部事件不天然获得执行权限 |
| Fast / Slow Path | 程序规则 → 项目 PM → supervisor | 增加跨项目升级层 |
| Thread | 一个 Issue 的长期任务身份 | 过程全文在 Issue，本地索引可重建 |
| Inline / Spill | 短摘要＋评论/文件引用 | 上下文成本优先 |
| 持久状态 | index/outbox/health ＋ Issue | 无数据库、无云端记忆 |
| Watchdog | 引擎健康与任务异常检测 | 不重复制造多层 busy 轮询 |

官方形态可以让 Router 承担更多记忆和拆需求；我们把项目内这项工作留给专职 PM。supervisor 只保留跨项目必要信息，避免单核心上下文成为团队的单点故障。

## 三、记忆与上下文设计

### 1. 三类信息分离

| 类型 | 保存位置 | supervisor 默认是否读取 |
|---|---|---|
| 需求、任务书、交付、QA、裁决 | GitHub Issue 正文/评论 | 否，按需按 id 取 |
| 当前运行、Assignment、游标、等待事项 | index / outbox | 只看全局 brief |
| 角色规则、授权边界、恢复入口 | 章程/手册 ＋ 短索引 | 启动只读必要集 |

凭据继续由认证配置和受控工具管理，不进入任何一层记忆内容。

### 2. `index.json` 示例

```json
{
  "schema_version": 1,
  "projects": {
    "repo-a": {
      "pm_session_ref": "state/sessions.json#pm-repo-a",
      "active_threads": 2,
      "waiting_supervisor": []
    }
  },
  "threads": {
    "owner/repo-a#12": {
      "stage": "working",
      "owner": "worker",
      "assignment_id": "asg_12_worker_1",
      "waiting_on": "worker_report",
      "latest_decision_ref": "issuecomment-123",
      "last_event_seq": 789,
      "retry_count": 0,
      "next_retry_at": null,
      "updated_at": "..."
    }
  }
}
```

另保存最后错误、worker/QA Assignment 引用、归档墓碑、必要短摘要。不保存长历史、任务书全文、日志全文或生产隐私。

### 3. 最小记忆包命令

```bash
python -m super_agent brief --for supervisor
python -m super_agent brief --for pm --project <name>
python -m super_agent context --issue <repo>#<n>
```

**supervisor brief 固定只含：**

1. 角色/授权恢复入口；
2. 当前项目及 PM 引用；
3. 等 supervisor 裁决的事项；
4. 等用户的事项；
5. 最近跨项目决定的引用；
6. 系统健康。

输出契约：

- 固定小节、行数和字节上限，目标一屏内。
- 无待裁决事项时不罗列所有正常项目的历史。
- 超限优先保留待用户、待裁决、发布/紧急异常；正常项压成计数。
- 被截去的内容必须给出「还有 N 项及取回命令」，不能无声丢失。
- 这是**有损但可回溯**的摘要，不宣称无损记忆。

**PM brief** 只含一个项目的活动 Thread、等待事项和必要引用。

**Issue context** 才取该 Issue 当前目标、最近决定、相关评论 id 和验收要求。

### 4. supervisor 换血完整流程

```text
旧 supervisor 到阈值
→ 暂停新的跨项目决策（已有 worker/QA 不中断）
→ 引擎生成全局 brief 与未决事项清单
→ 建立新 supervisor，读角色规则＋brief
→ 新 supervisor 自报上下文占用
→ 先切换会话登记键
→ 新 supervisor 逐项核对待裁决/待用户/发布事项
→ 引擎只向新 sid 投递
→ 停掉指向旧 sid 的投喂者/旧监控
→ 归档旧会话并回读
→ 恢复跨项目决策
```

- 不要求旧 supervisor 复述项目历史。
- 不把各 PM 交接档全部塞给新 supervisor。
- **归档不等于停止投喂**；必须切断旧 sid 的事件订阅和控制目标。
- 换血未完成时，worker/QA 可继续，新增 supervisor 决策保持排队。
- 恢复后必须能回答：哪个项目、哪张 Issue、等什么、谁有权决定、原文在哪。

PM 换血同理，但只冻结该项目的新派单，读单项目 brief；不得影响其他项目。

## 四、跨项目升级路由

统一升级短包：

```text
升级类型：授权 / 需求 / 冲突 / 发布 / 紧急故障
项目与 Issue：<repo>#<n>
当前阶段：<stage>
需要 supervisor 决定：<一个问题>
推荐选项与影响：<最多三项>
依据引用：<issue comment id / evidence id>
```

规则：

- PM 能解决的不上送；纯执行事件不进入 supervisor。
- 涉及密钥、生产、关单、发布、跨项目资源、权限扩大才进入 supervisor。
- 引擎只排序，不替 supervisor 裁决。
- supervisor 决定写回对应 Issue；引擎更新索引，PM 只收到本项目决定。
- 相同 Thread 后续事件优先回原 PM/负责人，不重复让 supervisor 拆需求。
- 引擎必须每次都从当前会话登记解析 PM/supervisor sid，不能长期使用启动时缓存。

## 五、仓库与协作治理（阶段 0 前置）

1. 用户明确批准建仓；本文不构成建仓指令。
2. 在项目配置中登记本仓，GitHub 通道验证通过后启用。
3. 必须新建专职 `pm-super_agent_for_kimi_code`，不能借调其他项目的 PM。该 PM 管本仓开发，不是全局 supervisor。
4. 本仓章程限定 worker 只能改本仓；工作台规则、凭据和其他项目代码不在改动范围。
5. 默认只允许本地 commit；无默认推送权。首次推送、发布须用户明确指令。
6. 本仓自己的章程、清单、依赖和测试约定在阶段 0 建立，不复制整套私有工作台规则。
7. 核验事件总线与文档治理两个前置接口的真实交付；未完成时以单文件 mtime 轮询兼容。
8. 私有工作台里已在飞的代码必须保持独立，不得借本仓建设覆盖他人工作。

## 六、建工权限与责任边界

### 1. 默认 `pm_created`

```text
引擎准备 Assignment 草案
→ queued / waiting_person
→ 项目 PM 创建 worker/QA
→ 登记 bind
→ 引擎取得 sid 并接续监控/回收
```

草案只含 Issue、角色、动作、上下文引用、固定画像和验收级别。此模式仍有去重、状态跟踪、后续唤醒和恢复收益，但不越权代建。

### 2. 显式预批后 `engine_preapproved`

固定画像代建属于规则调整，必须由用户或 supervisor 明确批准并写入项目规则后才算已批。

窄授权：

- 仅限 allowlist 中的项目、Issue 类型和动作。
- worker 固定画像。
- QA 画像按当时现行章程。
- cwd、角色、命名、测试级别、任务书评论由 Assignment 预定。
- 以 PM 派单适配层执行，并保持留痕与 Assignment 审计。
- 超画像、超范围、新模型、新权限回项目 PM，必要时升 supervisor。
- 配置关闭后立即回到 `pm_created`，已有 Assignment 不清空。

这是项目 PM 的执行权委托，不是 supervisor 的权力下放。引擎不因此获得建单、关单、发布、生产或密钥权限。

## 七、轻量存储与配置

### 1. 文件布局

```text
state/
├── index.json          # 跨项目运行索引＋待 supervisor 短引用
├── events.jsonl        # 有界事件日志
├── outbox.jsonl        # 外部动作状态机
├── health.json         # 心跳与依赖健康
└── engine.pid          # 单实例锁

config/
└── allowlist.json      # 可入库、无密钥、热更新
```

### 2. 读写规则

- 启动读 index，后续内存修改、临时文件写入、原子替换。
- 单进程、单写者，不引入数据库锁。
- events 有界轮转（例如 5–10 MiB、最多两份）。
- index 可从 Issue、事件总线、bind 和 outbox 重建。
- supervisor brief 由 index 现算，不维护第二份易漂移的全局长记忆。
- 归档保留墓碑：时间、原因、交付引用，而不仅是删除 sid。
- state 全部运行态不入 git；配置可入库但无密钥。

### 3. allowlist 示例

```json
{
  "schema_version": 1,
  "defaults": {"mode": "observe"},
  "projects": {
    "super_agent_for_kimi_code": {
      "repo": "<owner>/super_agent_for_kimi_code",
      "mode": "observe",
      "creation_mode": "pm_created",
      "issues": [1],
      "actions": ["collect", "reconcile"],
      "max_active_writers": 1
    }
  }
}
```

- 未列 repo、Issue、action 默认拒绝。
- 新 Issue 默认 observe，不自动继承写权限。
- `engine_preapproved` 同时需要配置和规则授权。
- 热更新先完整解析、校验路径/角色/动作/上限；无效配置保留上一份并告警。
- 热更新只影响后续事件，不回溯执行历史动作。
- 配置不含 token；token 由受控工具处理。

## 八、事件模型与状态机

Trigger 类型：

```text
github.comment_added
session.turn_finished
session.interaction_pending
session.task_finished
session.failed
timer.retry
config.changed
```

Trigger 至少包含：

```json
{
  "event_id": "稳定去重ID",
  "thread": "owner/repo#issue",
  "source": "github|wss|timer|config",
  "payload_ref": "评论ID、事件序号或配置版本"
}
```

采集：

- 会话侧：事件总线 long-poll。
- GitHub：按间隔查询活动 Issue 新评论，保存 comment id 游标。
- 文件事件：能复用则复用，不另写 watch 生产者。

不建 webhook，不暴露公网端口，不追求秒级 GitHub 响应。

### Fast Path

```text
去重
→ allowlist
→ 当前 Thread 负责人
→ 状态合法性
→ 模型/网络冷却与重试时刻
→ 执行动作或只更新索引
```

| 事件 | 动作 |
|---|---|
| worker turn 结束 | 先核运行中任务，再收交付，更新 `waiting_review` |
| 完整交付标记 | 项目 PM 核三件，按现行规则送 QA |
| QA 退回 | 回原 worker，保留 Thread 上下文 |
| QA 通过 | `waiting_release`，通知 supervisor |
| pending interaction | 仅按已有授权处理，否则升级 |
| 429/网络失败 | 冷却/退避，不重复创建 worker |
| 无实质变化 | 只记录，不通知任何人 |

### Slow Path

项目内判断交本项目 PM；只有跨项目、授权、需求冲突、发布等交 supervisor。

PM 输入只给 Issue 当前阶段、最新事件摘要、一个待判断问题和评论链接，不重灌评论链。

### 两种身份

- **Thread**：`owner/repo#issue`，跨会话持续到 Issue 关闭。
- **Assignment**：`asg_<issue>_<role>_<序号>`，一次执行。

Assignment 记录 sid、输入评论、开始时间、状态、重试、交付评论、归档墓碑。

阶段：

```text
queued / working / waiting_review / waiting_qa /
waiting_person / waiting_release / blocked / completed
```

`busy` 只描述会话，不充当任务状态；后台任务未结束时 Assignment 仍为 `working`。

## 九、薄派单适配层

新增 `dispatch_adapter.py`，作为引擎与工作台工具的唯一接口：

```python
choose_profile(assignment) -> Profile | Cooling
prepare_assignment(input) -> Assignment
create_worker(assignment) -> SessionRef       # 需预批
create_qa(assignment) -> SessionRef           # 需预批
bind_assignment(assignment) -> BindRef
send(assignment, text_ref) -> ControlResult
fetch(assignment) -> ReportRef
archive(assignment, reason) -> ArchiveResult
reconcile_close(thread) -> CloseStatus
resolve_role_session(project, role) -> SessionRef
```

要求：

- 优先复用工作台 Python 函数；子进程薄封装也必须返回结构化 dict / dataclass。
- 不靠正则解析人类 CLI 输出。
- 返回 `ok / code / session_id / prompt_id / source / error`。

## 十、outbox：防漏也防重

```text
prepared ──► in_flight ──► done
                 │
                 └──► uncertain ──► reconcile ──► done | waiting_person
```

- `prepared` 在外部调用**之前**落盘：崩在调用前，这条可以安全重放。
- 进入 `in_flight` 即表示「可能已经落地」：重启**绝不重发**，转 `uncertain` 进对账队列。
- 对账按记录的 `sid` / `prompt_id` / `comment_id` 核对后，标 `done` 或 `waiting_person`。
- `waiting_person` 是**未完成**状态：留在持久文件里（轮转不丢），单独计数，不进对账队列。

未完成项存在 `outbox_state.json`（原子写），`outbox.jsonl` 只留状态变更历史。因此轮转只丢历史，不丢未完成动作。

## 十一、模型冷却与退避

### 1. 唯一事实源

冷却状态只有一处记录，不被多个消费者各自推算。

### 2. 派单前双检

派单前同时检查：模型是否在冷却，以及是否已到重试时刻。任一不满足即不派。

### 3. 冷却处理

- 冷却期内不创建新会话，只更新索引。
- 429 与网络失败走退避，不重复建工、不重复评论。
- 冷却结束由定时器触发一次重扫，而不是靠忙轮询。

## 十二、归档与关单一致性

### 1. 创建与 bind 序列

```text
草案 → 创建会话 → 拿到 sid → bind → 记 Assignment
```

任一步失败都能从索引看出停在哪一步；创建成功但 bind 未完成时，恢复只补 bind。

### 2. 引擎主动归档

- 归档前核对：没有活动 Assignment、没有未收交付。
- 归档后保留墓碑（时间、原因、交付引用）。
- **归档不等于停止投喂**：必须同时切断事件订阅与控制目标。

### 3. supervisor 关单

- worker 已提前归档 → 不重复归档，也不伪装成功。
- 关单时 QA 仍活动 → 关单工具归档后由引擎复核一致性。
- 绑定清理失败或归档失败 → 进入 `close_reconcile`，不标 `completed`。
- 已关闭 Issue 的迟到事件 → 只记审计，不重新开工。

## 十三、常驻进程三件套与降级

采用标准库为主＋轻量异步：一个进程、一个事件循环、一个状态写者，默认串行外部动作，首版每项目一个活动写者。

### 1. 单实例与无窗自启

- 原子 pid 锁＋命令行/启动时间校验，stale 才接管。
- 计划任务条目（登录触发、无窗口、有限重启）**只是草案**，注册属实施期独立授权动作。
- 动作只含解释器、脚本和公开参数，不含 token。
- 事件总线/GitHub 失败由引擎指数退避。

### 2. 健康检查

不新开端口，`health.json` 原子更新，`doctor` 交叉验证 pid：

```json
{
  "pid": 1234,
  "mode": "observe",
  "last_loop_at": "...",
  "last_wss_seq": 100,
  "last_github_poll_at": "...",
  "last_successful_action_at": "...",
  "active_assignments": 1,
  "degraded": false,
  "last_error": ""
}
```

故障要能区分：进程死、事件总线不可用、GitHub 不可用、配置无效、outbox 卡住、回收失败。

### 3. 降级

- 事件总线不可用：沿用既有门面，不另造直连。
- GitHub 不可用：继续收会话事件，但禁止伪造评论/关单状态。
- 状态写失败：停止外部动作，保留只读观察并告警。
- 「总线下线」与「项目 observe」是两层不同的回退。

### 4. 命令

```bash
python -m super_agent run
python -m super_agent status
python -m super_agent once
python -m super_agent replay <issue>
python -m super_agent doctor
python -m super_agent brief --for supervisor
python -m super_agent brief --for pm --project <name>
python -m super_agent context --issue <repo>#<n>
```

- `once`：单轮处理。
- `status`：只显示需要人或 PM 的事项。
- `replay`：只重建索引，不执行动作。
- `doctor`：检查依赖、配置、游标、冷却、bind、残留 Assignment。

## 十四、安全模式

### observe

收集事件、计算准备动作，只记录，不派单、不评论。

### assist

自动内部状态、查询、回收、Thread 唤醒；默认仍由 PM 建会话，Issue 评论走现有角色工具。

### auto

显式预批后才开放：

- 固定画像代建；
- worker 完成后收交付；
- QA 退回归原 worker；
- 429/网络退避；
- 会话轮换与测试会话归档；
- 低风险阶段提醒。

**始终不自动**：发布、关单、生产写、扩大权限、删除历史、处理新需求。

## 十五、分阶段交付

每阶段都必须：先备份受影响的工作台工具/制度文件、只暂存本阶段文件、保留独立 commit、不推送。测试只用一次性 Issue 和探针会话，不得用在册助手作为靶子。

### 阶段 0：角色边界、记忆基线与只读骨架

**前置条件**

- 核验事件总线客户端、控制接口、降级的真实交付。
- 核验评论全文、临时文件、运行态边界治理的真实交付。
- 文件事件源可用则接入；否则允许单文件 mtime 兼容。
- 用户批准本仓治理；登记项目和专职 PM；明确推送边界。
- 写 allowlist，默认 `observe` / `pm_created`。
- 固化 supervisor/PM/引擎职责、升级短包和记忆输出契约。
- 代建预批未批准不阻塞阶段 0。

**交付物**

- 独立仓库骨架和最小模块。
- allowlist 热更新。
- index / events / outbox / health。
- 事件总线与 GitHub 只读采集。
- `once` / `status` / `doctor` / `replay` / `brief` / `context`。
- 单实例锁。
- 自启任务定义草案，**不注册**。

**正向验收**

- observe 连续运行 24 小时。
- 重启不丢游标，同事件不重复处理。
- 能从 supervisor brief 定位全部待裁决事项原文。
- PM brief 只包含指定项目。
- replay 能重建索引且外部动作数为 0。

**反例**

- 非法/越界配置被拒绝，上一份配置继续有效。
- brief 不含长评论、其他项目正文、密钥。
- 不创建会话、不写 GitHub、不开端口。
- 重复事件、过期事件不触发动作。

**回退点**

- 删除本次新建的自启草案（若有）、停止本次进程即可；未改现有链路。
- 保留 index/events 供排查，不删已有证据。

### 阶段 1：单项目闭环、建工权限与记忆恢复

**实现**

- Thread ＋ Assignment、Fast Path、dispatch adapter。
- outbox 对账、bind、归档墓碑。
- 模型冷却双检。
- 自启安装与健康实测（须当步授权）。
- 先以 `pm_created` 验证；预批落地后才测 `engine_preapproved`。

**主链路**

```text
测试 Issue
→ Assignment
→ 合规创建 worker
→ 短任务与完整交付标记
→ waiting_review
→ 归档回读与墓碑
```

**中断与反例**

- 创建后、bind 前杀引擎：恢复只补 bind，不重复建 worker。
- 评论结果未知：先查评论/台账，不重复评论。
- 发送超时：先查 prompt_id/会话历史，不重复发送。
- 唯一模型冷却：创建数为 0。
- allowlist 外 Issue：创建数为 0。
- QA/worker 未预批时：引擎代建调用必须被拒绝。
- 已归档 sid 再出事件：不再投喂。
- 所有探针最终 `archived=true`、`busy=false`、无运行中任务。

**记忆恢复验收**

- 模拟 supervisor 换血：新会话只读角色规则＋brief＋指定 Issue 引用即可继续待办。
- 不读其他项目历史，未决事项恢复率 100%。
- 模拟 PM 换血只影响该项目，其他项目继续。
- 新旧 sid 切换后，旧 sid 收到的新投喂数为 0。

**回退点**

- 将项目 `creation_mode` 改回 `pm_created` / `observe`。
- 停用本次计划任务前报创建项和 pid，只处置本次实例。
- 已创建测试会话完成归档；outbox 不确定项保留待人工对账，不强制重放。

### 阶段 2：QA、项目 PM 闭环与关单对账

**实现**

- 按现行规则送 QA；QA 退回归原 worker；通过进 `waiting_release`。
- Issue 新评论送当前负责人。
- 项目内 Slow Path 给项目 PM；跨项目/授权才上 supervisor。
- 关单一致性核对。

**正向验收**

- 完整走通开发 → QA → 退回 → 修复 → 通过。
- 全过程可由 Issue 评论复原。
- 本地无新增过程文档堆积。
- 项目 PM 能独立接续，supervisor 只收到必要裁决。
- QA 意见原文保留，不被引擎或 PM 改写。

**关单反例**

- worker 已提前归档：关单不重复归档、不报错伪装成功。
- QA 关单时仍活动：关单工具归档后引擎复核一致。
- 绑定清理失败/归档失败：进入 `close_reconcile`，不标 `completed`。
- 无 QA 评论：引擎不得绕过门禁或自行关单。
- 已 closed Issue 的迟到事件：只记审计，不重新开工。

**回退点**

- 只回退阶段 2 的自动流转，保留阶段 1 的 worker 闭环。
- QA/关单恢复现有 PM/supervisor 路径，不回滚已稳定的阶段 0/1。

### 阶段 3：多项目扩展与看门狗替换

**实施顺序**

1. 一次只新增一个项目，默认 observe。
2. 验证专职 PM 隔离、升级路由、跨项目优先级。
3. 比较旧看门狗与新引擎的动作。
4. 统计重复通知、漏通知、误报、上下文增长。
5. 稳定后只停用被覆盖的旧看门狗，不一次全删。

**退出门槛**

- 重复动作 0、测试会话泄漏 0、严重误报 0。
- 新机制连续一周稳定。
- 无跨项目上下文串线。
- supervisor brief 只随待办/异常增长，不随项目历史增长。
- 人工只处理真正需要人的事项。

**回退点**

- 新项目退回 observe，旧项目不受影响。
- 被暂停的旧看门狗可按清单逐个恢复；不删除旧实现。

### 阶段 4：有限主动工作

**开放顺序**

```text
observe → assist（内部状态/回收/唤醒）→ 预批后的少量 auto
```

**验收**

- 主动任务必须有书面规则与 allowlist。
- observe 阶段只记录「准备做什么」。
- 主动任务不得自动发布、关单、扩大权限或处理新需求。
- 外部不可信输入不得修改授权、allowlist 或执行环境。

**回退点**

- 一键回 observe；保留任务状态与审计，不删除历史。

## 十六、模块与测试边界

```text
bus_client.py        # 事件总线客户端
github_source.py     # GitHub 新评论采集
store.py             # index / events / outbox / health
policy.py            # allowlist、画像、冷却、角色动作权限
dispatch_adapter.py  # 派单、bind、归档、关单对账
memory.py            # 全局/项目 brief、Issue 恢复包
engine.py            # Fast Path、状态机、重试、升级路由
cli.py               # run / once / status / doctor / replay / brief / context
```

测试用标准库 `unittest` ＋ 本地假 HTTP 服务；真实链路只用一次性 Issue 和探针会话，结束归档并核对 bind/关单状态。

必测反例：

- supervisor brief 不含无关项目正文、密钥或长评论。
- 一个 PM 的 Slow Path 不送到另一个 PM。
- 引擎重启、supervisor 换血、PM 换血互不混淆。
- brief 超限时优先保留待裁决/待用户/紧急事项，且报告省略数量。
- 凭据可用但不出现在 prompt/index/events/outbox/Issue。
- 旧 sid 归档后不被事件或监控重新投喂。
- index 损坏时停止外部动作，不自动覆盖唯一可恢复线索。

## 十七、成功指标

### 上下文与角色

- supervisor 每次换血需要读取的 tokens / 文档数。
- 换血后未决事项恢复率（目标 100%）。
- 因 supervisor 上下文不足导致的重复调研次数。
- 项目 PM 自行闭环、无需 supervisor 介入的比例。
- supervisor 通知中真正需要裁决的比例。
- 新增项目时 supervisor 常驻上下文增量。

### 工程与成本

- 每张 Issue 人工跟进次数。
- PM 重复搬运 worker/QA 结果次数。
- worker 完成到下一步的延迟。
- 重复派单、重复评论、误报数量。
- 孤儿会话、错误 bind、未归档测试会话数量。
- 冷却中误派次数（目标恒为 0）。
- 每周需要人阅读的监控消息数。
- 单任务模型调用和 token 成本。
- 重启后人工恢复次数。
- 引擎停机发现与恢复时间。

## 最终形态

```text
supervisor：用户意图、授权、跨项目裁决、发布关单
项目 PM：项目内需求拆解与日常 Router
执行引擎：事件、状态机、回收、轻量记忆与恢复包
worker/QA：隔离执行与独立验证
GitHub Issue：长期过程事实
本地 JSON/JSONL：可重建运行索引
```

推荐顺序：

```text
授权与只读骨架
→ pm_created 单项目闭环＋记忆恢复
→ 经显式预批开放固定画像代建
→ QA/关单闭环
→ 多项目与看门狗替换
→ 有限自动化
```

最关键的五条纪律：

1. supervisor 不做 PM；项目 PM 不共享 supervisor 的全局权限。
2. supervisor 的记忆靠短摘要＋精确引用恢复，不靠长会话硬撑。
3. 未获代建预批不代建；模型冷却不派；不确定外部动作先查证。
4. 归档和关单必须对账；旧 sid 不得再被投喂。
5. 一切私有名词、本机路径与凭据不得进入本仓。
