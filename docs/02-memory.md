# Atlas V5 Memory Architecture

## 1. Principle

Atlas separates immediate conversational continuity from durable memory.

The active model should not perform memory housekeeping while it is working. The transcript records experience; an asynchronous memory processor later interprets that experience and decides what should survive. Where that classification requires meaning, the processor may use separate model inference while deterministic runtime handles queueing, persistence, and enforcement.

The computer analogy remains useful:

- live/contextual state behaves like working memory/RAM;
- indexed short-term memory is recent searchable history;
- embedded and canonical long-term memory behave like durable storage.

## 2. Live transcript

Atlas maintains one append-only canonical transcript per owner chat. Each transcript records owner turns, Atlas responses, tool requests, all tool observations, and references to artifacts. Creating or switching chats changes which owner transcript is selected; it does not merge conversational context between chats.

The transcript is descriptive, not interpretive. It does not decide what is important or write durable owner memory while the conversation is active.

Provider conversation state may keep the active model session warm, but Atlas owns the canonical transcript so continuity survives restart, compaction, model change, or provider change.

## 3. Context assembly

The canonical transcript and the model-visible working context are separate concerns. Atlas preserves the complete transcript in PostgreSQL while assembling a bounded projection for each foreground model turn.

The active-task checkpoint is a third, narrower concern. It is a small durable materialized state for unfinished foreground work, not long-term memory and not a duplicate transcript. Deterministic runtime events maintain its operational facts; the model may emit only bounded semantic task deltas inside the existing inference response. No checkpoint-only inference is permitted. While active, the checkpoint is protected from ordinary history/tool-evidence eviction and may survive service restart.

When a task finishes, this checkpoint may later provide useful source material to the asynchronous memory pipeline, but it is never automatically promoted to durable memory. Memory candidate extraction, distribution, reconciliation, and publishing remain background work and may lag the transcript by seconds without delaying owner-facing inference.

The initial operating policy keeps up to the latest 10 owner/Atlas exchanges inside a 64k-token working budget. Owner and Atlas text remains verbatim while it fits. If the selected window exceeds budget, Atlas first compacts older successful/read-heavy tool observations into small structural evidence records while retaining their full canonical observations in PostgreSQL; it then compacts other successful tool evidence and, only if necessary, drops the oldest exchanges. Failed, uncertain, prepared, or otherwise unresolved evidence is protected from normal compaction. The current owner turn is never silently truncated merely to satisfy the working budget.

A context capsule may still provide orientation across transcript eras, and future retrieval can inject older exact transcript, memory, or indexed knowledge selectively. Bounded working context therefore controls model cost and focus without becoming a retention policy for canonical history.
## 4. Short-term memory

Owner chat transcripts remain durable until the owner deletes the chat. A chat that is not currently selected is eligible for complete transcript indexing; selecting it again does not require replaying other chats into its foreground context.

Short-term retrieval is indexed so Atlas does not have to read many chat transcripts sequentially. Retrieval combines transcript identity/provenance with lexical/full-text and semantic/vector search. Deleting a chat removes that canonical transcript and its derived transcript indexes; separately promoted durable memories remain independent records unless the owner explicitly forgets or later erases them.

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

The memory-orbit retrieval substrate is now present. Canonical transcript turns are incrementally transformed into provenance-backed derived text chunks, checkpointed per transcript/index version, indexed with PostgreSQL full-text search, and embedded with the configured embedding model into pgvector. The active foreground tail is excluded from maintenance indexing, and chunk/embedding maintenance never runs on the foreground inference path.

Production now runs that deterministic maintenance pass from `atlas-v5-memory.timer`: it starts about 30 seconds after the timer is activated and then runs every five minutes. The oneshot worker is bounded by a four-minute service timeout and is stopped before deployments replace code or dependencies. Closed owner chats are therefore eligible for complete indexing on the next pass, while the latest configured owner exchanges of the currently active chat remain outside the derived index. This improves cross-chat freshness without putting indexing or embedding work onto the owner-facing critical path.

`memory.search` now performs bounded hybrid retrieval: exact/full-text candidates and cosine-similarity candidates are independently ranked and fused, while preserving transcript/turn provenance and exclusion constraints for iterative refinement. Query embedding is a retrieval primitive rather than memory reasoning; if it is temporarily unavailable, lexical recall remains usable. The current transcript vector schema is fixed at 1,536 dimensions and defaults to OpenAI `text-embedding-3-large` requested at that dimension.

At this checkpoint this was still a retrieval substrate rather than durable interpreted memory. The subsequent owner-directed memory milestone below adds explicit remember/correct/forget commands; context-capsule generation, automatic semantic promotion/reconciliation, visual attachment indexing, and the separate memory-reasoning worker that decides create/merge/supersede/discard remain staged. Derived lexical/vector indexes remain rebuildable from canonical transcript history.

## 10. Evidence-grounded historical recall — 2026-09-08

Hybrid retrieval is a locator, not a truth oracle. A relevant chunk establishes only that canonical transcript material was retrieved; it does not by itself establish every claim the model may wish to make about the past. Atlas therefore treats retrieval relevance and evidence sufficiency as separate questions. The conversational model remains responsible for semantic judgment, while runtime reports only deterministic facts about source identity, ordering, indexing coverage, and exact observations.

`evidence.read` now returns a structural envelope alongside the bounded exact payload: transcript ID, sequence, actor, evidence kind, timestamp, and tool operation/phase/action/provenance where applicable. This lets Atlas distinguish an owner statement, a prior Atlas/model statement, an owner attachment, and a runtime tool observation. A prior Atlas response proves what Atlas said; claims that an external tool was actually used or an effect actually occurred should be checked against the corresponding observation when practical.

`memory.search` coverage now reports canonical sequence ranges, the index checkpoint, active-tail/unindexed ranges, requested-range coverage, embedding coverage, transcript timestamps, and sequence-boundary intersections. `before_sequence` remains fail-safe: only whole chunks ending before the boundary participate in retrieval. If a chunk straddles the requested boundary, it is excluded rather than leaking later text into an earlier-history query, and the exclusion is reported explicitly so Atlas cannot mistake that search for exhaustive coverage.

Historical qualifiers such as *first*, *last*, *before*, *after*, *earliest*, and *latest* are separate claims. Structural coverage is necessary to reason about them but does not make semantic top-k retrieval exhaustive. Unless exact canonical evidence establishes the stronger chronology, Atlas should say "the earliest matching exchange I found" rather than upgrading that into "the first occurrence." Supported parts of an answer should still be returned while unsupported qualifiers are stated as uncertain.

This is intentionally a bounded trustworthiness milestone. It adds no confidence service, mandatory classifier, claim database, second inference call, or automatic memory promotion. Behavioral/adversarial evaluation should drive any further retrieval changes. The acceptance set includes false-premise recall, ambiguous twin events, chronology with incomplete structural coverage, a high-similarity near-match that does not answer the question, partial support where only one qualifier is uncertain, and claims about tool use that must be checked against the underlying observation. Success means useful supported details are answered, unsupported clauses are qualified, and absent evidence is not converted into remembered fact. Live source-authority testing passed this contract before owner-directed durable memory was added.

## 11. Durable owner-directed memory — 2026-09-08

Atlas now has an explicit canonical memory layer for owner-directed `remember`, `correct`, and `forget` instructions. Ordinary conversation is not promoted automatically. The conversational model interprets the owner's instruction and calls a bounded memory capability; deterministic runtime owns persistence, precedence, lifecycle state, and retrieval constraints.

`durable_memories` stores active owner-directed memories plus superseded and forgotten records. `memory_commands` is a durable operational ledger for explicit memory mutations with `pending`, `applied`, and `failed` state, source transcript/turn provenance, target/replacement identities, and timestamps. A failed mutation remains inspectable rather than disappearing as an inferred model-side state change.

`memory.remember` creates a concise self-contained active record and is idempotent for the same normalized active content. Re-remembering previously forgotten identical content is an explicit owner reversal: Atlas creates a new active record and clears the old recall guard without rewriting historical records.

`memory.correct` supersedes an active record, or creates an owner correction guard when the corrected information exists only in legacy transcript history. The replacement becomes active owner-authoritative memory; the old value remains canonical evidence but is no longer eligible to win recall.

`memory.forget` marks an active record forgotten, or creates a tombstone when the target exists only in transcript history. Forgetting never edits the canonical transcript. Instead, active correction/forget guards constrain transcript search before lexical/semantic ranking through a database-side anti-join whose query shape does not grow with the number of guards. Later working-context assembly also redacts guarded exact content from owner/model/tool history, and the provider tool loop removes newly forgotten content from subsequent reasoning rounds in the same foreground turn. A successful forget is acknowledged without repeating the forgotten value.

Durable active memories participate in `memory.search` ahead of transcript interpretation and are labeled as owner-authoritative state. They reuse the existing 1,536-dimensional background embedding pipeline. Only active durable rows are embedding candidates; superseded/forgotten records retain canonical audit state but their derived vectors are cleared and are not re-embedded or returned as active memories. Automatic memory promotion from ordinary conversation is still intentionally absent.

## 12. Cross-chat continuity handoffs — 2026-09-08

Multi-chat changes the role of summarization. A fresh owner chat should not inherit another chat's verbatim transcript, but it still benefits from a small amount of recent orientation. Atlas therefore derives revisioned continuity handoffs for closed owner chats in the background memory-maintenance pass. A first handoff summarizes at most the most recent configured source turns; later revisions combine the prior handoff with only newly appended canonical turns. Each revision records the transcript and exact sequence range it covers.

A handoff is derived orientation, not durable owner memory and not canonical evidence. The foreground context assembler may include the latest handoff from up to three recent other chats, explicitly labels that material as non-canonical, and removes it before trimming current-chat history when the working-context budget is under pressure. Exact historical claims still require `memory.search` and, where needed, canonical evidence reads.

Recall guards are applied before source material is summarized and again before a handoff is projected. Because a paraphrased summary cannot be made safe by exact-string redaction alone, a new correction or forget guard invalidates all derived continuity handoffs so the worker can rebuild them from canonical source material under the current suppression policy. Re-remembering content that clears a prior guard also invalidates the handoffs so restored information is not permanently omitted from derived orientation.

This is deliberately separate from automatic long-term memory curation. The continuity summarizer answers only "where were we / what were we doing?"; it does not decide which ordinary conversation facts should become durable memory.
