# ADR 0002 — OpenAI is the primary inference provider

Status: Accepted
Date: 2026-09-05

## Decision

Atlas V5 uses OpenAI as its primary and reference inference provider for the first implementation.

The first provider adapter and heliocentric build are designed around OpenAI's current capabilities while preserving an Atlas-owned seat that another compatible provider can occupy.

## Constraint

Atlas must not become an OpenAI application.

Atlas owns identity, transcript continuity, Environment Registry, workspace state, artifacts, memory, schedules, authority, credentials, capability enablement, and user-facing product semantics.

Provider-specific conversation state, tools, modalities, and reasoning controls are accelerators, not canonical Atlas state.

## Compatibility target

Anthropic is the first compatibility provider and architectural cross-check.

If it cannot occupy the same Atlas-owned seat without redesigning memory, workspace, schedules, authority, artifacts, or tool ownership, the provider boundary is too coupled.

Gemini and xAI remain valid later providers or specialists where their capabilities are useful.

Specific model names and provider feature lists are time-sensitive and do not become product semantics.