# Atlas Version 5

Atlas V5 is a model-led, single-owner personal agent runtime. It is a clean architectural restart of Atlas and is currently deployed at `atlas-agentic.co.za`.

The model is the primary semantic decision-maker: it interprets intent, selects capabilities, adapts to results, and decides when work is complete. The runtime owns deterministic concerns such as persistence, execution, authority, scheduling, authentication, environment boundaries, and observability.

## Current implementation — 2026-09-08

- **Provider:** OpenAI Responses API with provider-native web search and multimodal resource perception. Atlas owns the canonical transcript and uses `store=False` so provider state is not the continuity source.
- **Progressive capabilities:** the model receives a compact capability-family index and discovers operations through stable search/call controls instead of receiving the full tool catalog. Search returns small operation cards rather than full registry descriptors, and discovery rechecks current owner enablement on every search. Owner settings persist across startup; new capabilities start disabled and can be enabled in Control. Dispatched calls are bounded per model turn, with capacity reserved for completing preview/apply workflows.
- **Durable conversation:** PostgreSQL stores sequenced owner, Atlas, and structured tool-evidence turns (large bodies and binary snapshots use immutable artifact references) without trimming the canonical transcript for inference cost. Model input is a bounded working projection: up to the latest 10 owner/Atlas exchanges inside a 64k-token operating budget. A small durable active-task checkpoint is projected ahead of ordinary history so multi-step work can survive context eviction and restarts; runtime facts are recorded deterministically, while semantic task changes ride inside the existing model response with no checkpoint-only inference. Missing/invalid deltas preserve task state, explicit completion closes it, and revision/CAS protects concurrent updates. Only one foreground inference can own the conversation at a time. Database projections fetch at most 500 recent turns; the UI pages older history. Every provider tool-loop request is counted against the input budget, retaining the task checkpoint and exact evidence locators.
- **Evidence-grounded hybrid memory:** restart-safe, provenance-backed transcript chunks are indexed with PostgreSQL full-text search and pgvector semantic similarity. `memory.search` combines owner-directed durable memory with bounded lexical/semantic transcript candidates, reports structural coverage and provenance, and treats retrieved history as evidence candidates rather than automatic truth. Explicit owner `remember`, `correct`, and `forget` commands now write canonical durable memory through an auditable pending/applied/failed command ledger. Corrections and forget tombstones constrain transcript retrieval before ranking, and guarded content is redacted from later model working-context projections without rewriting canonical transcript history. Durable records reuse the background embedding pipeline. Context capsules, automatic semantic promotion/reconciliation, visual attachment indexing, and OEM-manual ingestion remain staged work.
- **Exact evidence:** attachment snapshots, acquired resources, public web/citation events and model task deltas retain provenance. Bounded evidence capabilities replay the original bytes/text without re-fetching a changed source.
- **Passkey authentication:** production access is protected by WebAuthn with required user verification and server-side sessions. API routes, files, and control surfaces sit behind the same owner session boundary.
- **Consequential actions:** approval binds the exact operation, canonical arguments, owner principal, capability identity, creation time, and expiry. Approval displays every canonical argument, including CC/BCC and nested payloads, and binds the decision to the displayed proposal hash. Actions are durably marked `executing` before dispatch; confirmed, failed, and uncertain outcomes remain distinct.
- **Reconciliation:** stale executing actions are moved to `uncertain` with durable owner attention rather than being silently replayed or reported as failed.
- **Scheduling:** one-time, interval, and cron tasks are supported. Schedule advancement and immutable queued run creation commit atomically. Scheduled runs use dedicated scheduled transcripts and cannot become or contaminate the foreground owner conversation.
- **Google Workspace:** Drive browsing and resource acquisition, Gmail search/read/send, and Calendar read/create/update/delete are integrated. Consequential mail and calendar mutations require owner approval.
- **GitHub:** the official GitHub MCP server is used over stdio with dynamic discovery. The current server boundary is read-only.
- **Local storage:** Atlas can browse and acquire files from the approved workspace.
- **Project folders:** Atlas inspects repositories and prepares bounded UTF-8 create/update proposals, moves and deletions as downloadable change bundles. Live owner project files stay unchanged; the production Projects mount is read-only. Bundles retain original/proposed bytes, file mode, baseline SHA-256 and a diff where applicable for owner editor/VCS integration. Delete proposals still require approval; move destinations must be absent. Protected paths and resolved aliases remain excluded from browsing, snapshots and diffs. Large UTF-8 reads support bounded line ranges.

- **Interface:** React and TypeScript provide the chat/control interface, activity and approval surfaces, responsive mobile layout, Markdown rendering, attachments, visible mobile owner logout, an operational Control console for runtime health, provider/integration setup and persistent capability authority, a Control-surface API restart action, and an installable PWA. Model setup verifies the API credential first and populates its model selector from the provider's live model catalog rather than a hard-coded list.
- **Deployment:** FastAPI, PostgreSQL, Alembic, systemd, and Caddy. Production runs as the unprivileged `atlas-v5` user on `127.0.0.1:8086`, with secrets held outside the application tree. Atlas pins its application interpreter in `.python-version` and deploys that exact uv-managed Python independently of Ubuntu's system Python. Routine deployment does not recurse through owner projects; project ACL reconciliation is explicit, while Jaco receives read-only inspection access to `/opt/atlas-v5`.
- **Backup/recovery:** [Offline snapshot and empty-target restore](docs/22-backup-and-recovery.md) cover PostgreSQL, artifacts, project checkpoints/change bundles and the owner enrollment marker. Deploy stops the runtime before replacing installed files or dependencies; failure leaves it stopped. Protected credentials require a separate operator backup.
- **CI:** GitHub Actions validates the locked Python and Node dependency sets, Ruff, a clean PostgreSQL 17 migration from zero to Alembic head, the full backend test suite, frontend lint, and the production frontend build on pushes to `main` and pull requests.

## Deliberate boundaries

Atlas does not currently provide general shell or code execution. GitHub remains read-only, project changes are staged for owner integration, and consequential external effects remain owner-gated. Transcript hybrid lexical/semantic recall and explicit owner-directed remember/correct/forget semantics are implemented on PostgreSQL plus pgvector. Automatic semantic promotion/reconciliation, context capsules, visual attachment indexing, and indexed OEM-manual retrieval remain staged work.

## Repository map

- `backend/atlas/` — API, auth, actions, capabilities, integrations, memory, scheduling, storage, transcript, and runtime code
- `frontend/` — React/Vite interface and PWA assets
- `migrations/` — Alembic database migrations
- `deployment/` — host bootstrap, deployment, systemd, Caddy, and integration setup
- `tests/` — backend behavior and boundary tests
- `docs/` — accepted architecture, Runtime Constitution, implementation plan, and implementation notes

The governing documents are:

- `docs/15-pre-implementation-baseline.md`
- `docs/17-runtime-constitution.md`
- `docs/18-implementation-plan.md`
- `docs/20-implementation-notes.md`
- `docs/21-runtime-invariant-test-matrix.md`

**Status:** Phase 0 foundation complete; Phase 1 working Atlas centre operational; Phase 5 memory now provides deterministic transcript indexing, hybrid lexical/semantic recall, evidence-grounded historical verification, and explicit durable owner-directed remember/correct/forget semantics. Automatic memory promotion/reconciliation, context capsules, and visual attachment indexing remain staged.
