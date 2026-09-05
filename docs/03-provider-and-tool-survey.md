# Provider and Tool Survey

Survey date: 2026-09-05. This document records current provider capabilities to inform Atlas V5; specific model names and provider features will change and must not become product semantics.

## 1. Why survey first

Atlas V5 should be designed around what capable modern models can already do. We should not invent an Atlas subsystem and then force the model through it when the provider, MCP ecosystem, or installed software already exposes the needed ability.

The survey therefore asks two questions for each provider:

1. What can the model do natively or through provider-hosted tools?
2. How can Atlas expose its own local tools without surrendering orchestration or continuity to that provider?

## 2. OpenAI

Current Responses API models support custom function calls and a broad hosted tool surface. Current flagship model pages advertise combinations of web search, file search, image generation, code interpreter, hosted shell, apply-patch, computer use, MCP, and deferred tool search. Reasoning effort and long context are exposed on current reasoning models.

OpenAI's Responses API can receive text and image inputs, maintain response/conversation state if desired, run responses in background mode, and expose custom tools alongside hosted tools and MCP.

Useful Atlas conclusion: OpenAI can supply both strong inference and substantial provider-native tooling, but Atlas should use those as optional abilities rather than make Responses state the owner of Atlas continuity.

Official references:
- https://developers.openai.com/api/reference/cli/resources/responses/methods/create
- https://developers.openai.com/api/docs/models/gpt-5.6-sol
- https://developers.openai.com/api/docs/guides/latest-model

## 3. Anthropic

Claude's current platform exposes ordinary client tools plus server tools including web search/fetch, code execution, tool search, Bash/text editing, computer/browser use, files/PDF/vision, and other provider capabilities. Current Claude models support adaptive thinking/effort for agentic work.
Anthropic also provides an MCP connector for remote HTTP/SSE servers and client-side MCP helpers that can work with local stdio MCP servers through an Atlas-owned MCP client. Large tool sets can use deferred loading plus tool search so that every schema does not need to occupy the prompt at once.

Useful Atlas conclusion: Claude can act as the decision-maker over a broad tool surface without Atlas imposing a separate planner. For local-machine work, Atlas should prefer owning the local MCP/client boundary rather than exposing local stdio services directly to a provider-hosted remote connector.

Official references:
- https://platform.claude.com/docs/en/agents-and-tools/mcp-connector
- https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview
- https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool
- https://platform.claude.com/docs/en/agents-and-tools/tool-use/code-execution-tool

## 4. Google Gemini

The Gemini API exposes custom function calling and built-in tools including Google Search, Google Maps, URL Context, File Search, Code Execution, and Computer Use. The Interactions API also supports remote MCP servers. Gemini's thinking models expose reasoning state/signatures, and Google supplies specialized agentic models such as Deep Research and Computer Use alongside general models.

Gemini 3 tool-context circulation is designed to let built-in tools and custom functions participate in the same multi-step interaction. Remote MCP currently uses HTTP-style server connections rather than local stdio directly.

Useful Atlas conclusion: Gemini is another viable model-led agent provider and its multimodal/search ecosystem may be particularly useful for research and document-heavy work. Atlas should normalize the ability, not duplicate Google's tool implementation.

Official references:
- https://ai.google.dev/gemini-api/docs/function-calling
- https://ai.google.dev/gemini-api/docs/tool-combination
- https://ai.google.dev/api/interactions-api
- https://ai.google.dev/gemini-api/docs/thinking

## 5. xAI

xAI's current API exposes built-in web search, X search, code execution, image generation, collections/file search, remote MCP, and custom function calling. Grok 4.6 accepts text and image input and exposes selectable reasoning effort. xAI also supports mixing provider-side tools and client-side functions in one agentic flow.
Useful Atlas conclusion: xAI is viable as another provider adapter rather than a special-case product path. Its Responses-compatible surface may reduce adapter friction, but Atlas must not assume compatibility implies identical semantics.

Official references:
- https://docs.x.ai/developers/tools/overview
- https://docs.x.ai/developers/tools/function-calling
- https://docs.x.ai/developers/tools/remote-mcp
- https://docs.x.ai/developers/grok-4-6

## 6. Cross-provider conclusions

All four surveyed providers support serious model-led tool use. This removes the main justification for a runtime planner that decides the workflow before inference.

Atlas V5 should therefore:

- call provider model APIs directly rather than adopt a provider-specific agent SDK;
- keep conversation, memory, workspace, schedules, identity, and continuity in Atlas;
- maintain a normalized capability map while preserving provider-specific feature metadata;
- allow the model to choose tools and sequence work;
- support Atlas-owned local MCP, especially for stdio and machine-local services;
- treat provider-hosted MCP as an optional transport, not the core local architecture;
- expose provider-native tools when they are genuinely useful;
- use deferred/tool-search mechanisms when a tool catalog is too large for direct context;
- keep authority and credential custody local wherever practical;
- record provider/model/tool usage for cost and diagnostics without making billing data part of reasoning semantics.

## 7. Capability classes for design

The capability map should classify abilities as model-native, provider-hosted, Atlas-local/MCP, external-service, or local-software. The model sees a useful semantic description; Atlas retains transport, auth, schema, availability, and effect metadata.

Provider switching must not require translating durable Work or memory from one vendor's agent framework to another. The next model receives Atlas's contextual state and available capabilities, then continues from there.
