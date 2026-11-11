[English](03-agent-communication.en.md) | [中文](03-agent-communication.zh.md)

# 03 · Agent 间简化通信

> 系列：设计哲学（design-philosophy）｜[01 核心架构](01-core-architecture.zh.md) ｜ [02 需求分析员](02-requirements-analyst.zh.md) ｜ 03 Agent 间通信 ｜ [04 面向人的 Reporter](04-human-reporter.zh.md) ｜ [05 产物回收站](05-artifact-recycle-bin.zh.md)｜ [06 Token 经济学](06-token-economics.zh.md) ｜ [07 永续上下文](07-perpetual-context.zh.md)
> 版本：2026-11-07
> 定位：减少 Agent 间的长文本转述、多轮确认和重复调研，同时保留需求与验收语义。
> 性质：设计报告，不授权修改规则、开发代码或改变角色权限。

## 一、目标

把现在的流程：

```text
发长任务说明
→ 对方复述理解
→ 长篇汇报
→ 上层再整理转发
```

改成：

```text
发送短指令＋对象引用
→ 接收方按引用读取必要材料
→ 返回结构化状态＋结果引用
```

目标是减少：

- token 与上下文占用；
- 转述失真；
- 重复调研；
- 多轮确认；
- supervisor 和 PM 的事务性工作。

需要说清一点：**可以取消的是通信过程中的客套、复述和长摘要，不是所有自然语言。**
需求、任务书、QA 结论仍然必须以 LLM 能理解的文本存在，只是不再通过聊天层层搬运。

## 二、基本原则

### 1. 通信简单化 ≠ 语义简单化

以下内容仍然需要自然语言或结构化语义：

- 用户目标；
- 需求与边界；
- 验收条件；
- 代码实现任务；
- QA 退回理由；
- 裁决依据。

变化在于：这些内容存储在 Issue、需求记录或交付记录中，通信时只传引用。

### 2. 状态变化不经过 LLM 聊天

例如：

- worker 完成；
- QA 通过；
- 任务等待用户；
- 会话需要轮换；
- 模型正在冷却。

这些应由事件和状态机处理，不必给 PM 或 supervisor 发一段「我已完成，请查收」。

### 3. 一次通信只表达一个意图

推荐指令类型：

```text
requirement.review
assignment.dispatch
assignment.resume
review.request
decision.request
session.rotate
assignment.stop
```

不要在一条消息里混合「分析需求＋改代码＋跑测试＋决定是否发布」。

## 三、统一通信格式

所有角色共用一个轻量信封：

```json
{
  "v": 1,
  "message_id": "msg_...",
  "type": "assignment.dispatch",
  "thread": "owner/repo#12",
  "assignment": "asg_12_worker_1",
  "from": "pm-repo-a",
  "to": "worker",
  "payload_ref": "issuecomment-456",
  "requested_action": "execute",
  "created_at": "..."
}
```

结果只返回：

```json
{
  "message_id": "msg_...",
  "status": "done",
  "result_ref": "issuecomment-789"
}
```

异常：

```json
{
  "status": "blocked",
  "reason_code": "requirement_ambiguous",
  "detail_ref": "requirement-REQ-003"
}
```

其中：

- `thread` 定位长期任务；
- `assignment` 定位一次执行；
- `payload_ref` 指向具体内容；
- `reason_code` 供程序路由；
- 长文本不进入信封。

## 四、各角色的通信方式

| 通信 | 指令内容 | 返回内容 |
|---|---|---|
| supervisor → Analyst | 需求 id ＋ 预审指令 | READY/QUESTIONS/BLOCKED ＋ 需求链接 |
| supervisor → PM | Issue 链接 ＋ 项目目标引用 | 已接单/需要裁决 ＋ Issue 链接 |
| PM → worker | Assignment id ＋ 任务书引用 | 状态 ＋ 交付评论引用 |
| PM → QA | Review id ＋ 待审对象引用 | 通过/退回 ＋ QA 结论引用 |
| PM → supervisor | Decision id ＋ 一个问题 | 裁决结果 ＋ 决定引用 |
| 引擎 → 任意角色 | 类型化事件 ＋ 状态引用 | 通常不需要自然语言回复 |
| worker/QA → Issue | 结构化交付正文 | GitHub comment id |

## 五、通信拓扑仍需保留

简化协议不意味着所有 Agent 可以自由互聊。

```text
supervisor ↔ Analyst
supervisor ↔ 项目 PM
项目 PM ↔ worker/QA
worker/QA → Issue
引擎 → 当前角色
```

继续保持：

- Analyst 不直接联系用户；
- worker 不直接要求用户决策；
- QA 结论不被 PM 改写；
- PM 不处理其他项目；
- supervisor 不接收日常执行流水。

## 六、可靠性设计

短指令同样需要完整工程保障：

- `message_id` 去重；
- outbox 防止重启漏发；
- 超时先对账，不能盲目重发；
- 接收方必须是当前登记的会话；
- 归档 sid 不再投递；
- 权限由程序和角色配置校验；
- payload 读取受项目范围限制；
- 所有决定和结果必须能回溯原文。

最重要的区分：

```text
accepted   已接收
done       已有结果
completed  业务验收通过
```

`accepted` 不能当作任务完成。

## 七、上下文控制

「只发链接」只有在读取也有边界时才真的省上下文。

每次读取采用：

```text
当前目标
＋ 当前阶段
＋ 本次动作
＋ 必要约束
＋ 少量相关引用
```

禁止收到链接后默认读取：

- 全部 Issue 评论；
- 整个项目历史；
- 所有 PM 状态；
- 全部需求事实。

## 八、与系列其他组件的关系

- 复用核心架构的 Thread、Assignment、Issue 与 outbox 模型。
- Analyst 使用 `requirement.review` 和需求 id，不另建长协议。
- Reporter 消费这些状态和引用，但不通过 Reporter 派发控制动作。
- 事件总线是传输基础设施，不是语义决策层。

## 九、推荐实施顺序

1. 先统一 `thread / assignment / payload_ref`。
2. 改造 supervisor ↔ Analyst。
3. 改造 PM → worker/QA。
4. 接入引擎通知与 outbox。
5. 最后再减少例行文字汇报。
6. 用 token、转述次数、重复调研和误投率验收。

## 十、验收指标

- 每次派发和结果返回的正文长度；
- supervisor/PM 的重复转述次数；
- 因转述导致的语义偏差数量；
- 因旧 sid 或归档会话造成的误投数量；
- 超时后重复建工、重复评论数量；
- 按需读取是否真正避免了全量评论与历史加载。
