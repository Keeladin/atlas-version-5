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

When a task finishes, this checkpoint may later provide useful source material to the asynchronous memory pipeline, but it is never automatically promoted to durable memory. As of 2026-09-09, the foreground model may opportunistically emit a bounded set of **non-authoritative memory candidate hints** inside hidden runtime metadata in the same inference as the owner-visible reply. Runtime only validates, provenance-binds, deduplicates pending equivalents, and queues those hints; reconciliation, reclassification, merge/supersession, and publishing remain background work. If no hint is emitted, canonical transcript history remains sufficient input for later memory processing.

The initial operating policy keeps up to the latest 10 owner/Atlas exchanges inside a 64k-token working budget. Owner and Atlas text remains verbatim while it fits. If the selected window exceeds budget, Atlas first compacts older successful/read-heavy tool observations into small structural evidence records while retaining their full canonical observations in PostgreSQL; it then compacts other successful tool evidence and, only if necessary, drops the oldest exchanges. Failed, uncertain, prepared, or otherwise unresolved evidence is protected from normal compaction. The current owner turn is never silently truncated merely to satisfy the working budget.

A context capsule may still provide orientation across transcript eras, and future retrieval can inject older exact transcript, memory, or indexed knowledge selectively. Bounded working context therefore controls model cost and focus without becoming a retention policy for canonical history.
## 4. Short-term memory

Owner chat transcripts remain durable until the owner deletes the chat. A chat that is not currently selected is eligible for complete transcript indexing; selecting it again does not require replaying other chats into its foreground context.

Short-term retrieval is indexed so Atlas does not have to read many chat transcripts sequentially. Retrieval combines transcript identity/provenance with lexical/full-text and semantic/vector search. Deleting a chat removes that canonical transcript and its derived transcript indexes; separately promoted durable memories remain independent records unless the owner explicitly retires or deletes them.

The capsule can act as a cheap first-stage locator. Atlas drills into exact transcript chunks only when precision is needed.

## 5. Durable memory substrate

PostgreSQL is the intended durable memory substrate from the beginning rather than an interim SQLite store. The reason is architectural: V5 expects concurrent foreground/background access, durable transactions, database-role separation, and hybrid structured/full-text/vector retrieval in one long-lived substrate.

PostgreSQL holds canonical records, transcript/index metadata, provenance, relationships, retention state, and structured memory. pgvector supplies semantic vector search. PostgreSQL full-text/lexical search and ordinary SQL/metadata filtering handle exact identifiers and structured retrieval.

Retrieval is therefore hybrid. Vectors help Atlas find memory; vectors are not the memory itself.

Large artifacts such as images, PDFs, documents, audio, or generated files remain in file/object storage. PostgreSQL stores artifact metadata, hashes, provenance, relationships, and storage references rather than making the database a blob store by default.

## 6. Durable memory writes

The active model is not the normal writer to durable memory. Its natural write surface is the transcript.

A separate memory processor reads closed or aging transcripts and may discard information, retain it temporarily, promote it to embedded long-term recall, preserve it as canonical durable memory, or merge/supersede an existing memory.
Explicit owner instructions such as "remember this", "correct that", "stop using that", or "delete that" remain transcript events, but runtime also records them as durable memory commands with an observable pending/applied/failed lifecycle. Owner corrections, retirement and deletion take precedence over stale derived memory and must not be resurrected by queued processing or older indexes.

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

At this checkpoint this was still a retrieval substrate rather than durable interpreted memory. Subsequent milestones below add explicit owner-directed remember/correct/retire/restore/delete commands, continuity capsules, and bounded candidate-driven semantic reconciliation. Visual attachment indexing and OEM-manual retrieval remain staged; a bounded incremental discovery sweep arrives with the `25a13` milestone below, while whole-history consolidation remains staged. Derived lexical/vector indexes remain rebuildable from canonical transcript history.

## 10. Evidence-grounded historical recall — 2026-09-08

Hybrid retrieval is a locator, not a truth oracle. A relevant chunk establishes only that canonical transcript material was retrieved; it does not by itself establish every claim the model may wish to make about the past. Atlas therefore treats retrieval relevance and evidence sufficiency as separate questions. The conversational model remains responsible for semantic judgment, while runtime reports only deterministic facts about source identity, ordering, indexing coverage, and exact observations.

`evidence.read` now returns a structural envelope alongside the bounded exact payload: transcript ID, sequence, actor, evidence kind, timestamp, and tool operation/phase/action/provenance where applicable. This lets Atlas distinguish an owner statement, a prior Atlas/model statement, an owner attachment, and a runtime tool observation. A prior Atlas response proves what Atlas said; claims that an external tool was actually used or an effect actually occurred should be checked against the corresponding observation when practical.

`memory.search` coverage now reports canonical sequence ranges, the index checkpoint, active-tail/unindexed ranges, requested-range coverage, embedding coverage, transcript timestamps, and sequence-boundary intersections. `before_sequence` remains fail-safe: only whole chunks ending before the boundary participate in retrieval. If a chunk straddles the requested boundary, it is excluded rather than leaking later text into an earlier-history query, and the exclusion is reported explicitly so Atlas cannot mistake that search for exhaustive coverage.

Historical qualifiers such as *first*, *last*, *before*, *after*, *earliest*, and *latest* are separate claims. Structural coverage is necessary to reason about them but does not make semantic top-k retrieval exhaustive. Unless exact canonical evidence establishes the stronger chronology, Atlas should say "the earliest matching exchange I found" rather than upgrading that into "the first occurrence." Supported parts of an answer should still be returned while unsupported qualifiers are stated as uncertain.

This is intentionally a bounded trustworthiness milestone. It adds no confidence service, mandatory classifier, claim database, second inference call, or automatic memory promotion. Behavioral/adversarial evaluation should drive any further retrieval changes. The acceptance set includes false-premise recall, ambiguous twin events, chronology with incomplete structural coverage, a high-similarity near-match that does not answer the question, partial support where only one qualifier is uncertain, and claims about tool use that must be checked against the underlying observation. Success means useful supported details are answered, unsupported clauses are qualified, and absent evidence is not converted into remembered fact. Live source-authority testing passed this contract before owner-directed durable memory was added.

## 11. Explicit owner memory lifecycle — 2026-09-09

Owner-directed memory uses `memory.remember`, `memory.correct`, `memory.retire`, `memory.restore`, and `memory.delete`, with a pending/applied/failed command ledger. `memory.forget` is absent from the registry. Correction supersedes a prior claim; retirement excludes retained content from recall; deletion removes selected payloads while preserving stable identities and relationships. Deleted identities are terminal.

Current search admits only applicable active records. Explicit `include_historical=true` also searches superseded memories; retired/deleted records remain excluded. Owner-directed and derived authority are projected separately. Owner writes share the `memory_state/owner` revision fence, which future reconciliation must also use with an expected revision.

Migration `25a11` introduces lifecycle/classification fields, provenance edges, deletion receipts and database payload constraints. Source-inclusive deletion redacts safely isolated exact passages, scrubs recorded candidate/provider-evidence dependencies and legacy command payloads, invalidates summaries and rewinds transcript indexing to a safe chunk boundary. `memory_only` retains source text. Artifact bytes, backup/WAL erasure and exhaustive paraphrase removal are not implemented.

See [Memory lifecycle](13-memory-lifecycle.md#10-owner-memory-lifecycle--implemented-2026-09-09) for operation semantics, transaction boundaries and limitations. Candidate-driven semantic publication is implemented in the `25a12` reconciliation milestone below; the `25a13` milestone routes explicit remember/correct through the same verification pipeline. Whole-history consolidation remains staged.

## 12. Cross-chat continuity handoffs — 2026-09-08

Multi-chat changes the role of summarization. A fresh owner chat should not inherit another chat's verbatim transcript, but it still benefits from a small amount of recent orientation. Atlas therefore derives revisioned continuity handoffs for closed owner chats in the background memory-maintenance pass. A first handoff summarizes at most the most recent configured source turns; later revisions combine the prior handoff with only newly appended canonical turns. Each revision records the transcript and exact sequence range it covers.

A handoff is derived orientation, not durable owner memory and not canonical evidence. The foreground context assembler may include the latest handoff from up to three recent other chats, explicitly labels that material as non-canonical, and removes it before trimming current-chat history when the working-context budget is under pressure. Exact historical claims still require `memory.search` and, where needed, canonical evidence reads.

Recall guards are applied before source material is summarized and again before a handoff is projected. Because a paraphrased summary cannot be made safe by exact-string redaction alone, a new correction or retirement guard invalidates all derived continuity handoffs so the worker can rebuild them from canonical source material under the current suppression policy. Re-remembering content that clears a prior guard also invalidates the handoffs so restored information is not permanently omitted from derived orientation.

This is deliberately separate from automatic long-term memory curation. The continuity summarizer answers only "where were we / what were we doing?"; it does not decide which ordinary conversation facts should become durable memory.

## 13. Foreground memory-candidate hint intake — 2026-09-09

The conversational model may append one hidden `<atlas_runtime>{...}</atlas_runtime>` envelope to its normal response. The envelope can contain the existing semantic `task_state_delta` and an optional `memory_candidates` array; neither is rendered to the owner. This unifies hidden semantic side-channel metadata without making provider output canonical runtime state.

Each memory candidate is a bounded proposal with `kind`, `content`, `scope`, `confidence`, `durability`, `proposed_action`, and optional `subject`, `namespace`, and `evidence`. Runtime does **not** accept model-supplied source IDs. It attaches the exact owner transcript, owner turn, and provider-evidence turn itself, then stores the candidate as `pending` in PostgreSQL.

The intake distinction is deliberate: `identity` and durable preferences may be suggested as long-term cross-chat candidates; project implementation state normally belongs in project scope; deployments, machine breakdowns, applications, travel, housing, and similar circumstances should remain short-term/project state or transcript history. The prompt explicitly discourages a growing monolithic `user_profile` blob.

Candidate confidence means only "how strongly the model believes the owner conveyed this candidate." It is not authority to persist the content as active memory. Pending/leased/retained-short-term candidates are not returned by `memory.search` and do not affect context assembly. Exact active candidate duplicates are collapsed deterministically across `pending`, `leased`, and `retained_short_term` states.


## 14. Candidate-driven reconciliation — 2026-09-09

Migration `25a12` adds the first bounded asynchronous semantic reconciler. The five-minute memory maintenance service may claim a small batch of eligible candidates with durable lease tokens, evaluate them against a consistent snapshot of canonical source evidence, applicable active memories, lifecycle restrictions, and the current `memory_state/owner` revision, then choose one semantic outcome: `discard`, `retain_short_term`, `create`, `equivalent`, `merge`, or `supersede`; `25a13` adds `conflict`, `historical_predecessor` and `ground_legacy`. The production host configuration enables this bounded loop; the library default remains disabled so generic/test installs opt in deliberately.

A lease is fenced by an opaque token as well as expiry. If a worker stalls and another worker reclaims the candidate, the old worker cannot publish even when the owner-memory revision is unchanged. Expired attempts are closed in the reconciliation ledger; attempt counts are bounded. A newly reasoned attempt receives a new operation identity, while transport replay of the same concrete publication reuses its operation ID through `SharedStateWriter`.

Evaluation reads occur under a short `REPEATABLE READ` snapshot; no database transaction is held while the model reasons. Publication rechecks the lease, candidate eligibility, canonical transcript `content_revision`, source-turn availability, and expected `memory_state/owner` revision in the protected write transaction. Owner remember/correct/retire/delete changes therefore win over in-flight derived inference.

Derived publication is a separate write path. It writes `record_kind=derived`, preserves candidate kind/scope/scope-key/durability/subject/namespace, may only merge or supersede an active **derived** memory from the evaluated snapshot, and cannot restore retired memory, clear owner suppression, or impersonate owner-directed authority. `equivalent` names an existing applicable memory; if the exact source lineage is new, provenance changes and the memory revision advances, otherwise storage reports a true `no_change`. Distinct turns are preserved as provenance, not automatically treated as independent confidence evidence.

`retain_short_term` does not create a second searchable memory representation. The candidate remains non-authoritative with `review_after` and `expires_at`; ordinary transcript retrieval remains the recall source. Unresolved project scope cannot publish durable memory until runtime has a canonical project identity.

Canonical transcripts now carry monotonic `content_revision`. Appends and owner-directed redactions advance it. Lexical chunks and continuity capsules record the revision they read; continuity publication is rejected if the source changed, and stale capsules are excluded from projection. Transcript embeddings update only the exact chunk/source revision they read, while durable-memory embeddings update only a still-active memory with the same fingerprint. These fences prevent a pre-deletion maintenance worker from republishing removed content after the deletion transaction wins.

## 15. Read-only live memory observability — 2026-09-10

Control now exposes a bounded owner-only inspection surface for the memory pipeline. The initial view is a canonical PostgreSQL snapshot of candidate counts, recent candidates, reconciliation attempts and recent durable memories. Selecting a candidate follows its canonical source turn, structured candidate metadata, content-free reconciliation attempt ledger, resulting/linked memory and provenance. Deleted source text remains excluded, and the model's free-form reconciliation reason remains intentionally ephemeral rather than being persisted for the UI.

After the initial snapshot, the same Control panel opens a same-origin Server-Sent Events stream. The API does not depend on an in-process worker event bus because memory maintenance runs in a separate systemd process; instead, it polls a compact fingerprint of durable PostgreSQL memory state and emits a fresh bounded snapshot only when that fingerprint changes. Each event carries the fingerprint as its SSE event ID, so browser reconnects resume from the last observed canonical state. Periodic keep-alives preserve idle connections, while an ordinary refresh remains available as a fallback.

This is observability, not authority. The stream does not add memory edit/approve controls, does not make candidates searchable, and does not change reconciliation policy or publication semantics. Its purpose is to let the owner observe real candidate → reconciliation → durable-memory traffic before tuning model policy, thresholds or model choice.

The systemd memory pass also publishes a bounded machine-local observer snapshot at `/var/lib/atlas-v5/observer/memory.json`. It is built from the same read-only Control projections, includes recent candidate details and structured verification/reconciliation state, does not read or serialize Atlas credential/config stores, and is atomically replaced after each pass. The deployment grants the maintenance owner read-only ACL access to this observer directory only; protected auth/control state and `/etc/atlas-v5` remain outside that path.

## 16. Memory state machine, discovery sweep and conflict resolution — 2026-09-11

Migration `25a13` makes grounding explicit. Every durable memory carries `grounding_status`; ordinary recall and embedding admit only `verified` rows, and a verified row must reference either a reconciliation record or a canonical owner assertion turn. All memories that predate the migration become `legacy_unverified` with origin `legacy_pre25a13`, and each active one receives a pending `memory_review` obligation. Immediately after migration ordinary durable recall is therefore empty by design until the owner reviews a memory (confirm, edit or reject through `memory.obligations.resolve`) or the reconciler grounds it from fresh evidence. There is no bulk confirmation.

The graph-relation contract defines semantic identity by the supported proposition rather than classification metadata: differences in kind, scope, durability, subject, namespace or wording do not by themselves make a new memory, while genuinely different temporal meaning remains significant.

Explicit `memory.remember` and `memory.correct` no longer write durable memory directly. They bind the owner's turn as canonical evidence, queue an explicit candidate through the verification pipeline, and open an `explicit_remember`/`explicit_correct` obligation that resolves in the same transaction as the candidate's terminal outcome. The owner is told the request is queued, never that it is remembered, until the outcome reports publication; outcomes since the last reply are injected into the next foreground turn. A blind evidence reading also classifies the claim principal: an owner's request to store an external claim does not make it an owner fact, so such claims still require confirmation. An explicit correction naming its target is structural owner intent and may supersede an owner-directed row; the evidence-horizon guard still applies and derived candidates still conflict.

The background discovery sweep is a bounded incremental scan, not a whole-history consolidation. One `memory_discovery_state` cursor per owner transcript records the last scanned turn. Each five-minute pass takes up to four transcripts with live unscanned turns, scans at most twelve turns after the cursor, shows up to four earlier turns as citeable context, mints the same ephemeral evidence handles the foreground uses, anchors every candidate on the last scanned turn and records the window's latest `created_at` as its temporal horizon. Each proposal is validated individually at intake, so an out-of-schema item rejects only itself; only spelling variants of protocol tokens are folded (case, whitespace, hyphens, and `global` for `cross_chat`), while confidence words, semantic synonyms and unsupported kinds are rejected and counted by category in the maintenance output. The verifier stages apply the same rule to their own outputs: optional classification hints outside the schema are dropped and counted, over-long claim lists are truncated to eight and counted, and an invalid verdict or relation fails the attempt. The cursor advances by compare-and-set only when it is unchanged and no scanned turn was purged meanwhile; failed or stale attempts rotate the transcript to the back rather than pinning a slot, and owner purges rewind the cursor together with the lexical index. Runs are not leased: a concurrent manual run loses the compare-and-set at worst and intake deduplicates its candidates.

A memory conflict is raised when a supported claim disagrees with an active memory and event order cannot settle it, or when a derived claim would supersede an owner-directed row. Neither wording is uniquely current until the owner decides. `memory.search` annotates the target with the competing claim, and open conflicts plus the legacy review backlog are injected into foreground context so the model states both wordings and asks. The owner resolves through `memory.obligations.resolve` under a `review_version` that covers both wordings and the conflict, target and candidate state: `keep_current` retains the current memory and rejects the competing candidate; `accept_competing` and `restate` queue an explicit owner correction of the target in the same transaction and never write durable memory directly. Expired conflicts stay visible and resolvable.
