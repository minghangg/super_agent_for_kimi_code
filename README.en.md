[English](README.en.md) | [中文](README.md)

# super_agent_for_kimi_code

> Adding the two things a one-person, multi-project agent team has always lacked: **task automation** and **outsourced memory**.
> Not another Router. A program takes the deterministic transitions, the human keeps only exceptions and acceptance, and long-term memory lives in Issues instead of in some session's context window.
> This is a companion tool for [Kimi Code CLI (unofficial extension)](https://github.com/MoonshotAI/kimi-code), not affiliated with Moonshot AI.

---

## The shape of the problem

Over the past year, coding agents have proven they can do a single task well once they have tools, and that humans need to intervene step by step less and less. The real bottleneck moved from "can the model do it" to "how many things can a person watch at once".

There is an article that lays this out clearly — *Super-agent design exploration: toward a more autonomous agent system* from the DeepSeek Harness team:

> A person can typically follow only 3 to 5 active sessions at once. **The ceiling for one task is model capability; the ceiling for many tasks is human attention.**

Their answer is a two-layer architecture: a resident **Router** layer maintaining global continuity, and many context-isolated **Worker** layers executing concurrently. Events replace prompt-by-prompt instruction. A Fast Path of deterministic code filters out most noise; only what needs semantic judgement reaches the model (Slow Path). Context is managed with Inline & Spill, keeping anything beyond the window as a reference.

The reasoning is sound, and one corollary deserves attention: **the Router's work is often less complex than it looks, and a lighter model frequently gives the best balance of speed and quality.**

Our reality is more extreme than theirs: **one person, several projects, each with its own code, rules and verification gates.** No team, no multi-tenancy, no cloud cluster. At this scale many parts of the Super-agent design are surplus — but **the bottleneck it identifies is fully present**.

## Where we actually get stuck

What we run today looks roughly like this: a **supervisor** session aligns user intent, arbitrates across projects and handles release; each project has a dedicated **PM** session that splits requirements, writes briefs and dispatches workers and QA; workers and QA are one-shot isolated sessions that retire when done; **GitHub Issues are the process record**; a local WSS event bus unifies the session control plane.

> Throughout this file, `supervisor` / `PM` / `worker` / `QA` are **role names, not fixed people or sessions**. The names are configurable; the top-level role defaults to `supervisor`. Rename them and nothing changes.

It works, but it bleeds in three places:

**1. The PM has become a courier.** When a worker delivers, the PM reads the result, decides whether it should go to QA, writes a comment, then wakes the next session. Almost none of that needs semantic judgement, yet it consumes nearly all of the PM's context budget. The one thing it genuinely needs to think about — "is this delivery good enough?" — gets no room.

**2. The supervisor burns attention on progress-watching that should go into decisions.** Cross-project state is scattered across sessions and logs; answering "what is waiting on me right now" requires reading a pile of things. Every read brings the session closer to needing a rotation.

**3. Rotation loses memory.** Sessions must rotate at a threshold — that is good. But afterwards the new session faces an empty context while the project history is still in the old session's luggage. So either the work is investigated again (expensive, slow, possibly reaching different conclusions), or it is retold by a human — which makes a person the memory carrier, exactly what we want to eliminate.

These three are not the same defect, but they share one root: **we handed the deterministic work a program should do to a language model, and pushed long-term memory — which belongs to an archival system — into a context window.**

## What we are building

In one sentence: **give the deterministic work to a program, give long-term memory to Issues, and leave human attention for exceptions and acceptance.**

Two parts:

**First, a lightweight task scheduler.** It listens for events (new Issue comments, session turn finished, pending interaction, task finished, failure, retry timer, config change), decides the next move by deterministic rule, and issues the action. It does not split requirements, judge quality or arbitrate conflicts — PMs and the supervisor still do that.

**Second, outsourced memory.** Long-term process facts (requirements, briefs, deliveries, QA opinions, decisions) stay in GitHub Issues. Locally we keep only a **rebuildable** run index answering "who is doing what, waiting on what, where is the original text". On rotation, the new session reads a small brief plus precise references and continues — no reciting history.

**Why lightweight.** Because it is one person. The Super-agent Router's batching, sharding and subscription pass-through exist to solve "one Router cannot keep up at team scale". We are not at that scale and should not pay that complexity in advance. Being lightweight costs some automation ceiling; it buys something one person can read, maintain and repair.

**Why no LLM inside the engine.** Because deterministic judgement should not cost tokens. Whether a delivery goes to QA, whether a dispatch is allowed while a model cools down, whether the allowlist permits a write to this Issue — all have definite criteria. Written as code they are more reliable, faster and cheaper than written as prompts. **Anything that can be frozen into a rule should not be left to the model's judgement in the moment.** Where semantics are genuinely needed (how to split this requirement, whether this delivery is good enough), a PM or the supervisor still does it; the engine only wakes them.

**Why the Issue is the archive.** Because it grants four things free that we would otherwise build ourselves: searchability, a permission model, a permanent URL, and cross-session durability. More importantly, **a human can read it directly** — when something breaks you do not need to start the engine to learn what happened. The cost is latency and polling, which at our scale is entirely acceptable.

## Goals

- **Events advance Issues.** Progress is written back as Issue comments instead of being scattered across session logs. "Where is this up to?" is always recoverable from the Issue.
- **Programs do the deterministic transitions.** Collection, deduplication, state machine, cooling, retry, reconciliation, archiving — all in code.
- **Humans handle only exceptions and acceptance.** Anything needing arbitration arrives as a short escalation; everything else stays quiet. **Silence when no one needs to intervene** matters as much as finishing the work.

## Approach (directional)

Four steps, each reversible:

1. **Fix the role boundaries and the memory baseline first.** The supervisor is not a PM; a PM does not share the supervisor's global authority; the engine holds no production privilege. At the same time, freeze the "minimum memory pack" output contract: fixed sections, a byte ceiling, and when over budget, keep pending-arbitration and pending-user items first — and **always report how many items were omitted**. This is a lossy but traceable summary; it does not pretend to be lossless.
2. **Close the loop on one project.** One Issue, from event to dispatch to delivery to archive, fully reconstructible. If any step is interrupted, a restart completes only the unfinished step and repeats no external action.
3. **Wire in QA and close-out.** QA returns to the original worker; a pass moves to waiting-release; after close-out, verify archive consistency — and if it does not match, stop at "pending reconciliation" rather than pretend success.
4. **Only then talk about automation.** Observe by default. Internal actions such as automatic recycling and automatic wake-up open only after proven stable. Real session creation (auto-creating a worker session) must be a **narrow authorization: explicitly pre-approved, fixed profile, switchable off** — and it never includes release, close-out, production writes or privilege expansion.

One rule throughout: **for any external action you are unsure about, verify first; never blindly replay.**

---

## Status and roadmap

**Status:** the one-person, multi-project agent team already runs day to day (supervisor plus a dedicated PM per project plus one-shot workers and QA), with GitHub Issues as the process record and the WSS event bus as the session control plane. This repository is filling the gaps above one by one.

| Stage | Direction |
|---|---|
| **First stage** | Role boundaries, memory baseline, read-only skeleton. Event collection watches without acting; briefs can be generated, the index rebuilt, **external actions stay at exactly 0** |
| **Second stage** | Single-project loop plus memory recovery. Thread/Assignment model, deterministic fast path, thin dispatch adapter; pending-item recovery after simulated rotation is 100% |
| **Third stage** | QA loop and close-out reconciliation. Return to the original worker, pass to waiting-release, verify consistency after close-out |
| **Fourth stage** | Multi-project expansion and watchdog replacement. One project at a time, observe by default; disable the old mechanism only when duplicate actions, session leaks and false alarms are all 0 |
| **Fifth stage** | Limited autonomous work. Opened step by step: observe → assist → a small amount of pre-approved auto, with a one-switch return to observe |

No step deletes the old path; a rollback rolls back only its own stage.

**This is not a system meant to replace human judgement.** It aims at something plainer: take the judgement-free moving of paper out of a person's field of view, so the remaining judgement has somewhere to land.

---

## Running it in 30 seconds

```bash
git clone <this repo> && cd super_agent_for_kimi_code
python -m super_agent.cli doctor                       # health: process / bus / source / config / outbox / cursors / leftover Assignments
python -m super_agent.cli once --mode observe          # one round, compute only
python -m super_agent.cli run --mode observe --interval 60   # resident; Ctrl-C exits gracefully
python -m unittest discover -s tests -v                # L1 tests, including every counterexample
```

None of these commands needs network credentials: `--source none` is the default, and `doctor` reports bus failures without a traceback.
To see real data, set `github.source` to `fixture` (local JSON) or `live` (read-only REST, token from the environment).

### What exists now

| Module | Purpose |
|---|---|
| `super_agent/bus_client.py` | Connect to the local event bus: `healthz()` / `events(since, wait)` / `inject()`. A disconnect never raises |
| `super_agent/github_source.py` | Poll Issue comments; three sources: `fixture` / `live` (**GET only**) / `none` |
| `super_agent/store.py` | Atomic `index.json` replacement, bounded rotation of `events.jsonl` / `outbox.jsonl`, cursors, counters; the outbox state machine (`outbox_state.json` holds unfinished items, so rotation never loses them) |
| `super_agent/allowlist.py` | Three-level whitelist — project / Issue / action — with mtime hot reload; a broken config keeps the previous one (cross-process via `*.last-good`) |
| `super_agent/pidlock.py` | Atomic single-instance lock on `engine.pid` (`O_CREAT\|O_EXCL`); takes over only when stale |
| `super_agent/health.py` | Atomic `health.json` snapshot for liveness checks and for distinguishing a crash from a clean stop |
| `super_agent/dispatch_adapter.py` | Structured dispatch interface: returns dicts, parses only `--json`, never reads human text |
| `super_agent/session_events.py` | **Read-only** view of the workbench's session lifecycle: `replay(events, at)` reconstructs `sid → (role, issue, assignment, profile)` at a moment, `reconcile()` does the three-way check |
| `super_agent/engine.py` | Deterministic Fast Path: dedupe / allowlist / cooling / state machine; **no LLM** |
| `super_agent/cli.py` | `run` / `once` / `status` / `doctor` / `replay` / `reconcile` |

### Two allowlists, two different questions

The engine has two unrelated notions of "allowed". Mixing them produces wrong conclusions:

| Dimension | Where | Question it answers | Default |
|---|---|---|---|
| **Event types** | `engine.allowlist` (`config.py`) | Which events may the fast path read? | `["issue_comment.created"]` |
| **Project / Issue / action** | `config/allowlist.json` (`super_agent/allowlist.py`) | May a given action be performed on a given Issue in a given repo? | Anything unlisted is denied |

Being allowed to read an event type does not mean being allowed to write to a repository. The three-level whitelist denies any unlisted repo, Issue or action.
A newly added Issue starts in observe and **does not inherit write rights**. `creation_mode` accepts only `pm_created`; encountering `engine_preapproved` fails at load time (there is no approved creation path yet, and under `pm_created` both `create_worker` and `create_qa` return `creation_mode_forbidden`, preparing a draft only).

Hot reload polls mtime (no inotify required) but decides on `(mtime, size, sha256)`, so an edit that changes no byte count — `[28]` to `"28"` — is still caught. On change it parses and validates fully before taking effect, and **only affects future events, never retroactively**.
A broken config keeps the previous one and warns; the engine neither crashes nor changes behaviour.

### The outbox state machine: persist before acting

```
prepared ──► in_flight ──► done
                 │
                 └──► uncertain ──► reconcile ──► done | waiting_person
```

`prepared` is written **before** the external call, so a crash before the call is a known state: that item is safe to replay.
Entering `in_flight` means "this may already have landed". After a restart it is **never resent**; it becomes `uncertain` and joins the reconcile queue, checked against the recorded `sid` / `prompt_id` / `comment_id` before being marked `done` or `waiting_person`.
`waiting_person` is an **unfinished** state: it stays in `outbox_state.json` (rotation does not drop it), `doctor` counts it separately as `waiting-person`, and it enters neither the reconcile queue (nothing left to check) nor `outbox-stuck` (a person is already on it).

Startup runs once through `Engine.startup()`, used by both `once` and `run`: `store.recover_outbox()` → `replay_prepared()` → `reconcile_uncertain()`. Without a probe everything lands in `waiting_person` — **no guessing, no resending**. If any step raises, `health.json` records `degraded` plus `last_error` and the process exits rc=1; it never runs external actions while a state is uncertain.

`action_id = assignment_id + verb + input_ref` (a stable hash when there is no assignment). This is the **identity**: a second attempt yields `...#2`, `...#3`, so a second begin **never overwrites** an existing `uncertain` record. Attempt counts live in `outbox_state.json` under `attempts` and keep incrementing across restarts.

Unfinished items live in `outbox_state.json` (atomic write); `outbox.jsonl` holds only state-change history. Rotation triggered by `max_outbox_lines` therefore drops history and **never drops unfinished actions**.

### Single instance and health

`engine.pid` is created atomically with `O_CREAT|O_EXCL` and holds `{pid, started_at, cmdline_hash}`.
If the pid is alive and the command line matches, startup is refused (rc=3). If the pid is dead or the command line does not match, the lock is stale and is taken over.
A clean exit deletes the file; an abnormal exit is recovered by stale detection.

The command line is taken from the process itself: on Windows, `wmic` is tried first, **falling back automatically** to PowerShell `Get-CimInstance Win32_Process` (newer Windows no longer ships wmic). Only when both fail is the lock treated as live, so it is never taken over by mistake — but CIM is always tried.

`health.json` is replaced atomically once per round, with fields `pid / mode / last_loop_at / last_successful_action_at / active_assignments / degraded / last_error / running / phase`. `phase` is `running` (the loop holds the lock), `startup-failed` (exited after a failed startup reconciliation) or `stopped` (clean exit); snapshots without the field are inferred as `running`.
`health.write()` **enforces the invariant**: `startup-failed`/`stopped` are only persisted with `running=false`, and `running` only with `phase=running`. Contradictory input is corrected (`running=True` with `startup-failed`/`stopped` becomes `running=False`; `running=False` with `phase=running` becomes `phase=stopped`), a WARN line goes to stderr, and the snapshot is never dropped.
`doctor` uses these to separate failures, each with a readable label:

| Label | Meaning |
|---|---|
| `process-dead` | The pid lock points at a dead process, or `running=true` with no live process (a crash) |
| `startup-failed` | Startup reconciliation failed and the process exited (`phase=startup-failed`, detail carries `last_error`); not a crash. **Reported even when a stale lock remains**, never masked by "the next start will take over" |
| `bus-down` | The event bus is unreachable |
| `github-down` | The data source is unreachable |
| `config-bad` | The allowlist failed to parse (states that the previous copy is in use) |
| `outbox-stuck` | Some item has been `in_flight`/`uncertain` longer than `store.outbox_stale_s` (default 300s) |
| `waiting-person` | An action is waiting on a **human** (no probe for reconciliation, or the remote is unconfirmed); counted separately, never folded into `outbox-stuck` |

When `engine.pid` is stale but undeletable (another process holds a handle on Windows, `[WinError 32]`), the engine **refuses to start** and exits with the lock error (rc=3), printing `engine.pid: refused to start - ...`. It never runs on with an old lock and never raises a traceback.

### Two event streams: one is the engine's, one is the session's

```
workbench state/events.jsonl   ← written only by sess_events.emit(): who holds which session when
        │  (the engine reads only; no copying, no double write; configured by engine.session_events_path)
        ▼
super_agent/session_events.py ── replay --at / reconcile
super_agent/store.py:log_event() ─→ state/events.jsonl   the engine's own run events (run.started …)
```

**The order is fixed: write `store/events.jsonl` first, then inject into the event bus** (`Store.log_event` / `Engine._dispatch`). A failed inject **does not roll back** the store — the record is already on disk and is redelivered from `store/outbox.jsonl` (`Engine._dispatch` logs `ok`/`error` into `log_outbox`).

Session truth reconciliation (a non-empty three-way difference is rc=1; a missing third source is rc=2, **never treated as agreement**):

```bash
python -m super_agent.cli replay --sessions                 # current session view
python -m super_agent.cli replay --at 2026-11-08T03:44:20+08:00 --sessions   # a past moment
python -m super_agent.cli reconcile --live-file live.txt    # or omit --live-file to query the session list directly
```

`SESS_EVENTS_STATE_DIR` selects which stream is read (the same variable the writer uses), so self-tests never touch production `state/`.

**`run` is `once` in resident form**: the same round, repeated. `--interval` defaults to `github.poll_interval_s` (60s). Each round atomically replaces `state/run_heartbeat.json` (`{ts, round, ok, external_actions}`) for an external watchdog to check liveness.
A single round's exception is logged and the loop continues; only `--max-failures` (default 10) consecutive failures exit non-zero. `SIGINT`/`SIGTERM` finish between rounds, rewrite the cursor and index, then exit rc=0.
The resident path does not change the observe promise — it still calls `Engine.run_once()`, and external actions still go only through `_dispatch`.

```bash
python -m super_agent.cli run --mode observe --interval 60
```

**observe is the default mode and the only promise at this stage.** Under observe, `Engine.dispatch_enabled` is always `False`, and every external action converges on the single exit `Engine._dispatch` and increments `external_actions`.
So `external_actions=0` in `status` is not a hard-coded literal — it reads the persisted counter, and `tests/test_red_evidence.py` stubs `dispatch_enabled` to `True` to prove the number moves.

`status` lists only items that need a human, and prints "none" when there are none. **Stay quiet when no intervention is needed.**

---

## The listener for a dead session key (`session_alarm`)

> This section is a migration statement, not a manual. The **behavioural contract** is the code and `tests/test_session_alarm.py`.

This module is an "alarm listener" moved over from the private workbench: when a session key points at a dead session, the alarm is actually pushed to the **on-duty project manager**, and if nobody is on duty, it writes to the user channel as a log line. Three things were done during the move:

1. **Every private dependency was cut.** The module imports no session client and recognises no working directory.
   The liveness probe, the console script that sends messages, the session directory used to confirm delivery, and the four run-state paths (`state_root` / `sessions` / `log` / `state_file`) are **all injected by the caller** (`configure(...)`, or environment variables under `--check`). Without injection it degrades explicitly: `confirm_delivery(wire_root=None)` always returns `unknown`, `send_to` does not spawn, `live_pm_sids()` returns an empty table. **"Cannot find it" is never treated as "it is dead", and the module never goes looking for directories itself.**

2. **The event name is unchanged in this batch.** The constant `event.session.<role>_key_dead` is a **protocol constant** — consumers (the event bus and the health endpoint) match on the value, so changing it would be a protocol change. That is a discussion for when the seventh piece migrates.

3. **String literals in the source are assembled from fragments** (`"".join([...])`), so the repository's own privacy gate does not match itself.

   > **This is a technical pass, not a standard met.** Fragment assembly only hides the text from `grep`; the runtime value is unchanged and the code is no more general. The real criteria are the previous point (dependency injection) and the behavioural assertions in `tests/test_session_alarm.py`. Treating "grep is clean" as acceptance trades verifiability for appearance.

---

## Never in this repository

This is an open-source repository. It holds only the **generic foundation and the engine**. The following never enter it:

- `state/**` — run state: index, cursors, event logs, heartbeat, counters, worktrees
- `.kimi/report/**` — session reports, weekly reports, walkthrough conclusions
- `auth/**` — credential material, and any token or secret value
- any **absolute host path** (drive letter, user name, private workbench directory)
- any **private project name** or **private identity** (real name, email, account, committer name)
- any private session id, private issue number, or quotation of their text

This is not enforced by good intentions but by one re-runnable command:

```bash
python tools/privacy_gate.py                 # default: tracked files + current branch history + origin + run-state dirs
python tools/privacy_gate.py --refs all      # also scan every ref (see below)
```

The criterion is the exit code:

- **Run it before merging every ticket. rc≠0 is a QA P0** — never waived as "known but merge anyway".
- The default `--refs branch` must be rc=0. A hit prints `file:line: pattern: content` or `commit's author/committer matched pattern`, one line each.
- **Token shapes** (GitHub PATs `ghp_`/`gho_`/`ghu_`/`ghs_`/`ghr_`, `github_pat_`, `sk-`, AWS `AKIA`, PEM private-key blocks) go through the same file scan and fail on a hit. Each form carries a length floor, so `sk-learn`, `task-name` or a bare credential prefix does **not** produce a false positive.
- **The origin owner segment only WARNs, never fails**: a repository belonging to its own owner is a legitimate state, so a private word in the owner segment prints one `WARN` line and the exit code stays 0 — but it is never silent; the `WARN` must be visible. The repository path after the owner is still a failing channel.
- `--refs all` scans every ref. On this repository it is currently **expected to be red**: `refs/original/*` are backup refs left by an early filter-branch and still reach commits authored with a private identity. Clearing them requires the repository owner's approval to run `git gc` / `git reflog expire` and **is not part of the normal development flow**; once cleared, `--refs all` is expected to return to rc=0.
- The pattern strings in `tools/privacy_gate.py` are **assembled from fragments**, not written out whole — otherwise the gate would match itself. When editing that file, do not "helpfully" merge the fragments back together.
- Run-state directories (`state/`, `.kimi/`, `auth/`) appearing in `git ls-files` also fail the gate: it means run state or reports were committed by accident.
