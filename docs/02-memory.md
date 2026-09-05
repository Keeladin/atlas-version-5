# Atlas V5 Memory Architecture

## 1. Principle

Atlas separates immediate conversational continuity from durable memory.

The active model should not perform memory housekeeping while it is working. The transcript records experience; an asynchronous memory processor later interprets that experience and decides what should survive.

The computer analogy remains useful:

- live/contextual state behaves like working memory/RAM;
- indexed short-term memory is recent searchable history;
- embedded and canonical long-term memory behave like durable storage.

## 2. Live transcript

Atlas maintains a temporary append-only transcript for the current coherent conversation/session. It records owner turns, Atlas responses, relevant tool observations, and references to artifacts.

The transcript is descriptive, not interpretive. It does not decide what is important or write durable owner memory while the conversation is active.

Provider conversation state may keep the active model session warm, but Atlas owns the canonical transcript so continuity survives restart, compaction, model change, or provider change.

## 3. Context assembly

Early in a session the model-visible context may contain the whole transcript. As it grows, Atlas can provide a smaller relevant view while preserving the complete temporary transcript outside the model.

A new or resumed transcript may begin with a compact context capsule plus a small exact tail of recent turns when verbatim continuity is useful.
## 4. Short-term memory

When a transcript closes, Atlas associates a context capsule with it and retains the closed transcript in short-term memory for a configurable TTL.

Short-term memory is indexed so Atlas does not have to read many daily/session transcripts sequentially. Retrieval should combine metadata, lexical/full-text search, and semantic/vector search.

The capsule can act as a cheap first-stage locator. Atlas drills into exact transcript chunks only when precision is needed.

## 5. Durable memory substrate

PostgreSQL is the intended durable memory substrate from the beginning rather than an interim SQLite store.

PostgreSQL holds canonical records, transcript/index metadata, provenance, relationships, retention state, and structured memory. pgvector supplies semantic vector search. PostgreSQL full-text/lexical search and ordinary SQL/metadata filtering handle exact identifiers and structured retrieval.

Retrieval is therefore hybrid. Vectors help Atlas find memory; vectors are not the memory itself.

Large artifacts such as images, PDFs, documents, audio, or generated files remain in file/object storage. PostgreSQL stores artifact metadata, hashes, provenance, relationships, and storage references rather than making the database a blob store by default.

## 6. Durable memory writes

The active model is not the normal writer to durable memory. Its natural write surface is the transcript.

A separate memory processor reads closed or aging transcripts and may discard information, retain it temporarily, promote it to embedded long-term recall, preserve it as canonical durable memory, or merge/supersede an existing memory.
Explicit owner statements such as "remember this" remain transcript events but are strong retention signals for the processor. Corrections and later statements can supersede earlier memories without erasing provenance.

The model may receive broad read access to memory through safe database views/tools or higher-level retrieval tools. Ordinary durable writes remain controlled by the memory-processing path.

## 7. Provenance and external truth

Every derived durable memory should remain traceable to its source transcript, artifact, document, tool result, or owner statement.

External systems such as Gmail, Drive, GitHub, or another live database remain authoritative for their own current state. Atlas memory should preserve what improves future reasoning and continuity rather than duplicate every external fact it encounters.

## 8. Promotion and indexing

Promotion from short-term to long-term memory should reuse valid chunk boundaries, lexical indexes, embeddings, entity metadata, and provenance already created during short-term indexing.

Re-embedding is needed only when the representation contract changes or an index/model migration requires it.

Retention TTL, chunk sizes, overlap, ranking weights, summary lengths, embedding choice, and similar values are configurable operational parameters rather than architecture constants.

For the detailed lifecycle see `13-memory-lifecycle.md`.