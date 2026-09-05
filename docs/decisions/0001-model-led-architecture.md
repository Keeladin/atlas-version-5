# ADR 0001 — Model-led architecture

Status: Accepted
Date: 2026-09-05

## Decision

Atlas V5 treats the selected model as the primary agent and semantic decision-maker.

The model owns interpretation, relevance, workflow, tool choice, adaptation, clarification, sufficiency, and completion.

Atlas runtime owns deterministic orchestration, provider/tool execution, persistence, trigger mechanics, exact validation, authority enforcement, environment health, and faithful result reporting. It must not replace model judgment with a handcrafted planning loop.

Context is deliberately small. Each inference receives an Atlas seat bootstrap plus relevant current transcript/context. The Environment Registry, memory, workspace state, and detailed tool schemas are consulted or loaded on demand rather than injected wholesale.

## Consequences

Provider adapters expose real provider/model capabilities rather than reduce every provider to a lowest-common-denominator text endpoint.

Capabilities are enabled/provisioned by the owner and grouped semantically. Detailed raw tools may be discovered progressively.

Workspaces, memory, schedules, artifacts, and continuity are supporting Atlas faculties. They are inspectable without becoming mandatory owner workflows or runtime planners.

V4's obligation-led/runtime-planned execution architecture is not the migration target for V5.