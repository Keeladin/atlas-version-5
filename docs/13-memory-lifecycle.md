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

Within the set of still-valid sources, retrieval relevance does not by itself establish answerability. Runtime exposes source class, exact evidence locators, ordering, and structural indexing coverage; the model decides whether those facts are sufficient for the specific historical claim. A prior Atlas/model statement is evidence that Atlas made that statement, while a corresponding tool observation is stronger evidence that the operation actually occurred. Chronology claims such as "first" or "latest" require separate ordering support and must be qualified when search or coverage cannot establish exhaustiveness.

Within the set of still-valid sources, normal recall prefers the cheapest sufficient path:

1. current model-visible transcript/context when it is not known to be stale;
2. indexed short-term capsules and transcript chunks;
3. embedded long-term memory;
4. canonical durable records where authoritative owner state exists;
5. external authoritative systems whenever the question requires their current state.

This preserves continuity without making every conversation permanent or turning memory housekeeping into part of the active agent cycle. Memory mutation/recovery guarantees are further constrained by `17-runtime-constitution.md`.
## 10. Explicit owner memory commands — implemented 2026-09-08

Owner-directed memory now has a deterministic mutation path separate from automatic background curation. The conversational model may interpret an explicit instruction, but it does not write database rows itself. It calls `memory.remember`, `memory.correct`, or `memory.forget`; runtime records a durable command first and then applies or fails it transactionally.

The command ledger exposes `pending`, `applied`, and `failed` lifecycle state. Applied commands retain source transcript/turn provenance and target/replacement memory identities. This makes memory mutation observable and recoverable instead of hiding it in model prose.

Corrections and forgetting are precedence operations, not transcript edits. Superseded and forgotten content remains part of canonical history, but active guards prevent that stale content from being returned as ordinary transcript recall. Model-visible working-context projections also redact exact guarded content so recent tool/search evidence cannot immediately resurrect a value after it was forgotten.

Re-remembering the same forgotten content is a new explicit owner instruction and clears that content's suppression guard while retaining the old forgotten record for audit. Exact duplicate active remembers are idempotent.

This explicit command layer does not perform automatic CREATE/MERGE/SUPERSEDE decisions over ordinary conversation. That later background reasoning worker remains a separate milestone, as do context capsules and visual/attachment-aware memory.
