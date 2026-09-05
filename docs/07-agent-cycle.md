# Atlas V5 Agent Cycle

This document defines the conceptual flow of one agent interaction. It is deliberately not an API schema or implementation plan.

## 1. Before inference

Atlas prepares the model's current operating picture outside the active reasoning step.

That preparation may include:

- the owner's current message and relevant conversation;
- a compact contextual-memory snapshot;
- current objective and useful workspace state;
- a capability map covering native model abilities and available tools;
- effective authority/restrictions that materially affect choices;
- selected embedded-memory recall where relevance warrants it;
- recent tool results or failures needed to continue an existing task.

The purpose is orientation, not instruction-by-runtime.

## 2. Inference owns the workflow

The model interprets the request and decides the useful next action. It may answer directly, use a native ability, call one or more tools, inspect more context, ask the owner a genuine question, or change approach after new evidence.

Atlas does not predeclare the sequence of steps and does not require a separate planner to approve each reasoning transition.

The model may use multiple independent tools in parallel where the provider supports it and where the calls do not depend on one another.

## 3. Tool execution

When the model requests a tool, Atlas resolves the requested ability to its actual transport and executes it within the effective authority boundary.
Execution returns concrete results to the model: success, data, denial, authentication failure, missing resource, timeout, partial result, or another exact outcome.

Runtime does not reinterpret that result into the next workflow step. The model reasons over it.

## 4. Continuing the task

The model may continue using tools until it has enough evidence to complete the objective or until a genuine blocker requires owner input.

There may be provider-specific limits on one inference/tool sequence. Atlas may continue the same task with another inference while preserving the same contextual state and workspace. A provider turn limit must not become a product-level task model.

If Atlas changes models or providers, the next model receives the same Atlas-owned operating picture rather than a vendor-specific handover narrative.

## 5. Completion

The model decides when the requested objective is satisfied, using tool results and available evidence.

Where an effect can be checked exactly, Atlas may expose deterministic verification as another fact or tool result. For example, a Git diff, file hash, HTTP status, calendar event ID, or sent-message receipt can establish what actually happened.

Verification should improve truth without turning into a universal runtime workflow that the model must service.

## 6. After inference

Atlas updates conversational state and contextual memory. Information that leaves active relevance can enter the asynchronous memory outbox for classification, embedding, durable preservation, merge, or discard.

Execution/activity metadata may be retained for observability and diagnosis without being promoted into owner memory.

The owner receives the answer or result without waiting for background memory housekeeping.

## 7. Failure principle

Failures should be returned in the vocabulary of the failed boundary. An expired Google token is an authentication failure; a denied path is a filesystem permission problem; a missing tool is unavailable capability; a provider timeout is a provider failure.

The model decides how to adapt. Atlas should not hide precise failures behind generic states such as "unserviced" when a more useful technical truth is available.
