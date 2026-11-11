[English](07-perpetual-context.en.md) | [中文](07-perpetual-context.zh.md)

# 07 · Perpetual context

> Series: design philosophy ｜ [01 Core architecture](01-core-architecture.en.md) ｜ [02 Requirements Analyst](02-requirements-analyst.en.md) ｜ [03 Agent-to-agent communication](03-agent-communication.en.md) ｜ [04 Human-facing Reporter](04-human-reporter.en.md) ｜ [05 Artifact recycle bin](05-artifact-recycle-bin.en.md) ｜ [06 Token economics](06-token-economics.en.md) ｜ 07 Perpetual context
> Version: 2026-11-11
> Constraints: unofficial project, one person maintaining several projects, limited resources, no heavy database.
> Nature: the thesis of this series. It argues for a direction; it authorizes nothing.

## Executive summary

The industry is racing toward larger context windows. This paper argues that the race is aimed at the wrong problem.

A context window is not a container that keeps overflowing because it is too small. It is a container that keeps overflowing because **the wrong things are put into it**. Making it larger delays the overflow and simultaneously makes each turn more expensive, because every token in the window is re-read on every turn.

The thesis, in four parts:

1. **The supervisor's context should be perpetual.** A working relationship between one person and their top-level agent should last months. It should not be periodically destroyed and rebuilt, and it should not fill up with project detail.
2. **Project memory belongs outside the supervisor.** It lives in the record — in Issues, in documents, in the artifact store. The supervisor holds *pointers and judgements*, not *contents*.
3. **Therefore the correct architecture is an ant colony, not a bigger brain.** A queen ant does not know where the food is. The colony finds it, remembers it, and the queen ant keeps doing the one thing only she can do: deciding.
4. **Rotation then becomes cheap and safe.** When a session is rotated, only a minimal set of global facts moves. Everything else is re-read from the record on demand. Memory loss stops being a risk because memory was never in the session.

This is the highest-leverage claim in the whole series, and it is also the one most easily misread. It does not say context is unimportant. It says context is **too important to be filled with things that have a better home**.

## 1. The wrong diagnosis

### 1.1 Why "bigger window" is the intuitive answer

The symptom is real and it is easy to reproduce: an agent that has been working for a long time starts making mistakes it would not have made an hour ago. It forgets a constraint stated earlier. It re-litigates a settled decision. It confuses two similar items.

The intuitive diagnosis is "it ran out of room". The intuitive fix is "give it more room". Vendors compete on window size because the symptom is universal and the fix is legible.

But notice what the symptom actually is. The agent did not lose the information — it is still in the window. What it lost is the **ability to tell which parts of the window matter now**. A larger window does not fix that. It makes it worse: more irrelevant material competing for the same attention, and every turn paying to re-read all of it.

### 1.2 The real diagnosis: contamination, not capacity

Split what is in a long-running session's context:

```text
the user's intent and current priorities        ← must stay
standing authorizations and boundaries          ← must stay
the current open questions and who owns them    ← must stay
project A: file paths, function names, schema   ← belongs in project A's record
project B: a discussion from three weeks ago    ← belongs in project B's record
project C: the exact wording of a requirement   ← belongs in project C's record
the text of twelve past tool results            ← belongs nowhere
```

The first three lines are small. The rest is unbounded, and it grows with every project the supervisor touches. That is the actual mechanism: **the supervisor's context is not too small, it is being used as a filing cabinet.**

A filing cabinet in the supervisor's head is expensive in three ways at once:

- **It costs tokens every turn**, because it is re-read every turn.
- **It costs judgement**, because the signal-to-noise ratio degrades and the model has to spend capacity deciding what to ignore.
- **It costs continuity**, because the moment the window fills, the session must be rotated — and rotation destroys the three small things at the top along with the unbounded things below them.

That third cost is the one nobody budgets for, and it is the reason rotation feels dangerous.

### 1.3 What a bigger window would actually buy

Suppose the window doubled. What happens?

- The supervisor can go twice as long before rotating. Good.
- Every turn now carries up to twice the context. Bad, and it is a recurring cost, not a one-time one.
- The contamination problem is unchanged, just deferred.
- When rotation finally happens, the amount of context lost is larger, so the recovery pack has to be bigger, so the recovery costs more.

**A larger window converts a frequent small problem into a rare large one.** That is not obviously an improvement. It is a change in the shape of the failure, and for a long-lived relationship it is the wrong shape: you want failures to be frequent, small and cheap, not rare, large and catastrophic.

## 2. Perpetual context

### 2.1 What "perpetual" means precisely

Not "never compacted". Compaction is a fact of life and pretending otherwise is how designs become brittle.

**Perpetual means: the identity, the intent and the authorization survive compaction; everything else is re-derivable.**

A perpetual context has three properties:

1. **It is stable in what it holds.** The set of things in the supervisor's context does not grow with the number of projects. It grows with the number of *open decisions*, which is bounded by human attention.
2. **It is reconstructible in what it lost.** After a compaction, the supervisor can rebuild any project detail it needs by asking a precise question of the record — not by asking someone to re-explain.
3. **It is honest about what it does not know.** A supervisor that has outsourced project detail should say "I do not have that; let me look it up" rather than reconstructing a plausible guess from stale fragments.

Property 3 is the hardest and the most important. Outsourced memory only works if the agent is willing to admit the gap. An agent that half-remembers is worse than one that has forgotten cleanly, because it produces confident wrong answers and nobody knows to check.

### 2.2 The compact recovery pack

When a session is compacted, what must be carried forward is small enough to enumerate:

| Must survive | Why |
|---|---|
| **Who the user is and what they are trying to achieve** | Not reconstructible from any record; it is the premise of all the records |
| **Current authorizations and hard boundaries** | Reconstructing these from history is slow and error-prone, and getting them wrong is expensive |
| **Open decisions and who owns each** | This is the actual work queue; losing it means losing the thread |
| **Where the record is** | Pointers, not contents. Enough to ask the right question. |
| **Standing conventions that are not written down anywhere else** | If it is written down, it does not need to be here |

And what must explicitly **not** be carried:

- File paths and function names from any specific project.
- The content of past tool results.
- The text of resolved discussions.
- Any detail whose only justification is "it came up recently".

The test for inclusion is one question: **if this were lost, could the agent recover it in one lookup?** If yes, it does not belong in the pack. The pack is for things that cannot be looked up, because they were never written down anywhere except in the conversation.

That last clause is the real design constraint. **Anything that matters must be written down, or it must be in the pack.** There is no third option, and the failure mode is pretending there is.

### 2.3 Why this makes rotation safe

Rotation is currently scary because it is a lossy operation on the most valuable state. Under perpetual context, rotation becomes:

```text
rotate session
  → load the small pack (identity, intent, authorization, open decisions, pointers)
  → for each open decision, ask the record one precise question
  → resume
```

The cost is proportional to the number of **open decisions**, not to the length of history. A project with 200 closed Issues and 2 open ones costs the same to resume as a project with 5 Issues and 2 open ones. That is the property that makes long-running work possible.

It also changes what a rotation is *for*. It stops being an emergency procedure ("we ran out of window") and becomes a routine one ("this task is done; the session's remaining context is overhead"). The 35% rule from the previous paper is only safe because of this property — you can afford to throw away a session when nothing valuable lives only there.

## 3. The ant colony

### 3.1 The analogy, stated carefully

An ant colony works because no single ant knows the colony's plan. The queen ant does not know where the food is. Foragers find it, return, and the information propagates through local interactions. The colony's memory is distributed: in the pheromone trails, in the nest structure, in the bodies of the ants.

The mapping is not decorative. Each part corresponds to a real design decision:

| Ant colony | Team |
|---|---|
| Queen ant: reproduction and colony-level decisions | **supervisor**: intent, authorization, arbitration |
| Foragers: find and carry food | **workers**: execute one deliverable each |
| Pheromone trails: durable, decaying, addressable | **the record**: Issues, documents, artifacts |
| Local interaction rules, no central plan | **the engine**: deterministic transitions |
| Nest structure: stable, slowly changing | **roles and boundaries**: who may do what |

What the analogy gets right: **the queen ant's ignorance is a feature, not a limitation.** If the queen ant had to know where the food was, the colony could not scale past what one brain could hold. The queen ant's context is small *because* the colony is organized so it can be.

What the analogy gets wrong, and where we should not follow it: ants have no notion of "why". Our foragers do, and the record has to preserve intent, not just location. A pheromone trail says "food this way"; an Issue says "we want this, for this reason, subject to these constraints". The record must be richer than a trail, and correspondingly more expensive to maintain.

### 3.2 The evolution argument

The ant colony is not a metaphor chosen for charm. It is the **product of biological evolution**, and that is precisely why it is the right model for a resource-constrained agent team.

Consider what a single ant actually is:

- A nervous system with roughly a quarter of a million neurons — four orders of magnitude fewer than a human.
- No representation of the colony's goal. No map. No plan.
- No ability to reason about a problem it has not encountered before.
- A lifespan measured in weeks, so it cannot accumulate long experience.

By any measure an engineer would use, a single ant is a very poor agent: tiny context, no reasoning, no memory worth the name. And yet a colony of them **migrates across terrain, allocates foragers across food sources, builds structures with regulated temperature, and recovers from the destruction of most of its members.** These are tasks that a human would need a map, a plan and a meeting to attempt.

The resolution is that evolution did not optimize the ant. **It optimized the colony.** The intelligence is not in the node; it is in the interaction rules between nodes and in the durable traces they leave in the environment. A single ant's failure is cheap — it dies, and the colony loses one forager out of thousands. The colony's memory survives the death of every individual in it.

That is the argument this paper makes about agent teams, stated in biological terms:

| Ant colony | Agent team |
|---|---|
| Individual ant: no context, no reasoning, tiny brain | **worker / QA**: one deliverable, no long-term memory |
| Colony: solves problems no ant could solve | **the team**: solves problems no agent could solve |
| Pheromone trail: outlives the ant that laid it | **the record**: outlives the session that wrote it |
| Evolution selected for colony-level fitness | **design should select for team-level fitness** |

The transferable claim is uncomfortable but load-bearing: **if evolution found that a colony of very stupid nodes beats a small number of very smart ones, then making each node smarter is not obviously the right investment.** A larger context window is an attempt to make each ant smarter. The colony's answer was to make the *interaction* better and the *traces* more durable.

This is also why the design does not require a strong reasoning model at every node. A worker that reads a precise brief, edits the named files and runs the named acceptance command does not need to reason about the project. The reasoning happened earlier, in the brief, and it is preserved in the record. **The intelligence is amortized across the architecture rather than concentrated in each session.**

### 3.3 What distribution buys, concretely

Three properties that a single large context cannot have:

**Isolation.** Two projects cannot contaminate each other's decisions if they are in different sessions. A single large context has no such guarantee — everything is adjacent to everything.

**Parallelism.** Independent work proceeds without serializing on one context. A single context must process one thing at a time.

**Rotation granularity.** A project can be rotated without touching the others. A single context rotates all-or-nothing.

And one property it *loses*, which must be stated: **distribution loses the accidental cross-pollination** that comes from everything being in one place. A single context sometimes notices that two projects have the same problem. A distributed team only notices if someone asks. The mitigation is the supervisor's role — it is the one place where cross-project patterns can be observed, precisely because it holds pointers to all of them and contents of none.

### 3.4 The supervisor as the only shared node

This is the structural claim that makes the ant colony work: **the supervisor is the only node that sees all projects, and it sees them only as pointers.**

That combination is what makes cross-project judgement possible without context bloat. The supervisor can ask "which projects are blocked on the same dependency?" because it holds the open-decision list for all of them. It cannot answer "what does the schema look like in project B?" without a lookup, and it should not try.

The discipline is: **the supervisor's value is in the questions it can ask, not the answers it holds.** An agent holding answers is a cache. An agent holding questions is an orchestrator. Caches go stale; questions stay sharp.

## 4. Criteria: what goes in, what goes out

Four questions, applied to any candidate piece of information.

### 4.1 Does it belong to exactly one project?

If yes, it belongs in that project's record, and the supervisor holds a pointer at most. The test: **could another project's PM need this?** If not, it is project detail, and project detail in the supervisor's context is contamination.

### 4.2 Could it be recovered in one lookup?

If yes, it should not be in the pack. The pack is for the un-lookupable: intent, authorization, open decisions, and conventions that exist nowhere else.

The corollary is a maintenance rule: **when you find yourself putting something in the pack that could be written down, write it down instead.** The pack is the exception, and every addition to it should feel slightly uncomfortable.

### 4.3 Will it still be true next month?

Standing facts (who the user is, what the boundaries are) belong in the pack. Transient facts (what file we were editing, what the last error was) do not — they belong in the record with a timestamp, or nowhere.

A useful heuristic: **if the fact has a date attached, it is a record entry, not a pack entry.**

### 4.4 Is it a decision or a detail?

Decisions belong to whoever owns them and are tracked as open items. Details belong to the record. The failure mode is a decision recorded as a detail — "we decided to use approach B" buried in a comment — which is unrecoverable because nobody knows it was a decision.

The mitigation is a format rule: **decisions get their own entry, with the reason and the alternatives considered.** A decision without its reason is not recoverable; a decision with its reason can be re-litigated correctly when circumstances change.

### 4.5 A compact recall pack, concretely

The criteria above are easy to agree with and hard to apply. Here is the shape we actually use — small enough to write in a few minutes, specific enough to be wrong.

```text
─── identity ─────────────────────────────────────
Who the user is, in one or two sentences.
What they are ultimately trying to achieve.

─── standing rules ───────────────────────────────
Hard boundaries (never do X).
Standing authorizations (Y is pre-approved).
Conventions that are written down nowhere else.

─── open decisions ───────────────────────────────
For each: the question, who owns it, what is blocking it.
(If there are more than a handful, that is a signal that
 the supervisor is holding work it should have delegated.)

─── the map ──────────────────────────────────────
Which projects exist.
For each: where its record lives, and what it is currently doing.
Nothing about the contents.

─── recent history, compressed ───────────────────
What changed since the last pack, in a few lines.
Enough to avoid re-litigating settled questions.
```

Three things to notice about this shape:

**It is deliberately boring.** There is no place to put an interesting detail. That is the design: an interesting detail in the pack is a detail that will be re-read on every turn for the rest of the session's life.

**The "open decisions" section is the only part that grows.** Identity and standing rules are near-constant. The map grows with projects, slowly. Open decisions grow with human attention, which is the real constraint. If the pack is getting large, the diagnosis is that too many decisions are open — not that the pack needs a new format.

**The map is load-bearing.** Everything else in the pack could in principle be reconstructed; the map cannot, because without it the agent does not know what to look up. When people trim the pack to save space, the map is the last thing that should go.

A useful failure test: **hand the pack to a fresh session, give it one open decision, and see whether it can act without asking a question the pack should have answered.** If it has to ask "which project is this about?", the map is incomplete. If it has to ask "what is the current state of that file?", the pack is working correctly — that is a lookup, and lookups are what the record is for.

## 5. The counter-arguments

A thesis this strong deserves its objections, stated fairly.

### 5.1 "Lookups are slow, so keeping things in context is faster"

True, and it is the strongest objection. A lookup costs a round trip; having the fact already in context costs nothing extra on that turn.

The response: **the cost of a lookup is bounded, the cost of contamination is not.** A lookup costs one round trip. A contaminated context costs a fraction of every subsequent turn, forever, and it degrades judgement in ways that are hard to attribute. Over a long-running relationship, the accumulating cost dominates.

But the objection has a real kernel: **for short tasks, context beats lookup.** A one-shot worker that reads a file and edits it should absolutely keep the file in context. The perpetual-context argument applies to *long-lived roles*, not to every session. Applying it to a short worker is over-engineering.

### 5.2 "If it is not in context, the model will not think of it"

Also true, and this is the sharpest version of the objection. Outsourced memory requires the agent to *know to ask*. An agent that does not know a project exists will never look it up.

The response is that this is a **design responsibility, not a memory property**. The supervisor's context must contain an index of what exists — a list of projects, a list of open decisions — even though it contains none of the contents. The index is small, bounded, and it is what makes the lookups possible.

So the pack is not just "identity, intent, authorization". It is also "**a map of where everything else lives**". Without the map, outsourcing fails; with it, outsourcing works.

### 5.3 "This only works because your projects are small"

Partly true, and worth stating plainly. A team with a hundred projects would need a hierarchical index, and the supervisor's map would itself become large. Our design assumes a number of projects small enough that the map fits comfortably.

The claim is not that this scales to any size. The claim is that **it scales much further than putting contents in context does**, because the map grows with the number of projects while contents grow with the number of projects *times* their history.

### 5.4 "You are just describing a database with extra steps"

There is something to this. Outsourced memory plus a small index plus precise lookups is, structurally, a database with an LLM in front.

The response: **that is the point, and it is a good architecture.** The interesting question is not whether it resembles a database, but where the boundary is drawn. Our answer is: **the boundary is at semantic decisions.** Anything that requires reading and judging stays with the model; anything that is a lookup or a transition goes to the record or the engine. A database with a model in front is a perfectly reasonable description of a system where the model does judgement and the storage does storage.

What we reject is the opposite arrangement: a model that is *also* the storage. That is what a large context is, and it is the arrangement this paper argues against.

## 6. What this does not solve

- **It does not make a bad record good.** Outsourced memory is only as good as the record. If the Issues are vague, the lookups return vagueness, and the supervisor will confidently act on it.
- **It does not remove the need for rotation.** It makes rotation cheap, not unnecessary. A session still has a window.
- **It does not make the supervisor omniscient.** It makes the supervisor *honest about its ignorance*, which is different and more useful.
- **It does not eliminate the cross-pollination loss.** Distributed teams genuinely miss things that a single context would have caught. The supervisor partially compensates; it does not fully.
- **It does not settle the model question.** Whether a given model is good enough at "knowing when to look something up" is an empirical question, and it is the load-bearing one for this design.

## 7. The claim, in one paragraph

The industry's answer to context limits is a bigger window. Ours is a smaller context that never needs to be rebuilt. The difference is not a matter of degree: a bigger window keeps the same architecture and makes each turn more expensive; a perpetual context changes what the architecture holds and makes each turn cheaper. A supervisor should know the user's intent, the current authorizations, the open decisions, and where everything else lives — and nothing else. Everything else belongs in the record, where it can be found when it matters and costs nothing when it does not. The ant colony finds the food. The queen ant decides. That division of labour is what makes both of them able to work for a very long time.
