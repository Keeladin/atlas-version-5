# Atlas V5 Environment Registry

## 1. Purpose

The Environment Registry is Atlas's structured map of the environment available to the agent. It answers what exists, where it is, how it can be reached, whether it is enabled, and whether it is currently available.

It is a map, not the territory. The registry must not load files, mail, memories, tool schemas, or service contents into every inference merely because they exist.

## 2. Seat bootstrap

Every inference receives a very small Atlas bootstrap: identity, model-led operating principle, current conversational orientation, and the fact that the Environment Registry can be consulted.

The bootstrap should be stable and cheap enough to provide consistently. Provider conversation state may keep a session warm, but Atlas does not rely on a model secretly remembering bootstrap instructions from an earlier call.

## 3. Registry contents

The registry may describe domains such as model-native capabilities, provider-hosted capabilities, MCP and connected services, local software, workspaces, artifact storage, memory, schedules, and runtime services.

Entries carry compact semantic metadata and references. Detailed schemas, filesystem contents, mail, database rows, or other heavy state are obtained only when inference decides they are relevant.

## 4. Enabled projection

Control may know about configured or installed capabilities that are disabled. The agent-facing registry exposes only capabilities that the owner has enabled for use.

Disabled capabilities are not callable and need not occupy model attention. Enabled but temporarily unhealthy capabilities may remain visible with precise state such as `authentication_required` or `unavailable` so the model can reason about a real failure.
## 5. Capability grouping and tool detail

Raw tool catalogs are mechanisms, not the owner-facing model. Atlas groups them into meaningful capability families with clear descriptions.

The owner may enable capabilities such as Mail read, Mail send, Drive read, or Drive modify without choosing between raw functions such as `search_message`, `get_message`, and `get_thread`.

The model sees the meaningful capability and may load the underlying tool definitions only when required. Atlas may override vague upstream tool descriptions with clearer local metadata while preserving the original schema and transport.

## 6. Workspace-local instructions

The registry tells the model that a workspace exists and how to enter it. Once a software workspace is relevant, Atlas may discover and provide applicable `AGENTS.md` instructions from the workspace hierarchy.

`AGENTS.md` describes how to work in that local filesystem scope; it is not a replacement for the Environment Registry, global identity, memory, or live availability state.

## 7. Runtime ownership

Atlas updates the registry from deterministic reality: configured providers, MCP connections, credentials, installed software, filesystem/workspace registration, runtime health, and owner enablement.

The model does not maintain the registry. It consults the registry and decides what capabilities or resources are useful.

**Give the model a map and keys, not the contents of every room.**