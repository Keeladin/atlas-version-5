# Atlas V5 Workspace Model

## Purpose

A workspace is Atlas's maintained field of action around current work. It situates the model's capabilities in the files, artifacts, resources, instructions, and operational state that are relevant now.

It is primarily an internal working construct. The owner should not have to create or manage a workspace before asking Atlas to do ordinary work.

## Three overlapping concerns

A workspace can combine:

1. **Storage references** — files, documents, artifacts, generated outputs, scratch material, and external resource identities.
2. **Operational state** — working directory, repository/branch, recent results, open resources, research state, execution context, and other situational facts worth preserving.
3. **UI projections** — temporary views such as a mail list, file browser, document preview, research sources, generated artifact, or repo/file view.

These concerns may use different physical stores. "Workspace" is the conceptual boundary around what is currently relevant to doing the work.

## No rigid workspace classes

Software, documents, research, operations, and ad hoc work are useful descriptions, not hard-coded workflow classes.

One piece of work may move naturally between research, documents, code, mail, and local software. The model decides what resources it needs; the runtime preserves the state that emerges without routing through workspace-specific planners.
## Resource ownership

Workspaces point to resources rather than duplicating them unnecessarily. A Gmail message can remain authoritative in Gmail; a repository remains on disk/Git; a generated PDF lives in artifact storage; PostgreSQL stores metadata and relationships where useful.

Large artifacts should not be copied into contextual memory just because they belong to the workspace. Context assembly selects only what the current inference needs.

## Local instructions

For software workspaces, Atlas should prefer the existing `AGENTS.md` convention for repository/directory-scoped working instructions rather than invent an Atlas-specific equivalent.

Atlas resolves the relevant workspace/path first, then supplies only applicable instruction files from parent to child scope. `AGENTS.md` is local working guidance, not global identity, memory, authority, or live registry state.

## Continuity

Workspace state belongs to Atlas, not to the model provider. A model/provider change should inherit the same relevant working environment without requiring expensive rediscovery.

The workspace can remain warm while useful and be retired when its situational state no longer matters. Retirement does not delete durable artifacts or memory.

## Owner visibility

The main Atlas page may expose useful workspace projections contextually. Control may provide deeper inspection for diagnosis, but workspace internals should not become mandatory navigation.

If the owner must manually build a workspace before Atlas can work, or if the model repeatedly rediscovers state Atlas already knows, the workspace abstraction is failing.