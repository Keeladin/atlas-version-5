# Atlas V5 Design Roadmap

This roadmap is for architecture and planning only. Implementation begins only after the design is accepted.

## Phase A — Freeze the product model

Define the owner experience, internal faculties, architecture boundaries, and non-goals in plain language. Resolve terminology before designing schemas or services.

Acceptance questions:

- Is the model clearly the primary decision-maker?
- Can ordinary conversation remain ordinary conversation?
- Are workspace, memory, and schedules mostly passive Atlas faculties?
- Is Control for configuration/inspection rather than routine operation?
- Are adjacent deterministic utilities allowed to remain independent second cousins?

## Phase B — Complete the capability landscape

Expand the provider survey into a maintained matrix covering model families, modalities, reasoning controls, context/state features, native tools, MCP, function calling, structured output, streaming, caching, usage/cost interfaces, and provider-specific constraints.

Separately inventory practical MCP/local software candidates for filesystem, shell, Git/GitHub, Google Workspace, browser/computer interaction, office documents, databases, Docker/services, and other environments Atlas is likely to encounter.

The result should identify what Atlas can reuse before any Atlas-native feature is proposed.

## Phase C — Design the situated context contract

Specify the compact context every provider adapter receives: conversation, objective, contextual memory, workspace summary, capability map, authority summary, and recent results.

The contract must be provider-neutral at the Atlas level while allowing provider-specific fields where they improve performance.
## Phase D — Design passive faculties

Specify workspace lifecycle, memory flow, schedule execution, credential custody, diagnostics, and persistence without turning those mechanisms into the model's workflow.

Memory design must include contextual promotion, asynchronous write-back, provenance, merge/supersession, explicit owner corrections, and discard behavior.

Workspace design must explain how Atlas establishes and preserves the model's working environment without requiring the owner to manage a workspace manually.

Schedule design must distinguish time triggers, event triggers, and one-off deferred work while keeping scheduling mechanics outside inference.

## Phase E — Design authority at the effect boundary

Map local OS authority, external OAuth/token scopes, and any thin Atlas owner-policy layer. Avoid duplicating restrictions already correctly expressed by the operating system or external service.

Specify which effects require explicit owner confirmation, which can execute under standing authority, and how denials/failures are returned to the model for further reasoning.

## Phase F — Define the first vertical-slice acceptance tests

Before implementation, write scenario tests that exercise general agency rather than bespoke workflows. Candidate tests:

- find the latest CV in Drive, open/render it, and discuss it;
- revise a document and save a new copy;
- inspect Atlas's own repository, change a UI section, run the build, and verify the diff;
- summarize important mail every morning using a durable schedule;
- ingest a directory of already-normalized reference documents into long-term memory;
- switch provider/model during an ongoing task without losing the objective or workspace.

If a scenario requires a new hard-coded Atlas workflow, pause and revisit the architecture before implementing it.

## Phase G — Implementation gate

Only after Phases A–F are accepted should V5 receive code. The first implementation should be the smallest vertical slice that proves model-led reasoning, local tool use, contextual continuity, and effect-boundary authority together.
