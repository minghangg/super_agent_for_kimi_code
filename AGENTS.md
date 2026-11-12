# AGENTS.md（本仓 · 开源底座）

> 本文件只回答四件事：这仓是什么、Issue 在哪、运行态放哪、规则从哪来。**越薄越好**；工作台那一套流程规则不往这里复制。

## 1. 本仓是什么

`super_agent_for_kimi_code` 是**开源底座**：确定性任务引擎（事件采集、Assignment 状态机、outbox、关单对账）＋ 设计文稿。
它不含任何私有项目、私有身份、会话编号与凭据。配套的私有工作台是另一个仓，两者不是同一件事。

## 2. Issue 真源＝GitHub（单一真源）

- **过程与结论的唯一真源是 GitHub Issue**，即本仓 `origin` 指向的那张仓的 issue 列表。任何本地文件都不是第二真源。
- 本地 `state/index.json`（评论游标、seen 计数）**是可重建缓存**，不是台账：删掉重跑即可重建，丢它不丢事实。
- 因此：**看到本地索引与 Issue 不一致时，以 Issue 为准**；索引里出现别的仓的单号，说明它被旧工作台数据污染过，重建即修正。
- 单号只在本仓内解释。跨仓引用必须写全 `owner/repo#N`，不要用裸数字（`issuekey.py` 的线程键就是 `owner/repo#N`，数字键只为旧数据留只读兼容）。

## 3. 运行态放哪（与 #52 的三层骨架对齐）

`#52` 定的是 `auth/` `rules/` `data/` 三层：**`data/` 是运行态、`rules/` 是规则生效位**。三层均已落地：

- 运行态落在 `data/`（新根，默认）与 `state/`（旧根，迁移期并存，两者都已被 `.gitignore` 忽略）。`Store` 构造时先跑 `store.check_migration()`：`.migrating` 标记在、或 6 个关键 JSON 之一解析失败，即拒绝启动。选根由 `cli._build()` 调 `store.choose_root()`：`data/` 里有任何引擎文件就用它；没有而旧根有，则回退读旧根并 WARN。旧根默认是**所选根的同级 `state/`**（默认根下即 `<cwd>/state`，与上一版的落点相同），可用 `legacy_state_dir` 覆盖。**只回退一级**：旧根若嵌套在更上层（≥2 级）则找不到、会被静默忽略——这种布局必须显式设 `legacy_state_dir`。
- `auth/` **不入 git、不在克隆里预置**：`.gitignore` 整目录忽略，连占位文件也不跟踪，`git clone` 之后没有这个目录。需要放指针时自己 `mkdir -p auth` 再写文件；它一旦有文件被跟踪，门禁就判 `runtime path tracked (prefix auth/)` 并 rc=1。只放指针，不放值。说明在 `docs/auth.md`。
- `rules/candidates/**` 是备选池（14 份脱敏件＋`INDEX.md`），`rules/active/` 是生效位，由 `python -m super_agent.rules_init` 选择落位。迁移到 `data/` 时**只改目录名，不改语义**：运行态仍然「可删、可重建、永不入库」。
- 根目录出现的散落运行文件一律视为**配置未注入的回退产物**（见第 5 节），不入库、不登记为产物。

## 4. 规则从哪来

- 本仓**不复制**工作台的规则实体（流程手册、助手章程、项目规则）。入池件按 `rules/candidates/INDEX.md` 的 13 条脱敏规则改写后落 `rules/candidates/**`；未改写的原件仍留在工作台仓。
- 私有工作台的 `rules/shared/**` 通过绝对路径注入，不镜像进本仓；需要引用时只写「规则名」，不写路径。
- 需要在本仓新增约定时，写进本节，**不新开文件**。

## 5. 已知取舍（不要在维护单里"顺手修"）

- `state_root()` 未注入时回落到 `Path()`（即当前目录），于是 `monitor.log`／`sessions.json` 会落在 cwd。这是**缺省值悄悄变成一种语义**的隐患，已登记待办；修它属于引擎逻辑改动，不在纯维护单范围内。
- `config/allowlist.json` 里的 `repo` 是**公开占位名**，真实仓名走未跟踪的 `config/allowlist.local.json`。改占位名不会让引擎指向真实仓，改 overlay 才会。

## 6. 隐私门禁

```bash
python tools/privacy_gate.py            # 合并前必跑，rc≠0 即 P0
python tools/privacy_gate.py --refs all # 额外扫全部 ref
```

提交作者必须是 `safkc-bot <safkc-bot@users.noreply.github.com>`；用 `-c user.name=...` 覆盖成个人身份会让 `--refs all` 变红，且公开仓无法回收。
