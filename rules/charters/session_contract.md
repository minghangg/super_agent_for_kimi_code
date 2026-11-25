# Session Contract（全会话契约）

本文件由 `dispatch_cli` 在新建会话 warmup 时注入。所有 supervisor、AT、PM、worker、QA 和长命助手都适用。

## 1. 思考与输出

- 思考使用英文，短句、一词一义、主动语态。
- 输出使用用户语言，一句一个意思，不写空话。
- 答复给结论、数字、路径和下一步；长证据放文档或 Issue，不重复粘贴。

## 2. 写码前检查顺序

按序采用第一个可行方案：必要性 → 代码库复用 → 标准库 → 原生特性 → 现有依赖 → 极简实现 → 手写。

不得因为熟悉某库就假设项目已经安装它。先读 manifest、lockfile 和现有 imports。

## 3. 一个真源，短令引用

- Issue 是需求、交付、QA 和裁决的长期真源。
- 本地 `requirements.json`、索引和投影都是可重建缓存。
- PM 不重写需求；派单只发 requirement_ref/hash、阶段和引用路径。
- 新能力与旧规则冲突时，同一单更新旧规则，禁止沉默并存。

### 3.1 派单令的形态（**唯一真源，别处不再重复**）

**一条派令 = 本地链接 ＋ 短说明。** 任务内容写进 Issue（或 `D:/temp_safkc/` 草稿），派令本身不复述。

```
<一句话目标>。详见 #<n>／D:/temp_safkc/<文件>。本次注意：<一句>。
```

**三条硬判据**：

1. **≤ 3 行**。超过说明在复述 Issue 内容。
2. **不出现**「验收」「边界」「反例」「背景」「目标」这类小节标题——它们属于 Issue 或草稿。
3. **必须有一个可点的坐标**：`#<issue>` 或 `D:/temp_safkc/<file>`。没有坐标的派令＝两个真源。

**为什么**（用户 2026-11-22 令「避免多处指引，就至少是本地链接加短说明」）：长令与 Issue 会漂移，执行者不知道照哪个；而且长令本身吃掉派单方的上下文，把它从「判断」挤成「传话」。

**判据**：`grep -c "验收\|边界\|反例"` 命中派令正文 ⇒ 违规，改写。

### 3.2 新增能力必须接上生产路径

新增能力必须在同一单内接入真实入口，并写清它的**运行条件**。只写文档、写测试、写函数都不算接上；没有调用方，或生产路径永远不满足它的运行条件，都不算交付。旧规则与新能力冲突时，同单修正旧规则并明确旧路径的停用或并存期限。

## 4. AT 分支共同协议

每张 Issue 只走 supervisor 选择的 `flow:at` 或 `flow:standard`。普通分支沿用既有流程，不创建需求包、不使用 AT 门禁或看门狗；分支不明时停止外部动作。引擎按标签分流，无标签存量单回退项目 `at_mode`；双标签冲突会停止执行并交 supervisor。分支切换会阻断未完成 Assignment，worker/QA 不得自行恢复。

共同流程：

```text
草稿 Issue → AT 澄清/READY → supervisor 批准精确 hash
→ PM 确定性路由 → worker → QA → supervisor 收口
```

- 未批准不得创建 worker/QA Assignment。
- Assignment、交付、QA 和 PM verdict 必须绑定同一 requirement_id/revision/hash。
- 新 revision 暂停旧 Assignment；旧结果与旧 outbox 不推进新版本。
- T2 不可为省 token 降级。
- 非 AT 项目保持既有流程，不伪造需求包。
- 需求包 fence 只认当前 Issue 的 AT 会话绑定；批准 fence 只认带随机尾的 supervisor 配置键或 supervisor_sid 反查到的随机尾键，无内置名称。sid 不豁免键名要求；未验证来源不得改变需求或批准状态。
- 只有 READY 且来源完整的包可被批准；dispatch 再次核双来源。

worker、QA、PM 的相关评论必须带：

```text
<!-- requirement: owner/repo#123 revision: 2 hash: sha256:... -->
```

从 packet 的 `requirement_marker` 原样复制。缺标记、多个标记、旧 hash 或跨 Assignment 引用都会被拒绝。

## 5. 用户与 AT 的问卷协议

- AT 一轮只问一个问题，优先提供选项，允许补充时显示文本框。
- 用户点击“确认并回答 AT”后才提交，不在选择变化时自动提交。
- 旧 revision 页面返回 410，不保存、不回传。
- 答案先保存到本地 Markdown，再通知当前 AT。
- AT 消息只有一行：Issue、revision 和答案文档路径；不发送 JSON 或答案全文。
- 回传失败时页面必须明说未回传，不能显示已送达。
- AT 换血后从最新绑定解析接收人，旧 sid 零投喂。

## 6. 只用自己的身份

评论 `--role` 只能是操作者自己，不冒用、不代发。GitHub token 可能共用，作者名不能证明角色。

新评论使用五段式：

```text
[who]-[do]-[notify]-[project]-[cid]
```

`who` 是完整会话键名。合法动作：完工、收口、退回、裁决、告警、派单、接单、交接要求、交接完成、交接就绪、已归档、调试。

私有 Issue 与评论允许完整键名，包括五段式首行和正文（用户 2026-11-23 令）；`keyname_scan --issue` 默认豁免且不发 GitHub 请求。公开仓文档仍禁止写入实例键名；公开 tracker 需要旧检查时显式使用 `--public-issue`。

- QA：通过用 `完工`，退回用 `退回`，首行还带 `fix_needed`。
- PM verdict：用 `裁决`，带 `verdict` 和精确 `delivery:<cid>`。
- 交付：用 `完工`，带自己的完整键名和需求版本标记。
- `调试`、`已归档`、`接单` 不制造完工通知。

```bash
python -m super_agent.issue_cli comment --role <自己> --project <项目> --issue <n> \
  --do <动作> --notify <接收方> --session-key <自己的键名> --body-file <文件>
```

**评论不等于送达。** `--notify` 只决定首行写谁，**不产生任何投递**：引擎的帧生成只认 QA 结论与 PM 裁决，supervisor 的五段式评论（`派单`／`告警`／`裁决` 等）**不生成帧**。要对方立即收到，必须另跑 `dispatch_cli --session <目标 sid>`。

判据：`ctx.py <目标键名>` 的 `busy=` 变 True，或 `events.jsonl` 出现对应 `task` 事件。**只看「评论已发出」不算送达。** supervisor 侧细则见 `supervisor.md` §2.1。

## 7. 会话创建与复用

手工创建新会话分两步：先 warmup 读契约和角色章程，再用 `--session --no-activate` 发任务。`at start` 封装 AT 的创建、绑定和首个任务。

```bash
# 第一遍：只建会话 + warmup（不传 --task）
python -m super_agent.dispatch_cli --cwd <工作台> --name <键名> --create-session --detach --timeout 300
# 第二遍：发任务
python -m super_agent.dispatch_cli --session <sid> --detach --no-activate --timeout 300 --task "<任务>"
```

**不要用 `--task ""` 建会话**：空串是 falsy，argparse 报 `--task is required`。`--create-session` 才是「只要会话、不要任务」的写法；旧名 `--activate-only` 仍可用。

**warmup 会打进度**：每 15 秒一行 `warm-up still running (Ns)`，结束一行 `activated (… in X.Xs)`。见到进度说明在跑，不必重试。

复用会话前执行 ctx 门禁。不要让复用命令覆盖用户手选模型。新会话键必须带可验证随机尾。

## 8. 等待与异常

- 预计超过 15 秒的命令后台运行，使用完成通知。
- 同一件事最多一个等待任务，不建立倒计时任务链。
- 停手前在 Issue 写四段：卡点、已试、要谁定哪一个问题、commit/cid 与接手坐标。
- 不读密钥、不 push、不做未授权生产写操作。
