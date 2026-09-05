# Atlas V5 Architecture

## 1. Purpose

Atlas V5 is a persistent local agent environment. The model is the primary reasoning and decision-making component. Atlas exists to place that model in a useful local environment with continuity, tools, authority, and observability.

The architecture must not require the model to negotiate Atlas machinery before it can think or act. Runtime structure may support the agent, but must not become the agent.

## 2. Core principle

**The model makes decisions. Atlas provides state, tools, workspace, execution, boundaries, and continuity.**

A runtime loop still exists mechanically: submit context, receive model output or tool calls, execute tools, return results, and continue. That loop must not prescribe the workflow.

When the owner asks Atlas to achieve an objective, the model should work out the best route using what it already knows about its abilities and environment.

## 3. Situated inference

At the start of inference the model should already have a compact operating picture containing:

- relevant recent conversation and current objective;
- current contextual memory;
- native abilities of the selected model;
- available tools and useful tool families;
- active workspace and important resources;
- effective authority and important restrictions;
- relevant recent tool results and unresolved state.

This is the model's awareness of its current body and surroundings, not a runtime planning graph.
## 4. Capability map

Atlas maintains a live capability map outside the reasoning path. It should distinguish capability from current availability and from authority.

A capability may come from:

- the selected model itself, such as vision or document understanding;
- a provider-native hosted tool;
- a local or remote MCP server;
- an ordinary API;
- installed local software or a CLI;
- a small Atlas-specific function only where no better boundary exists.

The model should not begin each task by asking the runtime what Atlas can do. The useful capability map is supplied as context. Detailed schemas may be loaded on demand when a broad tool family becomes relevant.

Execution remains reality. A capability marked available can still fail because a token expired, a service stopped, a file vanished, or an OS permission changed. Runtime reports that fact; the model decides what to do about it.

## 5. Tool priority

Before implementing an Atlas capability, prefer:

1. native model capability;
2. native provider tool;
3. MCP or existing external application;
4. local CLI/API/software;
5. Atlas-native implementation only when genuinely necessary.

The test is not whether Atlas can implement something. The test is whether Atlas needs to own it.

OCR illustrates the boundary: a vision-capable model can understand a scanned page directly. Dedicated OCR remains useful for deterministic bulk extraction or indexing, but that is software infrastructure rather than agent reasoning.
## 6. Workspaces

A workspace is Atlas's current working environment, not primarily a user-operated feature.

A software workspace may contain a repository, branch, filesystem paths, shell, Git, language runtimes, tests, build state, recent diffs, and current objective. A document workspace may contain source documents, rendered views, local or Drive files, office software, exports, and notes. Research and operations workspaces expose the corresponding environment and tools.

Workspaces should retain enough local state that the model does not rediscover the same environment on every inference. The owner may inspect a workspace when diagnosing a problem, but ordinary use should not require managing it.

**Work says what must be achieved only when durable objective state is actually useful. Workspace is where the model achieves it.** V5 must not require every conversational request to become a formal Work object.

## 7. Authority

Authority is enforced primarily where an effect occurs.

Local authority can often be expressed by the Atlas OS identity, filesystem permissions, executable access, sockets, and service permissions. External systems retain their own OAuth, tokens, scopes, and account controls. Atlas may add a thin owner policy layer for consequential actions that need an explicit product-level rule.

The model should know the important effective boundaries, but it does not grant itself authority. It proposes actions; execution either succeeds within the boundary or returns a concrete denial/failure.

Judgment belongs to inference. Exact enforcement belongs to software.

## 8. Passive faculties

Workspace, memory, schedules, continuity, and most execution bookkeeping are Atlas faculties. They operate mostly behind the scenes.

They must be observable and diagnosable without becoming mandatory user workflows. The owner should be able to inspect what model ran, what workspace was active, what tools were used, what memory was loaded, what permission blocked an action, and why a task stopped.
## 9. Interface

The normal owner experience should remain small. Candidate primary surfaces are Home, Chat, Files/Documents where useful, and Control.

Home surfaces what matters and what needs attention. Chat is the natural interaction surface. Files/Documents provides direct access when browsing or opening material is useful. Control is primarily observability and configuration rather than routine operation.

Control may expose providers, models, routing, reasoning effort, credentials, MCP servers, permissions, schedules, memory inspection, usage/cost, active workspaces, and diagnostic history.

Internal nouns should not become navigation merely because they exist in the implementation.

## 10. Second cousins

Useful deterministic software related to Atlas is not automatically part of Atlas.

A file normalizer, duplicate detector, bulk OCR pipeline, media converter, backup utility, or other single-purpose program may exist completely independently. Atlas may know that it exists and may use it through CLI, API, or MCP if appropriate, but that does not make it an Atlas subsystem.

**Atlas should know its environment, not absorb its environment.**

## 11. Architectural test

For every proposed subsystem ask:

- Does this require understanding or judgment? Prefer the model.
- Does this require exact mechanics or enforcement? Prefer deterministic software.
- Does capable software already exist? Use it rather than recreate it.
- Can this run usefully without Atlas? It may be a second cousin.
- Does the owner have to understand this subsystem to request ordinary work? If yes, the abstraction is leaking.

V5 succeeds when Atlas feels broadly capable because the model can reason over a rich environment, not because Atlas contains a handcrafted workflow for every possible task.
