# Atlas V5 Authority and Control

## Authority principle

Authority is established primarily by provisioning and enforced at the real effect boundary.

If the owner does not want Atlas to use a capability, that capability should be disabled and absent from the agent-visible Environment Registry. If it is enabled, Atlas may use it within the actual technical permissions that still apply.

V5 has no general `CONFIRM` authority state. Repeated confirmation prompts would make the owner part of the runtime loop and recreate machinery the architecture is deliberately removing.

## Enabled versus disabled

Control may show configured capabilities as ON or OFF. The agent sees only the enabled projection.

For large integrations, the owner should control meaningful capability groups rather than hundreds of ambiguous raw tool functions. Finer read/write or sub-capability switches may exist when they are useful and understandable.

An enabled capability may still be temporarily unavailable. Authentication failure, stopped services, missing resources, or OS permission changes are reported precisely rather than treated as semantic policy decisions.

## Local and external boundaries

Local authority should rely on the Atlas service identity, filesystem permissions, executable/service/socket access, database roles, and container/sandbox boundaries where useful.

External systems retain their OAuth scopes, API keys, account roles, and service policies. Atlas should not duplicate these with a second elaborate policy engine.
## Model/runtime boundary

The model decides whether an action is useful. Runtime answers only deterministic authority questions such as whether the tool is enabled, the path is writable, the database role permits the query, or the credential contains the required scope.

A denial or failure is returned to inference as a concrete technical fact. The model decides how to adapt.

For Atlas's own durable memory, the active model is normally read-only. New durable memory is derived from the transcript by the asynchronous memory processor rather than arbitrary model-generated database writes.

## Control surface

Control is the owner's engineering/configuration page, separate from the normal Atlas/chat page.

It may expose:

- provider/model configuration and usage;
- capability groups and ON/OFF state;
- MCP servers, connected services, credentials, and authentication health;
- Environment Registry state and local software availability;
- memory inspection/correction/forgetting and retention settings;
- schedules and automation status;
- workspace/storage information;
- runtime/service health, logs, errors, latency, tokens, and cost.

The normal task path should not require visiting Control.

## Explainability target

When something fails, Control should make it possible to determine which model was in the seat, what context/workspace was relevant, what capability/tool was attempted, which boundary handled it, and what exact result came back.