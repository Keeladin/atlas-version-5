# ADR 0001 — Model-led architecture

Status: Accepted direction, design details pending

Date: 2026-09-05

## Decision

Atlas V5 treats the selected model as the primary agent and decision-maker.

The model receives a situated operating context containing the relevant objective, contextual memory, workspace, capabilities, authority, and recent results. It decides what workflow to follow and which available abilities to use.

The Atlas runtime executes requested effects, maintains state and passive faculties, enforces hard boundaries, and reports concrete results. It must not replace model judgment with a handcrafted planning loop.

## Consequences

Provider adapters must expose model capabilities rather than reduce every provider to a lowest-common-denominator text endpoint.

Tool availability should be maintained before inference and described to the model compactly. Detailed schemas may be discovered or deferred when required.

MCP and existing software are preferred integration boundaries. Provider-native tools may be used when useful without becoming required Atlas semantics.

Workspaces, memory, schedules, and most continuity mechanisms are internal faculties. They should be inspectable but should not become mandatory owner workflows.

V4's runtime-driven obligation/planning architecture is not the migration target for V5.
