# Atlas Version 5

Atlas V5 is a clean architectural restart of Atlas.

The architecture and implementation-planning gates have passed. `docs/15-pre-implementation-baseline.md`, `docs/17-runtime-constitution.md`, and `docs/18-implementation-plan.md` were accepted on 2026-09-05. Phase 0 is deployed and verified in production at `atlas-agentic.co.za`, and the Phase 1 model-led centre is now operational with durable conversation, progressive capability discovery, local/Drive/GitHub/Gmail access, provider-native web search, native resource perception, and owner-gated consequential actions.

## Product direction

Atlas is a persistent local agent environment in which the selected model is the primary semantic decision-maker.

The model interprets intent, chooses workflows and tools, adapts to results, and decides when work is complete. The runtime provides deterministic orchestration, execution, persistence, scheduling, authority enforcement, environment state, and observability without becoming a competing planner.

Normal use centres on one multimodal Atlas/chat page. A separate Control page exists for configuration, health, capability enablement, memory/schedule inspection, usage, and diagnostics.


## Current implementation snapshot — 2026-09-06

- OpenAI `gpt-5.6-sol` is the primary provider through the Responses API; Atlas owns the canonical transcript and sends `store=False`.
- Capability exposure is progressive: the model sees a compact family index and stable `atlas_capability_search` / `atlas_capability_call` controls instead of the full tool catalog.
- Local workspace browsing/upload and typed resource acquisition are implemented; acquired images/files are routed into provider-native perception.
- Google Workspace is authenticated for Drive and Gmail. Gmail search/read are automatic reads; email sending is a prepared action requiring owner approval.
- GitHub uses the official GitHub MCP server over stdio, with dynamic tool discovery and a read-only server boundary for the first integration stage.
- Provider-native web search is enabled so Atlas can independently obtain current public information when a task needs it.
- The top rail exposes discreet runtime, PostgreSQL, model and provider-context telemetry; transcript pressure uses OpenAI's input-token counter against the configured 1M context budget.
- The right activity rail presents `Needs You` approval widgets and recent capability activity.

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

Status: **Phase 0 complete; Phase 1 working Atlas centre operational and expanding**.
