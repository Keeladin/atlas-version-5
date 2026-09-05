# Atlas V5 Workspace Model

## Purpose

A workspace is the environment Atlas places around an objective so the model can work without repeatedly rediscovering the same local state.

It is primarily an internal faculty. The owner may inspect it when useful, but normal interaction should not require creating, naming, or managing workspaces manually.

## Workspace contents

A workspace may hold references to:

- current objective and relevant conversation;
- filesystem roots and working directories;
- repository, branch, diff, build, or test state;
- documents, rendered views, exports, and scratch material;
- available tools or environment-specific capabilities;
- recent tool results and failures;
- external resource identifiers needed to continue;
- temporary working notes that should survive provider/model switching.

The workspace should point to durable or external resources rather than duplicate them unnecessarily.

## Workspace types are descriptive, not rigid classes

Software, Documents, Research, Operations, and similar labels are useful ways to understand common environments. V5 should avoid hard-coding a separate workflow engine for each type.

A single objective may move across environments. For example, research may produce a document, or a software task may need browser research and then a Git repository. The model should be able to combine capabilities without crossing artificial product silos.

## Lifecycle

Atlas may establish a workspace when the current objective benefits from persistent working state. Lightweight conversation may need no explicit workspace beyond conversational context.
A workspace can remain warm while a task is active, suspend when idle, and be retired when its working state no longer matters. Retirement does not delete durable outputs or owner memory.

## Provider switching

The workspace belongs to Atlas, not to the model provider. If Atlas changes provider or model, the next model receives the relevant workspace summary and continues in the same environment.

Provider-native conversation state may be used as an optimization, but it must never be the only place where task continuity exists.

## Context relationship

Contextual memory and workspace overlap but are not identical.

Contextual memory is the compact information the model needs in its head now. Workspace is the broader working environment and state from which that context can be assembled.

A large Git diff, document collection, or directory tree may belong to the workspace while only a summary and the immediately relevant files are loaded into contextual memory.

## Owner visibility

Control may expose active and recent workspaces for diagnosis. Useful inspection includes current objective, model/provider, working paths/resources, recent tools, failures, changes made, and current status.

This visibility is analogous to opening a process monitor or terminal when diagnosing a computer. It is not meant to become the normal way to tell Atlas what to do.

## Design test

If the owner must manually construct the workspace before Atlas can handle a normal request, the abstraction is leaking.

If the model repeatedly rediscovers a repository, document set, or external resource that Atlas already encountered during the same objective, the workspace is not preserving enough useful state.
