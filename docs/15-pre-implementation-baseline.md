# Atlas V5 Pre-Implementation Architecture Baseline

Status: **Accepted architecture baseline — implementation planning may begin**

This document consolidates the architectural direction reached during the V5 design pass. It is the reference against which the remaining planning documents should be read.

## 1. Product shape

Atlas is a persistent local agent environment. The selected model is the cognitive centre and primary semantic decision-maker. Atlas supplies the environment in which that model can converse, perceive, act, remember, and be awakened later.

The normal product has two principal surfaces:

- **Atlas** — one main page centred on multimodal chat, with useful workspace projections around it;
- **Control** — a separate engineering/configuration surface for providers, capabilities, connections, memory, schedules, health, usage, and diagnostics.

Internal faculties do not become top-level product pages merely because they exist.

## 2. Model/runtime boundary

The model owns semantic decisions: interpretation, relevance, workflow, tool choice, adaptation, sufficiency, clarification, and completion.

The runtime owns deterministic orchestration: context assembly, provider calls, tool execution, persistence, trigger mechanics, exact validation, permission enforcement, health state, and faithful result reporting.

The runtime may contain substantial software, but it must not become a semantic planner competing with inference.

**Runtime governs execution and reality, not thought.** Its non-negotiable execution conduct is defined separately in `17-runtime-constitution.md`.
## 3. Context and continuity

Atlas owns continuity. The active provider may maintain conversational state as an optimization, but Atlas keeps the canonical temporary transcript and can reseat a model or provider without depending on hidden provider memory.

Each inference receives a small seat bootstrap plus the relevant model-visible portion of the current transcript. The Environment Registry remains outside prompt context and is consulted on demand.

Older context is not loaded wholesale. Closed transcripts produce a compact context capsule and move into indexed short-term memory for a configurable retention period.

## 4. Memory

The live transcript is the scribe: it records what happened in order without deciding what it means. It may contain text blocks plus references to artifacts and tool observations.

A separate asynchronous memory processor interprets closed or aging transcripts. It may discard information, retain it temporarily, promote it to embedded long-term memory, or preserve it as canonical durable memory. The active model is not the normal writer to durable memory.

Explicit owner instructions to remember, correct, or forget are durable commands with observable completion state and precedence over stale derived memory. Forgetting/correction must prevent queued or older derived representations from resurrecting superseded information.

The durable memory substrate is PostgreSQL. Semantic retrieval uses pgvector; exact and lexical retrieval use PostgreSQL search and metadata. Retrieval is hybrid rather than vector-only.

Large artifacts remain outside the database; PostgreSQL stores their metadata, provenance, relationships, hashes, and storage references. Embeddings are indexes over memory, not memory itself.

Retention periods, chunk sizes, overlap, ranking weights, summary size, and similar parameters remain configurable implementation choices.

## 5. Workspace and artifacts

A workspace is Atlas's maintained field of action around current work. It can combine storage references, operational state, external resources, and temporary UI projections.

Workspace labels such as software, documents, research, and operations are descriptive rather than rigid workflow classes. The model decides what resources it needs; the runtime preserves the situated state that emerges.
Artifacts are first-class conversation/workspace objects. Images, documents, audio, generated files, and other binary content live in temporary or durable artifact storage; transcripts refer to them by artifact identity and metadata rather than embedding their bytes.

The main interface may project useful workspace views such as recent or important mail, file browsing, document previews, research sources, generated artifacts, a small runtime-health strip, or a compact Needs You view for blocked/uncertain work around the central chat.

## 6. Multimodality

Atlas chat is multimodal by design. The core message model must support content blocks rather than assume `message = string`.

Text, images, documents, audio, tool results, and generated artifacts should be able to participate in the normal conversation whenever the selected model or enabled tool surface supports them.

Provider-native capabilities should be used before Atlas builds substitutes. If the model can see an image, generate an image, understand a document, execute code, or use another native ability, Atlas should make that ability naturally usable through the product.

## 7. Capabilities, tools, and authority

The Environment Registry is the agent's compact map of enabled capabilities and resources. It does not preload their contents or every underlying tool schema.

Capabilities are grouped semantically. The owner enables meaningful abilities; the model chooses the exact underlying tools and sequence. Large MCP catalogs should use progressive disclosure and clear Atlas-side descriptions rather than expose hundreds of ambiguous raw functions at once.

Authority is primarily established by owner enablement over provisioned capabilities. Atlas distinguishes provisioned, enabled, available, and usable-at-this-boundary state. Disabled capabilities are absent from the agent-visible environment and not callable through Atlas-controlled alternate paths.

Enabled capability does not override hard runtime containment. Secrets, protected configuration, privileged internal state, and sensitive runtime interfaces remain inaccessible to ordinary model-facing tools. There is no general `CONFIRM` authority state in V5; runtime enforcement reports exact technical failures back to inference.

For Atlas's own durable memory, the active model may have broad read access but is not the normal writer. Its natural write surface is the transcript; the asynchronous memory processor owns durable memory mutation.
## 8. Schedules and passive automation

A schedule is persisted intent bound to a deterministic trigger or execution window. Triggers may be one-off, recurring, time-windowed, or event-driven.

The runtime determines when a trigger is due and creates a distinct run with the stored intent. The model interprets that intent and decides how to achieve it using the capabilities available at execution time. Current authority applies at execution time; overlap, cancellation, missed/duplicate trigger handling, and shared-resource mutation are deterministic runtime concerns.

Operational details such as indexing windows, batch sizes, resource limits, or preferred run periods remain configurable rather than architectural constants.

## 9. Provider posture

OpenAI is the primary/reference inference provider. Anthropic is the first compatibility check. Gemini and xAI remain valid later providers or specialists.

Atlas owns identity, transcript, memory, workspace, schedules, authority, artifacts, Environment Registry, and product semantics. Provider-native conversation state and tools are accelerators, not canonical Atlas state.

Software workspaces should use existing conventions where useful. `AGENTS.md` is the preferred workspace-local instruction convention for software repositories rather than an Atlas-specific replacement.

## 10. Implementation philosophy

Implementation is heliocentric rather than use-case vertical-slice driven.

The centre is a working model with a usable multimodal interface and the minimum runtime execution spine required for identity, transcript/artifact continuity, Environment Registry state, secret isolation, capability enablement, durable action/effect truth, recovery, and owner-visible failure state.

The first orbit is the selected model/provider's native capabilities. The next orbit is MCP, connected services, and useful local software or independent utilities. Runtime capability grows in parallel with every orbit. Consequential capabilities are enabled only when the runtime can preserve the constitution's execution guarantees.

Memory, workspace state, scheduling, Control, and diagnostics mature around the functioning agent. They support the centre; they do not become competing centres of decision-making.

Implementation should not begin by hard-coding representative workflows. Validation later derives from the capabilities actually present and proves the architecture rather than inventing it.
## 11. Architectural invariants

The following are baseline decisions, not tuning knobs:

- the model is the primary semantic decision-maker;
- the runtime does not pre-plan ordinary workflows;
- Atlas owns continuity and canonical state rather than a provider session;
- the Environment Registry is queried on demand rather than dumped into every prompt;
- the transcript records experience; the memory processor interprets experience;
- durable memory writes are separated from active conversational inference;
- multimodal artifacts are first-class and referenced from transcripts;
- disabled capabilities are absent from the agent-visible environment;
- authority is enforced at real effect boundaries without a general confirmation loop;
- secrets and protected runtime state remain outside ordinary model-facing tools;
- consequential effects have durable identity, exact evidence, and crash-safe recovery semantics;
- untrusted external content is data rather than runtime/owner authority;
- schedules wake intent rather than encode workflows and each wake has distinct run identity;
- workspaces preserve the field of action rather than impose task classes;
- implementation grows heliocentrically around the model.

## 12. Deliberately configurable or deferred

Retention TTLs, transcript rollover thresholds, chunk sizes, embedding model, ranking formulas, database table shapes, artifact-store implementation, panel layout, model-routing heuristics, indexing windows, and similar operational parameters are not frozen by this baseline.

They should be selected experimentally during implementation and remain configurable where practical.

## 13. Owner-approved memory-candidate refinement — 2026-09-09

The foreground conversational model may emit a small bounded set of **non-authoritative memory candidate hints** as hidden metadata in the same inference that produces the owner-visible reply. This adds no memory-only inference call and does not make the active model a durable-memory writer.

Runtime validates the candidate schema, binds canonical transcript/turn/provider-evidence provenance, and queues valid hints. The asynchronous memory processor remains the only normal path that may interpret, reconcile, reclassify, merge, discard, or publish derived memory. A candidate has no retrieval authority merely because the conversational model proposed it. The canonical transcript remains sufficient source material when no candidate is emitted or when later memory processing chooses to ignore one.

This refinement deliberately distinguishes **candidate proposal** from **memory mutation**. Explicit owner `remember`, `correct`, and `forget` commands retain their stronger canonical command path and precedence semantics.

## 14. Implementation gate

This baseline and `17-runtime-constitution.md` were accepted by the owner on 2026-09-05, with the bounded foreground candidate-hint refinement above approved on 2026-09-09. Implementation planning may proceed without reopening settled architecture by default.

The implementation plan should translate these decisions into a heliocentric build sequence without reopening settled semantics through code-first experimentation. The constitution supplies runtime guardrails; it does not add a second semantic planner.