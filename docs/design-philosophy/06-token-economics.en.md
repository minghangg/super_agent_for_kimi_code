[English](06-token-economics.en.md) | [中文](06-token-economics.zh.md)

# 06 · Token economics

> Series: design philosophy ｜ [01 Core architecture](01-core-architecture.en.md) ｜ [02 Requirements Analyst](02-requirements-analyst.en.md) ｜ [03 Agent-to-agent communication](03-agent-communication.en.md) ｜ [04 Human-facing Reporter](04-human-reporter.en.md) ｜ [05 Artifact recycle bin](05-artifact-recycle-bin.en.md) ｜ 06 Token economics ｜ [07 Perpetual context](07-perpetual-context.en.md)
> Version: 2026-11-11
> Constraints: unofficial project, one person maintaining several projects, limited resources, no heavy database.
> Nature: a design argument. It authorizes no implementation and changes no rules.

## Executive summary

Most tokens in an agent team are not spent on the work. They are spent on **redoing** work, **re-explaining** work, and **watching** work. This paper separates those three from production, then walks through the architectural choices that remove each one.

Four claims:

1. **The expensive token is the repeated one, not the long one.** A single rework event costs more than any plausible saving on an individual prompt.
2. **Pre-work is cheaper than re-work — but only when the pre-work is bounded.** Unbounded clarification is just rework with better manners.
3. **A session that has spent its context is cheaper to replace than to top up.** The remaining percentage is not an asset; it is a liability with a discount.
4. **Deterministic transitions should cost zero tokens.** If a program can make the decision, a model making it is a bug that bills by the hour.

None of this is a claim about prompt engineering. Every mechanism below is structural: it changes *who* does a step, or *where* information lives, not how a sentence is phrased.

## 1. The cost model: four kinds of token

Split every token a team spends into four buckets.

| Kind | What it buys | Bounded by |
|---|---|---|
| **Production** | The deliverable itself — reading, writing, testing | The task |
| **Coordination** | Relaying, summarizing, watching, asking for status | The number of handoffs |
| **Rework** | A second attempt at the same deliverable | How often the premise was wrong |
| **Recovery** | Rebuilding context after a rotation, a stall or a compaction | How much context lived only in a session |

Total spend is the sum. The design lever is that **only production is fixed by the task.** The other three are properties of the architecture, and all three can be driven toward zero without making the deliverable worse.

### 1.1 Why one rework event dominates

Consider what a single failed delivery actually consumes:

```text
worker attempt #1        production
QA reviews it            coordination + a full independent re-run
QA writes a verdict      coordination
PM relays the verdict    coordination
worker attempt #2        production, again — plus the context of attempt #1
QA re-reviews            coordination + a second re-run
```

The second attempt does not start from zero. It starts from a context that already contains the wrong approach, the wrong files, and the wrong reasoning. That context is paid for twice: once when it was built, once when it is carried into attempt #2 and has to be reasoned around.

So the unit of accounting is not "tokens per request". It is **"tokens per delivered, accepted change"**. An architecture that spends twice as much on preparation and half as often on rework wins by a wide margin, and the margin widens as tasks get longer.

### 1.2 The one number worth remembering

In our own runs, roughly **one attempt in three** lands without rework. That number is the whole argument for pre-work: if two out of three attempts will be sent back, then the expected cost of an unprepared task is roughly three times its production cost. Cutting the failure rate in half is worth more than any per-prompt optimization, because it removes the coordination, the re-run and the carry-over context all at once.

It also sets the ceiling on pre-work: preparation that costs more than the rework it prevents is not preparation, it is a second job.

## 2. Mechanism by mechanism

Each subsection answers one question: **which of the four token kinds does this remove, and at what price.**

### 2.1 Thin roles: the PM does six things and relays without editing

**Removes:** coordination.

A project PM that investigates, reads implementation detail and runs tests is doing three jobs and billing the team for all three. Worse, everything it reads stays in its context and is carried forward into every subsequent decision on that project.

The discipline is narrow on purpose: a PM takes the item, decides two switches, relays verbatim, maintains the state machine, watches for stalls, and reports upward in a bounded format. It does not investigate. Investigation is a dispatched task with its own session, which is archived when it is done.

The relay rule matters as much as the role boundary. **Relay without editing** means the QA verdict reaches the worker as written, and the worker's result reaches QA as written. Every "let me rephrase this more constructively" step is a place where a model spends tokens to introduce a distortion, and then the team spends more tokens discovering the distortion.

**Price:** thin roles can lose information that only a generalist would notice. This is real. The mitigation is that the record — not the role — is the memory, so nothing is lost that was written down.

### 2.2 Pre-digested briefs and bounded clarification

**Removes:** rework, and the coordination that rework drags behind it.

A worker that receives "make the dashboard better" will invent a scope, and the invented scope will be wrong about half the time. A worker that receives a brief with the current facts, the exact files, the acceptance command and the explicit non-goals will not.

The important word is **bounded**. Clarification has a stopping rule: at most a small fixed number of questions, asked one at a time, and the answers go into the record rather than into a conversation. Without the stopping rule, clarification becomes an infinite regress where the team spends tokens deciding what to decide.

A pre-digested brief also front-loads the *cheap* model's work. A short investigation produces facts; those facts then let a stronger model spend its context on the actual problem instead of on discovery.

**Price:** preparation latency. A brief that takes twenty minutes to write delays the start by twenty minutes. This is only worth it when the expected rework is longer than the brief — which, at a one-in-three first-pass rate, it usually is.

### 2.3 The verification ladder: pay for the cheapest check that can fail

**Removes:** recovery, and a specific kind of waste — running an expensive check that could not possibly have caught the defect.

A four-level ladder:

| Level | What it proves | Cost |
|---|---|---|
| L0 static | Syntax, structure, configuration parsing, artifact matches source | Seconds |
| L1 unit / contract | Logic, interface, wording | Tens of seconds |
| L2 browser / real-device smoke | Real rendering, interaction, console | Minutes, plus an exclusive machine |
| L3 full gate plus release | Whether this can ship | Ten minutes, plus production risk |

The rule is to **start at the level that matches the change surface** and climb only when a lower level cannot decide. A CSS class rename does not need a browser suite. A storage-shape change cannot be settled by clicking a page by hand.

Two disciplines make the ladder save rather than cost:

- **Independent re-run.** The verifier runs the command itself rather than trusting a pasted result. A pasted result is a claim; a re-run is a fact. Accepting claims is how a green check becomes decorative.
- **A deliberately broken case.** Before trusting a gate, break one thing on purpose and confirm the gate goes red. A gate that cannot fail is not a gate, and every token spent running it is wasted.

**Price:** the ladder adds process. Its whole justification is that the alternative — running everything, every time — is more expensive *and* less informative.

### 2.4 The 35% rule: replace a spent session, do not top it up

**Removes:** production waste inside a degrading context, plus recovery later.

A short-lived worker session that has consumed more than a third of its window is no longer cheap to continue. Every further turn pays to re-read a context that is mostly about work already finished, and the quality of decisions made in a context full of stale detail is worse — which shows up later as rework.

So the gate is absolute for one-shot sessions: above the threshold, **do not dispatch new work into it; spawn a fresh session instead.** There is no override flag, because a fresh session is cheap and there is no reason to force more work onto a spent one.

Long-lived roles get a different, higher schedule — allow below a threshold, require handover above a second one, hard-refuse above a third — because their context contains something a fresh session cannot reconstruct cheaply: the ongoing relationship.

**Price:** the remaining context in the old session is thrown away. That is the point. It was already mostly overhead.

### 2.5 Redirect versus reject: a wrong premise cannot be patched

**Removes:** rework — specifically the second and third attempt that follows a first attempt built on the wrong premise.

Two kinds of failure look alike in a verdict and are not alike at all:

| Kind | Diagnosis | Correct response |
|---|---|---|
| **Defect** | The direction was right; the implementation has a bug or missed an acceptance item | Send it back **in place**. The context is still valid and still valuable. |
| **Drift** | The requirement was misread, or the approach is wrong | **Archive the session and start a new one.** Carry forward only the sentence explaining *why the old approach was wrong*. |

The second case is the expensive one to get wrong. Patching a wrong premise in place means paying for a context that contains the wrong approach, then paying again for the reasoning that talks the model out of it, and then discovering on the third attempt that the premise was still wrong.

The transferable rule: **if the second attempt would carry the same wrong premise, do not attempt it in place.** A new session with a corrected premise is cheaper than a corrected session.

**Price:** a fresh session loses whatever useful incidental knowledge the old one had accumulated. That knowledge should have been written into the record; if it was not, the loss is a signal that the record discipline is too weak, not that the rule is wrong.

### 2.6 One-shot workers and fixed long-lived sessions

**Removes:** recovery, and the quiet cost of cross-contamination.

Two shapes, used for different reasons:

- **One-shot worker**: created for one deliverable, archived when it is accepted. It never sees a second task, so it never carries a first task's context into a second task's decisions.
- **Fixed long-lived session**: a role that keeps its identity across tasks, precisely so its accumulated judgement is not rebuilt each time.

The failure mode this prevents is reuse without rotation. A long-lived session that takes on a different project's work does not become more efficient; it becomes a session whose context is a mixture of two things, and neither half is clean. The symptom is subtle — the answers stay plausible — which is why the discipline has to be a rule rather than a judgement call.

**Price:** more sessions to name, bind and archive. This is bookkeeping, and bookkeeping is the cheapest thing in the system.

### 2.7 One-line messages, details in the record

**Removes:** coordination, and the re-reading that long messages cause.

A message between agents should carry a verdict, a pointer, and nothing else. The reasoning, the evidence, the intermediate output — all of it belongs in the record, addressed by reference.

The reason is arithmetic. A message is read by its recipient, and then again on every subsequent turn in which it is still in context. A record entry is read only when someone asks for it. Putting a long explanation in a message means paying for it repeatedly; putting it in the record means paying for it once, and only if it turns out to matter.

This is also why **the local process document should not persist.** A task brief, a close-out note, a review write-up — these are consumed once and then become noise. Their durable form is the record entry, which is searchable, addressable and does not occupy anyone's context by default.

**Price:** the record has to actually be good. A one-line message that points at nothing is worse than a long message, because it forces a round trip to discover that the pointer was empty.

### 2.8 The LLM-free engine

**Removes:** coordination — all of it, for the transitions it covers.

The engine that moves an item between states, notifies the right role, retries a failed notification and reconciles a close-out makes **no semantic decisions**. It is a state machine. Its cost per transition is approximately zero, and it does not get tired, drift or hallucinate on the thousandth transition.

This is the single largest structural saving available, and it is the one most often left on the table. A team that has a model deciding "should this go to QA now" is paying a model to execute a rule that a `if` statement already encodes. The model's contribution is not judgement; it is latency and variance.

The boundary matters: the engine handles transitions that are **decidable from the record**. Anything requiring reading a requirement and forming an opinion stays with a model. The design question is never "can the engine do this" but "is this decision a lookup or a judgement".

**Price:** the engine must be fed by a clean record, and it must fail loudly when the record is ambiguous rather than guessing. An engine that guesses is worse than a model that guesses, because nobody reviews it.

## 3. What this costs, honestly

Every mechanism above has a real price, and a paper that lists only the savings is a sales document.

- **Latency.** Preparation, gating and independent re-runs all add wall-clock time before the first line of production. For a task that would have succeeded on the first attempt, this is pure overhead.
- **Rigidity.** A hard threshold at 35% throws away context that sometimes would have been useful. A hard rule about one-shot sessions forbids a reuse that occasionally would have been efficient.
- **Bookkeeping load.** More sessions, more archives, more records. Someone has to keep them, and that someone is usually the same person doing everything else.
- **Information loss at the relay.** "Relay without editing" means a badly written verdict reaches the worker badly written. The rule trades distortion for fidelity, and fidelity is not always the kinder choice.

The claim is not that these mechanisms are free. The claim is that they are **cheaper than the alternative at the failure rate we actually observe**, and that they degrade gracefully: if a threshold turns out to be wrong, moving it is a one-line change, not a redesign.

## 4. Transferable criteria

Five questions to ask of any step in an agent workflow. Each one, answered "yes", points at a specific saving.

1. **Can a program decide this?** If the decision is a lookup against the record, it belongs in code. A model doing it is a recurring bill for a constant answer.
2. **Is this session's context mostly about finished work?** If yes, replace it. The remaining window is not a resource; it is accumulated overhead that the next turn will pay to re-read.
3. **Would a second attempt carry the same wrong premise?** If yes, do not attempt it in place. Start clean and carry forward only the correction.
4. **Will this message be read more than once?** If yes, put the content in the record and send a pointer. Message content is paid for on every turn it stays in context.
5. **Is this information needed by more than one role?** If no, it does not belong in a shared brief. Shared briefs are the most expensive real estate in the system, because everything in them is read by everyone.

A sixth, for the record itself:

6. **If this record entry were deleted, could the team reconstruct it from something else?** If yes, it is a cache, and caches should not be treated as sources of truth. If no, it is a fact, and it must be written in the place that survives rotation.

## 5. What we cannot claim

This paper argues from structure, not from measurement. Specifically:

- **No per-mechanism numbers.** We do not have an A/B measurement of "with the 35% rule" versus "without". The rule is justified by the shape of the cost, not by a controlled experiment.
- **The one-in-three figure is an order of magnitude, not a constant.** It comes from our own runs on our own task mix. A different model, a different codebase or a different task shape will move it, and the argument only needs it to be *well below one*.
- **Token counts are not the only cost.** Wall-clock latency, human attention and the risk of a wrong release are all real and none of them appear in this accounting. A mechanism that saves tokens while adding a day of latency is not obviously a win.
- **The engine's zero cost is per transition, not per incident.** A state machine that is fed an ambiguous record will stall, and a stall costs a human. The zero is real only when the record discipline holds.

The honest summary: **this architecture is designed to make failure cheap, not to make success fast.** Success is bounded by the task. Failure is bounded by the design, and that is the part worth engineering.
