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

Explicit owner requests to remember, correct, retire, restore, or delete still enter through the transcript rather than direct model database writes, but runtime records them as durable memory commands with pending/applied/failed state. Corrections, retirement and deletion take precedence over queued or stale derived material.

## 8. Promotion reuse

Promotion is a lifecycle decision, not a second ingestion pipeline.

Existing chunking, timestamps, provenance, lexical indexes, embeddings, and entity metadata should be reused whenever their representation remains valid.

Canonical source content stays separable from derived indexes so indexes can be rebuilt without rewriting history.

## 9. Retrieval order and authority

Retrieval is constrained before relevance ranking by explicit owner corrections/retirement/deletion, supersession/tombstones, source authority, and freshness. A semantically strong older match must not override a newer canonical correction or a live authoritative source.

Within the set of still-valid sources, retrieval relevance does not by itself establish answerability. Runtime exposes source class, exact evidence locators, ordering, and structural indexing coverage; the model decides whether those facts are sufficient for the specific historical claim. A prior Atlas/model statement is evidence that Atlas made that statement, while a corresponding tool observation is stronger evidence that the operation actually occurred. Chronology claims such as "first" or "latest" require separate ordering support and must be qualified when search or coverage cannot establish exhaustiveness.

Within the set of still-valid sources, normal recall prefers the cheapest sufficient path:

1. current model-visible transcript/context when it is not known to be stale;
2. indexed short-term capsules and transcript chunks;
3. embedded long-term memory;
4. canonical durable records where authoritative owner state exists;
5. external authoritative systems whenever the question requires their current state.

This preserves continuity without making every conversation permanent or turning memory housekeeping into part of the active agent cycle. Memory mutation/recovery guarantees are further constrained by `17-runtime-constitution.md`.
## 10. Owner memory lifecycle — implemented 2026-09-09

The registered owner operations are `memory.remember`, `memory.correct`, `memory.retire`, `memory.restore`, `memory.delete`, and `memory.commands.list`. `memory.forget` is not registered. An ambiguous request to “forget” needs clarification when retaining versus deleting the content materially changes the outcome; otherwise the model follows the explicit intent.

| Record state | Stored payload | Recall |
| --- | --- | --- |
| active | Content, provenance and optional derived embedding | Applicable current recall |
| superseded | Prior content and replacement relationship | Explicit `include_historical=true` memory search only |
| retired | Content retained for restoration | Excluded from current and historical memory search |
| deleted | Content, fingerprint, embedding and embedding metadata are NULL; ID, relationships, deletion timestamp and operation ID remain | Excluded; the identity cannot be restored or corrected |

Commands have a separate pending/applied/failed lifecycle. `memory.correct` distinguishes a correction from a change over time; the latter records the transition time. `memory.restore` reactivates a retired identity. Explicitly remembering the same retired content also restores it; remembering deleted information creates a new identity.

Lifecycle writes serialize on `memory_state/owner` through `SharedStateWriter`. The mutation, revision change, command completion and shared-write receipt commit together. A failure rolls them back, then records failed command state separately. Owner commands evaluate current state inside the serialized callback; future derived publishers must evaluate a revision and provide it as `expected_version`.

Memory records carry kind, scope, scope key, durability, subject/namespace and temporal fields. Current owner commands support chat and cross-chat scope; project-scoped publication is deferred until runtime can bind a project identity. Derived records are projected with derived authority, while owner-directed records have owner authority. Lifecycle and applicable scope constrain memory search before relevance ranking.

### Deletion boundary

`memory.delete` supports `memory_only` and `memory_and_sources` (the default). Both null selected memory payloads and their recorded derived dependants, invalidate/scrub related candidates and their inline provider-evidence payloads, and retain a content-free deletion receipt. The source-inclusive operation also replaces the selected exact text in supporting transcript payloads with `[Content deleted by owner]`. Turn IDs and sequence numbers remain intact. If any selected supporting passage cannot be isolated by the current exact-content matcher, the transaction fails rather than removing unrelated text.

Affected transcript chunks are removed and their checkpoints rewind to the beginning of the earliest invalidated chunk, preserving earlier unrelated turns in a multi-turn chunk when re-indexing. Affected transcript summaries/task projections and continuity capsules are invalidated. Related legacy command payloads are scrubbed. PostgreSQL constraints enforce the null payload and retained deletion identity of deleted records.

The implemented boundary covers selected database payloads and recorded dependencies, not an exhaustive semantic search for every paraphrase. `memory_only` retains original source text. Artifact files, external systems, archived WAL, backups and restore-time deletion replay are outside this implementation. Deletion must not be described as physical erasure from every storage medium. Existing maintenance/foreground paths need additional revision coordination before claiming protection against every in-flight reader or derived writer.

Automatic reconciliation/promotion remains a separate milestone. Its future writer must respect retirement/deletion provenance and the shared revision fence; pending candidates do not acquire recall authority.

## 11. Foreground candidate hints — implemented 2026-09-09

Ordinary foreground conversation may now produce a bounded `memory_candidates` list inside the hidden runtime envelope of the same model response. This is an opportunistic handoff, not a second memory-processing path and not a durable-memory write.

Runtime accepts only the bounded schema, attaches canonical source provenance itself, and stores valid candidates as `pending`. The model cannot supply or overwrite canonical source turn IDs. Pending candidates have no recall authority and are not injected into foreground context.

The candidate schema separates stable identity/preferences from project state and temporary circumstances through `kind`, `scope`, and `durability`. This is intentionally granular rather than a single append-only profile document. The canonical transcript remains the complete fallback source, so the candidate channel may be sparse without losing history.

The next lifecycle stage is asynchronous reconciliation: compare a pending candidate against existing valid memory and precedence guards, then semantically choose discard, retain-short-term, create, merge, or supersede. Any derived durable-memory mutation from that worker must use the shared-state write contract in `23-shared-state-write-contract.md`, so the semantic decision is committed only against the memory resource version it actually evaluated. That reconciliation stage is not enabled by this intake milestone.
