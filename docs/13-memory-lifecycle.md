# Atlas V5 Memory Lifecycle

This document defines how Atlas moves information from live conversation into recent searchable context and, only when justified, into long-term memory.

## 1. Principle

Conversation is not automatically permanent memory.

The live transcript acts as a scribe. It records what was said and what happened, including tool requests, all tool observations, and references to artifacts, without interpreting those events into durable memory during active work.

When a transcript closes, Atlas creates a compact context capsule and retains the transcript temporarily in indexed short-term memory. A separate asynchronous memory processor later decides what deserves promotion, extension, correction, merge, or discard.

## 2. Lifecycle

1. **Live transcript** — current temporary conversational/work history.
2. **Context capsule** — compact handover/orientation summary associated with a closed transcript.
3. **Indexed short-term memory** — recent transcripts/capsules with configurable retention.
4. **Embedded long-term memory** — selected durable semantic recall.
5. **Canonical durable memory** — explicit facts, preferences, decisions, or records that should remain authoritative until changed.

The Environment Registry and external source systems are separate from this lifecycle.

## 3. Transcript rollover

A transcript may close at a natural session/topic boundary, when context size makes continuation inefficient, or at a configured rollover boundary.

No particular clock time, number of turns, token threshold, or retention duration is architectural. These are configurable and should be tuned from real use.

Closing a transcript removes it from the immediate working set; it does not immediately delete it.
## 4. Context capsule

The capsule records only what a future model needs to regain orientation: current topics, decisions, unresolved matters, relevant workspace/resources, important entities, and any clear continuation point.

A capsule is a derived, versioned summary with provenance back to its source transcript. It is replaceable orientation material rather than canonical owner memory, even when a model helps write the narrative summary.

A new transcript can use the previous capsule plus a small exact tail of recent turns when verbatim continuity matters. The full prior transcript remains searchable while it is retained.

## 5. Indexed short-term memory

Closed transcripts and capsules are indexed so recent recall does not require scanning many transcript files sequentially.

Retrieval should combine:

- metadata such as time, workspace, entity, artifact, or source;
- lexical/full-text search for exact identifiers and phrases;
- semantic/vector search for conceptually related wording.

The capsule can locate the likely transcript first; Atlas can then retrieve only the relevant raw chunks.

Temporary index entries share the retention lifecycle of the short-term material they serve unless promoted.

## 6. Artifact references

Binary artifacts do not live inside transcript text. A transcript records an artifact identity plus useful metadata and conversational context.

The artifact bytes live in temporary or durable artifact storage. Memory processing may preserve captions, extracted text, semantic representations, provenance, or the artifact reference itself when useful.

## 7. Background processing

The memory processor operates outside the active conversational inference path and is the normal writer to durable memory.
It may:

- allow short-term material to expire;
- extend short-term retention;
- promote material to embedded long-term recall;
- preserve a canonical durable memory;
- merge or supersede an existing durable memory;
- retain provenance without retaining all raw conversational detail.

Explicit owner requests to remember, correct, or forget still enter through the transcript rather than direct model database writes, but runtime records them as durable memory commands with pending/applied/failed state. Corrections and forgetting take precedence over queued or stale derived material.

## 8. Promotion reuse

Promotion is a lifecycle decision, not a second ingestion pipeline.

Existing chunking, timestamps, provenance, lexical indexes, embeddings, and entity metadata should be reused whenever their representation remains valid.

Canonical source content stays separable from derived indexes so indexes can be rebuilt without rewriting history.

## 9. Retrieval order and authority

Retrieval is constrained before relevance ranking by explicit owner corrections/forgetting, supersession/tombstones, source authority, and freshness. A semantically strong older match must not override a newer canonical correction or a live authoritative source.

Within the set of still-valid sources, normal recall prefers the cheapest sufficient path:

1. current model-visible transcript/context when it is not known to be stale;
2. indexed short-term capsules and transcript chunks;
3. embedded long-term memory;
4. canonical durable records where authoritative owner state exists;
5. external authoritative systems whenever the question requires their current state.

This preserves continuity without making every conversation permanent or turning memory housekeeping into part of the active agent cycle. Memory mutation/recovery guarantees are further constrained by `17-runtime-constitution.md`.