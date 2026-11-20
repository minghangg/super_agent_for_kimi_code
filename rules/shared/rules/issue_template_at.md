# AT 需求草稿与需求包模板

> supervisor 创建草稿 Issue；AT 在 Issue 评论发布版本化需求包。AT 不再另写一份待粘贴的技术方案正文。

## 1. 草稿 Issue 正文

```markdown
# <用户目标>

> 用户原话：<原样记录>
> 项目：<项目>
> 阶段：AT 需求澄清；未批准不得开发。

## 关联
<来源 Issue/cid；无则写无>
```

建单默认 `stage:on-hold`。supervisor 绑定 AT 并下令开工时才摘挂起。

## 2. AT 需求包字段

- `requirement_id`：`owner/repo#issue`。
- `revision`：从 1 开始；语义变化必须递增。
- `objective`：一句话结果。
- `context_refs`：事实来源 Issue/cid/文件坐标。
- `scope` / `out_of_scope` / `constraints`：范围与约束。
- `acceptance` / `negative_tests`：均不得为空。
- `risk_tier`：T1 或 T2。
- `review_policy`：方案 QA、实现 QA、worker 模式和测试级别。
- `open_questions`：READY 时必须为空。
- `interaction`：可选单选/多选选项与补充设置。

## 3. 发布与批准

AT 使用 `python -m super_agent.requirements publish` 发布 questions 或 READY。supervisor 使用 `requirements approve` 批准精确 package cid、revision 和 hash。

READY 不是批准；批准不是 QA 通过。PM 只取 `requirements packet`，不另写六段任务书。

## 4. 用户问卷

一轮只问一个问题。用户通过本地链接选择选项、补充说明并确认。答案全文写本地 Markdown，AT 只收一行路径通知。旧 revision 页面必须拒绝提交。

## 5. 反例

- open_questions 非空却 READY：拒绝。
- 未批准创建 worker/QA：拒绝。
- T2 降为同会话方案开发或跳过方案 QA：拒绝。
- 同 revision 改变语义：拒绝，必须递增 revision。
- 旧版本交付、QA、verdict、问卷或 outbox：不得推进当前状态。
