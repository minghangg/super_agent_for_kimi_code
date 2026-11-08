[English](02-requirements-analyst.en.md) | [中文](02-requirements-analyst.zh.md)

# 02 · Requirements Analyst: requirement pre-review and clarification

> Series: design philosophy ｜ [01 Core architecture](01-core-architecture.en.md) ｜ 02 Requirements Analyst ｜ [03 Agent-to-agent communication](03-agent-communication.en.md) ｜ [04 Human-facing Reporter](04-human-reporter.en.md) ｜ [05 Artifact recycle bin](05-artifact-recycle-bin.en.md)
> Version: v3 supplement · 2026-11-07
> Constraints: unofficial, single user, many projects, limited resources, no heavy database.
> Nature: a design proposal. It authorizes no charter change, no role session creation, no web service and no implementation.

## 1. The problem

The user must talk to the supervisor repeatedly, and the supervisor must investigate repositories just to understand a requirement. The results:

- Multi-round clarification eats the supervisor's context.
- Project facts, user preferences and past decisions pile up in the main session.
- When the supervisor rotates, the requirement background has to be handed over again.
- A vague requirement can reach a PM's dispatch queue too early, causing rework.
- The user has to read a long conversation instead of answering a few key questions.

The proposal is therefore a **long-lived, high-effort Requirements Analyst** between the supervisor and project PMs, owning requirement pre-review, fast probing, ambiguity detection and clarification material.

## 2. Role and boundaries

### 2.1 Responsibilities

1. Pre-review the supervisor's reading of user intent.
2. Probe relevant repositories, Issues and existing facts quickly, read-only.
3. Find the ambiguities that genuinely affect execution.
4. Decide whether the user must answer, and avoid over-asking for the sake of "completeness".
5. Produce a structured clarification report and a local HTML question page.
6. Analyse the answers into a sharper, issue-ready requirement package.
7. Maintain traceable long-term facts that improve later judgements.

### 2.2 What it does not do

- Does not contact the user directly; it talks only to the supervisor.
- Does not open or close Issues, dispatch workers/QA, or release.
- Does not hold the supervisor's production privileges.
- Does not modify project code, rules or credentials.
- Does not split development Assignments for the project PM.
- Does not judge implementation quality for QA.
- Does not promote an unverified inference into a long-term fact.

### 2.3 How it differs from existing roles

```text
Analyst     what are we really building; which ambiguities must be cleared
supervisor  approve or not, how to authorize, whether to open an Issue, cross-project arbitration
project PM  how a settled requirement splits into development and acceptance tasks
scout       read-only evidence on demand; owns no clarification conclusion
QA          does the design or the implementation meet acceptance
```

The Analyst is not a second supervisor and not another project PM.

## 3. The revised role picture

```text
                          user
                           ▲
                           │ answers a few key questions only
                           │ HTML report relayed by the supervisor
                           │
                     supervisor
              understand, authorize, arbitrate, open Issues
                     ▲          │
                     │          │ stable requirement link / short instruction
                     │          ▼
                     │   Requirements Analyst
                     │   long-lived, high effort, read-only probing
                     │   pre-review / clarification / package proposal
                     │          │
                     │          ▼
                     │     local requirement record and report
                     │
                     │ READY + link to the requirement package
                     │
              supervisor reviews, opens the Issue
                           │
                           ▼
                        project PM
                           │
                           ▼
                       worker / QA
```

The user still faces only the supervisor. The Analyst opens no second channel to the user.

## 4. End-to-end flow

```text
user states a requirement
   ↓
supervisor writes a minimal requirement record
   ↓
sends only the internal link to the Analyst
   ↓
Analyst reads the record + fast read-only probing
   ↓
decides whether an execution-affecting ambiguity exists
   ├─ no: returns READY + link to the requirement package
   │        ↓
   │     supervisor reviews, arbitrates, opens the Issue, hands to the project PM
   │
   └─ yes: generates an HTML clarification report
             ↓
          supervisor gives the report to the user
             ↓
          user answers in one batch
             ↓
          answers are written back into the same record
             ↓
          Analyst re-checks and returns the final package link
             ↓
          supervisor reviews, arbitrates, opens the Issue, hands to the project PM
```

This frees the supervisor from item-by-item questioning and repository investigation.

## 5. Fast communication between the supervisor and the Analyst

### 5.1 supervisor → Analyst

One stable internal reference is enough:

```text
req_id: REQ-20261107-003
record: state/requirements/REQ-20261107-003/request.json
action: review | recheck
project_hint: repo-a
```

Send the "what we are not doing" with it. Writing out-of-scope items into the record is cheaper than explaining them later.

### 5.2 Analyst → supervisor

```text
req_id: REQ-20261107-003
status: READY | QUESTIONS | BLOCKED
package_ref: state/requirements/REQ-20261107-003/analysis.json
report_ref: state/requirements/REQ-20261107-003/report.html   # only for QUESTIONS
open_questions: 2
```

Add one line saying why the question is needed, but do not restate the body inside the envelope.

### 5.3 State definitions

```text
received      accepted, pre-review not finished
investigating read-only probing in progress
questions     waiting for the user's answers
ready         a reviewable requirement package exists
blocked       cannot proceed; the supervisor must act
closed        an Issue was opened, or the item is deliberately dropped
```

The Analyst changes only the requirement record's state. It does not change the Issue's stage.

## 6. A long-lived role and its factual memory

### 6.1 Why the role must be long-lived

Requirement judgement depends on accumulated knowledge:

- how the user has described similar requirements before;
- the project's existing constraints and vocabulary;
- which ambiguities have already been cleared;
- which decisions must not be asked about again;
- which repositories, modules and boundaries relate to the current requirement.

So the Analyst needs a long-lived identity — but not a session that never rotates:

> **Long-term facts live in external records. The long-lived session only holds the current understanding and judgement.**

### 6.2 Three layers of fact

| Layer | Content | How it is used |
|---|---|---|
| This requirement's facts | repo location, related modules, current behaviour, the user's own words | Used for this requirement |
| Confirmed long-term facts | vocabulary, preferences and boundaries the user has settled | Reused by later requirements in the same project |
| Provisional assumptions | inferences not yet confirmed | Must be verified or asked about; never used to open an Issue directly |

Long-term fact example:

```json
{
  "fact": "this project forbids automatic release",
  "source": "issuecomment-123",
  "confirmed_at": "2026-11-07T12:00:00+08:00",
  "scope": "repo-a"
}
```

Rules:

- Every fact carries a source and a scope.
- When a new decision supersedes an old one, keep the historical reference.
- Read precisely by project and requirement id. No vector database.
- On rotation, the Analyst reads only the long-term rule summary plus the current requirement — not the whole history.
- The summary and the original text stay separate; the original still lives in the Issue or the project evidence.

## 7. Storage: JSON is enough

One directory per requirement:

```text
state/requirements/
├── index.json
└── REQ-20261107-003/
    ├── request.json
    ├── facts.json
    ├── analysis.json
    ├── questions.json
    ├── answers.json
    └── report.html
```

| File | Content |
|---|---|
| `request.json` | the user's own words, the supervisor's paraphrase, project hints, timestamps, status |
| `facts.json` | confirmed repository facts and evidence references |
| `analysis.json` | the requirement package, ambiguities, recommendations, risks |
| `questions.json` | questions, options, whether each is mandatory |
| `answers.json` | user answers and submission times |
| `report.html` | a static report generated from the structured data |
| `index.json` | id, status, project, update time, links |

Write requirements:

- single writer;
- temp file plus atomic replace;
- the index holds short metadata only;
- no secrets, no unrelated personal data, no full logs;
- a bounded `events.jsonl` may record state changes, but no complex event sourcing is needed per edit.

### 7.1 Minimal `questions.json`

```json
{
  "req_id": "REQ-20261107-003",
  "revision": 1,
  "questions": [
    {
      "id": "q1",
      "required": true,
      "question": "Is modifying production data allowed for this task?",
      "kind": "single",
      "options": ["not allowed", "only the designated test records"],
      "recommended": "not allowed",
      "why": "this answer decides the privilege boundary"
    }
  ]
}
```

### 7.2 Minimal requirement package

```json
{
  "req_id": "REQ-20261107-003",
  "status": "ready",
  "project": "repo-a",
  "objective": "one-sentence goal",
  "scope": ["what we will do"],
  "out_of_scope": ["what we will not do"],
  "acceptance": ["decidable acceptance criteria"],
  "constraints": ["privileges, red lines, resource limits"],
  "evidence_refs": ["issuecomment-123"],
  "open_questions": []
}
```

## 8. The HTML clarification report and its web boundary

### 8.1 Why a page is worth it

- Each question shows the recommended option and its impact.
- Single choice, multi-choice and a short free-text supplement.
- Mandatory questions and defaults are visible.
- Everything is submitted in one batch.
- The page can be regenerated after a revision.
- It works on desktop and mobile browsers.

It is first a **local requirement workbench**, not a new project-management back office.

### 8.2 The first version needs no WSS

```text
GET  /requirements/<req_id>
POST /requirements/<req_id>/answers
```

- The page reads the current questions.
- The user submits answers.
- Answers are written back into the same requirement.
- The supervisor is notified of the result.
- A page refresh shows the latest state.

Add WSS only when real use proves that refresh or notification is insufficient. Do not make the first version carry extra protocol and front-end complexity just for a live feel.

### 8.3 Security boundary

- Listen on `127.0.0.1` only.
- Use a random, unguessable req id or a short-lived access token.
- Only requirement answers may be written.
- The page must not run a shell, change rules, alter privileges or read credentials.
- Reports, answers and logs carry no secrets.
- Starting/stopping the local service and registering a scheduled task still require implementation-time authorization.
- HTML is generated from trusted templates and structured data; user text must be escaped against injection.

## 9. When to ask

The Analyst must not interrupt the user just because more questions are possible. Ask only when the answer materially changes:

1. which project or module is touched;
2. what will and will not be done;
3. the acceptance criteria;
4. data, privilege or security boundaries;
5. whether an external write is allowed;
6. a real cost/risk trade-off between approaches.

Examples:

- The project already has a button-colour convention: adopt it, do not ask.
- Whether to modify production data directly: must ask.
- "Please improve the experience": must be turned into concrete pages, behaviours and acceptance criteria, or no Issue.
- Several low-risk equivalent implementations: give a recommendation, do not hand every choice to the user.

With no execution-affecting ambiguity, go straight to `READY`. The goal is the fewest questions, not the most.

## 10. What the requirement package should contain

The material returned to the supervisor contains at least:

1. the user's original goal;
2. the supervisor's current understanding;
3. verified repository facts and their references;
4. the suggested project and impact scope;
5. a one-sentence goal;
6. what will and will not be done;
7. acceptance criteria and necessary counterexamples;
8. privilege, resource and security boundaries;
9. decisions the user has already given;
10. unresolved risks or blockers.

Only after the supervisor's review is an Issue opened. The project PM receives an approved Issue, not the Analyst's exploration.

## 11. Implementation order

### Step 1: a minimal loop with no web

- JSON requirement records.
- req-id communication between the supervisor and the Analyst.
- A static HTML report.
- Answers written back by hand.
- `READY / QUESTIONS / BLOCKED` states.

Acceptance:

- the supervisor does not copy long requirement text;
- the Analyst probes read-only and changes no code;
- no ambiguity means READY immediately;
- ambiguities produce only the questions that affect execution;
- the whole requirement can be reconstructed from its req id.

### Step 2: answering on a local page

- GET the page, POST the answers.
- Write back into the same req id.
- Atomic saves; a `revision` prevents a stale page from overwriting newer answers.
- Notify the supervisor after submission.

Acceptance:

- the page is reachable on loopback only;
- only the current requirement's answers can be written;
- repeated submissions do not create several requirements overwriting each other;
- no secrets and no command-execution entry point.

### Step 3: long-term facts and rotation recovery

- Organize confirmed facts by project.
- Facts carry a source and a scope.
- New decisions supersede old ones.
- On rotation the Analyst reads only the long-term summary plus the current requirement.

Acceptance:

- questions already answered are not asked again;
- provisional assumptions never enter long-term facts;
- a new session recovers the current requirement accurately;
- growing history does not cause full injection on every requirement.

### Step 4: real-time capability on demand only

Add WSS only when real use proves refresh/notification insufficient. Real-time features must never take priority over a working requirement loop.

## 12. Relationship to the core architecture

- This supplement changes none of the supervisor/PM/engine responsibilities.
- The Requirements Analyst sits before the PM, as the supervisor's requirement assistant.
- It reuses the core "link + short summary + on-demand reference" communication rule.
- It reuses the event bus for notification/control, but requirement storage is independent of the session event cache.
- Once process-document governance is in force, the JSON/HTML of an un-opened requirement is an explicit run-state exception; after an Issue is opened, long-term facts live in the Issue and project records, so process documents do not pile up again.
- Launching a new role requires a separate charter change and authorization. This document creates no role-creation right.

## 13. Main risks and controls

| Risk | Control |
|---|---|
| The Analyst becomes a second supervisor | Talks only to the supervisor; no right to open Issues, dispatch or release |
| Over-asking burdens the user | Ask only about goals, acceptance, privileges and major trade-offs |
| The long-lived session's context bloats | Facts externalized, read on demand, rotate at a threshold |
| Provisional guesses pollute long-term memory | Facts carry a source; unconfirmed assumptions never enter long-term records |
| The local page becomes a control entry point | Answers only; no shell, config or privilege |
| Process files pile up again | Keep only the current requirement and long-term facts; after an Issue opens, they belong to the Issue |
| Overlap with scout/PM | The Analyst judges requirements, scout provides evidence, the PM splits tasks |

## Conclusion

Add this role, positioned as **the supervisor's long-lived requirement pre-review and clarification assistant**.

The right design is not another chat window but:

> **a long-lived high-effort Analyst + a stable requirement id + a lightweight JSON fact store + a single-batch local HTML report.**

It reduces the supervisor's investigation, multi-round questioning and context consumption at the same time, without changing the responsibilities of the existing PM, worker and QA. The first version should complete the requirement loop with no real-time web, then add page answering and long-term facts step by step.
