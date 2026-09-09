# Atlas V5 Architecture

## 1. Purpose

Atlas V5 is a persistent local agent environment. The selected model is the primary reasoning and semantic decision-making component. Atlas places that model in a useful local environment with continuity, capabilities, artifacts, memory, authority, schedules, and observability.

The architecture must support the model without turning runtime machinery into a second agent.

## 2. Model/runtime boundary

**The model decides what work means and how to proceed. The runtime executes, persists, enforces, and reports reality.**

The model owns interpretation, relevance, workflow, tool choice, adaptation, clarification, sufficiency, and completion.

The runtime owns deterministic context assembly, provider calls, tool execution, persistence, schedule triggering, exact checks, permission enforcement, health state, and faithful result reporting.

A mechanical loop exists: prepare context, call model, execute requested actions, return results, continue as needed. The loop does not predeclare the workflow. Runtime conduct is constrained by `17-runtime-constitution.md`, which defines hard isolation, effect truth/recovery, trust, concurrency, and persistence guarantees without adding semantic planning.

## 3. Seat bootstrap and Environment Registry

Each inference receives a small, stable Atlas bootstrap plus the relevant current transcript/context. Atlas does not dump the owner's whole environment into every prompt.

The Environment Registry is maintained outside the prompt as the structured map of enabled capabilities, services, local software, workspaces, memory, schedules, artifact storage, and runtime state.

The model consults that map on demand. Detailed schemas, files, mail, database rows, and other heavy state are loaded only when useful.
## 4. Capabilities and authority

Capability descriptions are semantic and grouped. The owner enables meaningful abilities; the model chooses the exact underlying tools and sequence. Large raw catalogs use progressive disclosure rather than occupying context wholesale.

Disabled capabilities are absent from the agent-visible environment. Enabled capabilities remain subject to hard runtime containment, real OS permissions, credential scopes, database roles, service policy, and runtime availability. Provisioned, enabled, available, and usable-at-this-boundary are separate facts.

V5 has no general confirmation state. Owner enablement is the discretionary capability gate; secrets and protected runtime state remain outside ordinary model-facing tools regardless of enablement.

## 5. Workspaces and artifacts

A workspace is Atlas's maintained field of action around current work. It may combine filesystem or artifact references, external resources, repository/document state, recent results, scratch material, and UI projections.

Workspace labels such as software, documents, research, or operations are descriptive rather than rigid classes or workflow engines.

Artifacts are first-class. Binary content lives outside transcripts and normally outside relational rows; transcripts and workspace state refer to artifacts through identities, metadata, provenance, and storage locations.

Software workspaces may use applicable `AGENTS.md` files as local working instructions once the relevant workspace/path is known.

## 6. Memory and continuity

Atlas owns the canonical temporary transcript. Provider conversation state is an optimization, not the source of continuity.

The transcript records experience without interpreting it. Closed transcripts produce context capsules and enter indexed short-term memory. A separate asynchronous memory processor decides what is discarded, retained, promoted, merged, or superseded. Explicit remember/correct/retire/restore/delete instructions have durable command state and precedence over stale derived memory.
Durable memory uses PostgreSQL as the canonical substrate, with pgvector for semantic retrieval and PostgreSQL lexical/full-text plus metadata search for exact retrieval. Large artifacts remain in file/object storage with database metadata.

The active model is not the normal writer to durable memory. Its natural write surface is the transcript; the memory processor performs durable memory mutation.

Owner chats are independent interaction streams, not separate owners of Atlas state. Durable memory, projects, schedules and other shared mutable resources live outside chat transcripts. Conversations and background workers may propose changes; runtime owns canonical provenance, idempotency, version fencing and the atomic commit. The minimal shared-state mutation boundary is defined in `23-shared-state-write-contract.md`.

## 7. Schedules

Schedules are persisted intents bound to deterministic triggers or execution windows. Runtime wakes Atlas when due; inference decides what the stored intent means and how to achieve it using the capabilities available at execution time.

## 8. Interface and Control

The normal product has two principal surfaces: one Atlas page centred on multimodal chat, and one Control page for configuration and engineering inspection.

Workspace projections such as mail lists, file browsing, document previews, research sources, generated artifacts, and compact health/status information may appear around chat when useful.

Control may expose providers/models, capability enablement, connections/credentials, Environment Registry state, memory, schedules, storage/workspaces, runtime health, usage/cost, and diagnostics.

## 9. Implementation posture

Implementation is heliocentric. Begin with a working model and direct multimodal interaction together with the minimum runtime execution spine needed for identity, transcript/artifact continuity, secret isolation, capability enablement, durable effect truth/recovery, and observable failure state. Then support the selected provider's immediate/native capabilities and expand outward through MCP, connected services, and local software while the runtime evolves in parallel.

No hard-coded workflow should be introduced merely to make one example task pass. Capability-derived validation follows architecture; it does not define it.

For the consolidated review baseline, see `15-pre-implementation-baseline.md`. For runtime invariants, see `17-runtime-constitution.md`.