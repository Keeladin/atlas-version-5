# ADR 0002 — OpenAI is the primary inference provider

Status: Accepted
Date: 2026-09-05

## Decision

Atlas V5 will use OpenAI as its primary and reference inference provider.

OpenAI is the default provider against which the first Atlas V5 provider adapter, model routing strategy, situated-context contract, tool exposure model, and acceptance scenarios will be designed.

## Constraint

Atlas must not become an OpenAI application.

Atlas owns identity, conversation state, contextual memory, embedded memory, workspace state, schedules, authority, credentials, tool inventory, continuity, and user-facing product semantics.

Provider-specific features may enrich the active model seat, but they must not become canonical Atlas state.

A compatible provider must be able to occupy the same Atlas seat without redesigning memory, workspace, schedules, authority, or tool ownership.

## Compatibility target

Anthropic is the first compatibility provider and architectural cross-check.

If Claude cannot occupy the same contextual seat without architectural surgery, the provider boundary is considered too coupled.

Gemini and xAI remain valid optional providers and may be selected for specialist workloads where their capabilities are advantageous.