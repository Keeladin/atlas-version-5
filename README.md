# Atlas Version 5

Atlas V5 is a model-led, single-owner personal agent runtime. It is a clean architectural restart of Atlas and is currently deployed at `atlas-agentic.co.za`.

The model is the primary semantic decision-maker: it interprets intent, selects capabilities, adapts to results, and decides when work is complete. The runtime owns deterministic concerns such as persistence, execution, authority, scheduling, authentication, environment boundaries, and observability.

## Current implementation — 2026-09-07

- **Provider:** OpenAI Responses API with provider-native web search and multimodal resource perception. Atlas owns the canonical transcript and uses `store=False` so provider state is not the continuity source.
- **Progressive capabilities:** the model receives a compact capability-family index and discovers operations through stable search/call controls instead of receiving the full tool catalog. Search returns small operation cards rather than full registry descriptors, and identical discovery is reused within the active turn. Dispatched calls are bounded per model turn, with capacity reserved for completing preview/apply workflows.
- **Durable conversation:** PostgreSQL stores sequenced owner, Atlas, and structured tool-evidence turns (large bodies and binary snapshots use immutable artifact references) without trimming the canonical transcript for inference cost. Model input is a bounded working projection: up to the latest 10 owner/Atlas exchanges inside a 64k-token operating budget. A small durable active-task checkpoint is projected ahead of ordinary history so multi-step work can survive context eviction and restarts; runtime facts are recorded deterministically, while semantic task changes ride inside the existing model response with no checkpoint-only inference. Missing/invalid deltas preserve task state, explicit completion closes it, and revision/CAS protects concurrent updates. Only one foreground inference can own the conversation at a time.
- **Exact evidence:** attachment snapshots, acquired resources, public web/citation events and model task deltas retain provenance. Bounded evidence capabilities replay the original bytes/text without re-fetching a changed source.
- **Passkey authentication:** production access is protected by WebAuthn with required user verification and server-side sessions. API routes, files, and control surfaces sit behind the same owner session boundary.
- **Consequential actions:** approval binds the exact operation, canonical arguments, owner principal, capability identity, creation time, and expiry. Actions are durably marked `executing` before dispatch; confirmed, failed, and uncertain outcomes remain distinct.
- **Reconciliation:** stale executing actions are moved to `uncertain` with durable owner attention rather than being silently replayed or reported as failed.
- **Scheduling:** one-time, interval, and cron tasks are supported. Schedule advancement and immutable queued run creation commit atomically. Scheduled runs use dedicated scheduled transcripts and cannot become or contaminate the foreground owner conversation.
- **Google Workspace:** Drive browsing and resource acquisition, Gmail search/read/send, and Calendar read/create/update/delete are integrated. Consequential mail and calendar mutations require owner approval.
- **GitHub:** the official GitHub MCP server is used over stdio with dynamic discovery. The current server boundary is read-only.
- **Local storage:** Atlas can browse and acquire files from the approved workspace.
- **Project folders:** Atlas can inspect repositories and perform bounded single-file create/update, preview unified diffs, detect stale files with SHA-256, write atomically, preserve file modes, checkpoint dirty Git state, and move files within a project. Large UTF-8 reads can be reacquired by line range instead of flooding the active model context. Deletes require approval. Protected paths and secret/key material are hidden from normal project browsing and rejected for both normal project reads and writes, including resolved symlink targets, Git diffs and checkpoints. Pinned directory handles prevent symlink redirection, and moves never overwrite an existing destination.
- **Interface:** React and TypeScript provide the chat/control interface, activity and approval surfaces, responsive mobile layout, Markdown rendering, attachments, visible mobile owner logout, a Control-surface API restart action, and an installable PWA.
- **Deployment:** FastAPI, PostgreSQL, Alembic, systemd, and Caddy. Production runs as the unprivileged `atlas-v5` user on `127.0.0.1:8086`, with secrets held outside the application tree. Atlas pins its application interpreter in `.python-version` and deploys that exact uv-managed Python independently of Ubuntu's system Python. Routine deployment does not recurse through owner projects; project ACL reconciliation is explicit, while Jaco receives read-only inspection access to `/opt/atlas-v5`.
- **CI:** GitHub Actions validates the locked Python and Node dependency sets, Ruff, a clean PostgreSQL 17 migration from zero to Alembic head, the full backend test suite, frontend lint, and the production frontend build on pushes to `main` and pull requests.

## Deliberate boundaries

Atlas does not currently provide general shell or code execution. GitHub remains read-only, project writes are narrowly scoped, and consequential external effects remain owner-gated. Durable personal memory and indexed OEM-manual retrieval are planned work rather than completed features.

## Repository map

- `backend/atlas/` — API, auth, actions, capabilities, integrations, scheduling, storage, transcript, and runtime code
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

**Status:** Phase 0 foundation complete; Phase 1 working Atlas centre operational and being hardened before broader memory and field-knowledge work.
