[English](03-agent-communication.en.md) | [中文](03-agent-communication.zh.md)

# 03 · Simplified agent-to-agent communication

> Series: design philosophy ｜ [01 Core architecture](01-core-architecture.en.md) ｜ [02 Requirements Analyst](02-requirements-analyst.en.md) ｜ 03 Agent-to-agent communication ｜ [04 Human-facing Reporter](04-human-reporter.en.md) ｜ [05 Artifact recycle bin](05-artifact-recycle-bin.en.md)
> Version: 2026-11-07
> Goal: less long-text relaying, less multi-round confirmation and less duplicate investigation between agents — while keeping requirement and acceptance semantics intact.
> Nature: a design report. It authorizes no rule change, no code and no privilege change.

## 1. Objective

Turn this:

```text
send a long task description
→ the receiver paraphrases its understanding
→ a long report comes back
→ the layer above reorganizes and forwards it
```

into this:

```text
send a short instruction + object references
→ the receiver reads only what the reference points at
→ a structured status + result reference comes back
```

The goal is less:

- token and context consumption;
- distortion through paraphrase;
- duplicate investigation;
- multi-round confirmation;
- clerical work for the supervisor and PMs.

One clarification: **what can be removed is the pleasantries, the restatement and the long summary — not all natural language.** Requirements, briefs and QA conclusions must still exist as text an LLM can understand. They simply stop travelling through chat, hop by hop.

## 2. Principles

### 2.1 Simpler communication is not simpler semantics

The following still need natural-language or structured semantic content:

- the user's goal;
- requirement and boundaries;
- acceptance conditions;
- the implementation task;
- the reason QA returned work;
- the basis for a decision.

What changes is where it lives: in the Issue, the requirement record or the delivery record. Communication carries only references.

### 2.2 State changes do not travel through LLM chat

For example:

- a worker finished;
- QA passed;
- a task is waiting on the user;
- a session needs rotation;
- a model is cooling down.

Events and the state machine handle these. No PM or supervisor needs to receive "I have finished, please check".

### 2.3 One message, one intent

Recommended instruction types:

```text
requirement.review
assignment.dispatch
assignment.resume
review.request
decision.request
session.rotate
assignment.stop
```

Never mix "analyse the requirement + change the code + run the tests + decide whether to release" into one message.

## 3. One envelope for everyone

```json
{
  "v": 1,
  "message_id": "msg_...",
  "type": "assignment.dispatch",
  "thread": "owner/repo#12",
  "assignment": "asg_12_worker_1",
  "from": "pm-repo-a",
  "to": "worker",
  "payload_ref": "issuecomment-456",
  "requested_action": "execute",
  "created_at": "..."
}
```

A result returns only:

```json
{
  "message_id": "msg_...",
  "status": "done",
  "result_ref": "issuecomment-789"
}
```

An exception:

```json
{
  "status": "blocked",
  "reason_code": "requirement_ambiguous",
  "detail_ref": "requirement-REQ-003"
}
```

Where:

- `thread` locates the long-lived task;
- `assignment` locates one execution;
- `payload_ref` points at the actual content;
- `reason_code` lets a program route it;
- long text never enters the envelope.

## 4. How each role communicates

| Channel | Instruction | Reply |
|---|---|---|
| supervisor → Analyst | requirement id + pre-review instruction | READY/QUESTIONS/BLOCKED + requirement link |
| supervisor → PM | Issue link + project goal reference | accepted / needs arbitration + Issue link |
| PM → worker | Assignment id + brief reference | status + delivery comment reference |
| PM → QA | Review id + object under review | pass/return + QA conclusion reference |
| PM → supervisor | Decision id + one question | the decision + decision reference |
| engine → any role | typed event + state reference | usually no natural-language reply |
| worker/QA → Issue | structured delivery body | GitHub comment id |

## 5. The topology still holds

A simpler protocol does not mean every agent may chat with every other agent.

```text
supervisor ↔ Analyst
supervisor ↔ project PM
project PM ↔ worker/QA
worker/QA → Issue
engine → the current role
```

Still true:

- the Analyst never contacts the user directly;
- a worker never asks the user to decide;
- a PM never rewrites QA's conclusion;
- a PM never handles another project;
- the supervisor never receives the daily execution stream.

## 6. Reliability

Short instructions still need full engineering support:

- `message_id` deduplication;
- an outbox so a restart loses nothing;
- on timeout, reconcile first — never blindly resend;
- the receiver must be the currently registered session;
- an archived sid is never delivered to;
- privileges are checked by the program against role configuration;
- payload reads are bounded by project scope;
- every decision and result must be traceable to its original text.

The distinction that matters most:

```text
accepted   received
done       a result exists
completed  business acceptance passed
```

`accepted` is not completion.

## 7. Context control

"Send only a link" saves context only if reading is bounded too.

Every read uses:

```text
current goal
+ current stage
+ this action
+ necessary constraints
+ a few relevant references
```

Receiving a link must never default to reading:

- every Issue comment;
- the whole project history;
- every PM's state;
- every requirement fact.

## 8. Relationship to the rest of the series

- Reuses the core Thread, Assignment, Issue and outbox models.
- The Analyst uses `requirement.review` and a requirement id; it builds no separate long protocol.
- The Reporter consumes these states and references, but control actions never go through the Reporter.
- The event bus is transport infrastructure, not a semantic decision layer.

## 9. Recommended order

1. Unify `thread / assignment / payload_ref` first.
2. Rework supervisor ↔ Analyst.
3. Rework PM → worker/QA.
4. Wire the engine notifications and the outbox.
5. Only then cut down routine prose reports.
6. Accept on tokens, paraphrase counts, duplicate investigation and mis-delivery rate.

## 10. Acceptance metrics

- Body length of each dispatch and each result.
- How often the supervisor/PM re-paraphrases.
- Semantic deviation caused by paraphrase.
- Mis-deliveries caused by a stale sid or an archived session.
- Duplicate session creation and duplicate comments after a timeout.
- Whether on-demand reading genuinely avoids loading all comments and all history.
