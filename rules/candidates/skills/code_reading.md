# 读代码：三跳 ＋ 四坑（`ag.py`）

> 工具：`.kimi/tools/ag.py`（薄封装 ast-grep 0.51.0，只用标准库）。**不改 kimi-code 配置**，一律通过 Bash 调用。
> 背景与实测数据：`.kimi/report/ast_grep_reading_tool_20261027-2137.md`。

## 1. 三跳（顺序固定，不许倒着用）

1. **Grep（文本）**——先确认代码长什么样：函数是否带返回注解、是否 `async`、装饰器是 `@router.` 还是 `@app.`、要查的东西在不在 SQL／模板／正则的字符串里。
2. **ast-grep（结构）**——用 `ag.py outline` 建地图（哪个文件有哪些函数），用 `ag.py find` 精确定位调用点、审计唯一写入口；`ag.py routes` 出接口表。
3. **Read（精读）**——只读第 2 跳命中的那几个位置，按行号直接读，不整文件读。

## 2. 四坑（模式写错会**静默 0 命中**，不报错）

| 坑 | 现象 | 正确做法 |
|---|---|---|
| 返回注解 | `def $F($$$A): $$$B` 只命中 53/502 | 签名里的返回注解是模式的一部分：`def $F($$$A)$$$RET: $$$B`（`$$$RET` 空匹配全覆盖） |
| async | `async def` 是另一种节点，上面的模式一条都不命中 | 单独跑一条 `async def $F($$$A)$$$RET: $$$B`；`ag.py outline` 已内置两条 |
| 双 `$$$` | `repo.update_order($$$A, status="paid", $$$B)` 实测 0 命中 | 同一参数表里只能有一个 `$$$`；改用单锚点 `repo.update_order($A, status=$S, $$$B)` |
| 看不见字符串 | `status = "paid"` 结构搜索 0 命中，因为写在 SQL 里 | AST 里没有 SQL／模板／正则内容；这类查询只能回第 1 跳用 Grep |

## 3. 何时不要用 ast-grep

- **跨文件数据流／调用链**：它不做可达性分析，看不出 A 调 B 调 C。
- **动态派发**：`getattr`、字典查表、字符串拼出的方法名、框架反射注入——AST 上没有静态调用边。
- **证明「没人调用」**：0 命中只能说明「这个模式没匹配上」，不能说明「代码里没有」。要下这种结论必须 Grep 交叉验证（搜裸函数名）。
- 大段阅读：不要拿 `find` 反复扫来代替 Read；命中位置确定后直接 Read。

## 4. `ag.py` 用法（三条示例）

```bash
# ① 建地图：全仓 Python 函数清单（去重、按文件+行号排序、默认截断 200 行）
python3 .kimi/tools/ag.py outline app --limit 400

# ② 结构精查：唯一写入口审计（单锚点，不写双 $$$）
python3 .kimi/tools/ag.py find 'repo.update_order($A, status=$S, $$$B)' app

# ③ 接口表：FastAPI 路由 -> 紧随其后的 def（含 @app. 与 @router.）
python3 .kimi/tools/ag.py routes app
```

前端：`outline frontend/src --lang tsx`（内置 `function $C(...)` 与 `const $C = (...) => {...}` 两条模式）。

## 5. 退出码与内置护栏

| 码 | 含义 | 处理 |
|---|---|---|
| 0 | 有命中 | —— |
| 2 | selftest 失败（内置函数模式在 `app/api/pay.py` 0 命中） | 模式／语言可能写错，先修工具再查业务 |
| 3 | find 的模式里有 ≥2 个 `$$$`（非内置模式） | 改单锚点；要清单用 `outline` |
| 4 | 0 命中 | 按 stderr 提示逐条自查（返回注解／async／双 `$$$`／目录与语言） |
| 5 | 找不到 ast-grep | 按上游文档装（`pip install ast-grep-cli`），装进项目自备的虚拟环境 |

- 二进制优先级：`env AST_GREP` → 项目自备的 ast-grep 路径 → `PATH` 里的 `ast-grep`。
- **永远不要用 `/usr/bin/sg`**：那是 shadow 包的 setgroups 工具（`sg group -c cmd`），不是 ast-grep；`ag.py` 会主动跳过它。
- `find`／`outline` 执行前默认先跑 selftest（`--no-selftest` 可跳）；自检：`python3 .kimi/tools/test_ag.py`（无 ast-grep 时整组 skip）。
