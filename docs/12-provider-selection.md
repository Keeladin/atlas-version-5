# Provider Selection for Atlas V5

Survey date: 2026-09-05.

## 1. Question

This is not a benchmark contest about which frontier model is "best". Atlas assumes the major providers are all capable enough to reason well.

The question is narrower: **which provider exposes the best control surface for the Atlas V5 architecture?**

Atlas wants inference to make most workflow decisions. The provider therefore matters most where it affects model-led tool use, local execution, reasoning control, context handling, and provider lock-in.

## 2. Selection criteria

The comparison weights these concerns most heavily:

1. Model-led tool selection and multi-step inference.
2. Clean client-side execution for Atlas-local tools and software.
3. Ability to scale to a large capability catalog without flooding context.
4. Reasoning/effort controls that Atlas can route dynamically.
5. Native tools that remove unnecessary Atlas implementation.
6. Long-context and continuation support.
7. Multimodal/document capability.
8. MCP support and fit with an Atlas-owned local MCP boundary.
9. Ability to preserve provider-specific strengths without making Atlas provider-specific.

Model benchmark scores are deliberately not a primary criterion.## 3. Capability comparison

| Concern | Anthropic | OpenAI | Google Gemini | xAI |
|---|---|---|---|---|
| Client-defined function/tool calls | Strong | Strong | Strong | Strong |
| Provider-hosted web/search | Web search + web fetch | Web search | Google Search + URL context + Maps | Web search + X search |
| Provider-hosted code | Python/Bash code execution | Code interpreter / hosted shell | Python code execution | Code interpreter |
| Local/client shell | Trained-in client Bash schema | Local shell environment supported | Via custom function/tool | Via custom function/tool |
| Local/client file editing | Trained-in text editor schema | Apply-patch/local tool surface | Via custom function/tool | Via custom function/tool |
| Computer/browser control | Trained-in client computer + browser toolsets | Computer use | Computer Use | Custom tools; narrower native surface |
| Large tool catalog | Native deferred loading + BM25/regex tool search | Native tool search/deferred tools | No equivalent general tool-search surface found | No equivalent general tool-search surface found |
| Remote MCP | Yes | Yes | Yes | Yes |
| Local stdio MCP path | Official client-side MCP helpers | Atlas can expose tools/functions; hosted MCP is remote | Atlas-owned MCP client needed | Atlas-owned MCP client needed |
| Reasoning control | Adaptive thinking + effort | none/low/medium/high/xhigh/max on current GPT-5.6 family | thinking levels, usually minimal/low/medium/high | low/medium/high/xhigh |
| Context | 1M on current top/general models | ~1.05M on current flagship family | 1M on current Gemini 3 models | 500K on Grok 4.6 |
| Structured outputs | Yes | Yes | Yes | Yes |
| Vision | Yes | Yes | Yes | Yes |
| Native audio/video input | More limited in ordinary Messages path | Separate realtime/audio model surface | Strong native audio/video/PDF input | More limited; text/image primary |

The table describes exposed platform capabilities, not model intelligence rankings.

## 4. Anthropic fit

Anthropic has an unusually direct match to the V5 idea that **the model decides and Atlas executes**. Its tool-use contract explicitly lets Claude choose when and how to call a tool while the client performs the actual operation.

More importantly for a local Atlas, Anthropic publishes trained-in client tool schemas for Bash, text editing, computer control, browser control, and memory. Atlas can execute those operations locally while Claude uses interfaces it was specifically trained to operate.Anthropic also exposes tool search with deferred loading for hundreds or thousands of tools, plus programmatic tool calling and context-management features intended for long-running agents. Its SDK includes helpers for local stdio MCP clients, prompts, and resources even though the hosted MCP connector itself requires remote HTTP.

This combination is particularly attractive for Atlas because it lets the model remain the planner without requiring Atlas to invent a capability-discovery planner of its own.

## 5. OpenAI fit

OpenAI is the closest overall alternative and in some areas exposes the broadest native tool surface. Current Responses models can use web search, file search, image generation, code interpreter, shell, apply-patch, skills, computer use, MCP, and tool search. Current GPT-5.6 models also expose a very wide reasoning-effort range and roughly 1.05M context.

OpenAI also supports local shell-style execution and apply-patch workflows, so it is not limited to provider-hosted sandboxes. Its Responses API has strong continuation, compaction, background, streaming, and structured-output primitives.

The main architectural caution is not capability but temptation: Responses can own substantial conversation and agent state. Atlas should deliberately use only the pieces that help inference and tool use, while keeping identity, contextual memory, schedules, workspace state, and durable continuity local.

## 6. Gemini fit

Gemini offers the strongest broad multimodal surface of the four: text, images, audio, video and PDF inputs, plus Google Search, Maps, URL context, file search, code execution, computer use and custom functions. Current Gemini 3 models provide 1M context and explicit thinking levels.

Gemini is therefore especially attractive for document-heavy, visual, research, mapping, audio and video tasks. Its tool-context circulation and thought preservation are useful for multi-step work.

The current weakness for Atlas is capability-catalog ergonomics. Google supports remote MCP and custom function calling, but the surveyed API does not expose a general deferred tool-search facility equivalent to Anthropic/OpenAI. Atlas would have to do more of the large-tool catalog preparation itself.

## 7. xAI fit

xAI exposes a clean agentic API with function calling, remote MCP, web search, X search, code execution, collections search and image tools. Grok 4.6 exposes selectable reasoning effort and 500K context, and xAI supports an OpenAI-compatible Responses surface.

That makes xAI straightforward to support as an additional provider. Its current exposed surface is simply narrower for Atlas's local-workspace use case than Anthropic or OpenAI, especially around trained local editing/computer tool contracts and large-tool discovery.## 8. A decisive Atlas-specific issue: the model ladder

Atlas wants to route inference dynamically: cheap/fast for routine work, stronger/deeper for difficult work, while the workspace and contextual memory remain the same.

OpenAI currently has a notable advantage here. GPT-5.6 Luna, Terra and Sol all expose the same ~1.05M context window, 128K output ceiling, image input, the same broad tool surface, and the same `none` through `max` reasoning-effort ladder. The model can therefore change without Atlas also changing the shape of its seat.

Anthropic's current ladder is excellent but less uniform. Sonnet 5 and stronger current models use 1M context and adaptive thinking/effort, while Haiku 4.5 is a 200K model with older extended-thinking semantics and different configuration. A cheap-to-strong handoff therefore crosses a larger API/capability boundary.

Gemini has strong thinking controls and 1M context across much of Gemini 3, but model/tool availability and preview/stable combinations vary more. xAI's current primary reasoning model exposes a 500K context window.

For Atlas, **capability continuity across model tiers is itself a provider capability**.

## 9. Current recommendation

### Primary reference provider: OpenAI

On the V5 criteria, OpenAI is the current best overall fit by a small but meaningful margin.

The reason is not an intelligence benchmark. It is the combination of:

- a very broad native tool surface;
- client/local execution paths including shell and apply-patch;
- MCP and custom tools;
- native tool search for large catalogs;
- roughly 1M context;
- unusually granular reasoning effort controls;
- and, most importantly, a Luna/Terra/Sol model ladder that keeps those capabilities substantially consistent while Atlas changes inference cost/effort.

This directly supports the V5 idea that the model gets into an already-prepared seat and does the reasoning, while Atlas remains responsible for local state, execution and authority.### Mandatory second provider: Anthropic

Anthropic should be the second adapter implemented and the architectural cross-check, not an afterthought.

Its client-side tool design is exceptionally aligned with Atlas: Bash, text editor, computer and browser schemas are provider-defined but executed by the client, and Claude is trained specifically to use those interfaces. Its tool search and local-stdio MCP helpers are also excellent fits for a local agent.

If Atlas can support OpenAI and Anthropic cleanly without branching its core semantics, the provider abstraction is probably healthy. If the second provider requires redesigning memory, workspace or tool ownership, the abstraction is wrong.

### Gemini: deliberate specialist provider

Gemini should be planned as a first-class later adapter because its native multimodal surface is unusually broad. Audio, video, PDF, visual reasoning, Google Search, Maps and URL context may make it the preferred inference provider for certain workspaces or individual turns.

### xAI: supported additional provider

xAI is sufficiently capable and exposes the necessary custom-tool/MCP primitives to remain in the design. It currently adds less unique value to the core Atlas execution model than the other three, except for X-native search and its particular model characteristics.

## 10. Architectural consequence

"OpenAI is the primary provider" must never mean "Atlas is an OpenAI agent."

Atlas owns the portable contract. Provider adapters translate that contract and advertise provider-specific accelerators. The capability map may tell the model that a native OpenAI tool, Anthropic client tool, Gemini built-in tool, or ordinary Atlas-local MCP tool is available without pretending that all four are identical.

The design target is therefore:

**portable core + provider-specific accelerators.**

Provider switching should preserve owner identity, contextual memory, embedded memory, workspace, schedules and local authority. Only the inference engine and its exposed native capabilities change.## 11. Decision status

This is a **design recommendation**, not a provider lock-in decision yet.

Before implementation begins, the acceptance scenarios should be used to validate that the primary and mandatory-secondary providers can both express the same Atlas task lifecycle without moving decision-making back into runtime code.

The provider choice should be reconsidered when exposed platform capabilities materially change; specific model names must never become Atlas product semantics.

## 12. Official references consulted

OpenAI:
- https://developers.openai.com/api/docs/models/gpt-5.6-luna
- https://developers.openai.com/api/docs/models/gpt-5.6-sol
- https://developers.openai.com/api/reference/cli/resources/responses/methods/create
- https://developers.openai.com/api/docs/guides/latest-model

Anthropic:
- https://platform.claude.com/docs/en/agents-and-tools/tool-use/how-tool-use-works
- https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-reference
- https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool
- https://platform.claude.com/docs/en/agents-and-tools/mcp-connector
- https://platform.claude.com/docs/en/models/overview

Google:
- https://ai.google.dev/gemini-api/docs/tools
- https://ai.google.dev/gemini-api/docs/function-calling
- https://ai.google.dev/gemini-api/docs/thought-signatures
- https://ai.google.dev/gemini-api/docs/models

xAI:
- https://docs.x.ai/developers/tools/overview
- https://docs.x.ai/developers/tools/remote-mcp
- https://docs.x.ai/developers/models/grok-4.6
- https://docs.x.ai/developers/model-capabilities/text/reasoning