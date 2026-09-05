# Memory Lifecycle

This document defines how Atlas V5 moves information from live conversation into recent searchable context and, only when justified, into long-term memory.

## 1. Principle

Conversation is not automatically permanent memory.

Atlas keeps a temporary working transcript while a conversation or session is active. When that transcript closes, Atlas derives a compact handover summary and moves the closed transcript into short-term memory for a limited retention period.

Short-term memory is searchable and may be indexed immediately. Promotion to long-term memory therefore reuses work already done instead of reprocessing the same material from scratch.

## 2. Memory layers

1. **Live transcript** — current conversational history used for immediate continuity.
2. **Context capsule** — compact handover summary written when a transcript closes.
3. **Short-term memory** — recent closed transcripts and capsules retained for a configurable TTL.
4. **Embedded long-term memory** — selected durable semantic recall.
5. **Canonical durable memory** — explicit facts, preferences, decisions or records that must remain authoritative.

The Environment Registry is separate from these layers. It describes what exists and how Atlas can reach it; it is not conversational memory.
## 3. Live transcript

The live transcript is temporary working history, not long-term memory.

It accumulates owner turns, Atlas responses and relevant tool observations while the current conversational context remains coherent. Early in a session the model-visible context may simply contain the whole transcript.

Atlas may close a transcript when:

- a natural conversational/session boundary is reached;
- a materially different topic begins;
- context size makes continuation inefficient;
- a configured daily rollover occurs, with midnight as a simple guaranteed boundary.

Closing a transcript does not delete it. It moves it out of the model's immediate working set.

## 4. Context capsule

Before a transcript is closed, Atlas derives a compact context capsule and appends or associates it with the closed transcript.

The capsule records only what a future model needs to regain orientation: current topics, decisions, unresolved matters, active workspace or resources, relevant people/entities and any clear continuation point.

A new transcript may begin with the previous capsule plus a small raw tail of exact recent turns when verbatim continuity is useful.
## 5. Indexed short-term memory

Closed transcripts and capsules enter short-term memory for a configurable TTL, for example 14 or 30 days.

Short-term memory should be searchable without reading every transcript sequentially. Retrieval should combine:

- metadata filtering such as date, workspace and entities;
- lexical search for exact names, identifiers and phrases;
- semantic/vector search for conceptually similar wording.

The daily/session capsule can be embedded as a cheap first-stage locator. If a capsule matches, Atlas can then search or read only the relevant raw transcript chunks.

The index is temporary with the memory it serves. Expiry of a short-term item removes its temporary index entries unless that item has been promoted.

## 6. Background memory processing

Memory classification runs outside the active conversational inference path.

When a transcript closes or during a later maintenance pass, a background process can classify recent memory as:

- discard when its TTL expires;
- retain in short-term memory longer;
- promote into embedded long-term memory;
- promote into canonical durable memory;
- merge or supersede an existing long-term memory.
## 7. Promotion should reuse existing indexing

Promotion is a retention decision, not a second ingestion pipeline.

By the time short-term memory is evaluated for promotion it may already have chunk boundaries, timestamps, provenance, lexical terms, embeddings and entity metadata. Atlas should preserve and reuse those derived representations when they remain valid.

Re-embedding is required only when the long-term index deliberately uses a different representation contract or when an embedding/index version has changed.

Canonical source content and provenance remain separate from derived indexes so indexes can be rebuilt without rewriting memory history.

## 8. Retrieval path

A normal recall path is:

1. Use live transcript and current context when sufficient.
2. Search short-term capsules/index for recent context.
3. Drill into only the relevant transcript chunks when exact detail is needed.
4. Search embedded long-term memory when the information is older or durable.
5. Prefer canonical durable records when an authoritative fact or decision exists.

This gives Atlas continuity without making every conversation permanent and without forcing the active model to perform memory housekeeping while it is working.