[English](04-human-reporter.en.md) | [中文](04-human-reporter.zh.md)

# 04 · A human-facing Reporter status centre

> Series: design philosophy ｜ [01 Core architecture](01-core-architecture.en.md) ｜ [02 Requirements Analyst](02-requirements-analyst.en.md) ｜ [03 Agent-to-agent communication](03-agent-communication.en.md) ｜ 04 Human-facing Reporter ｜ [05 Artifact recycle bin](05-artifact-recycle-bin.en.md) ｜ [06 Token economics](06-token-economics.en.md) ｜ [07 Perpetual context](07-perpetual-context.en.md)
> Version: 2026-11-07
> Goal: a read-only, accurate, drill-down-able status view for the user, replacing the need to ask several agents or read comment streams.
> Nature: a design report. It authorizes no rule change, no code, no web service and no privilege change.

## 1. Objective

To check status, the user should no longer need to:

- open several agent sessions;
- ask the supervisor;
- read Issue comment streams;
- wait for an LLM to summarize on the spot.

They open one Reporter page instead:

> **What is running, how far has it got, what needs me, and is anything at risk?**

## 2. What the Reporter is

The Reporter is a **read-only status viewer**, not a new management agent.

It should:

- read state from the run index, Issues and events;
- produce human-facing summaries;
- offer drill-down into projects, tasks and evidence;
- surface what needs the user first;
- label data freshness and source.

It should not:

- dispatch work;
- chase people;
- change task state;
- arbitrate in place of the supervisor;
- call an expensive LLM just to render status;
- hold production or credential privileges.

## 3. Information architecture

### 3.1 Home: exactly four questions

```text
① What needs me?
② Which tasks are moving?
③ Which tasks are blocked or abnormal?
④ Is the system itself healthy?
```

Suggested layout:

```text
Needs you
├─ requirements awaiting answers: 2
├─ designs awaiting arbitration: 1
└─ releases awaiting acceptance: 1

Projects
├─ Project A: running 2 ｜ waiting QA 1
├─ Project B: no anomalies
└─ Project C: cooling, 1 queued

System
├─ event bus: healthy
├─ engine: healthy
└─ GitHub sync: 3 minutes ago
```

Compress healthy projects hard. Do not fill the screen with "all good" messages.

### 3.2 Project page

Each project shows:

- its current goal;
- active Issues;
- current stage;
- owner;
- what it is waiting on;
- the latest meaningful progress;
- the next step;
- risks and blockers.

It does not show a worker's full chat transcript.

### 3.3 Issue detail

Use "summary + timeline + evidence references":

```text
goal
current state
latest decision
latest delivery
QA conclusion
items awaiting the user
links to related Issues/comments
```

Open the original text only when detail is needed. Do not expand every comment by default.

### 3.4 Requirement clarification page

Wired to the Analyst design:

- questions awaiting answers;
- recommended options;
- the impact of each question;
- one batch submission;
- return to the status page after submitting.

The Reporter may render this page, but answers are written through a separate, restricted requirement interface.

## 4. Technical approach

The first version should be generated purely by program:

```text
index.json
+ requirements/index.json
+ current GitHub Issue state
+ event bus / engine health
        ↓
read-only status projection
        ↓
local HTML Reporter
```

No database needed.

It can be:

- a single local web service;
- plain HTTP GET pages;
- refreshed on a timer or on events;
- cache only short status values;
- always link back to the original text.

WSS is not a first-version requirement. When live updates are needed, reuse the event bus, or start with a page timer.

## 5. Status must be honest

Every status should distinguish:

- running;
- waiting for a worker's delivery;
- waiting for QA;
- waiting for the supervisor;
- waiting for the user;
- waiting for model cooling;
- finished but unreleased;
- closed;
- unknown or stale.

Never display:

- `busy=false` as "completed";
- an HTTP 200 as "business success";
- a stale cache as "live and healthy";
- an unverified delivery as "acceptance passed".

Every status item carries at least:

```text
source + updated-at + stale flag + detail link
```

## 6. Relationship to the supervisor

Once the Reporter is live:

- when the user asks "how is it going", they read the Reporter;
- the supervisor no longer loads project material just to answer status questions;
- the supervisor still answers "why was this decided";
- the Reporter answers "what is the state right now".

When states conflict, the Reporter shows the conflict and both sources. It does not decide which is correct.

## 7. Privileges and safety

- Listen on `127.0.0.1` only by default.
- Keep the read-only pages and the answer submission interface separate.
- The page shows no tokens, secrets, personal data or unrelated logs.
- Never generate an executable command from user input.
- Detail views respect project scope.
- Public or LAN access requires separate authentication design and explicit approval.

## 8. Relationship to the rest of the series

```text
simplified agent-to-agent communication
        ↓
produces structured state, result references and events
        ↓
the Reporter projects them into human-readable pages
```

They share a data contract but serve different purposes:

- agent-to-agent communication reduces cost between machines;
- the Reporter reduces the cost of a human obtaining state.

The Reporter never becomes a dispatch entry point. It may show the Analyst's questions and answers, but the right to change a requirement stays with the separate, restricted interface.

## 9. Implementation order

1. Ship a static/local read-only status page.
2. Wire active Issues, Analyst requirements and system health.
3. Add project and Issue drill-down.
4. Add a single "needs you" entry point.
5. Verify accuracy before considering live refresh.
6. Judge success by whether the user still has to ask an agent for status.

## 10. Acceptance metrics

- Clicks and waiting time needed to see global status.
- Whether everything needing the user fits on one screen.
- Agreement rate between displayed state and the Issue/engine source of truth.
- Whether stale state is explicitly marked.
- How often the user asks the supervisor for status.
- Whether the page leaks credentials, privacy or unrelated project detail.
