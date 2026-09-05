# Provider Selection for Atlas V5

Survey date: 2026-09-05
Decision status: **Accepted — OpenAI primary/reference provider**

Detailed provider capabilities are recorded in `03-provider-and-tool-survey.md`. Specific model names and platform features are intentionally treated as time-sensitive rather than permanent Atlas semantics.

## 1. Selection question

The choice is not a benchmark contest about which frontier model is universally best.

Atlas needs a provider that supports model-led tool use, multimodal interaction, strong native capabilities, reasoning controls, continuation/context handling, custom tools, MCP or equivalent integration, and enough flexibility for a local Atlas runtime to remain the owner of continuity and execution.

## 2. OpenAI

OpenAI is the primary and reference inference provider for the first V5 implementation.

The decision is based on the breadth of its current model/tool surface, strong function/tool integration, multimodal support, native hosted capabilities, reasoning controls, context/continuation facilities, and ability to combine provider-native and Atlas-owned tools.

The architectural caution is equally important: Atlas must not delegate identity, transcript continuity, memory, workspace, schedules, authority, artifacts, or product semantics to provider-managed state merely because the provider can host some of them.

## 3. Anthropic

Anthropic is the first compatibility provider and architectural cross-check.

Its client-executed tool model, strong coding/computer tooling, tool discovery, and MCP support make it especially useful for verifying that Atlas's provider boundary is genuine rather than an OpenAI-shaped core with adapters in name only.
If the first compatibility provider cannot occupy the same Atlas-owned seat without redesigning memory, workspace, schedules, authority, or tool ownership, the abstraction is too coupled.

## 4. Gemini and xAI

Gemini remains a strong later provider/specialist, particularly where its multimodal and Google-native surfaces are useful.

xAI remains a valid additional provider with custom tool/MCP support and provider-specific strengths.

Neither is required to shape the initial core architecture.

## 5. Portable core, provider-specific accelerators

Atlas does not normalize providers down to a lowest-common-denominator text endpoint.

The portable core owns:

- owner/agent identity;
- transcript continuity;
- Environment Registry and enabled capability projection;
- workspace and artifact state;
- memory and schedules;
- credentials/authority boundaries;
- user-facing product semantics.

Provider adapters advertise their native modalities, tools, reasoning controls, context behavior, and other accelerators. The active model may exploit those strengths without making them canonical Atlas state.
## 6. Re-evaluation

Provider capabilities change quickly. Atlas should re-survey them periodically and can change its preferred provider without changing the architecture.

The decision that matters architecturally is not a model name. It is that **the inference provider is replaceable while Atlas remains the owner of the seat and surrounding environment**.

See ADR `decisions/0002-openai-primary-provider.md` for the accepted decision.