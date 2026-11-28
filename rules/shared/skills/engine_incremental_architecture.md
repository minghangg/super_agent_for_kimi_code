# `super_agent/engine.py` 增量维护架构指引

> 立档：2026-11-24，用户令。适用于 **#127 / #128 / #129 及后续所有改动 `engine.py` 的单**。
> 依据：一次引擎体量审计（约 5800 行、`Engine` 150 余个方法、20 余个测试文件直接访问私有成员）。
>
> **2026-11-25 补充（QA 在 #128 指出）**：本指引原只写 `engine.py`，导致 `requirements.py`（549 行）、`wss_bus.py`（4289 行）、`decide_watch.py`（4237 行）等**同样超线的文件**不受约束。现明确：**§四「必须去新模块」与 §八 反例适用于仓内所有超过 300 行的文件**，不只 `engine.py`。§十 的行数判据仍以 `engine.py` 为强制报告项，其他超线文件按 worker 章程 §3.1 报告。

## 一、一句话规则

**新增的可拆分工具类、纯函数、数据类，一律写进新模块，不得再写进任何超过 300 行的文件。**

`engine.py` 从今天起**只减不增**。允许它变短、允许它保留转发方法，**不允许它继续长胖**。其他已超线文件同理。

## 二、为什么

`Engine` 是 God object：轮次编排、评论分流、QA/PM/AT 路由、Assignment 同步、归档、关单对账、健康诊断、replay 全在一个类里。138/149 方法互相 `self.xxx()` 调用，任何局部改动都可能跨路径影响。

一次性大重构风险太高（测试直接依赖 84 个私有属性名）。所以采取**边生产边维护**：每次改动顺手把该块下沉，不单独立项，也不停功能开发。

## 三、目标结构（逐步达成，不要求一次到位）

```text
super_agent/engine.py          ← 兼容门面：保留 Engine、build_engine、转发方法
super_agent/engine_parts/
    rules.py        常量、正则、时间、JSONL tail、TASK_PREFIX_RE
    identity.py     session stream、issue session 绑定、PM/QA/AT 键白名单、archived 检查
    triage.py       _triage、envelope 构造/校验/记录
    qa_flow.py      QA verdict 匹配、_route_review、_on_review、refused review、仲裁
    pm_flow.py      PM verdict、PM frame、PM stall、release identity
    at_flow.py      AT verdict、question URL、AT frame、AT stall
    supervisor.py   supervisor route/sid、undeliverable notice、release 去重
    dispatch.py     _dispatch、_route_action、refusal streak、route retry
    assignment_flow.py  assignment sync、delivery、waiting review retry
    archive.py      archive thread、archived sid ledger、server crosscheck
    reconcile.py    close conditions、close reconcile、closed needs、release timeout
    orchestration.py run_once、run_round、_round_issue、cursor guard
    ops.py          status、doctor、doctor subchecks、replay 系列
```

**命名注意**：包名用 `engine_parts/`，不要用 `engine/`——仓内已有 `super_agent/engine.py`，同名包会造成导入歧义。模块名不要叫 `assignments.py`／`health.py`，仓根已有同名模块。

## 四、什么必须去新模块

以下四类**一律不得写进任何超过 300 行的文件**（`engine.py`、`requirements.py`、`wss_bus.py`、`decide_watch.py` 等同等对待）：

1. **纯函数**：不依赖 `self`、不读写实例状态的函数。
2. **常量与正则**：`_now`、`_parse_ts`、`TASK_PREFIX_RE`、`REVIEW_TARGET_STATES`、`_ENVELOPE_TYPES` 等。
3. **数据类／NamedTuple／Enum**：批次、轮次、游标、扫描策略等结构定义。
4. **独立工具类**：解析器、校验器、格式化器、缓存器等不持有宿主对象状态的类。

**判据**：一个东西如果**不访问宿主对象的任何可变状态**，它就不该留在那个大文件里。

## 五、什么可以留在 `engine.py`

- `Engine` 类本身及其**已有**方法（改动可以，新增尽量避免）；
- **转发方法**：`def _route_review(self, ...): return self._qa_flow.route_review(...)` 这类一行转发，用于保护测试调用面；
- `build_engine` 与 `__all__` 导出。

**新增方法例外**：若新方法必须访问 `self` 的多个可变状态（如 `_index`、`_store`、`_cfg` 的组合），可暂留 `engine.py`，但**必须在 PR/交付里说明为什么不能下沉**，且不得成为下一个 God 块。

## 六、迁移纪律（每单遵守）

1. **行为不变**：下沉只搬代码，不改逻辑、不改 event type、不改 ledger key、不改去重规则。
2. **保留转发**：搬走的方法在 `Engine` 上留同名转发，测试不用改。
3. **一次一块**：一单只下沉一个职责域，不跨域混做。
4. **先测后搬**：搬前跑对应测试组记录基线，搬后必须一致。
5. **共享状态不动**：`run_round` 里的 round-scoped 缓存重置（`_index_skip_logged`、`_stream_error`、`_archived_cache`、`_live_check`、`_session_state_cache`）生命周期属行为，拆分不得改变。

## 七、验收命令（每单至少跑对应组）

```bash
# triage/envelope
.venv/Scripts/python -m pytest -q tests/test_markers.py tests/test_iss85_pm_task.py tests/test_iss92_chain.py

# route/watchdog
.venv/Scripts/python -m pytest -q tests/test_supervisor_route.py tests/test_engine_pm_route.py tests/test_engine_at_verdict.py

# round/cursor/health
.venv/Scripts/python -m pytest -q tests/test_index_unreadable.py tests/test_comment_paging.py tests/test_cursor_guard.py tests/test_startup_health.py

# archive/reconcile
.venv/Scripts/python -m pytest -q tests/test_archive_thread.py tests/test_close_reconcile.py
```

### 7.1 全量测试的归属（2026-11-25 修正）

**全量 `pytest tests/` 只有 SR 与 QA 可以跑；worker 不可以。**

| 角色 | 允许跑全量 | 说明 |
|---|---|---|
| **supervisor（SR）** | ✅ 可以 | 发版前跑一次；需要整体回归时随时可跑 |
| **QA** | ✅ 可以 | 阶段 3 收口时跑；阶段 1 有 P0 时不跑 |
| **worker（`wk-*`）** | ❌ 不可以 | 只跑上表的分组（L0/L1 定向） |
| PM | ❌ 不可以 | 不跑任何测试 |

依据：`rules/charters/worker.md` §3「worker 只跑 L0/L1；L2/L3 归 QA」、`rules/charters/qa.md` §3「全量回归：前置全过后执行」。

**同机同时只允许一套全量**——多套并发会争用临时目录与运行态文件，实测 5 个 pytest 并发时谁都跑不完。

**这条修正的由来**：本节原末行写「# 全量（每单收尾）`.venv/Scripts/python -m pytest -q`」，等于把全量写成了「每单都跑」，与 worker 章程直接冲突。2026-11-25 实测：`wk-ISS-129` 的 worker 据此跑了全量，与 scout 起的全量并发，5 个 pytest 进程互相争用，全部无结果。**指引不得与章程冲突；冲突时以章程为准。**

**执行点**：`super_agent/full_suite_guard.py` 在 `tests/conftest.py` 里装上闸——`wk-*`／`pm-*` 键跑全量直接拒绝启动，同机第二套全量也拒绝。

## 八、反例（违反即退回）

1. **新增纯函数写进 `engine.py`** ⇒ 违规，退回改到 `engine_parts/`。
2. **新建 `Engine` 子类或 mixin 塞回同一文件** ⇒ 换皮扩单，退回。
3. **新建 mixin 架构本身**（用户 2026-11-28 令）⇒ 违规，退回。判据：`class A(...)` 的某个基类通过 `self.` 访问子类的私有成员（`self._foo`／`self._bar()`），该基类即 mixin。**把它定义在新文件里不算合格**——耦合没减，只是从「同文件」变成「跨文件共享 `self`」；后果是基类无法单独测试、宿主改私有方法会静默破坏基类、依赖在 `import` 里看不见。正确做法：状态作为参数显式传入，或抽成持有自身状态的独立类（组合而非继承）。存量 `AtRoutes`／`ReviewRoutes`／`RefusedRoutes` 属历史遗留，不追溯，但**新单不得新增同类结构**。
4. **下沉时改了逻辑或 event/ledger 语义** ⇒ 不是迁移，退回。
5. **搬走但没留转发方法，导致测试大面积改** ⇒ 违反「保护调用面」，退回。
6. **一单内跨多个职责域下沉** ⇒ 违反「一次一块」，退回拆小。
7. **`engine.py` 行数比改前增加** ⇒ 需在交付里说明理由，否则视为违规。

## 九、与现有单的关系

- **#127**（分页与 cursor）：读侧逻辑属 `engine_parts/orchestration.py` 或独立 `paging.py`；`_pause_read`／`resume_read` 若为纯逻辑应下沉。
- **#128**（AT 历史与批准门禁）：投影与门禁逻辑属 `engine_parts/at_flow.py`。
- **#129**（session broker）：新模块 `session_broker.py` 直接建在仓根，**不进 `engine.py`**。
- **#130–#133** 同样适用本指引。

## 十、判据

**每次交付都要报 `engine.py` 的行数变化。** 行数不变或变短 = 合格；行数增加 = 必须解释。

**测试范围按 §7.1**：worker 只跑定向组；全量只有 SR 与 QA 可以跑。

---

## 十一、超限文件「每次改动必须净降 10%」（用户 2026-11-25 令，适用 #130／#131／#132 及后续所有单）

### 11.1 条文

**300 行是目标，不是硬墙。** 大模块**允许适当超线**，但必须给出合理理由。**已经努力分拆仍拆不动，说明情况即可**，不判违规。

**超线文件照常可以改，合理必要的改动一律允许。** 但每次修改必须**边改边打扫**：本次改动把该文件行数净降 ≥10%（改前 × 0.9），并拆出独立模块。

**这条是「打扫量」，不是「准入闸」。** 不降行数不能变成不改文件——回避修改超线文件比行数超标更严重（见 11.2 第 0 条）。

- 现行基准（2026-11-25 现取，改前值）：

| 文件 | 改前 | 本次上限（×0.9） |
|---|---|---|
| `super_agent/engine.py` | 5787 | **5208** |
| `super_agent/wss_bus.py` | 4289 | **3860** |
| `super_agent/decide_watch.py` | 4237 | **3813** |
| `super_agent/dispatch_cli.py` | 1537 | 1383 |
| `super_agent/console.py` | 1436 | 1292 |
| `super_agent/cli.py` | 1385 | 1246 |
| `super_agent/reporter.py` | 1350 | 1215 |

- **每改一次，基准重设一次**：本轮降到 5208，下一轮改动时改前值就是 5208，上限 4687。**不许把「已经降过了」当作永久豁免**。

### 11.1.1 例外：努力拆过仍拆不动（用户 2026-11-25 三次明令）

**用户原话：「300 行是目标，但是大模块允许适当超线，要给合理的理由。如果已经努力分拆了，还是不行，说明情况即可。」**

例外成立需同时给两样：

1. **合理的理由**：说明为什么这一块不能独立成模块——典型是「强耦合宿主可变状态」（`self._index`／`self._store`／`self._cfg` 的组合读写）、「跨模块共享生命周期」、「拆出去会造成循环导入」。
2. **努力过的证据**：说明试过哪几种拆法、各自卡在哪。**只写「拆不动」不算**，要写「试了 A 拆法，卡在 X；试了 B，卡在 Y」。

**判据**：交付里有这两段 ⇒ 例外成立，QA 不判 P0，只记 P2 备注。**缺任一段 ⇒ 按未达标处理。**

**例外的边界**：例外只豁免「本次净降 10%」，**不豁免「严禁回避」**——该改还得改，只是这一轮降不到位。

### 11.2 四条硬判据（第 0 条最高优先）

0. **★ 允许修改，严禁回避（用户 2026-11-25 二次明令）**：超线文件**照常可以改**，合理必要的改动**必须**落在这个文件里。**严禁因为「怕降行数」而回避修改它**——包括但不限于：
   - 另建旁路实现同一逻辑，把功能挪到新文件里，原文件留着烂；
   - 复制一份改过的副本；
   - 绕开该文件改调用方；
   - 以「这文件太大不敢动」为由拒接或拖单。

   **回避比行数超标更严重**：行数超是历史债，回避是让债永久化。判据：交付里出现「未改 X 文件因为超线」⇒ 直接 `P0` 退回。

1. **边改边打扫**：每次修改**同时**把该文件净降 ≥10%。这是打扫的量，**不是准入闸**——不满足也不能变成不改。一次降不到位就是拆得不够，继续拆，不是不动手。**唯一例外见 11.1.1（努力拆过仍拆不动，给理由＋证据即可）。**
2. **必须过测试**：降行数后该文件相关的定向测试组必须全绿（`engine_incremental_architecture.md` §七 的分组命令），全量归 QA 阶段 3。降行数导致测试变红 ⇒ 拆法不合格，退回重拆，**不许改测试让红变绿**。
3. **拆的是真模块**：按 §三／§四 落进 `engine_parts/` 或仓根新模块，不许新建子类／mixin 塞回同一文件（§八 第 2 条）。新模块必须满足 worker 章程 §3.2 门槛（≥3 函数或 ≥1 类、或被 ≥2 处调用、或有独立测试）。

### 11.3 与旧条文的关系

- 原 §一「只减不增」是本条的**弱形式**：不增即可。本条把它收紧为「每次改动净降 10%」。
- **本条取代 §一 对超限文件的适用**：超限文件不再满足于「行数不变」，必须净降。
- 未超 300 行的文件不受本条约束，仍按 worker 章程 §3.1 执行。

### 11.3.1 为什么是「边改边打扫」而不是「先封存」

用户 2026-11-25 原话：**「允许修改超线文件，合理必要的修改是允许的，但是必须边修改边打扫，是为了让系统更容易维护。更严谨因为此规则而回避修改超线文件，使系统更难维护。」**

- **封存大文件 ≠ 可维护**：不让人碰，缺陷就永远留在里面，只能在外面绕，结果是两份逻辑、两个真源。
- **边改边打扫**才是：功能需求照做，顺手把该块沉下去，文件越来越薄。
- 判据：一个改动如果**真的必须**在大文件里做，就做；做了就顺手拆一块出来。

### 11.4 交付必须报告

worker 交付里对每个被改的超限文件必须写：

```text
engine.py: 5787 → 5208（净降 579 行，10.0%），拆出 engine_parts/orchestration.py（N 行）
```

**未达标时改写成例外格式**（11.1.1）：

```text
wss_bus.py: 4289 → 4200（净降 89 行，2.1%，未达 10%）
理由：A/WSS 三块共享同一 socket 生命周期与重连状态，拆出会造成双向回调。
已试：①抽 WssFrame 数据类 → 卡在 17 处直接读 self._frames；②抽重连策略 → 卡在 3 处回调依赖 self._cfg 组合。
```

缺这行 ⇒ QA 按 §八 第 6 条判 P0 退回。**例外格式缺「理由」或「已试」任一段 ⇒ 按未达标判 P0。**

### 11.5 判据（可复跑）

```bash
# 改后行数必须 ≤ 改前 × 0.9
python -c "a,b=5787,5208;print('OK' if b<=a*0.9 else 'FAIL')"
# 新模块存在且非空
wc -l super_agent/engine_parts/<新模块>.py
# 定向测试组全绿
.venv/Scripts/python -m pytest -q tests/test_<相关>.py
```

**未达标时改用例外判据**：

```bash
# 交付里必须同时有「理由：」和「已试：」两段
grep -c "^理由：\|^已试：" <交付文件>   # 必须 ≥2
```

---

## 十二、文件结构与公共抽象

**条文与判据见 `rules/shared/rules/worker_conduct.md` §八（唯一真源，本节不复述）。**

本节只强调与超限文件规则的关系：**归位目录 + 公共抽象**是「边改边打扫」的另一半——行数下降靠拆模块，拆出来的模块要放对位置、公共能力要统一提取，否则只是把大文件切碎成散件。

- 拆出的模块按职责归入对应目录（`engine_parts/` 等），不堆仓根；
- 拆出的公共函数／常量／正则／数据类／工具类，若被 ≥2 处使用，提取到公共模块，不各留一份；
- 一次性使用、无第二调用方的逻辑**不提取**（过度抽象同样是债）。

