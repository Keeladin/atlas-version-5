# Atlas V5 Agent Cycle

This document defines the thin conceptual loop. It is deliberately not a workflow engine or implementation schema.

## 1. Context

Atlas supplies a small seat bootstrap plus the relevant model-visible portion of the live transcript. The Environment Registry, workspace state, memory, and detailed tool schemas remain outside the prompt until inference decides they are needed.

Atlas may retrieve or expose additional context on demand without deciding what the model should conclude from it.

## 2. Inference

The model interprets the owner's request and owns semantic branching: what matters, what to inspect, which capability to use, what sequence makes sense, whether more evidence is needed, when to adapt, whether clarification is necessary, and when the objective is satisfied.

Runtime does not predeclare steps, obligations, or capability routes.

## 3. Tool/action

When the model requests an enabled capability, Atlas resolves the request to the underlying provider-native tool, MCP operation, service API, database access, or local software interface and executes it within the real authority boundary.

The model may use more than one underlying tool to satisfy one meaningful capability. The owner does not need to choose between raw operations such as mail search versus mail get/read.

## 4. Result

Runtime returns exact results in the vocabulary of the boundary that produced them: data, success, permission denial, authentication requirement, timeout, missing resource, unavailable capability, partial result, or other concrete outcome.

Runtime does not translate the result into a semantic next step. The model reasons over the result.
## 5. Continue or complete

The cycle repeats as needed:

`context → inference → tool/action → result → inference`

Provider turn limits, continuation APIs, or background modes are transport concerns rather than Atlas task semantics. Atlas owns enough transcript/workspace state to reseat another inference when required.

The model decides semantic completion. For consequential effects, runtime action/evidence state establishes whether the effect actually occurred; a model completion statement is not an execution receipt. Deterministic checks such as hashes, diffs, IDs, status codes, or query results may be exposed as exact facts.

## 6. Transcript

Owner messages, Atlas responses, artifact references, tool requests, and all tool observations are appended to the live transcript. Large or binary observations may be represented by stable artifact references. The transcript records the interaction; it does not decide relevance or perform memory classification.

When the transcript later closes, asynchronous memory processing occurs outside this cycle.

## 7. Ownership rule

A useful architectural test is:

- if the question requires meaning, judgment, relevance, adaptation, or sufficiency, it belongs to inference;
- if it requires exact execution, iteration, persistence, triggering, validation, or enforcement, it belongs to runtime/software.

This boundary is more important than the mechanics of the loop itself. Runtime execution guarantees, crash recovery, effect truth, trust boundaries, and concurrency are governed by `17-runtime-constitution.md`.