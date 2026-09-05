# Atlas Version 5

Atlas V5 is a clean architectural restart of Atlas.

The architecture gate has passed. `docs/15-pre-implementation-baseline.md` and `docs/17-runtime-constitution.md` were accepted on 2026-09-05. Implementation planning may now begin; runtime code should follow the accepted baseline and constitution rather than rediscover architecture in code.

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

The accepted architecture is defined by `docs/15-pre-implementation-baseline.md` together with `docs/17-runtime-constitution.md`. The consolidation history and runtime-guardrail review are recorded in `docs/16-architecture-review-notes.md`.

Status: **architecture accepted; implementation planning next**.