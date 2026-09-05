# Open Design Questions

These are intentionally unresolved. They should be answered during planning rather than hidden inside implementation choices.

## Provider and model routing

- What is the minimum provider-adapter contract?
- Which provider-specific abilities should Atlas expose directly rather than normalize away?
- How should automatic model escalation consider complexity, risk, latency, and cost?
- How does an owner override interact with automatic routing?

## Capability map

- What information must always be present in the compact capability map?
- When should detailed schemas be deferred until the model asks for them?
- How are model-native, provider-hosted, MCP, API, and local-software abilities represented consistently?
- How quickly should availability changes propagate into contextual state?

## Workspace

- What creates, reuses, suspends, and retires a workspace?
- What state belongs in a workspace versus contextual memory?
- How does a provider switch inherit the same working environment without expensive rediscovery?

## Memory

- What deterministic filters run before model-assisted memory classification?
- When is a fact canonical long-term memory versus merely embedded for recall?
- What gets discarded, and when?
- How are owner correction, forgetting, provenance, and supersession represented?

## Authority and automation

- Which effects can rely entirely on OS/service permissions?
- Which effects deserve a thin Atlas policy or owner confirmation layer?
- How are schedules and event triggers represented without recreating V4 Work/Cadence machinery?
- What observability is necessary to diagnose a failed autonomous run without making the owner operate it manually?
