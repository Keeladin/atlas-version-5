# Atlas V5 Model Prompt Control and Audit

Status: **Accepted design direction / implementation record**
Date: 2026-09-12

## 1. Why this document exists

Atlas V5 treats the model as the primary semantic decision-maker while the runtime owns deterministic execution, persistence, authority and hard boundaries. During normal development the foreground prompt accumulated many failure-specific corrections: evidence cautions, chronology rules, memory lifecycle mechanics, capability warnings and runtime bookkeeping. Each rule was individually defensible, but together they changed the model's operating posture.

A full prompt audit on 2026-09-12 showed that the always-on foreground instruction was dominated by restraint and procedure rather than semantic priorities. The problem was therefore treated as a **control hierarchy and prompt provenance problem**, not a sampling-temperature problem.

This document records where model-visible controls come from, which layer should own them, and how future prompt changes should be audited.

## 2. Control hierarchy

Controls should live at the lowest layer that can enforce them correctly:

1. **Runtime enforcement** — permissions, capability enablement, secret isolation, path containment, approval boundaries, action identity, concurrency, version fences and other deterministic guarantees belong in code. The conversational model need not be repeatedly warned about rules the runtime already enforces.
2. **Semantic priorities** — the always-on conversational constitution tells Atlas what to optimize for: understand owner intent, produce a useful outcome, exercise semantic judgment, form and express views, disagree when warranted, surface useful implications, calibrate uncertainty and match verification effort to consequence.
3. **Epistemic/source hierarchy** — the model needs a compact persistent rule for distinguishing owner assertions, prior model statements, durable memory/continuity context and runtime/tool observations, and for escalating to canonical evidence when exact reconstruction matters.
4. **Dynamic task/context state** — active-task checkpoints, current capability families, current-chat history and continuity handoffs are injected only as current state.
5. **Subsystem contracts** — memory lifecycle mechanics, reconciliation schemas, scheduling rules and other specialist procedures should be supplied only to the subsystem or turn that needs them, rather than shaping every ordinary conversation.
6. **Technical output contracts** — hidden runtime metadata schemas may remain model-visible when the model must emit them, but they are explicitly separated from the conversational constitution.

Higher-level semantic priorities do not weaken lower-level runtime enforcement. They give the model meaningful options inside boundaries the runtime already guarantees.
## 3. Prompt provenance

Model-visible text must have an identifiable source in the repository or in bounded runtime state. The 2026-09-12 audit traced every current model call (`complete_text`, `stream_text`, and provider `responses.create` paths) and catalogued the text that can reach a model, including apparently irrelevant wrappers.

The principal sources are:

- `backend/atlas/runtime/bootstrap.py` — minimal Atlas seat identity and model/runtime boundary.
- `backend/atlas/runtime/conversation.py` — foreground conversational constitution, context/source labels and hidden runtime output contract.
- `backend/atlas/runtime/task_state.py` — protected active-task developer projection.
- `backend/atlas/memory/discovery.py` — bounded background memory discovery contract.
- `backend/atlas/memory/continuity.py` — cross-chat handoff summarizer contract.
- `backend/atlas/memory/reconciliation.py` — blind evidence reading, proposal comparison and memory-graph reconciliation contracts.
- `backend/atlas/schedules/runner.py` — scheduled owner-intent wrapper.
- `backend/atlas/providers/openai.py` — provider-visible resource wrappers, tool descriptions and context-compaction notices.
- `backend/atlas/api/app.py` — model connection-test instruction.

The audit artifact was generated from the live working tree on the Atlas host and saved outside the repository at `/home/jaco/Workspace/Exports/atlas-v5-model-prompts-audit.txt`. It is a point-in-time inspection artifact, not a canonical configuration file. The repository source named above is authoritative for what Atlas sends at any later revision.

Prompt provenance therefore follows the same principle as the rest of Atlas: **source code and canonical runtime state are authoritative; derived audits are evidence about a particular revision, not authority over later revisions.**

## 4. 2026-09-12 foreground rebalance

The first control-hierarchy pass reduced the always-on foreground base from roughly 987 words / 7,095 characters to roughly 334 words / 2,475 characters. Including the hidden runtime-output contract, the instruction reduced from roughly 1,303 words / 9,532 characters to roughly 597 words / 4,526 characters.

The change was intentionally subtractive before additive. Detailed memory lifecycle mechanics were removed from ordinary foreground conversation because the capability registry and runtime own the operational contract. Repeated uncertainty/evidence warnings were collapsed into a proportional verification rule. Developer injections were reduced to short source labels. Runtime metadata remained available, but was separated under an explicit `RUNTIME OUTPUT CONTRACT` heading.

The foreground constitution now positively prioritizes useful intent resolution, semantic judgment, reasoned disagreement, relevant initiative, natural continuity, best-supported commitment and proportional verification. Negative prohibitions are reserved for places where an actual hard boundary or protocol contract requires them.

This is not intended to make Atlas indiscriminately proactive. It restores an action space: Atlas may choose among several useful conversational moves while runtime boundaries remain unchanged.
## 5. Review rule for future prompt changes

Before adding a permanent foreground instruction, ask in order:

1. Can the runtime enforce this deterministically? If yes, put it in runtime rather than teaching the model a prohibition.
2. Is this a general semantic priority or only a repair for one historical failure? Failure-specific instructions should normally become tests, subsystem contracts or conditional injections.
3. Does the model need this information on every ordinary turn? If not, inject it only when the relevant state or subsystem is active.
4. Is the same epistemic rule already expressed elsewhere? Prefer one strong rule over repeated warnings; repetition changes behavioural weighting even when wording is individually correct.
5. Does the instruction tell Atlas what to prioritize, or only what to avoid? Prefer positive priorities except where a protocol or hard semantic boundary genuinely needs a prohibition.
6. Does the change preserve provenance — source class, authority and the distinction between model judgment and runtime fact?

A prompt audit should be regenerated whenever a significant new model-facing subsystem is added or when conversational behaviour changes unexpectedly. The audit should enumerate actual model call sites first, then trace their instruction strings, developer messages, user wrappers, tool descriptions and dynamically injected state. Searching for the word `prompt` alone is not sufficient.

The behavioural constitution should remain small enough to review as a coherent whole. If it becomes a history of every bug Atlas has ever made, the control hierarchy has failed.

## 6. Relationship to other architecture documents

This document refines, rather than replaces, `01-architecture.md` and `17-runtime-constitution.md`. The architecture remains model-led; the runtime constitution remains the source of hard runtime invariants. `13-memory-lifecycle.md` remains authoritative for memory lifecycle semantics and `23-shared-state-write-contract.md` for shared-state mutation fencing.

The important separation is deliberate: **the runtime constitution constrains what can happen; the conversational constitution guides what Atlas should choose to do.**