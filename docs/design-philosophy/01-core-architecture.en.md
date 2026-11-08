[English](01-core-architecture.en.md) | [中文](01-core-architecture.zh.md)

# 01 · Core Architecture

> Series: design philosophy ｜ 01 Core architecture ｜ [02 Requirements Analyst](02-requirements-analyst.en.md) ｜ [03 Agent-to-agent communication](03-agent-communication.en.md) ｜ [04 Human-facing Reporter](04-human-reporter.en.md) ｜ [05 Artifact recycle bin](05-artifact-recycle-bin.en.md)
> Version: v3 · 2026-11-07
> Constraints: unofficial project, one person maintaining several projects, limited resources, no heavy database.
> Nature: a design baseline. It does not authorize creating repositories, changing rules, or starting implementation.

## Executive summary

This project is not "putting some official Router onto a personal machine". It fills two gaps for a **one-person, multi-project team**:

1. **Task automation** — events advance Issues, so PMs move less paper and the supervisor spends less attention watching progress.
2. **Outsourced memory** — project history leaves the supervisor's session. When a session is rotated, only a minimal set of global facts is restored, so the handover does not lose the core context.

Roles must stay separate:

- **supervisor** (a configurable role name; the default is `supervisor`): user intent, authorization, cross-project arbitration, release and close-out.
- **Project PM**: one dedicated PM per project. Splits requirements, writes briefs, dispatches workers and QA, handles day-to-day routing.
- **LLM-free engine**: events, state machine, recycling, lightweight index, recovery packs. It makes no semantic decisions.
- **worker / QA**: isolated execution and independent verification. The existing quality discipline is unchanged.

Storage stays minimal:

```text
GitHub Issue       long-lived process record
index.json         cross-project run index and pending references
events.jsonl       bounded event log
outbox.jsonl       state machine for external actions
health.json        health snapshot
```

No database, no vector search, no message queue, no cloud Router, no multi-tenancy, and no "super PM" persona.

## 1. Roles and information flow

### 1.1 supervisor: cross-project authorization and arbitration

Typically a high-effort, high-capability model. It is the user's alignment point and the only supervisor across projects.

Responsibilities:

1. Translate the user's words into executable project goals, boundaries, priorities and acceptance criteria.
2. Decide which project an item belongs to, and open GitHub Issues under existing policy.
3. Arbitrate resources and priority across projects.
4. Arbitrate PM↔QA conflicts, requirement conflicts, two-failed-rounds cases and incident escalations.
5. Perform designated actions: open/close Issues, release, production writes, authorization, emergency handling.
6. Know which credential may be used for which action, and use credentials only through controlled tools.

It does not:

- Watch workers item by item, read whole project logs, or write briefs on a PM's behalf.
- Keep project history, interface detail or long briefs resident in its own context.
- Write secret values into prompts, the index, Issues, the outbox or handover material.
- Take back a project PM's day-to-day execution rights just because its own privileges are broader.

**The supervisor is not a PM, and it is not another per-project Router.**

### 1.2 `pm-<project>`: the in-project Router

Responsibilities:

1. Receive one sentence of requirement plus boundaries from the supervisor.
2. Read the project's Issues and rules; split work, write briefs, dispatch workers and QA.
3. Decide the two switches: design review, and implementation acceptance.
4. Track the project's Assignments, close-out, rework and re-dispatch.
5. Maintain a compact run summary for the project.
6. Escalate only what crosses projects: authorization, conflicts, release.

It does not:

- Hold the supervisor's global authorization.
- Open/close Issues, release or write to production on the supervisor's behalf.
- Handle other projects' work or read unrelated context.
- Forward raw worker logs or full comment chains to the supervisor.
- Write implementation, or judge quality in place of QA.

### 1.3 The LLM-free engine

Responsibilities:

- Collect and deduplicate Triggers; map them to Issues.
- Deterministic Fast Path, state machine, cooling, retry, outbox.
- Thin adapter calls for dispatch, bind and archive.
- Maintain a rebuildable cross-project index.
- Generate the supervisor's global brief, each PM's project brief, and per-Issue recovery packs.
- Notify only the currently registered PM/supervisor, and only for items that meet escalation conditions.

It is not a third PM, not a supervisor, holds no production credentials, and does not split requirements, arbitrate semantics or accept quality.

### 1.4 Information flow

```text
                            user
                             │
                             ▼
                ┌───────────────────────────┐
                │ supervisor                │
                │ translate / authorize /   │
                │ open Issues / arbitrate   │
                │ release / cross-project   │
                └────────────┬──────────────┘
                             │ one sentence + boundaries
             ┌───────────────┼───────────────┐
             ▼               ▼               ▼
       pm-project-A    pm-project-B    pm-super_agent...
       in-project      in-project      dedicated PM
       Router          Router          for this repo
             │               │               │
             └───────────────┼───────────────┘
                             ▼
                   LLM-free engine
        ┌──────────────────────────────────┐
        │ Triggers / Fast Path / state     │
        │ outbox / cooling / bind / recycle│
        │ global index / briefs / recovery │
        └──────────────────────────────────┘
                     │                 │
                     ▼                 ▼
               worker / QA        GitHub Issue
               isolated runs      full record
```

- User intent: user → supervisor → the owning PM → worker/QA.
- Day-to-day execution: event → engine → owning PM.
- Arbitration: PM → supervisor. Never PM → PM, and never straight to the user.
- Workers and QA write to Issues; PMs forward only necessary conclusions.

## 2. Mapping to the Super-agent architecture

| Super-agent concept | Here | Difference we must keep |
|---|---|---|
| Router | Project PM; cross-project semantics by the supervisor | No single long-context Router owns every project's detail |
| Worker | Isolated worker/QA sessions | QA stays independent; tiered verification unchanged |
| Trigger | Event bus, GitHub, timers, config changes | An external event does not by itself grant execution rights |
| Fast / Slow Path | Program rules → project PM → supervisor | One extra escalation layer across projects |
| Thread | The long-lived task identity of one Issue | Full process lives in the Issue; the local index is rebuildable |
| Inline / Spill | Short summaries plus comment/file references | Context cost comes first |
| Persistent state | index/outbox/health plus Issues | No database, no cloud memory |
| Watchdog | Engine health and stuck-task detection | Do not build another layer of busy-polling |

An official Router can carry more memory and requirement decomposition. We leave the in-project version of that work to a dedicated PM. The supervisor keeps only cross-project essentials, so one core context never becomes the team's single point of failure.

## 3. Memory and context design

### 3.1 Three kinds of information, kept apart

| Kind | Where it lives | Read by the supervisor by default |
|---|---|---|
| Requirements, briefs, deliveries, QA, decisions | Issue body/comments | No — fetched by id on demand |
| Current runs, Assignments, cursors, waiting items | index / outbox | Only the global brief |
| Role rules, authorization limits, recovery entry points | Charter/handbook plus a short index | A minimal set at startup |

Credentials stay with the auth configuration and controlled tools. They enter none of these layers.

### 3.2 `index.json` example

```json
{
  "schema_version": 1,
  "projects": {
    "repo-a": {
      "pm_session_ref": "state/sessions.json#pm-repo-a",
      "active_threads": 2,
      "waiting_supervisor": []
    }
  },
  "threads": {
    "owner/repo-a#12": {
      "stage": "working",
      "owner": "worker",
      "assignment_id": "asg_12_worker_1",
      "waiting_on": "worker_report",
      "latest_decision_ref": "issuecomment-123",
      "last_event_seq": 789,
      "retry_count": 0,
      "next_retry_at": null,
      "updated_at": "..."
    }
  }
}
```

Also kept: the last error, worker/QA Assignment references, archive tombstones and a short summary. Not kept: long history, full briefs, full logs or production privacy data.

### 3.3 Minimum memory-pack commands

```bash
python -m super_agent brief --for supervisor
python -m super_agent brief --for pm --project <name>
python -m super_agent context --issue <repo>#<n>
```

**The supervisor brief contains exactly:**

1. role and authorization recovery entry points;
2. current projects and their PM references;
3. items waiting on the supervisor;
4. items waiting on the user;
5. references to recent cross-project decisions;
6. system health.

Output contract:

- Fixed sections, a line budget and a byte budget; one screen is the target.
- When nothing waits for arbitration, do not list every healthy project's history.
- Over budget, keep waiting-user, waiting-arbitration and release/incident items; compress healthy projects to counts.
- Anything truncated must state "N more items, here is the command to fetch them". Nothing disappears silently.
- This is a **lossy but traceable** summary. It does not claim lossless memory.

A **PM brief** contains only one project's active Threads, waiting items and necessary references.

**Issue context** is the only thing that fetches an Issue's current goal, latest decisions, relevant comment ids and acceptance requirements.

### 3.4 Full supervisor rotation

```text
old supervisor hits the threshold
→ pause new cross-project decisions (running workers/QA are not interrupted)
→ engine generates the global brief and the pending-items list
→ start the new supervisor; it reads role rules + brief
→ new supervisor reports its own context usage
→ switch the session registry key first
→ new supervisor verifies each pending-arbitration / pending-user / release item
→ engine delivers only to the new sid
→ stop the feeders and monitors that point at the old sid
→ archive the old session and read it back
→ resume cross-project decisions
```

- The outgoing supervisor is not asked to recite project history.
- PM handover documents are not dumped on the incoming supervisor.
- **Archiving is not the same as stopping delivery.** The old sid's event subscription and control target must be severed.
- While rotation is in flight, workers and QA keep running; new supervisor decisions queue.
- After recovery, the new session must be able to answer: which project, which Issue, waiting on what, who may decide, where the original text is.

PM rotation works the same way, but freezes only that project's new dispatches and reads a single-project brief. Other projects are unaffected.

## 4. Cross-project escalation

One uniform short form:

```text
Escalation type: authorization / requirement / conflict / release / incident
Project and Issue: <repo>#<n>
Current stage: <stage>
Decision needed from the supervisor: <one question>
Options and impact: <up to three>
Evidence references: <issue comment id / evidence id>
```

Rules:

- Anything a PM can settle does not escalate; pure execution events never reach the supervisor.
- Only secrets, production, close-out, release, cross-project resources and privilege expansion reach the supervisor.
- The engine sorts; it never decides.
- The supervisor's decision is written back to the Issue; the engine updates the index; the PM receives only its own project's decision.
- Follow-up events on the same Thread go to the original PM/owner first; the supervisor is not asked to re-split the requirement.
- The engine resolves PM and supervisor sids from the current session registry every time. It never reuses a startup-time cache.

## 5. Repository and collaboration governance (stage 0 prerequisite)

1. The user explicitly approves the repository; this document is not an instruction to create one.
2. Register the repository in project config and enable it only after the GitHub channel is verified.
3. A dedicated `pm-super_agent_for_kimi_code` must be created. Do not borrow another project's PM. That PM manages this repository, not the whole team.
4. This repository's charter limits workers to this repository. Workbench rules, credentials and other projects' code are out of scope.
5. Local commits only by default; no standing push right. First push and release require an explicit user instruction.
6. This repository's own charter, manifest, dependencies and test conventions are established in stage 0. Do not copy the whole private workbench rule set.
7. Verify the real delivery of the two prerequisite interfaces (event bus and process-document governance). Until they land, fall back to single-file mtime polling.
8. In-flight code in the private workbench must stay independent. Building this repository must not overwrite anyone else's work.

## 6. Creation rights and responsibility boundaries

### 6.1 Default: `pm_created`

```text
engine prepares an Assignment draft
→ queued / waiting_person
→ the project PM creates worker/QA
→ bind is registered
→ the engine takes the sid and continues monitoring and recycling
```

The draft contains only the Issue, role, action, context references, a fixed profile and the acceptance level. This mode still delivers deduplication, state tracking, later wake-ups and recovery — without exceeding authority.

### 6.2 Explicit pre-approval: `engine_preapproved`

Creating sessions from a fixed profile is a rule change. It counts as approved only when the user or the supervisor approves it explicitly and it is written into project rules.

Narrow authorization:

- Only allowlisted projects, Issue types and actions.
- A fixed profile for workers.
- The QA profile follows whatever charter is current at the time.
- cwd, role, naming, test level and the brief comment are all fixed by the Assignment.
- Executed through the PM dispatch adapter, keeping the audit trail and Assignment records.
- Out-of-profile, out-of-scope, new-model or new-privilege cases return to the project PM, and escalate to the supervisor if needed.
- Turning the config off returns to `pm_created` immediately; existing Assignments are not cleared.

This delegates a project PM's execution right. It is not a delegation of the supervisor's authority, and it grants the engine no right to open/close Issues, release, write to production or touch secrets.

## 7. Lightweight storage and configuration

### 7.1 File layout

```text
state/
├── index.json          # cross-project run index + short refs pending supervisor
├── events.jsonl        # bounded event log
├── outbox.jsonl        # external-action state machine
├── health.json         # heartbeat and dependency health
└── engine.pid          # single-instance lock

config/
└── allowlist.json      # committable, secret-free, hot-reloaded
```

### 7.2 Read/write rules

- Read the index at startup; afterwards mutate in memory, write a temp file and replace atomically.
- Single process, single writer. No database locking.
- Rotate events with a bound (for example 5–10 MiB, at most two files).
- The index is rebuildable from Issues, the bus, bind records and the outbox.
- The supervisor brief is computed from the index. Do not maintain a second global long-term memory that can drift.
- Archiving leaves a tombstone: time, reason, delivery reference — not just a deleted sid.
- All of `state/` stays out of git. Config may be committed, but never with secrets.

### 7.3 allowlist example

```json
{
  "schema_version": 1,
  "defaults": {"mode": "observe"},
  "projects": {
    "super_agent_for_kimi_code": {
      "repo": "<owner>/super_agent_for_kimi_code",
      "mode": "observe",
      "creation_mode": "pm_created",
      "issues": [1],
      "actions": ["collect", "reconcile"],
      "max_active_writers": 1
    }
  }
}
```

- An unlisted repo, Issue or action is denied by default.
- A new Issue starts in observe and does not inherit write rights.
- `engine_preapproved` requires both configuration and rule authorization.
- Hot reload parses and validates paths/roles/actions/limits completely first; an invalid config keeps the previous one and warns.
- Hot reload affects only future events; it never rewrites history.
- The config carries no tokens; tokens are handled by controlled tools.

## 8. Event model and state machine

Trigger types:

```text
github.comment_added
session.turn_finished
session.interaction_pending
session.task_finished
session.failed
timer.retry
config.changed
```

A Trigger carries at least:

```json
{
  "event_id": "stable dedup id",
  "thread": "owner/repo#issue",
  "source": "github|bus|timer|config",
  "payload_ref": "comment id, event seq or config version"
}
```

Collection:

- Session side: long-poll the event bus.
- GitHub: poll active Issues for new comments on an interval, keeping a comment-id cursor.
- File events: reuse when available; do not write another watch producer.

No webhooks, no public port, no attempt at sub-second GitHub latency.

### Fast Path

```text
dedupe
→ allowlist
→ current Thread owner
→ state legality
→ model/network cooling and retry time
→ perform the action, or only update the index
```

| Event | Action |
|---|---|
| worker turn finished | check running tasks first, then collect the delivery, move to `waiting_review` |
| complete-delivery marker | the project PM checks the three items and sends to QA per current rules |
| QA returns work | back to the original worker, keeping the Thread context |
| QA passes | `waiting_release`, notify the supervisor |
| pending interaction | handle only under existing authorization, otherwise escalate |
| 429 / network failure | cool down and back off; do not create another worker |
| nothing material changed | record only; notify nobody |

### Slow Path

In-project judgement goes to the project PM. Only cross-project, authorization, requirement conflicts and release go to the supervisor.

What the PM receives: the Issue's current stage, a summary of the latest event, one question to answer, and comment links. Not the comment chain.

### Two identities

- **Thread**: `owner/repo#issue`. Persists across sessions until the Issue closes.
- **Assignment**: `asg_<issue>_<role>_<n>`. One execution.

An Assignment records the sid, input comment, start time, status, retries, delivery comment and archive tombstone.

Stages:

```text
queued / working / waiting_review / waiting_qa /
waiting_person / waiting_release / blocked / completed
```

`busy` describes a session, not a task. While a background task is still running, the Assignment remains `working`.

## 9. Thin dispatch adapter

`dispatch_adapter.py` is the engine's only interface to the workbench tooling:

```python
choose_profile(assignment) -> Profile | Cooling
prepare_assignment(input) -> Assignment
create_worker(assignment) -> SessionRef       # requires pre-approval
create_qa(assignment) -> SessionRef           # requires pre-approval
bind_assignment(assignment) -> BindRef
send(assignment, text_ref) -> ControlResult
fetch(assignment) -> ReportRef
archive(assignment, reason) -> ArchiveResult
reconcile_close(thread) -> CloseStatus
resolve_role_session(project, role) -> SessionRef
```

Requirements:

- Reuse workbench Python functions where possible. Even a subprocess wrapper must return a structured dict or dataclass.
- Never parse human-oriented CLI output with regular expressions.
- Return `ok / code / session_id / prompt_id / source / error`.

## 10. outbox: no missed actions, no duplicate ones

```text
prepared ──► in_flight ──► done
                 │
                 └──► uncertain ──► reconcile ──► done | waiting_person
```

- `prepared` is persisted **before** the external call. A crash before the call is therefore known and safe to replay.
- Entering `in_flight` means "this may already have landed". After a restart it is **never resent**; it becomes `uncertain` and joins the reconcile queue.
- Reconciliation checks the recorded `sid` / `prompt_id` / `comment_id`, then marks `done` or `waiting_person`.
- `waiting_person` is **not finished**: it stays in the durable file (rotation does not drop it), is counted separately, and never enters the reconcile queue.

Unfinished items live in `outbox_state.json` (atomic write); `outbox.jsonl` only keeps state-change history. Rotation therefore drops history, never unfinished work.

## 11. Model cooling and backoff

### 11.1 One source of truth

Cooling state is recorded in exactly one place. No second consumer recomputes it.

### 11.2 Double check before dispatch

Before dispatching, check both that the model is not cooling and that the retry time has arrived. Either failing means do not dispatch.

### 11.3 Handling cooling

- Do not create sessions while cooling; only update the index.
- 429 and network failures back off. Do not recreate workers or repost comments.
- Cooling expiry triggers one rescan from the timer, not busy-polling.

## 12. Archive and close-out consistency

### 12.1 Create and bind sequence

```text
draft → create session → obtain sid → bind → record Assignment
```

A failure at any step is visible in the index. If creation succeeded but bind did not, recovery only completes the bind.

### 12.2 Engine-initiated archive

- Before archiving, confirm there is no active Assignment and no uncollected delivery.
- After archiving, keep a tombstone (time, reason, delivery reference).
- **Archiving is not the same as stopping delivery.** Sever the event subscription and control target at the same time.

### 12.3 Supervisor close-out

- The worker was already archived → do not archive twice and do not pretend success.
- QA still active at close-out → the close-out tool archives, then the engine re-verifies consistency.
- Bind cleanup or archive failed → enter `close_reconcile`; do not mark `completed`.
- A late event on an already-closed Issue → record an audit entry only; do not restart the work.

## 13. The resident-process trio and degradation

Mostly the standard library plus lightweight async: one process, one event loop, one state writer. External actions are serialized by default; one active writer per project in the first version.

### 13.1 Single instance and windowless autostart

- Atomic pid lock plus command-line and start-time verification; take over only when stale.
- The scheduled-task entry (logon trigger, no window, bounded restarts) is a **draft only**. Registering it is a separate implementation-time authorization.
- The action contains only the interpreter, a script and public arguments — no tokens.
- Bus/GitHub failures back off exponentially inside the engine.

### 13.2 Health

No new port. `health.json` is updated atomically and `doctor` cross-checks the pid:

```json
{
  "pid": 1234,
  "mode": "observe",
  "last_loop_at": "...",
  "last_bus_seq": 100,
  "last_github_poll_at": "...",
  "last_successful_action_at": "...",
  "active_assignments": 1,
  "degraded": false,
  "last_error": ""
}
```

Failures must be distinguishable: process dead, bus unreachable, GitHub unreachable, invalid config, outbox stuck, recycle failure.

### 13.3 Degradation

- Bus unavailable: keep using the existing facade; do not invent a direct connection.
- GitHub unavailable: keep consuming session events, but never fabricate comments or close-out state.
- State write failure: stop external actions, keep read-only observation, and alert.
- "Bus offline" and "project in observe" are two different fallback layers.

### 13.4 Commands

```bash
python -m super_agent run
python -m super_agent status
python -m super_agent once
python -m super_agent replay <issue>
python -m super_agent doctor
python -m super_agent brief --for supervisor
python -m super_agent brief --for pm --project <name>
python -m super_agent context --issue <repo>#<n>
```

- `once`: one round.
- `status`: only items that need a human or a PM.
- `replay`: rebuild the index; perform no actions.
- `doctor`: check dependencies, config, cursors, cooling, bind records and leftover Assignments.

## 14. Safety modes

### observe

Collect events and compute prepared actions. Record only; dispatch nothing, comment nowhere.

### assist

Automatic internal state, queries, recycling and Thread wake-ups. Sessions are still created by PMs by default, and Issue comments go through the existing role tooling.

### auto

Opened only after explicit pre-approval:

- session creation from a fixed profile;
- collecting a delivery when a worker finishes;
- returning QA rejections to the original worker;
- 429/network backoff;
- session rotation and archiving of probe sessions;
- low-risk stage reminders.

**Never automatic**: release, close-out, production writes, privilege expansion, history deletion, handling new requirements.

## 15. Staged delivery

Every stage must: back up the affected workbench tooling and policy files first, stage only this stage's files, keep an independent commit, and not push. Tests use one-off Issues and probe sessions only — never an in-service assistant as a target.

### Stage 0: role boundaries, memory baseline, read-only skeleton

**Preconditions**

- Verify the real delivery of the bus client, the control interface and the degradation path.
- Verify the real delivery of full-comment, temp-file and run-state governance.
- Wire the file event source if available; otherwise allow single-file mtime compatibility.
- The user approves this repository's governance; register the project and a dedicated PM; define the push boundary.
- Write the allowlist with `observe` / `pm_created` defaults.
- Fix the supervisor/PM/engine responsibilities, the escalation short form and the memory output contract.
- Unapproved creation pre-approval does not block stage 0.

**Deliverables**

- An independent repository skeleton and the minimum modules.
- allowlist hot reload.
- index / events / outbox / health.
- Read-only collection from the bus and from GitHub.
- `once` / `status` / `doctor` / `replay` / `brief` / `context`.
- Single-instance lock.
- A draft autostart definition, **not registered**.

**Positive acceptance**

- observe runs continuously for 24 hours.
- Restart loses no cursor and reprocesses no event.
- Every pending-arbitration item can be located from the supervisor brief down to its original text.
- A PM brief contains only its own project.
- `replay` rebuilds the index with zero external actions.

**Counterexamples**

- An illegal or out-of-scope config is rejected and the previous one stays in force.
- The brief contains no long comments, no other project's body, no secrets.
- No sessions created, no GitHub writes, no port opened.
- Duplicate and expired events trigger nothing.

**Rollback**

- Delete the autostart draft created in this stage (if any) and stop this stage's process. Existing paths were untouched.
- Keep index/events for diagnosis; delete no existing evidence.

### Stage 1: single-project loop, creation rights, memory recovery

**Implementation**

- Thread + Assignment, Fast Path, dispatch adapter.
- outbox reconciliation, bind, archive tombstones.
- Model cooling double check.
- Autostart install and a real health measurement (authorized at that step).
- Validate with `pm_created` first; test `engine_preapproved` only after the pre-approval lands.

**Main path**

```text
test Issue
→ Assignment
→ compliant worker creation
→ short task and complete-delivery marker
→ waiting_review
→ archive, read-back and tombstone
```

**Interruptions and counterexamples**

- Kill the engine after creation but before bind: recovery completes only the bind; no duplicate worker.
- Comment outcome unknown: check comments/ledger first; do not repost.
- Send timeout: check prompt_id/session history first; do not resend.
- The only model is cooling: created count is 0.
- Issue outside the allowlist: created count is 0.
- QA/worker not pre-approved: the engine's creation call must be refused.
- An archived sid emits another event: no more delivery.
- Every probe ends `archived=true`, `busy=false`, with no running task.

**Memory recovery acceptance**

- Simulate supervisor rotation: the new session continues the pending work having read only role rules, the brief and the given Issue references.
- No other project's history is read; pending-item recovery is 100%.
- Simulated PM rotation affects only that project; others continue.
- After the sid switch, the old sid receives zero new deliveries.

**Rollback**

- Set the project's `creation_mode` back to `pm_created` / `observe`.
- Before disabling this stage's scheduled task, report the created entries and pid; touch only this instance.
- Archive the test sessions that were created; keep uncertain outbox items for manual reconciliation rather than force-replaying them.

### Stage 2: QA, project-PM loop and close-out reconciliation

**Implementation**

- Send to QA per current rules; QA rejections return to the original worker; passes move to `waiting_release`.
- New Issue comments go to the current owner.
- In-project Slow Path goes to the project PM; only cross-project/authorization goes to the supervisor.
- Close-out consistency check.

**Positive acceptance**

- Develop → QA → reject → fix → pass runs end to end.
- The whole process can be reconstructed from Issue comments.
- No new process-document pile-up appears locally.
- The project PM continues independently; the supervisor sees only necessary arbitration.
- QA's original text is preserved, never rewritten by the engine or the PM.

**Close-out counterexamples**

- The worker was archived early: do not archive twice and do not fake success.
- QA still active at close-out: the close-out tool archives, then the engine re-verifies.
- Bind cleanup or archive failed: enter `close_reconcile`; do not mark `completed`.
- No QA comment: the engine may not bypass the gate or close the Issue itself.
- A late event on a closed Issue: audit only; do not restart.

**Rollback**

- Revert only stage 2's automatic flow; keep stage 1's worker loop.
- QA/close-out return to the existing PM/supervisor path; stages 0 and 1 are not rolled back.

### Stage 3: multi-project expansion and watchdog replacement

**Order**

1. Add one project at a time, in observe.
2. Verify dedicated-PM isolation, the escalation route and cross-project priority.
3. Compare the old watchdog's actions with the new engine's.
4. Measure duplicate notifications, missed notifications, false alarms and context growth.
5. Once stable, disable only the old watchdogs that are covered. Never delete them all at once.

**Exit thresholds**

- Duplicate actions 0, probe-session leaks 0, severe false alarms 0.
- The new mechanism is stable for one continuous week.
- No cross-project context bleed.
- The supervisor brief grows with pending/incident items only, not with project history.
- Humans handle only what genuinely needs a human.

**Rollback**

- Move the new project back to observe; older projects are unaffected.
- Paused old watchdogs can be restored one by one from a list; the old implementation is not deleted.

### Stage 4: limited autonomous work

**Opening order**

```text
observe → assist (internal state / recycle / wake-up) → a small amount of pre-approved auto
```

**Acceptance**

- Autonomous work requires written rules and an allowlist.
- In observe, only record "what it would do".
- Autonomous work may not release, close out, expand privileges or handle new requirements.
- Untrusted external input may not modify authorization, the allowlist or the execution environment.

**Rollback**

- One switch returns to observe; task state and audit trail are kept, history is not deleted.

## 16. Modules and test boundaries

```text
bus_client.py        # event bus client
github_source.py     # GitHub new-comment collection
store.py             # index / events / outbox / health
policy.py            # allowlist, profiles, cooling, per-role action rights
dispatch_adapter.py  # dispatch, bind, archive, close-out reconciliation
memory.py            # global/project briefs, Issue recovery packs
engine.py            # Fast Path, state machine, retry, escalation routing
cli.py               # run / once / status / doctor / replay / brief / context
```

Tests use the standard library `unittest` plus a local fake HTTP service. Real-path tests use only one-off Issues and probe sessions, archived afterwards with bind/close-out state verified.

Counterexamples that must be tested:

- The supervisor brief contains no unrelated project body, secret or long comment.
- One PM's Slow Path never reaches another PM.
- Engine restart, supervisor rotation and PM rotation never bleed into each other.
- Over budget, the brief keeps pending-arbitration/pending-user/incident items and reports the omitted count.
- Credentials are usable but appear in no prompt/index/events/outbox/Issue.
- An archived old sid is never fed again by events or monitoring.
- A corrupt index stops external actions; the only recoverable trace is not overwritten.

## 17. Success metrics

### Context and roles

- Tokens and documents the supervisor must read at each rotation.
- Pending-item recovery rate after rotation (target 100%).
- Repeated investigation caused by insufficient supervisor context.
- Share of projects a PM closes without supervisor involvement.
- Share of supervisor notifications that genuinely need arbitration.
- Increment to the supervisor's resident context when a project is added.

### Engineering and cost

- Human follow-ups per Issue.
- Times a PM re-relays worker/QA results.
- Delay from worker completion to the next step.
- Duplicate dispatches, duplicate comments, false alarms.
- Orphan sessions, wrong binds, unarchived probe sessions.
- Mis-dispatches during cooling (target always 0).
- Monitoring messages a human must read per week.
- Model calls and token cost per task.
- Manual recovery steps after a restart.
- Time to notice and recover from an engine outage.

## End state

```text
supervisor   user intent, authorization, cross-project arbitration, release/close-out
project PM   in-project requirement decomposition and daily routing
engine       events, state machine, recycling, lightweight memory and recovery packs
worker/QA    isolated execution and independent verification
GitHub Issue long-lived process record
local JSON/JSONL  rebuildable run index
```

Recommended order:

```text
authorization and read-only skeleton
→ pm_created single-project loop + memory recovery
→ fixed-profile creation opened after explicit pre-approval
→ QA / close-out loop
→ multi-project and watchdog replacement
→ limited automation
```

The five disciplines that matter most:

1. The supervisor is not a PM; a project PM does not share the supervisor's global authority.
2. The supervisor's memory is restored from short summaries plus precise references, not from a long-lived session.
3. No creation without pre-approval; no dispatch while cooling; verify before acting when an external action is uncertain.
4. Archive and close-out must be reconciled; an old sid must never be fed again.
5. No private name, host path or credential ever enters this repository.
