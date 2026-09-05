# Atlas V5 Capability Map

## Purpose

The capability map is the model's compact awareness of what it can currently do. It is maintained by Atlas outside the reasoning path and supplied as part of situated context.

It is not a workflow registry and it does not tell the model what step comes next.

## Capability classes

A useful first classification is:

- **model-native** — reasoning, vision, document understanding, audio, structured output, or other abilities intrinsic to the selected model;
- **provider-hosted** — web search, code execution, file search, computer use, image generation, and similar server-side provider tools;
- **Atlas-local/MCP** — local filesystem, shell, Git, services, or other tools exposed through an Atlas-owned MCP/client boundary;
- **external-service** — Gmail, Drive, Calendar, GitHub, databases, SaaS APIs, and similar connected systems;
- **local-software** — LibreOffice, Python, Docker, Git, ffmpeg, a normalizer, or another installed program Atlas can use through a suitable interface.

These classes describe origin and execution boundary, not importance.

## What the model needs to know

The compact map should emphasize semantic usefulness rather than transport detail. For each capability the model may need:

- what it can accomplish;
- whether it is currently available;
- important scope or resource boundaries;
- whether use causes side effects;
- whether owner confirmation may be required;
- whether more detailed tool definitions can be loaded on demand.

Atlas retains transport, credentials, protocol details, exact schemas, health checks, and provider metadata unless the model actually needs them.
## Deferred detail

Large tool catalogs should not be dumped into every prompt. Atlas can supply a stable high-level map and let the model request detailed definitions for a relevant tool family when needed.

Provider-native tool-search/deferred-loading features may accelerate this, but Atlas must keep the same conceptual behavior across providers that lack those features.

## Availability

Capability awareness should be refreshed independently of user requests where practical. MCP connect/disconnect events, credential health, local service state, installed software, and provider/model changes can update the map in the background.

Availability is advisory context, not a guarantee. The execution boundary remains the final source of truth.

## Model switching

Switching models changes model-native and provider-hosted abilities. Atlas-local, external-service, and local-software capabilities generally remain attached to the Atlas environment.

The capability map is therefore rebuilt or amended when the selected model changes, without redefining the task or workspace.

## Tool choice principle

The model should choose an ability based on the objective, evidence needed, cost/latency, and quality. Atlas may provide metadata that helps that choice, but should not replace it with a hard-coded workflow router.

If two tools provide equivalent outcomes, implementation can later support preferences or ranking. Those preferences should be operating policy, not task semantics.

## Environment awareness

Atlas may know about independent second-cousin utilities even when they are not always connected as callable tools. That knowledge can still support useful suggestions.

Example: Atlas may recognize a directory of raw mixed documents and know that a separate normalizer exists. It can suggest using the normalizer and, if an interface is available and authority permits, offer to invoke or feed it. The normalizer remains a separate product regardless.
