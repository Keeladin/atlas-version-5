# Atlas V5 Architecture Review Notes

Date: 2026-09-05
Status: consolidation and runtime-guardrail review complete; baseline and constitution accepted by owner

This document records contradictions and drift found while consolidating the first V5 design pass into `15-pre-implementation-baseline.md`.

## Resolved contradictions

### Fat situated context vs on-demand environment

Earlier documents described conversation, contextual memory, workspace state, capability map, authority, and recent results as a bundle supplied to the model before inference.

Final direction: only a small seat bootstrap plus relevant live transcript/context is routinely supplied. The Environment Registry, workspace state, memory, and detailed tools are consulted or loaded on demand.

### Capability map vs Environment Registry

Earlier documents treated a capability map as a prompt-resident inventory.

Final direction: capabilities are one enabled projection of the broader Environment Registry. The registry remains outside prompt context and progressively exposes detail.

### Runtime policy/confirmation vs provisioning

Earlier authority text retained a thin policy/confirmation layer for consequential effects.

Final direction: V5 has no general `CONFIRM` state. The owner enables or disables meaningful capabilities; runtime enforces real OS, credential, scope, database, and service boundaries.
### Memory outbox vs transcript-as-scribe

Earlier memory text described active information leaving context through a generic memory outbox.

Final direction: the live transcript is the model's natural write surface and faithful temporary journal. Closed/aging transcripts are interpreted asynchronously by a separate memory processor, which is the normal writer to durable memory.

### Generic durable storage vs selected long-term substrate

Earlier documents intentionally left the durable memory store unspecified.

Final direction: PostgreSQL is the intended canonical memory substrate from the first implementation, with pgvector plus PostgreSQL lexical/full-text and metadata retrieval. Large artifacts remain outside the relational database.

### Objective-centric workspace vs field of action

Earlier workspace documents centred the workspace around a formal or durable objective.

Final direction: workspace is the maintained field of action around whatever work is occurring. It combines storage references, operational state, external resources, and contextual UI projections without requiring a formal task object.

### Multi-page product candidates vs two-surface product

Earlier architecture proposed Home, Chat, Files/Documents, and Control as candidate top-level surfaces.

Final direction: one primary Atlas page centred on multimodal chat, with contextual workspace projections around it, plus a separate Control page.

### Text-first chat vs multimodal core

Earlier documents did not make multimodal conversation a foundational message/storage requirement.

Final direction: messages are content blocks; images, documents, audio, tool results, and generated artifacts can participate naturally. Binary artifacts are stored separately and referenced from transcripts.
### Vertical-slice implementation vs heliocentric growth

Earlier roadmap text proposed acceptance scenarios and the smallest vertical slice before implementation.

Final direction: implementation grows heliocentrically. Establish the working model and direct interface first, then support the provider's immediate/native capabilities, then expand through MCP, connected services, and local software while runtime evolves in parallel.

Tests validate capabilities and architecture after they exist; they do not invent the architecture or permanent product feature list.

### Provider recommendation vs accepted anchor

Earlier provider-selection text still described OpenAI as a recommendation pending scenario validation.

Final direction: OpenAI is accepted as the primary/reference provider. Anthropic is the first compatibility check. Provider choice remains replaceable and does not own Atlas continuity or semantics.

## Post-consolidation red-team review

Independent architecture reviews after commit `1425447` did not overturn the model-led direction, but they exposed runtime contracts that the baseline had left implicit: hard credential/configuration isolation, crash-safe consequential effects, schedule/run concurrency, untrusted-content boundaries, explicit memory-command completion, persistence topology, and durable owner-attention state.

Those findings are treated as advisory gap detection rather than implementation prescriptions. V5's response is the dedicated `17-runtime-constitution.md`, which states the required execution guarantees without restoring a runtime planner, mandatory Work objects, or a general confirmation state.

The supporting architecture documents were reconciled to that constitution. The owner reviewed and accepted the revised baseline and Runtime Constitution together on 2026-09-05.

Remaining questions in `06-open-design-questions.md` are implementation choices unless implementation exposes a genuine architectural conflict that warrants an explicit amendment.