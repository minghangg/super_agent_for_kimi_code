# AT 前置需求与弱 PM：操作手册

## 1. 用户看到的流程

```text
用户目标
→ supervisor 建草稿 Issue
→ AT 调研并提问
→ 用户打开本地链接，选选项/补充，点“确认并回答 AT”
→ AT 形成 READY 需求包
→ supervisor 批准
→ PM 按固定策略调度 worker/QA
→ supervisor 收口
```

用户不阅读 JSON，不需要登录远端管理后台。首版服务只监听 `127.0.0.1`，链接只能在本机使用。

## 2. 一次性部署

项目策略表中显式开启：

```json
{
  "projects": {
    "example-project": {
      "repo": "owner/repo",
      "cwd": ".",
      "issues": [123],
      "actions": ["collect", "reconcile", "bus_inject", "archive"],
      "roles": ["worker", "qa", "pm", "at"],
      "at_mode": true
    }
  }
}
```

公开默认是 `false`。关闭此开关即回退既有流程，不需要删除 Issue、Assignment 或需求历史。

启动问卷服务：

```bash
python -m super_agent.at serve start --allow-send
python -m super_agent.at serve status
python -m super_agent.at serve stop
```

默认地址为 `http://127.0.0.1:58731`。引擎 `engine.at_question_port` 与服务 `--port` 必须一致。`start` 是前台服务命令：在终端保持运行，或由 agent 的后台任务/系统服务管理器托管；另开终端执行 `status` 和 `stop`。需要日志时由托管方重定向输出。

`--allow-send` 才会把答案回传真实 AT。未给时页面可保存答案文档，但会明确提示未回传。

## 3. supervisor：建单、绑 AT、批准

```bash
python -m super_agent.issue_cli create --role supervisor --project <项目> \
  --title "<用户目标>" --body "用户原话：……"
python -m super_agent.at start --role supervisor --project <项目> \
  --issue <n> --cwd <项目仓绝对路径>
python -m super_agent.issue_cli open --role supervisor --project <项目> --issue <n>
```

AT READY 后：

```bash
python -m super_agent.requirements sync --project <项目> --issue <n>
python -m super_agent.requirements show --project <项目> --issue <n>
python -m super_agent.requirements approve --role supervisor \
  --project <项目> --issue <n> --revision <revision> --hash <sha256:...>
```

批准前核对目标、范围、验收、反例和 T1/T2。权限、生产和重大取舍必须先取得用户明确批准。

## 4. AT：发布问题或 READY

生成草稿：

```bash
python -m super_agent.requirements init --project <项目> --issue <n> \
  --objective "<一句话目标>" --risk-tier T1 --output <需求包.json>
```

一个带选项的问题示例：

```json
{
  "schema_version": 1,
  "requirement_id": "owner/repo#123",
  "revision": 1,
  "status": "clarifying",
  "objective": "交付 AT 标准流程",
  "context_refs": ["issue-123"],
  "scope": ["核心闭环"],
  "out_of_scope": ["管理后台"],
  "constraints": ["首版无新依赖"],
  "acceptance": ["批准后可生成调度包"],
  "negative_tests": ["旧 revision 不能提交"],
  "risk_tier": "T1",
  "review_policy": {
    "plan_qa": false,
    "implementation_qa": true,
    "worker_mode": "plan_and_implement",
    "test_level": "L1"
  },
  "open_questions": ["本次先交付哪个边界？"],
  "interaction": {
    "choice_mode": "single",
    "allow_custom": true,
    "options": [
      {"id": "core", "label": "先交付核心", "description": "不做管理后台"},
      {"id": "full", "label": "一次交付全部", "description": "包含管理后台"}
    ]
  },
  "approval_ref": "",
  "updated_at": ""
}
```

发布：

```bash
python -m super_agent.requirements validate --file <需求包.json>
python -m super_agent.requirements publish --role analyst --project <项目> \
  --issue <n> --file <需求包.json> --session-key <AT 的 at-... 键名>
```

用户回答后，AT 收一行：`【AT 答案】#123 r1 已回答；全文：<路径>`。读取该 Markdown，形成下一 revision。还有问题就继续一轮一个问题；没有问题就将 `open_questions` 置空并发布 READY。

## 5. 用户：打开与回答

通常直接点击 AT/supervisor 发来的链接。也可自行查看：

```bash
python -m super_agent.at questions
python -m super_agent.at open <question_id>
```

页面操作：选择选项 → 填写可选补充 → 点击“确认并回答 AT”。提交后页面显示已回传；失败时显示未回传原因。过期链接返回 410，不保存也不回传。

## 6. PM：只路由

```bash
python -m super_agent.requirements packet --project <项目> --issue <n> \
  --assignment <assignment_id> --output <packet.json>
python -m super_agent.pm_router --packet <packet.json> --event start
```

后续事件：`plan_delivered`、`plan_qa`、`implementation_delivered`、`implementation_qa`、`requirement_changed`。QA 事件使用 `pass/reject/redirect/requirement_ambiguous`。

PM 只执行返回的角色、阶段和复用/新建会话，不解释需求。

## 7. worker/QA 评论

从 packet 复制版本标记：

```text
<!-- requirement: owner/repo#123 revision: 2 hash: sha256:... -->
```

worker 交付、QA 结论和 PM verdict 都必须带。缺标记、旧 hash、跨 Assignment 或旧 delivery 坐标都会被拒绝。

## 8. 需求变更与恢复

AT 发布新 revision 后，引擎自动将旧 Assignment 标为 `waiting_requirement`。新 revision 批准后，同版本可恢复，不同版本新建 Assignment。启动恢复发现旧 prepared outbox 时，将其关闭为 superseded，不执行外部发送。

## 9. 验收入口

```bash
python -m pytest tests/test_requirements.py tests/test_at_requirement_engine.py \
  tests/test_at_requirement_questions.py tests/test_at_interaction_page.py \
  tests/test_pm_router.py -q
python -m super_agent.at serve status
python -m super_agent.requirements sync --project <项目> --issue <n>
```
