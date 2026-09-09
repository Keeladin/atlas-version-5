# Atlas V5 Runtime Constitution

Status: **Accepted runtime constitution**
Date: 2026-09-05

## 1. Purpose

This constitution defines the non-negotiable conduct of the Atlas V5 runtime. It does not define workflows and it does not compete with the model for semantic control.

The architecture says what Atlas is. This constitution says what must remain true while the runtime executes, persists, schedules, exposes tools, and survives failure.

If an implementation violates this document in order to make a feature easier, the implementation is wrong until the constitution is deliberately amended.

## 2. The runtime does not think for the model

Meaning, relevance, workflow, tool choice, adaptation, sufficiency, clarification, and semantic completion belong to inference.

Runtime may make deterministic decisions from explicit state: whether a trigger is due, a capability is enabled, a credential is valid, a path is contained, a query is read-only, an action already succeeded, or a timeout elapsed.

Runtime must not infer the owner's intent, reinterpret tool results into workflow steps, or introduce a hidden planner around the model.

**Runtime governs execution and reality, not thought.**
## 3. Capability state and hard boundaries

Atlas distinguishes four facts: a capability may be **provisioned**, **enabled**, **available**, and **usable at this exact effect boundary**.

The owner-facing discretionary control is ON/OFF enablement. Disabled capabilities are absent from the agent-visible environment and must not be callable through another Atlas-controlled path.

Enablement never overrides hard runtime boundaries. Secrets, runtime authority/configuration, protected internal state, sensitive paths, privileged control sockets, and similar security-critical resources remain inaccessible to ordinary model-facing tools.

Alternative execution paths must preserve the same boundary. Enabling shell, filesystem, code execution, or another broad tool must not expose credentials or privileged internal interfaces that allow a disabled capability to be reconstructed indirectly.

## 4. Secrets remain runtime property

Credentials and secret material must not be placed in model context, transcripts, ordinary artifacts, general tool output, shell environments, or model-readable database views.

Where a tool needs a credential, runtime should broker only the scoped authority needed for that operation and retain custody of the underlying secret.

Credential isolation is a hard execution property, not an owner-tunable capability switch.
## 5. Consequential effects have durable execution truth

Before dispatching a consequential effect, runtime creates a durable action identity sufficient to distinguish that effect from a retry or duplicate.

The action record must preserve its exact execution state and evidence. At minimum the runtime must be able to distinguish not-started/prepared, executing, succeeded, failed, cancelled, and **uncertain** outcomes.

Where supported, runtime should use idempotency keys or equivalent effect identities. After a crash or lost response, an uncertain effect is reconciled before replay; it is never blindly repeated merely because the transcript lacks a success line.

The model decides whether the owner's objective is semantically satisfied. Runtime establishes whether a consequential effect actually happened.

Durable effect evidence must outlive temporary transcript retention.

## 6. The transcript is a faithful scribe

The transcript records owner/model messages, artifact references, tool requests, and tool observations in order. Runtime does not decide which observations are semantically relevant while recording them. Explicit owner deletion is a controlled exception to payload immutability: selected content may be replaced by a deletion marker while turn identity, ordering and deletion provenance remain. This does not permit ordinary inference to rewrite history.

Large or binary observations may be stored as artifacts with stable references in the transcript.

The model-visible context is a bounded projection of the transcript; the canonical transcript and the context selected for an inference are different things.
## 7. External content is data, not authority

Mail, web pages, documents, repositories, retrieved memory, MCP results, and other external content carry provenance and are treated as untrusted data unless Atlas has an explicit reason to treat a particular source as workspace instruction.

Content encountered through a tool cannot redefine Atlas identity, enable capabilities, change runtime authority, disclose secrets, alter protected configuration, or override higher-level owner/runtime instructions merely because it contains imperative language.

Trust/provenance metadata should survive into tool results, artifacts, transcript references, and memory derived from them where practical.

## 8. Runs have identity and concurrency is explicit

Every autonomous or scheduled wake has its own durable run identity and conversational/execution context. A scheduled run does not silently enter whichever foreground chat happens to be active.

Foreground and background runs may share durable resources, but runtime must make conflicting mutation explicit through serialization, leases, version checks, resource locks, or another deterministic coordination mechanism appropriate to the resource.

A chat transcript is not the concurrency boundary for Atlas-owned durable state. For mutable shared resources, runtime binds canonical provenance and a durable operation identity, applies an appropriate resource-version fence, and commits the mutation, version transition and result atomically. A stale writer must conflict explicitly rather than silently overwrite a newer value. The minimal common contract is specified in `23-shared-state-write-contract.md`.

Duplicate triggers, missed triggers, cancellation, restart catch-up, and overlap behavior must be explicit and observable rather than accidental consequences of process timing.

A schedule never receives privileged authority. Current capability enablement, credentials, containment, and effect rules are evaluated when the scheduled work executes.

## 9. Active-task continuity is protected but is not memory

Foreground multi-step work may maintain a small durable active-task checkpoint separate from the canonical transcript and durable memory. The checkpoint is a materialized operational state, not a narrative transcript summary.

Deterministic runtime facts such as tool operation/status, evidence references, bounded resource identifiers, action state and errors are written by runtime from structured events. The model may contribute only semantic task state such as objective, constraints, decisions, findings, open questions and next step. If runtime can derive a field deterministically, model-authored task state may not write it.

Task-state semantic updates must be a by-product of the existing foreground inference response. Atlas may not add a checkpoint-only model call. The active checkpoint is bounded, survives restart through Atlas persistence, and is protected from ordinary working-context eviction while the task remains active.

A dedicated projector retains every admitted semantic field: objective, constraints, decisions, findings, open questions and next step. `replace` replaces semantic fields only. Missing or invalid deltas leave task meaning unchanged; only an explicit valid `status=complete` completes it. Task identity and database revision fence stale responses, and state transforms serialize against the current revision so runtime events cannot overwrite semantic updates or each other.

Unresolved action references remain durable across task replacement and completion. The protected projection includes the unresolved count and up to 16 references; overflow has explicit exact, revision-pinned pagination through `evidence.task.read`. Semantic unresolved work is always projected in full.

Completed tool/resource detail may be compacted or reacquired by reference without erasing the checkpoint needed to state the current objective, what changed, unresolved work and the next intended step.

## 10. Memory commands have precedence and completion state

The transcript remains the normal write surface of the active model, and background memory processing remains separate from conversational inference. As an owner-approved refinement, the foreground model may also emit a bounded set of **non-authoritative memory candidate hints** in hidden runtime metadata as a by-product of the same reply inference. Those hints are not durable memory, do not enter retrieval merely because they were proposed, and may be ignored or reclassified later.

Runtime may deterministically validate the candidate envelope, attach canonical transcript/turn/provider-evidence provenance, deduplicate exact pending proposals, and queue them. It must not reinterpret the candidate semantically at intake. Where memory classification, reconciliation, merge/supersession, or promotion requires semantic judgment, the asynchronous memory processor owns that judgment and may use its own model inference. No memory-candidate-only foreground model call is permitted.

Explicit owner instructions to remember, correct, retire, restore, or delete something are not merely hints. Runtime records them as durable memory commands with a visible lifecycle such as pending, applied, or failed; that explicit path outranks ordinary inferred candidates.

Owner corrections, retirement and deletion outrank stale derived memory. Superseded claims are historical; retired claims are excluded from model recall; deleted identities retain no memory payload and cannot be restored. Provenance invalidation and revision fencing must prevent older transcripts, queued jobs, capsules, embeddings or short-term indexes from re-publishing invalidated information. See `13-memory-lifecycle.md` for the implemented deletion scope and remaining integration limits.

Derived memory may be created asynchronously, but provenance, supersession, deletion, and canonical owner instructions constrain what retrieval is allowed to return as current truth.

Live external systems remain authoritative for facts whose meaning is inherently current, such as latest mail or present calendar state.

## 11. Provider-native tools may not bypass Atlas guarantees

Provider-native perception and computation may be used freely when useful: reasoning, vision, document understanding, image generation, research, sandboxed computation, and similar abilities.

Any provider-native or external tool that performs a consequential effect must still satisfy Atlas's capability enablement, hard-boundary, action-identity, evidence, and recovery contracts.

If Atlas cannot enforce or attest those guarantees for a provider-hosted effect, that effect must not be exposed as an Atlas-controlled capability.
## 12. Persistence classes remain separated

Canonical structured state, large artifacts, and secrets are different persistence classes and must not be collapsed merely for implementation convenience.

The intended V5 topology is:

- PostgreSQL for canonical structured runtime/memory metadata and searchable durable state;
- artifact/file storage for large binary or rendered content;
- protected encrypted secret storage outside model-readable/queryable data surfaces.

Persistent runtime state must live outside the source checkout and have a defined backup/recovery strategy before it is treated as durable.

Operations that require cross-record consistency, especially explicit retirement/deletion and effect-state transitions, must define their transactional boundary rather than rely on eventual coincidence.

## 13. Failure and owner attention are durable facts

Blocked, failed, uncertain, waiting-for-owner, and authentication-required states must not disappear because a provider turn ended or a transcript rolled over.

Runtime records these states and makes them observable through Control and, where owner action is useful, through a small owner-attention projection on the main Atlas surface.

Runtime reports failures in the vocabulary of the boundary that produced them and does not hide precise technical truth behind generic semantic states.
## 14. Capability families are descriptive, not a second tool protocol

Capability families such as Mail Read, Mail Send, Drive Read, or Filesystem are useful for Control, enablement, discovery, and progressive disclosure.

They do not replace the executable identity of the underlying provider/MCP/local operation. Runtime may improve human/model descriptions and grouping, but must preserve the real schema, transport, provenance, and effect identity beneath the group.

The model may choose among the enabled underlying operations without the owner having to understand their plumbing.

## 15. Provider state is never canonical runtime state

Provider conversation state, background jobs, hosted tool sessions, caches, or model-specific continuation features may be used as accelerators.

Atlas must retain enough of its own transcript, action, workspace, artifact, schedule, registry, and memory state to recover or reseat inference without treating provider state as canonical truth.

Changing model/provider must not silently change runtime authority or erase in-flight execution truth.

## 16. Minimum execution spine before consequential tools

Heliocentric implementation begins with the model and direct multimodal interaction, but consequential external actions must not be enabled until the runtime has the minimum spine needed to govern them.

That spine includes identity, transcript/artifact identity, Environment Registry and enablement state, hard secret/configuration isolation, durable run/action identity, exact effect evidence/recovery, and observable failure/owner-attention state.

The spine grows in parallel with capabilities. It is not a workflow engine and does not decide what the model should do.
## 17. What this constitution does not define

This document deliberately does not freeze database table shapes, retention periods, chunk sizes, lock implementations, sandbox technology, queue technology, provider SDK details, or UI geometry.

Those are implementation choices so long as they preserve these invariants.

It also does not restore V4's obligation planner, mandatory Work objects, confirmation state, or runtime-authored semantic workflows.

## 18. Review rule

This constitution and `15-pre-implementation-baseline.md` were accepted by the owner on 2026-09-05.

During implementation, any proposed shortcut that weakens one of these guarantees must be surfaced as an explicit design change rather than introduced silently in code.

## Implemented conversation and recovery contracts

Canonical turns receive a transactionally allocated per-transcript sequence. PostgreSQL may retain multiple owner-chat transcripts while exactly one is marked as the currently selected owner chat. Foreground inference is fenced per transcript: a second foreground request for the same chat returns HTTP 409 before appending an owner turn, while different chat transcripts retain separate task and evidence state. A waiting action does not keep a finished inference slot occupied. Memory mutations take their provenance from the inference run's transcript rather than whichever chat the UI happens to select later.

Run outcome aggregates all action outcomes independently of inference completion. Conditional action transitions prevent cancellation or a late result from rewriting executing/uncertain truth. A successful sibling cannot clear uncertainty. Complete argument-schema validation precedes proposal and dispatch. An executor exception, including `ValueError`, cannot by itself prove that no effect occurred.

Schedule advancement and durable queued occurrence creation commit atomically. Each occurrence keeps immutable owner intent and a unique schedule/time identity. Queued work resumes after restart; interrupted dispatched work is never automatically replayed. Heartbeats fence abandoned inference runs, preserve task/effect evidence, and release their foreground slot with owner attention. Synchronous integrations run outside the main event loop.

Canonical resource bytes live in immutable local artifacts, with metadata and provenance in PostgreSQL. Owner attachments, public provider web/citation events, and accepted/rejected model task deltas retain their source relationship. Bounded exact evidence reads preserve original markup and whitespace. Public provider events do not include hidden provider search passages or internal reasoning that the provider never returns.


Owner enablement is durable database state, separate from discovered availability. Startup refreshes capability metadata without resetting that decision. Discovery, normal dispatch, prepared-action execution and provider-native web tools obey current enablement; disabled schedules retain queued occurrences without starting new inference. Already dispatched effects may finish. Owner authentication, recovery controls and canonical evidence persistence remain runtime responsibilities.

The approval surface displays the complete canonical proposal, and its approval decision carries the hash of the reviewed version. Missing, invalid, expired or changed proposals cannot be approved. Shared owner project changes are staged outside live project files for owner integration; advisory project locks are not a concurrency guarantee against arbitrary editors. Production mounts owner Projects read-only.

Every provider tool-loop request is token-counted before dispatch. Earlier tool rounds may be replaced by exact evidence locators; the current task's semantic core and owner request cannot be dropped to fit. If that protected input cannot fit, inference stops with a recoverable interruption instead of sending an oversized request. Canonical history is retained and queried through bounded pages. Offline backup/recovery is defined in `22-backup-and-recovery.md`; runtime maintenance precedes installed-code changes.
