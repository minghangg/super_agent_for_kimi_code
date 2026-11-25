# 代码风格：命名与注释（用户 2026-11-25 令）

> 本文件是「代码长什么样」的唯一真源。文件规模与模块划分见
> `rules/shared/skills/engine_incremental_architecture.md`，公共抽象见
> `rules/shared/rules/worker_conduct.md` §八。三份合起来才完整，本文件只管**名字**和**注释**。
>
> 为什么单独成文：仓库此前只有「文件多大」和「东西往哪放」两条规矩，**没有一个字说名字怎么起、注释怎么写**。
> 结果是大文件里堆满 `_do2`、`tmp`、`d`，读的人得先猜。体量规则治不了这个。

## 1. 命名（PEP 8 ＋ Google Python Style Guide）

### 1.1 强制项

| 对象 | 写法 | 正例 | 反例 |
|---|---|---|---|
| 模块／包 | 全小写，可用下划线；包名尽量不用下划线 | `pick_worker_model.py`、`engine_parts/` | `PickWorkerModel.py`、`Engine-Parts/` |
| 类 | `CapWords` | `CharterMissing`、`DispatchAdapter` | `charter_missing`、`dispatch_adapter` |
| 函数／方法／变量 | 小写＋下划线 | `resolve_root`、`session_dir` | `resolveRoot`、`ResolveRoot` |
| 常量 | 全大写＋下划线 | `HOLD_LABEL`、`REFUSED_RETRY_ROUNDS` | `holdLabel`、`hold_label` |
| 内部（模块内私有） | **单**前导下划线 | `_state_root()` | `__state_root()` |
| 异常类 | `CapWords` ＋ `Error` 后缀 | `CharterMissing`（非错误语义可不带）、`ConfigError` | `CHARTER_MISSING` |

出处：模块／包名见 [PEP 8 · Package and Module Names](https://peps.python.org/pep-0008/#package-and-module-names)；
类名 `CapWords` 同篇 · Class Names；函数与变量小写带下划线同篇 · Function and Variable Names。
Google 侧同口径见 [Google Python Style Guide §3.16.2](https://google.github.io/styleguide/pyguide.html#3162-naming-conventions)。

### 1.2 三条禁令（每条都有代价）

1. **禁止双前导下划线**（`__name`）。Python 用名字改写（name mangling）实现它，结果是
   `_FooBar__name`——**更难读、更难测、也不是真私有**。要表达「内部」用**单个**下划线。
   出处：Google Style Guide §3.16.2「we discourage its use as it impacts readability and testability」。
2. **禁止拼音与英文混拼、禁止无意义缩写**。`DaZhePromotion` → `AlibabaPromotion`；
   `AbstractClass` 缩成 `AbsClass` 属违规。出处：阿里巴巴 Java 开发手册 · 命名规约。
3. **禁止单字母变量名 `l`／`O`／`I`**。它们与数字 `1`／`0` 在多数字体下无法区分。
   出处：PEP 8 · Names to Avoid。

### 1.3 一条判据

**看名字能说出「它是什么」还是「它怎么做的」**：公开 API 的名字反映**用途**，不反映实现。
叫 `get_user_list()` 不叫 `query_db_and_map()`。出处：PEP 8 · Overriding Principle。

## 2. 注释与文档字符串

### 2.1 模块必须有 docstring

每个模块的第一条语句是模块 docstring，**一句话说明这个模块是什么、为什么存在**。
不是「工具函数集合」这类空话，要写清它解决的问题。

```python
"""Role charters: a baseline that always loads, plus an optional supplement.

Why this module exists (ISS-95): before this, the charter a fresh session read
was a single file path handed to ``dispatch_cli``. ...
"""
```

出处：[Google Style Guide §3.8.2](https://google.github.io/styleguide/pyguide.html#382-modules)
「Files should start with a docstring describing the contents and usage of the module.」

**现状**：`super_agent/` 63 个模块中 62 个已有模块 docstring，仅 `at.py` 缺。判据见 §4。

### 2.2 函数 docstring 的三个触发条件

满足**任意一条**就必须有 docstring：

1. 属于公开 API；
2. 体量不小（超过 40 行）；
3. 逻辑不显然（有隐式前提、副作用、非常规返回）。

出处：Google Style Guide §3.8.3。**只有一行且自解释的私有小函数不必写**——
「只重复函数名」的 docstring 是噪音，同样违规。

### 2.3 注释写「为什么」，不写「是什么」

代码已经说了「是什么」。注释唯一不可替代的价值是**记录代码无法表达的决策**：
为什么选这个阈值、为什么这里要特判、这个分支对应哪次事故。

```python
# 好：记录一次真实事故
# 429 报文里的「预计 ... 重置后恢复」由 supervisor 手工登记，不能自动推断。

# 坏：重复代码
# 把 index 加一
index += 1
```

### 2.4 注释必须随代码一起改

改了参数、返回值、核心逻辑，**同一笔改动里改注释**。代码与注释不同步，
比没有注释更坏——读的人会相信错的注释。
出处：阿里巴巴 Java 开发手册 · 注释规约「代码修改的同时，注释也要进行相应的修改」。

### 2.5 禁止注释掉代码

要废弃的代码**删掉**，git 记得住。注释掉的代码无法被测试、无法被搜索、会随时间腐坏，
并且让读者分不清「暂时禁用」还是「忘了删」。

## 3. 一处反向边界

**不要为了合规写废话。** 本节所有要求都是「缺了会让人误判」时才成立。
一个 `def _now(): return time.strftime(...)` 不需要 docstring；
一个只有三行的 `_bad(profile)` 也不需要。**过度注释与缺少注释同样是债。**

出处：PEP 8 · A Foolish Consistency is the Hobgoblin of Little Minds
「Readability counts」——规则的目的是可读，不是规则的完整性。

## 4. 判据（可复跑）

```bash
# 文件行数、函数行数、模块 docstring 三项一次跑完
python tools/module_size.py

# 只看超线文件
python tools/module_size.py --files

# 只列出缺模块 docstring 的文件
python tools/module_size.py --docstrings
```

退出码：`0` ＝ 无新增违规；`1` ＝ 有；`2` ＝ 读不到输入（没跑过的检查不许显示为通过）。

**阈值与出处**：

| 项 | 阈值 | 出处 |
|---|---|---|
| 单文件行数 | 300 行（本仓目标，见 `engine_incremental_architecture.md`）；750 行是行业告警线 | SonarQube S104 默认 750；本仓取更严的 300 |
| 函数行数 | 40 行 | Google Style Guide §3.18「If a function exceeds about 40 lines, think about whether it can be broken up」 |
| 模块 docstring | 必须有 | Google Style Guide §3.8.2 |

### 4.1 历史超线文件不因体量变红（用户 2026-11-25 令）

**已经超线的文件，正常修改不会让判据失败。** 只有两件事失败：

1. **原本不超线的文件越过线**——这是本次改动引入的新问题；
2. **超线文件增长超过容差 30 行**——容差是刻意留的：一次正常修改加几行很常见，禁止它等于禁止修改。

「每次净降 10%」是**目标，不是闸门**。工具会显示每个文件的 `净降 N`／`+N`／`未变`，
评审一眼看出这次有没有打扫，但不会因为它没降够就判红。

**为什么不设成硬闸**（实测教训）：第一版让「涨过基线」直接失败，结果**只要碰 `engine.py` 就红**。
这与 `worker.md` §3.1.1 第 0 条「严禁因怕降行数而回避修改」直接冲突——
把大文件锁死，人只能在旁边另建一份实现，那比原来的债更坏。
**判据的作用是让人看得见，不是让人不敢动。**

**历史超限文件照常可以改**，处置办法见
`rules/shared/skills/engine_incremental_architecture.md` §十一（边改边打扫，每次净降 ≥10%）。
