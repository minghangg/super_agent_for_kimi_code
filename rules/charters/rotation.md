<!-- 换血流程（safkc 通用基线）。私有补充见配置 charter.supplement_root。 -->

# 换血（rotation）：把角色从一个会话交到下一个

> 用户 2026-11-20 令：长命工种全部支持；演练只做 PM 一个 ＋ 长命一个；**不用 supervisor 演练**；运维单放工作台私有仓，正文放规则，每周关单重建。

## 0. 三条铁律

1. **新键必须带随机尾**（`-<8位hex>`）。日期形（`-20261116`）会被白名单拒——第十四任 PM 就是这样掉出去的。
2. **旧键必须删**。不删就是白名单里的僵尸键，指向已归档会话。
3. **记录进运维单**，不进本地临时文件。本地 `rotation_*.md` 是 30 天缓存，不是真源。

## 0.1 AT 专项接续

AT 换血时，交接只带当前 requirement_ref/revision/hash、未决问题、最近答案文档路径和必要事实，不复制全部项目历史。更新 `sessions.json` 后还必须更新每个在飞 Issue 的 `at` 绑定；用一条新问题或现有待答问题实测答案只回新 sid，旧 sid 零投喂。旧 revision 页面仍须返回 410，不能因换血绕过版本校验。

PM 换血只带 approved 调度包、当前路由阶段和异常队列；新 PM 不重写需求、不重判 review_policy。worker/QA 的版本绑定随 Assignment 保留，换会话不能改变 revision/hash。

## 1. 五步流水线（PM 与长命相同）

| 步 | 动作 | 命令（全部 safkc） | 判据 |
|---|---|---|---|
| 0 | **核旧模型，记下来** | 读旧会话 wire 的最后一条 `usage.record.model` | 有值；这是新会话必须沿用的模型 |
| 1 | 核旧会话占用 | `python -m super_agent.rotation show --key <键>` | `old_ctx` 有值；<60% 时换血收益低，须说明理由 |
| 2 | 要求对方写交接档 | `python -m super_agent.console send <旧sid> "请按 handoff 模板写交接档"` | 对方回 `[交接完成]` |
| 3 | 建新会话（**第一遍：建 + warmup**） | `python -m super_agent.dispatch_cli --cwd <工作台> --name <新键> --detach --task ""`（**不带 `--no-activate`**） | 新会话 wire 出现 `Read done.`，首条消息含契约与章程路径 |
| 3b | **第二遍：发真正的内容** | `python -m super_agent.dispatch_cli --session <新sid> --detach --no-activate --task <任务>` | 任务正常执行 |
| 4 | **把模型改回第 0 步的值** | `POST /api/v1/sessions/<新sid>/profile` `{"agent_config":{"model":"<第0步的值>"}}`，再发一轮让它生效 | 新会话 wire 的 `usage.record.model` **等于**旧会话的值 |
| 5 | 换登记 ＋ 归档 | `python -m super_agent.sessions_ledger set <键> <新sid>` 然后 `archive <旧sid>` | `session.rotated` 与 `session.archived` 都落事件流 |
| 6 | 写记录 | `python -m super_agent.rotation record --key <键> --trigger-reason … --ordered-by … --handoff-doc …` | 记录贴进运维单 |

**新建会话＝两条命令，不是一条（用户 2026-11-20 定）**：warmup 只在 `if not args.no_activate and not args.session` 时跑（`dispatch_cli.py:1396`），所以**建会话时不带 `--no-activate`，复用会话时才带**。

```bash
# 第一遍：建会话 + 自动 warmup（读契约 + 读章程 + 告知键名）
python -m super_agent.dispatch_cli --cwd <工作台> --name <新键> --detach \
  --timeout 300 --task ""

# 第二遍：发真正的内容（--session 本身就不跑 warmup，加 --no-activate 免掉多余等待）
python -m super_agent.dispatch_cli --session <新sid> --detach --no-activate \
  --timeout 300 --task "<真正要做的事>"
```

**为什么分两遍**：第一遍的 `--task` 内容会被 warmup 挤掉——warmup 占掉一轮，`--timeout` 预算被它吃掉，主任务赶不上就报 `round did not start within 120s`。分两遍后各拿各的预算，两轮都完整。

**判据**：
- 第一遍后 wire 有 `Read done.`，`context.append_message` 首条含 `session_contract.md` 与角色章程路径；
- 第二遍后 wire 出现任务的实际输出。

**别用 `--no-activate` 建会话**——那会跳过 warmup，新会话读不到契约与章程，「换了会话但没换到位」。真发生了就补：用 `dispatch_cli.warmup_prompt(<charter>, <新键>)` 生成文本，作为一条 task 发进去。

**第 4 步不可省（用户 2026-11-20 令）**：新会话是用默认模型建的，**换血不得改变模型**——用户可能手工给这个角色调过模型，那个选择是长期决定，不是本次换血的副产品。换血只换会话，不换模型。

- 判据写法：换血前后各读一次 `usage.record.model`，**必须相同**。
- 模型别名以 `GET /api/v1/models` 返回的 `provider/model` 为准；`cdy2Api-intl` 的正确写法是 `cdy2Api-intl/deepseek-v4.1-flash-sg`（**不是**双写 `cdy2Api-intl/cdy2Api-intl/...`，后者报 `40001 not configured`）。
- `profile` 只改登记，**要发一轮才生效**——不验 wire 等于没改。

**第 5 步的 `set` 会自动发 `session.rotated`**——那是记录的数据来源，不要手工补事件。

**归档事件的顺序**：`rotation.collect` 只认 `session.rotated` **之后**的 `session.archived`。先归档后记账会让 `old_session_archived=false`，必须在 rotated 落盘之后再归档（或补写一条归档事件）。

## 2. 写记录：9 项自动，4 项人填

```
python -m super_agent.rotation record --key scout \
  --trigger-reason ctx_threshold --ordered-by supervisor \
  --handoff-doc state/asst_handoff/scout_20261120-xxxx.md
```

| 自动（9） | 人填（4） |
|---|---|
| `role`／`old_session`／`old_session_archived`／`old_ctx`／`new_session`／`new_session_created_by`／`new_ctx`／`ordered_at`／`rotation_id` | `trigger_reason`／`ordered_by`／`handoff_doc`／`model_change` |

**人填项缺一个就报错**，不写空串。`trigger_reason` 与 `ordered_by` 是两个独立参数——「ctx 到线自动换」和「用户明令换」是两回事，记录要能分开查。

**枚举**：
- `trigger_reason` ∈ `ctx_threshold`／`user_order`／`model_change`／`failure`
- `ordered_by` ∈ `supervisor`／`user`

## 3. 自检四项

```
python -m super_agent.rotation check --key <键> [--feed-since <归档时刻 epoch>]
```

| 检查 | 失败意味着 |
|---|---|
| 新键有随机尾 | 白名单会拒，这个键不是凭据 |
| 旧键已从 `sessions.json` 删除 | 僵尸键，指向已归档会话 |
| 旧 sid 有 `session.archived` | 旧会话还在跑 |
| 归档后零投喂（`--feed-since` 给了才查） | `archived=true` 不等于不再收消息 |

**零投喂怎么查**：走 daemon 的 `GET /journal?sid=<旧sid>&verb=send&since=<归档时刻>`，**不读 `state/wss_bus_journal.jsonl`**——那个文件属于启动 daemon 的那个工作台，不是通用接口。

## 4. 五段式（每条边一个动作）

| 边 | 首行 |
|---|---|
| 要求交接 | `[supervisor]-[交接要求]-[<长工键>]-[<项目键>]` |
| 写完了 | `[<长工键>]-[交接完成]-[supervisor]-[<项目键>]` |
| 新会话可上岗 | `[supervisor]-[交接就绪]-[<新键>]-[<项目键>]` |
| 换血记录 | `[supervisor]-[已归档]-[No Notify]-[<项目键>]-[<cid>]` |

记录用 `[已归档]`：它在静默动作里，不通知任何人；`parse_delivery`/`parse_review` 都不认它；运维单本身挂起，引擎不扫。三重保证零消息风暴。

## 5. supervisor 换血（跨会话，**用户发起**）

与其他角色**不同**：发起人是用户，执行人是新旧两个 supervisor。

```
用户发起
  ↓
旧 supervisor：① 写交接记录  ② 删自己的 cron  ③ 建继任者会话
  ↓
旧 supervisor：发命令让新 supervisor 初始化
  ↓
新 supervisor：初始化完成 → 创建自己的 cron
  ↓
新 supervisor：归档旧 supervisor 会话
```

| 步骤 | 谁执行 | 注意 |
|---|---|---|
| 写交接记录 | 旧 supervisor | 与其他角色相同 |
| 删 cron | **旧 supervisor 自己** | `CronDelete` 只能删自己会话的 cron，**没有跨会话删除接口** |
| 建继任者 | 旧 supervisor | 新键带随机尾 |
| 初始化新会话 | 旧 supervisor 发令 | 新 supervisor 自己执行 |
| 建 cron | **新 supervisor 自己** | cron 不能跨会话转移 |
| 归档旧会话 | **新 supervisor** | 与其他角色相反：不是自己归档自己 |

**中间有窗口期**：旧 cron 删了、新 cron 没建，这段时间没有巡查。写进交接记录，不要假装没有。

## 6. 运维单

| 项 | 规则 |
|---|---|
| 位置 | 工作台私有仓（13 项含完整 sid，公开仓必须打码，打码就无法独立还原） |
| 正文 | **规则**（本文件的引用 ＋ schema），不放记录 |
| 记录 | 评论，每条换血一条 |
| 状态 | 当期单永久 `stage:on-hold`（引擎不扫） |
| 生命周期 | **每周手工关旧单、建新单**；旧单正文留指向新单的链接 |

**重建时搬什么**：只搬索引（`state/asst_handoff/rotation_*.md` 的文件列表 ＋ 当前生效记录的 cid 列表），不搬全文——全文在旧单里，链接可达。

**为什么手工不用 cron**：cron 挂在 supervisor 会话上，换血即失效——正是本单在治的病。正文写「上次重建日期」，让忘记可见。

### 6.1 每次换血的七步（用户 2026-11-20 令）

**运维单平时挂着 `stage:on-hold`（引擎不扫）。换血时临时摘掉，靠它的评论通知驱动流程，完成后再挂回去。**

| # | 动作 | 谁 | 命令／判据 |
|---|---|---|---|
| 1 | **摘掉 `stage:on-hold`** | supervisor | `python -m super_agent.issue_cli open --issue <运维单>`；核它打印的在飞列表里有这张单 |
| 2 | **下令** | supervisor | `dispatch_cli --session <旧sid> --ctx-force --task "【换血令】写交接档 …"`；写明「记录发运维单 #N」 |
| 3 | **对方回复** | 旧任 | 在运维单发一条评论（五段式 `[<旧键>]-[交接就绪]-[<supervisor>]-[<项目>]`），附交接档路径 |
| 4 | **收到通知** | supervisor | 引擎按评论投帧（这就是摘掉 hold 的目的：**不摘就没有通知，只能靠轮询**） |
| 5 | **继续工作** | supervisor | 建新会话 → `sessions_ledger set` → 派新会话读交接档 → 归档旧会话 → `rotation record` |
| 6 | **换血完成** | 新会话 | 在运维单发 `[<新键>]-[交接完成]-[<supervisor>]-[<项目>]` |
| 7 | **挂回 `stage:on-hold`** | supervisor | `python -m super_agent.issue_cli hold --issue <运维单>`；核它不在在飞列表里 |

**三步不可省，省了就出问题**：

- **省 1（不摘 hold）**：引擎不扫这张单，对方的回复**不会触发任何通知**。supervisor 只能靠 `sleep` 轮询或反复 `grep`——`session_contract.md` §5 明令禁止的做法。
- **省 2（不写运维单号）**：对方不知道记录发哪里，只能发在别处或本地文件，记录失去唯一真源。
- **省 7（不挂回）**：这张单一直在飞，引擎每轮都扫它，产生「超时未关」「PM 停摆」类假告警，且下一轮换血前它已被误扫多时。

**判据**：换血结束后跑 `python -m super_agent.issue_cli list --project <项目> | grep <运维单号>`，**必须为空**（挂起单不出现）。

## 7. 本地缓存

`state/asst_handoff/rotation_<角色>_<时刻>.md`，30 天。**不是真源**：删掉它，运维单上的记录仍然完整。这条是验收判据。

## 8. 演练范围（用户令）

- 覆盖：一个长命（`scout`）＋ 一个 `pm-*`
- **不做**：supervisor（用户令）、其余同类型（机制相同，逐一无收益）

运维单正文须写明这一点，避免后人以为全部演练过。
