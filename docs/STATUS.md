# 当前状态（事实页）

> 本页只记「现在到哪一步」，一句话一条，不写历史。方向性叙述见 `README.md` §现状与路线图；两者冲突时**以本页为准**。
> Issue 真源是本仓 `origin` 指向的 GitHub Issue；跨仓引用必须写全 `owner/repo#N`，裸 `#N` 只在本仓语境解释。里程碑写作 `M0` 与 `milestone 1`／`milestone 2`，避免隐私门禁把机器代号误判为私有机名。

## 已完成

- **M0 已完成**：引擎骨架与 observe 模式，`external_actions` 恒为 0（#19）。
- **milestone 1 已完成**：单 Issue 闭环，Thread × Assignment 状态机＋确定性快通道（#20）。
- **milestone 2 已完成**：QA 直通、`waiting_release`、关单对账四条件两轮，引擎只核对不关单（#21）。
- **Reporter 已完成**：只读首屏、三类动作桶、unknown 不读成 none（#49）。
- **#52 已实施**：`auth/` `rules/` `data/` 三层骨架落地；运行态默认根为 `data/`，旧根 `state/` 由 `choose_root()` 回退读取并 WARN（见 `AGENTS.md` §3）。
- **AT 模式已实现（#110）**：Issue 版本化需求包、精确批准、Assignment revision/hash 门禁、T1/T2 PM 路由、旧 outbox 失效、本地选项问卷和答案文档。操作见 `docs/at_mode.md`；公开默认 `at_mode=false`。

## 在办与待开工

- **#22 待开工**：M3 看门狗退役清单＋逐项回退手册；退役对象为 decide_watch 哨、monitor 巡查、pmw 轮询、reporter 心跳监护。

## 增量评论读取的硬上限与解除（#127）

引擎读一单新评论时按页翻，预算由 `github.max_comment_pages` 决定（默认 10）。

- **为什么要有上限**：一次翻页用尽预算、又没翻到 cursor 时，`cursor` 以上有没有洞是**未知数**。此时消费任何一段都可能跳过中间没读到的评论，所以引擎不消费、不推进 cursor，改写 `read_paused` 并留一行 `read.pagination_incomplete`。
- **硬上限是真停读**：挂起后该单**每轮一次请求都不发**，不是「读完再重试」。`read.skipped` 事件每轮一条，`needs_person` 只留一行，不随轮次增长。
- **唯一解除入口**：

  ```bash
  python -m super_agent.cli resume-read --issue <n> --by <谁> \
      [--max-pages <下一轮预算>] [--note <为什么>]
  ```

  退出码：`0` 已解除，`1` 该单没有挂起，`2` 参数不可用（缺 `--by`／单号读不出）。
- **`--max-pages` 是对下一次读取真正生效的预算**（写到该单的 `read_budget`，只用一次，不改变全局配置）。它同时**加深刻探深度**：末页探测按预算一路翻到短页为止，所以 200／300 这种「整页数倍、又没有 `Link`」的列表也能被读到末页（#128 L2）。不给该参数就沿用 `github.max_comment_pages`；列表比配置窗口大时会再次挂起，属预期。
- **一条命令一次到位**：解除同时清 `read_paused`、把 `state` 从 `needs_person` 放回 `idle`、并 dismiss 那行 `read.pagination_incomplete`，所以 `status` 不再列出它，无需再跑 `resolve --comment 0`。事件为 `read.resumed`。
- **为什么翻页不认 `direction`**：逐 Issue 评论接口 `GET /repos/{repo}/issues/{n}/comments` 恒按 id 升序返回，`direction` 被忽略（实测含带 token 的私有仓）。因此读侧以 `Link` 头的 `rel="last"` 定位末页，再从末页倒推，用「整页都不超过 cursor」判停。见 `docs/STATUS.md` 本节的实测记录与 `tests/test_iss127_incremental_read.py::TheEndpointIgnoresOrdering`。

## AT 历史完整性门禁（#128）

AT 模式每轮都从 Issue 重建需求投影，所以那次读取必须是**整份列表**，不能只是前缀——前缀里缺的页上如果放着需求包，投影里就当作「没人发布过」。

- **两种声明，不混用**：`contiguous` 只证明「cursor 以上没有洞」（够一轮增量消费）；`pagination_complete` 才证明「翻到底了」（够重建投影）。两者都由一次翻页自己给出，不从返回的行去猜。
- **不完整就不写投影**：AT 路径翻不到末页时写 `read_paused`＋一行 `requirement.history_incomplete`，该轮**不调用** `_apply_requirement_comments`，也不写 `requirements.json`。与页级挂起同因时只留一行，解除后不会残留。
- **投影存 `history_complete`**：写在该单 entry 上，**不是**采集语义字段，不进 `SEMANTIC_FIELDS`、不改 `requirement_hash`。`dispatch_packet` 与 `cmd_approve` 都只认存下来的字面 `True`；`cmd_approve` 读的是**持久投影**（`req.load_projection`），不是本轮重建结果。缺少该字段＝拒绝，不是「默认可以」。
- **写回不抹字段**：`_project`／sync／publish／approve 一律传 `previous=req.load_projection(...)`，`history_complete` 与 `seen_comment_ids` 因此跨轮保留。传 `history_read_complete=None` 表示「本次不采集」，保留原答案而不是清空。
- **一次翻页两个用途**：`github_source.paged_batch` 让增量与历史用同一次请求、同一个 `cursor_before`，避免两次翻页之间来了新评论而对不上。

## 挂起

- **#36 挂起**：跨平台适配（launchd／systemd、POSIX pidlock、CI matrix），未开工。
- **#30 挂起**：Requirements Analyst 最小闭环（无 Web、不建会话），与引擎解耦可并行，等开工令。
- **#26 已关闭、内容并入 #22**：PM 总线唤醒协议的四类消息已被 milestone 1／milestone 2 吃掉，消息类型表转为 #22 的退役验收清单。
- **#23 已关闭、内容并入 #43**：M4 三档分级不存在、assist 零外部动作数据；「恒不自动清单」搬进 #43 当硬规则。
- **#43 挂起**：长期提醒单，不开发不验收，在所有在飞单关闭前保持 open。

## 已知缺腿清单

- **`dispatch_adapter` 的通用派单动词未全部实现**：`resolve`／`create-worker`／`create-qa`／`bind`／`send`／`fetch`／`archive`／`reconcile-close` 在通用 console 适配面仍有缺口，报 `unsupported_verb` 并非静默成功。与此同时，引擎 `_archive_one()` 已有直接 session API 回退，实际 outbox 也存在 archive 动作；不得再把「通用适配面缺动词」写成「引擎 archive 不可达」。
- **`reedkit/*.sh` 9 个脚本未迁移**：`atroute`／`rclose`／`rhandoff`／`rhouse`／`rissue`／`rpm`／`rpmlife`／`rrotate`／`rwatch`。其叙述性文本（`echo` 提示、`usage`、注释）需重写才过隐私门禁，属独立扩容，归后续单。本仓只复制了 `pmc.sh`／`pmd.sh` 两条链。

## 判据

- 回归：`pytest tests -q` 全绿；`python tools/privacy_gate.py` 必须 clean（rc=0）。
- 提交作者恒为 `safkc-bot <safkc-bot@users.noreply.github.com>`。
- 运行态永不入库：`state/` 与根目录回退产物均在 `.gitignore` 内。
