# Provider and Tool Survey

Survey date: 2026-09-05

This is a dated capability survey, not permanent product semantics. Provider features and model names change. Atlas uses the survey to identify abilities it can reuse rather than rebuild.

## 1. Survey questions

For each provider Atlas cares about:

1. What can the model/provider already do natively?
2. What modalities can it receive and produce?
3. What hosted tools, custom tools, MCP paths, reasoning controls, continuation/state features, and usage information are available?
4. Can Atlas execute local tools while keeping continuity and authority locally owned?

## 2. OpenAI

OpenAI exposes strong custom-tool/function calling together with a broad provider-native tool surface, multimodal inputs/outputs across its model/tool portfolio, reasoning controls, streaming/continuation facilities, and MCP/tool-search style integration.

Atlas conclusion: OpenAI can provide the primary inference seat and a rich first orbit of native capabilities. Atlas should exploit those capabilities while keeping transcript, memory, workspace, artifacts, schedules, authority, and Environment Registry state under Atlas ownership.

## 3. Anthropic

Anthropic exposes strong model-led tool use, client-executed coding/computer interfaces, MCP integration, deferred/tool-search mechanisms, vision/document capability, and reasoning controls.

Atlas conclusion: Anthropic is an excellent compatibility cross-check because its tool model strongly supports the same principle that inference decides and the client/runtime executes.

## 4. Google Gemini

Gemini exposes custom function calling, built-in research/search/code/computer capabilities, strong multimodal document/image/audio/video handling across its model family, and remote MCP/tool integration.

Atlas conclusion: Gemini is a strong later provider or specialist, particularly for multimodal/research-heavy work.
## 5. xAI

xAI exposes custom tool/function calling, remote MCP, web/X search, code execution, image capabilities, file/collection retrieval, and reasoning controls across its current platform.

Atlas conclusion: xAI remains a valid additional provider without requiring separate Atlas product semantics.

## 6. Cross-provider architectural conclusions

All surveyed providers support serious model-led tool use. Atlas therefore does not need a runtime planner to decide workflows before inference.

Atlas should:

- call model/provider APIs through replaceable adapters;
- preserve provider-native strengths instead of normalizing them away unnecessarily;
- keep identity, transcript continuity, memory, workspace, artifacts, schedules, and authority Atlas-owned;
- maintain an Environment Registry with an enabled capability projection;
- expose raw tool schemas progressively when a meaningful capability becomes relevant;
- own local MCP/client boundaries where local execution or credential custody requires it;
- use provider-native tools when they remove unnecessary Atlas implementation;
- record usage/cost and technical failures for Control/diagnostics without turning them into workflow semantics.

## 7. Tool hierarchy

Before Atlas builds something new, prefer:

1. model-native capability;
2. provider-native capability;
3. MCP or existing external service;
4. ordinary local software/CLI/API;
5. Atlas-native implementation only when genuinely necessary.

The survey supports the V5 principle: **Atlas should know and use its environment rather than absorb it.**