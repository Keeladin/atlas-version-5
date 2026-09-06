# Atlas Version 5

Atlas V5 is a clean architectural restart of Atlas.

The architecture and implementation-planning gates have passed. `docs/15-pre-implementation-baseline.md`, `docs/17-runtime-constitution.md`, and `docs/18-implementation-plan.md` were accepted on 2026-09-05. Phase 0 is now deployed and verified in production at `atlas-agentic.co.za`; Phase 1 will build the working OpenAI-backed Atlas centre on that foundation.

## Product direction

Atlas is a persistent local agent environment in which the selected model is the primary semantic decision-maker.

The model interprets intent, chooses workflows and tools, adapts to results, and decides when work is complete. The runtime provides deterministic orchestration, execution, persistence, scheduling, authority enforcement, environment state, and observability without becoming a competing planner.

Normal use centres on one multimodal Atlas/chat page. A separate Control page exists for configuration, health, capability enablement, memory/schedule inspection, usage, and diagnostics.

## Core shorthand

- The model is the cognitive centre.
- Runtime governs execution and reality, not thought, and is bound by a separate Runtime Constitution.
- The Environment Registry is a map consulted on demand.
- The transcript records experience; the memory processor interprets it.
- Workspaces preserve the current field of action.
- Enabled capabilities are usable; disabled capabilities are absent from the agent view.
- Schedules bind intent to triggers rather than encode workflows.
- Existing provider abilities, MCPs, services, and local software are preferred over Atlas reimplementation.
- Implementation grows heliocentrically from the working model and interface.

The accepted design authorities are `docs/15-pre-implementation-baseline.md`, `docs/17-runtime-constitution.md`, and `docs/18-implementation-plan.md`. Consolidation history and runtime-guardrail review are recorded in `docs/16-architecture-review-notes.md`.

Status: **Phase 0 deployed and cut over; Phase 1 working Atlas centre next**.