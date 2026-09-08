# Atlas V5 Memory Architecture

## 1. Principle

Atlas separates immediate conversational continuity from durable memory.

The active model should not perform memory housekeeping while it is working. The transcript records experience; an asynchronous memory processor later interprets that experience and decides what should survive. Where that classification requires meaning, the processor may use separate model inference while deterministic runtime handles queueing, persistence, and enforcement.

The computer analogy remains useful:

- live/contextual state behaves like working memory/RAM;
- indexed short-term memory is recent searchable history;
- embedded and canonical long-term memory behave like durable storage.

## 2. Live transcript

Atlas maintains a temporary append-only transcript for the current coherent conversation/session. It records owner turns, Atlas responses, tool requests, all tool observations, and references to artifacts.

The transcript is descriptive, not interpretive. It does not decide what is important or write durable owner memory while the conversation is active.

Provider conversation state may keep the active model session warm, but Atlas owns the canonical transcript so continuity survives restart, compaction, model change, or provider change.

## 3. Context assembly

The canonical transcript and the model-visible working context are separate concerns. Atlas preserves the complete transcript in PostgreSQL while assembling a bounded projection for each foreground model turn.

The active-task checkpoint is a third, narrower concern. It is a small durable materialized state for unfinished foreground work, not long-term memory and not a duplicate transcript. Deterministic runtime events maintain its operational facts; the model may emit only bounded semantic task deltas inside the existing inference response. No checkpoint-only inference is permitted. While active, the checkpoint is protected from ordinary history/tool-evidence eviction and may survive service restart.

When a task finishes, this checkpoint may later provide useful source material to the asynchronous memory pipeline, but it is never automatically promoted to durable memory. Memory candidate extraction, distribution, reconciliation, and publishing remain background work and may lag the transcript by seconds without delaying owner-facing inference.

The initial operating policy keeps up to the latest 10 owner/Atlas exchanges inside a 64k-token working budget. Owner and Atlas text remains verbatim while it fits. If the selected window exceeds budget, Atlas first compacts older successful/read-heavy tool observations into small structural evidence records while retaining their full canonical observations in PostgreSQL; it then compacts other successful tool evidence and, only if necessary, drops the oldest exchanges. Failed, uncertain, prepared, or otherwise unresolved evidence is protected from normal compaction. The current owner turn is never silently truncated merely to satisfy the working budget.

A context capsule may still provide orientation across transcript eras, and future retrieval can inject older exact transcript, memory, or indexed knowledge selectively. Bounded working context therefore controls model cost and focus without becoming a retention policy for canonical history.
## 4. Short-term memory

When a transcript closes, Atlas associates a context capsule with it and retains the closed transcript in short-term memory for a configurable TTL.

Short-term memory is indexed so Atlas does not have to read many daily/session transcripts sequentially. Retrieval should combine metadata, lexical/full-text search, and semantic/vector search.

The capsule can act as a cheap first-stage locator. Atlas drills into exact transcript chunks only when precision is needed.

## 5. Durable memory substrate

PostgreSQL is the intended durable memory substrate from the beginning rather than an interim SQLite store. The reason is architectural: V5 expects concurrent foreground/background access, durable transactions, database-role separation, and hybrid structured/full-text/vector retrieval in one long-lived substrate.

PostgreSQL holds canonical records, transcript/index metadata, provenance, relationships, retention state, and structured memory. pgvector supplies semantic vector search. PostgreSQL full-text/lexical search and ordinary SQL/metadata filtering handle exact identifiers and structured retrieval.

Retrieval is therefore hybrid. Vectors help Atlas find memory; vectors are not the memory itself.

Large artifacts such as images, PDFs, documents, audio, or generated files remain in file/object storage. PostgreSQL stores artifact metadata, hashes, provenance, relationships, and storage references rather than making the database a blob store by default.

## 6. Durable memory writes

The active model is not the normal writer to durable memory. Its natural write surface is the transcript.

A separate memory processor reads closed or aging transcripts and may discard information, retain it temporarily, promote it to embedded long-term recall, preserve it as canonical durable memory, or merge/supersede an existing memory.
Explicit owner instructions such as "remember this", "correct that", or "forget that" remain transcript events, but runtime also records them as durable memory commands with an observable pending/applied/failed lifecycle. Owner corrections and forgetting take precedence over stale derived memory and must not be resurrected by queued processing or older indexes.

The model may receive broad read access to memory through bounded read-only database views/tools or higher-level retrieval tools that exclude secrets and protected runtime state. Ordinary durable writes remain controlled by the memory-processing path.

## 7. Provenance and external truth

Every derived durable memory should remain traceable to its source transcript, artifact, document, tool result, or owner statement.

External systems such as Gmail, Drive, GitHub, or another live database remain authoritative for their own current state. Canonical corrections, supersession, deletion/tombstones, source authority, and freshness constrain what retrieval may present as current truth. Atlas memory should preserve what improves future reasoning and continuity rather than duplicate every external fact it encounters.

## 8. Promotion and indexing

Promotion from short-term to long-term memory should reuse valid chunk boundaries, lexical indexes, embeddings, entity metadata, and provenance already created during short-term indexing.

Re-embedding is needed only when the representation contract changes or an index/model migration requires it.

Retention TTL, chunk sizes, overlap, ranking weights, summary lengths, embedding choice, and similar values are configurable operational parameters rather than architecture constants.

For the detailed lifecycle see `13-memory-lifecycle.md`.

## 9. Implementation checkpoint — 2026-09-08

The first memory-orbit implementation is now present: canonical transcript turns can be incrementally transformed into provenance-backed derived text chunks, checkpointed per transcript/index version, and searched with PostgreSQL full-text search through the bounded `memory.search` capability. The active foreground tail is excluded from maintenance indexing, and indexing never runs on the foreground inference path.

This is intentionally only the retrieval substrate. Semantic embeddings/pgvector ranking, context-capsule generation, durable remember/correct/forget commands, and the separate memory-reasoning worker that decides create/merge/supersede/discard remain staged. Derived indexes remain rebuildable from canonical transcript history.
