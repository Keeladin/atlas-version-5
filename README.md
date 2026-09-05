# Atlas Version 5

Atlas V5 is a clean architectural restart of Atlas.

The repository remains documentation-only until the pre-implementation architecture baseline is reviewed and accepted. No runtime, UI, database, provider adapter, tool host, or migration code should be added before that gate.

## Product direction

Atlas is a persistent local agent environment in which the selected model is the primary semantic decision-maker.

The model interprets intent, chooses workflows and tools, adapts to results, and decides when work is complete. The runtime provides deterministic orchestration, execution, persistence, scheduling, authority enforcement, environment state, and observability without becoming a competing planner.

Normal use centres on one multimodal Atlas/chat page. A separate Control page exists for configuration, health, capability enablement, memory/schedule inspection, usage, and diagnostics.

## Core shorthand

- The model is the cognitive centre.
- Runtime governs execution and reality, not thought.
- The Environment Registry is a map consulted on demand.
- The transcript records experience; the memory processor interprets it.
- Workspaces preserve the current field of action.
- Enabled capabilities are usable; disabled capabilities are absent from the agent view.
- Schedules bind intent to triggers rather than encode workflows.
- Existing provider abilities, MCPs, services, and local software are preferred over Atlas reimplementation.
- Implementation grows heliocentrically from the working model and interface.

Review `docs/15-pre-implementation-baseline.md` first. The consolidation contradictions and their resolutions are recorded in `docs/16-architecture-review-notes.md`.

Status: **architecture review candidate; no implementation yet**.