# 会话管理（列 / 归档 / 恢复）—— **supervisor 的能力**（2026-10-16 建）

> 适用范围：team（**本文件不含专名/机器值/凭据值**）
> 一句话：**把堆积的 kimi-code 会话列表整理干净** —— 能列出（含已归档）、能批量归档、能恢复。
> **⚠ 边界（2026-11-02 实测）**：归档只整理列表，**不防投喂**——`send`／`dispatch --session` 仍能把消息送进去。防再被叫醒见 §2b。
> 由来：用户 2026-10-16 要求「整理 session 列表，后面不会再用的归档一下」，随后裁定「**刚才那能力作为 supervisor 的 session 管理能力**」。

## 0. 它解决什么

派出去的独立会话（`dispatch_session`／子代理）会**堆在 kimi web UI 的会话列表里、不自动清理**（见 `.kimi/skill/dispatch_session.md` 的「上限」）。本能力＝把它们按**「还会不会用」**分类，把不会用的归档掉，让列表只剩在办的活。

## 1. 机制（**唯一真源＝本地 kimi-code 服务 REST**）

| 动作 | 调用 |
|---|---|
| 列（**默认不含已归档**） | `GET /api/v1/sessions?include_archive=false` |
| 列（**含已归档**） | `GET /api/v1/sessions?include_archive=true` |
| **批量归档** | `POST /api/v2/sessions:archive`，body `{"ids":[…]}` |
| **批量恢复** | `POST /api/v2/sessions:restore`，body `{"ids":[…]}` |
| （单条归档；本能力少用） | `POST /api/v1/sessions/{id}:archive` |

- base 默认 `http://127.0.0.1:58627`（探活 `GET /api/v1/meta` 返 `code:0`）；
- **token 在 `$KIMI_CODE_HOME/server.token`** —— **只许程序读，永不打印、永不进记录**（同 `.kimi/resource/**` 的口径）；
- 响应统一包在 `{code,msg,data,request_id,details}` ⇒ **取值取 `data`**。
- 会话的判读字段：`id`／`workspace_id`／`title`／`updated_at`／`busy`／`archived`。

## 2. ⚠ 四条实测坑（2026-10-16 全部踩过或验过）

1. **列已归档必须用参数名 `include_archive`** —— 我试过的 `?archived=true`／`?include_archived=true`／`?status=archived` **都被静默忽略**（返回条数与默认一样），会让人误判「没有归档会话」。实测：默认 129 条 vs `include_archive=true` **239 条**。
2. **归档后该会话即从默认列表消失** ⇒ 之后用默认列表找不到它是**正常**，别当成"被删了"（我第一次复核查它时就因此报了个 `IndexError`）。
3. **API 侧没有 un-archive**：`POST …:unarchive` 返 `40001 unsupported action`、`DELETE …:archive` 返 **404**。**唯一的恢复路径是 `POST /api/v2/sessions:restore`** —— 这也是 `dispatch.py:186` 在用的那条（归档会话收不下 turn ⇒ 派活前先 restore；`README` 里那句「归档的会自动 restore」就是它）。
4. **归档「可逆但会从视野消失」** ⇒ 因为第 3 条，**批量归档前必须先把选中的清单原样打印出来**（id／workspace／时间／标题），归档后再复核「**选中的都不在默认列表里、其余一个没少**」。

### 2b. ⚠⚠ 归档**不防投喂**（2026-11-02 steward 实测，推翻旧口径）

> **改动时刻**：2026-11-02 21:37（steward，<角色> 批准）。
> 依据：一次性会话 `session_b9bd4e62` 实跑，见下表与下方记录。

**旧口径（错）**：`v2 归档＝安全／能停一切`。
**实测口径（准）**：`v2 归档`只做两件事——**移出默认列表 + 标 `archived=true`**。它**拦不住后续投喂**。

| 动作 | 归档后的 `archived` | 能不能把消息送进去 |
|---|---|---|
| `POST /api/v2/sessions:archive` | 置 `True`，移出默认列表 | —— |
| `python tools/sess.py send <归档id> "…"` | **仍是 `True`** | **能**。返回 `ok=True status=running`，消息真进 wire，`busy` 转 True |
| `dispatch.py --session <归档id>` | **自动 restore，变回 `False`** | **能**。它先查 `archived`、发现真就调 `sessions:restore`，再投（见 `dispatch.py:205-215`） |

实测记录（2026-11-02，一次性会话）：

```
$ python tools/sess.py send session_b9bd4e62-… "归档后测试"
queued http=200 ok=True {"status":"running",...}
# 查 /api/v1/sessions?include_archive=true → archived=True busy=True

$ python .../dispatch.py --session session_b9bd4e62-… --task "只回 OK"
[dispatch] restored archived session session_b9bd4e62-…
OK
# 再查 → archived=False          ← 归档被它解除了
```

**⇒ 防「归档后还被叫醒」的正确做法，必须两步：**

1. **杀掉投喂者进程**（`taskkill /PID <pid> /F`；Git Bash 的 `kill` 对 Windows 原生进程无效）；
2. **让投喂者读不到旧 id** —— 看门狗必须**每轮重读** `state/sessions.json`，不能在启动时读一次缓存。

> 反例（2026-11-02 真事故）：`issue_watch.py` 在 `while` **之前**读一次 `sessions.json`，
> PM 换血后内存里仍是旧 id，每 300s 往已归档的旧会话投喂，6 小时投了 9 次。
> 修法：`reg = json.loads(...)` 移进 `while` 内。参考 `tools/reedkit/rrotate.sh` 第 4f 步（残留投喂者自检）。

## 3. 分类口径（**该归档什么、该留什么**）

**归档**（按确定性从高到低三档）：
1. **验证/探针沙箱**：`1+1=?`／`ping`／`probe-*`／`verify-*`／`warmup` 这类**纯能力验证**会话（通常集中在同一个 workspace，可整组清）。
2. **已收口的执行会话**：报告已落盘、成果已提交/已推送、结论已进台账的（例：`b1-prune-phase*`、`b4-*`、`*-review`、`*-stats-sync`）。
3. **无标题 / 叫 `New Session` 的历史会话**（多为其他项目早期的空壳）。

**必留（不归档）**：
- **当前在跑的**（`busy=true`，或 `fleet_status --status running` 里能看到）；
- **下一步要复用的** —— ⚠ **最易踩的一条**：复用前**先核 id 真的存在**（拉 `include_archive=true` 搜一遍）。**id 记错时的报错形态是"拿不到答案"，极易被误判成"模型没回"**（2026-10-16 实测：记录里的 A5 复用会话 id 根本不存在，另有一个真实存在的同名 `-2` 会话）；
- **不是本项目的会话**（别的项目、别的成员的）⇒ **不擅自归档，交用户判**；
- **判定不了的一律不归档**（宁可留）。

## 4. 一句话流程

1. 拉 `include_archive=true` 摸清总量（**含归档数常远大于默认列表**，别以为"看到的就是全部"）；
2. 按 §3 分类，**把要归档的清单打印出来**（存档进报告/台账）；
3. 只对**非 `busy` 且确凿不用**的批量 `archive`；
4. 复核两条：选中项都不在默认列表 + **其余一个没少**；把「**归档 N 个／留了哪些／怎么恢复**」写进台账；
5. 需要时 `restore` 回来。

## 5. 不做（用户未提要求）

- **不删会话**：`POST /api/v1/sessions/{id}:delete` 是破坏性的，**本能力不用它**；
- **不封装成工具**（本能力＝3 个 REST 调用，封装收益低；将来若要批量/定时再说）；
- **不替用户决定别的项目的会话。**
