<!-- 换血流程（safkc 通用基线）。私有补充见配置 charter.supplement_root。 -->

# 换血（rotation）：把角色从一个会话交到下一个

> 用户 2026-11-20 令：长命工种全部支持；演练只做 PM 一个 ＋ 长命一个；**不用 supervisor 演练**；运维单放工作台私有仓，正文放规则，每周关单重建。

## 0. 三条铁律

1. **新键必须带随机尾**（`-<8位hex>`）。日期形（`-20261116`）会被白名单拒——第十四任 PM 就是这样掉出去的。
2. **旧键必须删**。不删就是白名单里的僵尸键，指向已归档会话。
3. **记录进运维单**，不进本地临时文件。本地 `rotation_*.md` 是 30 天缓存，不是真源。

## 1. 五步流水线（PM 与长命相同）

| 步 | 动作 | 命令（全部 safkc） | 判据 |
|---|---|---|---|
| 1 | 核旧会话占用 | `python -m super_agent.rotation show --key <键>` | `old_ctx` 有值；<60% 时换血收益低，须说明理由 |
| 2 | 要求对方写交接档 | `python -m super_agent.console send <旧sid> "请按 handoff 模板写交接档"` | 对方回 `[交接完成]` |
| 3 | 建新会话 | `python -m super_agent.dispatch_cli --cwd <工作台> --name <新键> --detach --task <初始化>` | 新会话 warmup 有 `Read done.` 回执 |
| 4 | 换登记 ＋ 归档 | `python -m super_agent.sessions_ledger set <键> <新sid>` 然后 `archive <旧sid>` | `session.rotated` 与 `session.archived` 都落事件流 |
| 5 | 写记录 | `python -m super_agent.rotation record --key <键> --trigger-reason … --ordered-by … --handoff-doc …` | 记录贴进运维单 |

**第 4 步的 `set` 会自动发 `session.rotated`**——那是记录的数据来源，不要手工补事件。

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

## 7. 本地缓存

`state/asst_handoff/rotation_<角色>_<时刻>.md`，30 天。**不是真源**：删掉它，运维单上的记录仍然完整。这条是验收判据。

## 8. 演练范围（用户令）

- 覆盖：一个长命（`scout`）＋ 一个 `pm-*`
- **不做**：supervisor（用户令）、其余同类型（机制相同，逐一无收益）

运维单正文须写明这一点，避免后人以为全部演练过。
