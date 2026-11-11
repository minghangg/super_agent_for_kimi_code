[English](README.en.md) | [中文](README.md)

# super_agent_for_kimi_code

> **A scheduler and memory layer for one person running several AI coding projects at once.**
> You do not need a bigger context window. You need the right things to stop living in your context window.
> An unofficial companion to [Kimi Code CLI](https://github.com/MoonshotAI/kimi-code), not affiliated with Moonshot AI.

---

## The problem, in one paragraph

You are one person with several projects. Each project has an AI agent session that writes code, a session that reviews it, and a session that decides what to do next. It works — until it doesn't. The deciding session spends its day copying results from one place to another. Your own top-level session fills up with project detail and has to be restarted. And when it restarts, it has forgotten everything except what you can retype.

The ceiling is not the model. **The ceiling is how much one person can hold in their head at once.**

## What this project does

Two things, and only two:

1. **A program moves the paperwork.** When a worker delivers, a deterministic state machine — plain code, no model involved — decides who is notified next. A model is only consulted when a decision genuinely needs judgement.
2. **Memory lives in Issues, not in sessions.** Requirements, briefs, deliveries, review verdicts and decisions are recorded where they can be searched. A restarted session reads a short brief plus a few references and continues.

Everything else in this repository follows from those two.

### The two claims worth arguing about

**Claim 1 — You do not need a bigger context window.** A window overflows not because it is too small but because the wrong things were put in it. Making it bigger delays the overflow *and* makes every turn costlier — everything in the window is re-read on every turn. Move project detail out; keep intent, authorization and open decisions in. See [07](docs/design-philosophy/07-perpetual-context.en.md).

**Claim 2 — A single agent does not need to be smart.** An ant has a tiny brain, no map and no plan, yet a colony migrates across terrain and allocates foragers across food sources. Evolution optimised the colony, not the ant. Keep each agent narrow; put the intelligence in the interaction rules and the durable record. See [07 §3.2](docs/design-philosophy/07-perpetual-context.en.md).

## 60 seconds

| | |
|---|---|
| **Roles are thin** | A project manager agent takes the item, decides two switches, relays verdicts verbatim, and reports upward in five lines. It does not investigate. |
| **Transitions are code** | "This delivery goes to review now" is a rule, not a judgement. A state machine executes it for free and never gets tired. |
| **Memory is a record** | Issues are the long-term truth. The local index is a rebuildable cache — delete it and it comes back. |
| **Rotation is routine** | Because nothing valuable lives only in a session, throwing a session away is cheap. That is what makes long-running work possible. |

```text
  you ──► supervisor  (long-lived, small context: intent · authorization · arbitration)
              │
     ┌────────┼────────┐
     ▼        ▼        ▼
   PM (A)   PM (B)   PM (C)      one per project
     │        │        │
     ▼        ▼        ▼
 worker→QA worker→QA worker→QA   one-shot, isolated, archived when done
     │        │        │
     └────────┼────────┘
              ▼
        Issues = the durable record
        requirements · briefs · deliveries · verdicts · decisions

  the engine (plain code) moves items between these boxes;
  it never decides whether the work is any good.
```

## Try it

No dependencies beyond Python 3.10+. Nothing to install, no credentials needed.

```bash
git clone <this repo> && cd super_agent_for_kimi_code

python -m super_agent.cli doctor
#   health check: process · event bus · data source · config · outbox · cursors
#   reports FAIL honestly when something is wrong — that is the point of it

python -m super_agent.cli once --mode observe
#   one round, compute only. observe performs zero external actions by design.
#   expect: mode=observe issues=0 ... external_actions=0

python -m super_agent.cli reporter --out status.html
#   a self-contained status page: "what needs me right now". open it in a browser.

python -m unittest discover -s tests -v      # 30 files, every counterexample included
```

To point it at real data, set the source to `fixture` (local JSON) or `live` (read-only REST, token from the environment). The default is `none`, so nothing leaves your machine.

**What you see in the first five minutes:** an honest health report, a round that computes but touches nothing, and a status page answering one question — *what needs a human*. That is the whole shape; everything else is elaboration.

## Want the ideas, not the code

Seven short papers, each arguing one design decision. Read 01 and 07 if you only have ten minutes.

| # | Paper | The argument in one line |
|---|---|---|
| [01](docs/design-philosophy/01-core-architecture.en.md) | **Core architecture** | Roles, outsourced memory, events, staged delivery — the whole shape. |
| [02](docs/design-philosophy/02-requirements-analyst.en.md) | **Requirements Analyst** | Vague requirements arrive too early. Pre-review makes them decidable first. |
| [03](docs/design-philosophy/03-agent-communication.en.md) | **Agent-to-agent communication** | Short instructions plus references beat long explanations, which are re-read every turn. |
| [04](docs/design-philosophy/04-human-reporter.en.md) | **Human-facing Reporter** | A status view should be read-only, honest about unknowns, and answer one question. |
| [05](docs/design-philosophy/05-artifact-recycle-bin.en.md) | **Artifact recycle bin** | Usable while work runs, reclaimable after, traceable throughout. |
| [06](docs/design-philosophy/06-token-economics.en.md) | **Token economics** | The expensive token is the repeated one. Four kinds of spend, each removable. |
| [07](docs/design-philosophy/07-perpetual-context.en.md) | **Perpetual context** | Chasing a bigger window is the wrong direction. Ants solved this already. |

Every paper ships in Chinese and English. [Index](docs/design-philosophy/README.md).

## For engineers: the one page that matters

```text
super_agent/
  cli.py               once · run · doctor · status · brief · replay · reporter
  engine.py            the state machine: triage by fixed rules, dispatch at most one action
  assignments.py       Thread × Assignment lifecycle (queued → working → waiting_review → …)
  store.py             index / events / outbox / health — all rebuildable except the record
  github_source.py     read-only Issue comment collection
  dispatch_adapter.py  create, bind, archive — the thin layer that talks to sessions
  brief.py · ctx_gate.py · reporter.py    recovery pack · context gate · status page
tests/                 30 files, stdlib unittest, every counterexample encoded
config/                allowlist: which project, which issue, which action is permitted
```

Three properties worth knowing before you read the code:

- **No dependencies.** Standard library only. `pyproject.toml` declares none.
- **Observe is the default.** `--mode observe` performs zero external actions; the counter is asserted to stay at 0. Nothing happens until you explicitly allow it.
- **Two allowlists, two questions.** One says *which issues may this engine touch*; the other says *which actions may it take*. They are checked separately, in that order.

Design status and what is actually finished: [docs/STATUS.md](docs/STATUS.md). Conventions and the privacy gate: [AGENTS.md](AGENTS.md).

## A note on numbers

This project reports effects qualitatively rather than promising percentages. The one figure we use — **roughly one attempt in three lands without rework** — is our own observation on our own task mix, stated as an order of magnitude, not a guarantee. A different model or codebase will move it. See [06 · Token economics](docs/design-philosophy/06-token-economics.en.md) §5 for what we explicitly cannot claim.

## License

MIT. See [LICENSE](LICENSE).
