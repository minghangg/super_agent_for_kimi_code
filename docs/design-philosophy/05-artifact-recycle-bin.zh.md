[English](05-artifact-recycle-bin.en.md) | [中文](05-artifact-recycle-bin.zh.md)

# 05 · 临时产物 Recycle Bin 与元数据回收

> 系列：设计哲学（design-philosophy）｜[01 核心架构](01-core-architecture.zh.md) ｜ [02 需求分析员](02-requirements-analyst.zh.md) ｜ [03 Agent 间通信](03-agent-communication.zh.md) ｜ [04 面向人的 Reporter](04-human-reporter.zh.md) ｜ 05 产物回收站
> 版本：2026-11-07
> 定位：中间产物「工作期间可用、关单后限期可回收、全程可追溯」，但不建立复杂数据库。
> 性质：设计方案，不修改规则、不执行清理、不授权自动删除。

## 一、先纠正一个认识

**Recycle Bin 能解决存储堆积、过程追溯和延迟删除，但它单独防不住 worker 影响生产。**

生产安全仍由以下机制负责：

- 项目权限和命令白名单；
- 工作目录隔离；
- 生产写入授权；
- 测试靶子与真实环境隔离；
- 操作审计和停止机制。

保存临时脚本只能帮助事后追查，不能阻止脚本已经造成的修改。因此方案必须把「产物回收」和「生产防护」分开。

## 二、先分类，不能所有文件统一丢进回收站

| 类别 | 例子 | 去向 |
|---|---|---|
| 正式需求与结论 | 任务书、worker 交付、QA 结论、用户裁决 | 全文进 Issue 评论 |
| 正式项目资产 | 测试用例、fixtures、项目脚本、构建配置 | 留在项目仓并正常提交 |
| 临时工作产物 | 探索笔记、临时脚本、中间输出、诊断日志 | 工作期使用，随后进入 Recycle Bin |
| 截图与二进制证据 | 截图、导出文件、测试附件 | Recycle Bin，Issue 只保留引用 |
| 密钥与敏感数据 | token、凭据、生产个人数据 | 禁止进入；发现后告警并走专门处置 |
| 长期运行状态 | sessions、游标、outbox、健康状态 | 现有运行态，不按临时文件回收 |

核心规则：

> **正式交付进 Issue，项目资产进仓库，只有有期限的临时产物进 Recycle Bin。**

## 三、不建议把全部元数据塞进文件名

文件名适合人类快速识别，不适合承载完整元数据：

- 长度有限，Windows 路径限制更明显；
- 中文、空格、保留字符需要转义；
- 修改元数据就得改名，容易破坏引用；
- 项目、Issue、owner 等字段可能重复或变更；
- 文件名无法可靠表达 hash、过期策略和保留锁。

推荐：

```text
稳定文件名 ＋ 每个产物的 sidecar JSON ＋ 轻量索引
```

文件名只保留：

```text
<artifact_id>__<safe_slug>.<ext>
```

例如：

```text
art_01KABC...__login-repro.py
art_01KABD...__qa-result.log
```

元数据放同名 sidecar：

```text
art_01KABC...__login-repro.py
art_01KABC...__login-repro.json
```

## 四、目录结构

每个项目独立目录，但不要只按项目组织——否则关单后仍要扫大量文件才能找到对应 Issue。

```text
recycle-bin/
├── config.json
├── index.json
├── events.jsonl
└── projects/
    └── repo-a/
        └── ISS-123/
            ├── art_01__diagnose.py
            ├── art_01__diagnose.json
            ├── art_02__test-output.log
            └── art_02__test-output.json
```

对于量大或长期项目，可增加日期层级：

```text
projects/repo-a/2026-11/ISS-123/
```

`artifact_id` 是永久内部引用，目录或文件名调整不影响 Issue、Assignment 和 Reporter 的引用。

### 存放位置

- 放在工作台规定的运行态/证据根目录，不进入项目 Git 工作树。
- 遵循 `KIMI_CODE_HOME` 和现有路径解析规则，不写死用户目录。
- 文件事件源可以监听配置和产物登记事件，但**配置目录不等于产物存储目录**。
- 使用与目标工作区相同的磁盘卷，尽量保证移动是原子 rename，而不是大文件复制。

## 五、元数据设计

每个产物的 sidecar 至少包含：

```json
{
  "schema_version": 1,
  "artifact_id": "art_01KABC...",
  "project": "repo-a",
  "repo": "owner/repo-a",
  "issue": 123,
  "thread": "owner/repo-a#123",
  "assignment_id": "asg_123_worker_2",
  "created_by": {
    "role": "worker",
    "session_id": "session_..."
  },
  "kind": "script",
  "media_type": "text/x-python",
  "original_path": "<workspace>/diagnose.py",
  "file_name": "diagnose.py",
  "extension": ".py",
  "size_bytes": 4821,
  "sha256": "...",
  "created_at": "...",
  "last_used_at": "...",
  "issue_closed_at": null,
  "retention": {
    "policy": "temporary",
    "days": 7,
    "expires_at": "...",
    "pinned": false
  },
  "lifecycle": "active",
  "sensitivity": "normal",
  "evidence_ref": null
}
```

### 关键字段含义

- `kind`：script、log、note、screenshot、archive、test-output、other。
- `original_path`：追溯来源，不用于自动重新执行。
- `sha256`：识别内容是否改变，避免把「同名不同内容」混为一谈。
- `assignment_id`：知道是哪一次执行产生的。
- `expires_at`：回收判据。
- `pinned`：因未决 Issue、审计或用户要求而暂停回收。
- `lifecycle`：active、quarantined、expired、deleted。

### 索引只保存短信息

`index.json` 不必复制全部 sidecar，只保留：

```text
artifact_id / project / issue / kind / size /
lifecycle / expires_at / relative_path
```

整理时读索引，无需打开产物内容。索引损坏时可通过 sidecar 重建。

## 六、元数据从哪来

**首选在创建时登记，而不是事后读文件猜测。**

给 worker/QA 提供统一入口：

```bash
python -m super_agent artifact register \
  --issue <repo>#123 \
  --assignment <asg_id> \
  --kind script \
  --path <file> \
  --retention temporary
```

登记工具自动补齐：

- 项目、Issue、Assignment；
- 当前角色和 session；
- 文件类型、大小、扩展名；
- 创建时间、hash；
- 回收策略。

worker 只需显式提供业务上无法自动判断的 `kind` 和用途。

对历史散文件：

- 可以根据现有路径、所属任务和 mtime 辅助归类；
- 无法确定项目或 Issue 的一律进入 `unclassified`；
- 不读取内容来「猜」归属；
- 未分类文件不自动删除。

## 七、生命周期与自动回收

```text
active
  │ 使用完毕或任务进入交付
  ▼
quarantined
  │ 等待 Issue 关单和保留期
  ▼
expired
  │ 自动回收任务执行
  ▼
deleted（仅保留删除墓碑）
```

### 建议默认策略

| 策略 | 建议期限 | 适用 |
|---|---:|---|
| `scratch` | 关单后 7 天 | 临时脚本、探索笔记、中间输出 |
| `evidence` | 关单后 30 天 | QA 截图、日志、测试附件 |
| `pinned` | 不自动删除 | 未决问题、用户指定保留、审计材料 |

配置支持全局默认、项目覆盖和单产物覆盖：

```json
{
  "default_retention": {
    "scratch_days": 7,
    "evidence_days": 30
  },
  "projects": {
    "repo-a": {
      "scratch_days": 7,
      "evidence_days": 30
    }
  }
}
```

### 期限从何时开始

从以下两者中**较晚者**计算：

- 产物最后使用时间；
- 所属 Issue 关闭时间。

这样开发期间不会因为文件「创建已满七天」而被删掉。

关单后：

- scratch 再留 7 天；
- evidence 再留 30 天；
- 未关单、有活动 Assignment 或被 pin 的产物不删。

## 八、删除安全机制

自动回收是持续写操作，首版必须谨慎开放。

### 首次启用

1. 配置期限与范围；
2. 生成拟删除清单；
3. 列项目、Issue、文件数、总字节、最早/最晚时间；
4. 用户批准该回收政策；
5. 先 dry-run；
6. 才允许后台自动执行。

### 每次回收前

必须同时满足：

- sidecar 有效；
- 项目在 allowlist；
- Issue 已关闭；
- 没有活动 Assignment；
- 没有 pin；
- 已到 `expires_at`；
- 文件 hash 与登记一致；
- 路径仍在 Recycle Bin 根目录内；
- 不是符号链接或指向外部的 junction；
- 没有占用锁或最近写入。

发现 hash 变化，说明文件登记后被改过：**停止删除并告警，不按旧元数据回收。**

删除后只保留短墓碑：

```json
{
  "artifact_id": "art_...",
  "issue": 123,
  "deleted_at": "...",
  "size_bytes": 4821,
  "sha256": "...",
  "reason": "retention_expired"
}
```

墓碑可再保留一段固定时间，用于证明「何时删了什么」。

## 九、Windows 与异常处理

必须考虑：

- 中文、空格、保留文件名和超长路径；
- 大小写不敏感导致的名字冲突；
- 文件被进程占用，无法移动或删除；
- 跨磁盘移动不是原子操作；
- junction/symlink 指向外部目录；
- 进程在移动中途崩溃；
- 同一 Assignment 重复登记同一文件。

对应规则：

- artifact id 避免命名冲突；
- 优先同卷移动；
- 移动采用临时名＋原子替换；
- 移动和删除前解析真实路径；
- 被占用则延后，不强制杀进程；
- 崩溃后通过 index 和 sidecar 对账；
- 同路径重复登记要检查 hash，不能静默覆盖旧记录。

## 十、与系列前四项的衔接

### 与 01 核心架构

新增产物生命周期设施，不新增 LLM 角色：

```text
Artifact Registry
├─ 登记与元数据
├─ 项目/Issue 归类
├─ 保留策略
└─ 自动回收与墓碑
```

它属于确定性程序，不做需求判断或质量裁决。

### 与 02 需求分析员

Analyst 只读引用需求相关证据：

```text
artifact://art_01KABC...
```

可以读取必要证据，但不能因为生成了调研文件就无限保存，也不能把敏感内容复制进需求事实库。

### 与 03 Agent 间通信

通信中只传：

```json
{
  "artifact_ref": "art_01KABC...",
  "kind": "test-output",
  "sha256": "..."
}
```

不传文件全文，不依赖长文件名携带语义。

### 与 04 Reporter

Reporter 增加只读存储视图：

- 每个项目的临时产物数量与占用；
- 未来 7 天将回收的数量；
- 已 pin 的产物；
- 未分类、hash 异常、删除失败；
- Issue 详情中的证据链接。

Reporter 只能展示，不提供「一键清理」或执行脚本入口。

### 与事件总线／过程文档治理／文件事件

- 事件总线：传输文件登记、关单、过期等事件。
- 过程文档治理：正式文本进 Issue，临时产物进 Recycle Bin，避免过程文档重新堆积。
- 文件事件源：复用文件事件与热更新；回收策略变化不重启。
- 关单对账：只关闭业务会话，不立即删除证据，保留期由本方案管理。

## 十一、实施顺序

### 阶段 1：只登记，不自动删除

- artifact id、sidecar、index；
- 按项目/Issue 归档；
- hash 与大小登记；
- Reporter 显示占用；
- dry-run 生成回收清单。

### 阶段 2：接通关单与保留期

- Issue closed 后计算期限；
- scratch 7 天、evidence 30 天；
- pin 和活动 Assignment 阻止回收；
- 验证关单前不误删。

### 阶段 3：有限自动回收

- 先单项目；
- 每次回收有清单、日志和墓碑；
- 失败可重试，但不扩大范围；
- 验证一周后再推广到其他项目。

### 阶段 4：历史文件整理

- 先盘点，不读内容；
- 能确定的按元数据归类；
- 不确定的进 unclassified；
- 历史删除仍须单独清单和授权。

## 十二、验收标准

- 不打开文件内容即可按项目、Issue、角色、类型和期限分类。
- 正式 Issue 评论与项目测试文件不会被纳入回收。
- 开发中、QA 中和活动 Assignment 的文件不会被提前删除。
- 关单后 scratch 7 天、evidence 30 天策略可配置并准确执行。
- pin、hash 变化、未知归属、外部链接均能阻止删除。
- 进程重启不会重复移动、覆盖产物或丢失墓碑。
- Reporter 可显示占用、到期和异常，但不能直接执行清理。
- Issue 关单后仍可在保留期内找到对应产物。
- 超过保留期后，存储占用按预期下降。

## 结论

> **稳定 artifact id ＋ sidecar 元数据 ＋ 项目/Issue 目录 ＋ 轻量索引 ＋ 7/30 天分类保留 ＋ 安全自动回收。**

不要把全部元数据编码进文件名，也不要让事件配置目录承担存储语义。
