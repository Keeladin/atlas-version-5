# Atlas Version 5

Atlas V5 is a model-led, single-owner personal agent runtime. It is a clean architectural restart of Atlas and is currently deployed at `atlas-agentic.co.za`.

The model is the primary semantic decision-maker: it interprets intent, selects capabilities, adapts to results, and decides when work is complete. The runtime owns deterministic concerns such as persistence, execution, authority, scheduling, authentication, environment boundaries, and observability.

## Current implementation — 2026-09-07

- **Provider:** OpenAI Responses API with provider-native web search and multimodal resource perception. Atlas owns the canonical transcript and uses `store=False` so provider state is not the continuity source.
- **Progressive capabilities:** the model receives a compact capability-family index and discovers operations through stable search/call controls instead of receiving the full tool catalog. Dispatched calls are bounded per model turn, with capacity reserved for completing preview/apply workflows.
- **Durable conversation:** PostgreSQL stores owner, Atlas, and structured tool-evidence turns without trimming the canonical transcript for inference cost. Model input is a bounded working projection: up to the latest 10 owner/Atlas exchanges inside a 64k-token operating budget, with successful tool evidence compacted and older exchanges dropped only when the budget requires it.
- **Passkey authentication:** production access is protected by WebAuthn with required user verification and server-side sessions. API routes, files, and control surfaces sit behind the same owner session boundary.
- **Consequential actions:** approval binds the exact operation, canonical arguments, owner principal, capability identity, creation time, and expiry. Actions are durably marked `executing` before dispatch; confirmed, failed, and uncertain outcomes remain distinct.
- **Reconciliation:** stale executing actions are moved to `uncertain` with durable owner attention rather than being silently replayed or reported as failed.
- **Scheduling:** one-time, interval, and cron tasks are supported. Scheduled runs use dedicated scheduled transcripts and cannot become or contaminate the foreground owner conversation.
- **Google Workspace:** Drive browsing and resource acquisition, Gmail search/read/send, and Calendar read/create/update/delete are integrated. Consequential mail and calendar mutations require owner approval.
- **GitHub:** the official GitHub MCP server is used over stdio with dynamic discovery. The current server boundary is read-only.
- **Local storage:** Atlas can browse and acquire files from the approved workspace.
- **Project folders:** Atlas can inspect repositories and perform bounded single-file create/update, preview unified diffs, detect stale files with SHA-256, write atomically, preserve file modes, checkpoint dirty Git state, and move files within a project. Deletes require approval. Protected paths and secret/key material are hidden from normal project browsing and rejected for both normal project reads and writes, including resolved symlink targets.
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
