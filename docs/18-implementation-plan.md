# Atlas V5 Implementation Plan

Status: **Accepted implementation plan — Phase 0 complete; Phase 1 centre operational**
Date: 2026-09-05

Implementation checkpoint (2026-09-08): the model-led conversation path now supports separately named owner chats with create/switch/rename/delete controls and per-chat canonical transcripts; progressive capability runtime, generic resource acquisition, local workspace, Google Drive/Gmail, read-only GitHub MCP, provider-native web search, approval widgets, provider-token context-pressure telemetry, workspace/project operations, schedules and action reconciliation are implemented. Memory now includes deterministic transcript chunking/checkpointing, bounded hybrid PostgreSQL full-text/pgvector recall with lexical fallback, evidence-grounded historical verification, and explicit durable owner-directed remember/correct/forget commands with precedence guards. Context capsules, automatic durable memory reasoning/promotion, visual attachment indexing and OEM-manual ingestion remain staged. See `20-implementation-notes.md` for implementation evidence and recovery remediation.

## 1. Purpose

This plan translates the accepted `15-pre-implementation-baseline.md` and `17-runtime-constitution.md` into an implementation sequence.

It does not reopen the architecture. Implementation choices may be tuned as evidence appears, but any change to the accepted architecture or Runtime Constitution must be made deliberately rather than discovered silently in code.

V5 is built heliocentrically: establish a functioning model-led centre, then expand its environment outward while the runtime grows in parallel.

## 2. Current server reality

The target host is Ubuntu 26.04.1 LTS with Python 3.14.4, Node 22.18.0, npm 10.9.3, Git 2.53.0 and Caddy 2.11.4.

The host does not currently have PostgreSQL installed. Ubuntu supplies PostgreSQL 18 and `postgresql-18-pgvector`, which are the preferred native packages for V5.

Existing Atlas V4 is a user-systemd service under UID 995 (`atlas`) and listens on `127.0.0.1:8080`.

Caddy currently routes `atlas-agentic.co.za` to `127.0.0.1:8080` and redirects `www.atlas-agentic.co.za` to the canonical hostname.

V4 remains intact during V5 development. No V4 state is migrated or deleted by this plan.
## 3. Proposed technology stack

- **Backend/runtime:** Python with FastAPI and an ASGI server such as Uvicorn.
- **Frontend:** React + TypeScript, built with Vite and served by the Atlas backend as static assets.
- **Primary provider:** OpenAI through a thin Atlas-owned provider adapter.
- **Database:** native PostgreSQL 18 with pgvector.
- **Artifact storage:** filesystem-backed storage outside the Git checkout, addressed by stable artifact IDs.
- **Secrets:** protected runtime-owned storage outside model-readable database/views and ordinary tool environments.
- **Service management:** systemd.
- **Edge/TLS:** existing Caddy installation and `atlas-agentic.co.za` DNS.

The backend and built frontend share one localhost HTTP endpoint. This keeps Caddy, origin handling, streaming, cookies/auth and deployment simple.

The project should pin Python and JavaScript dependencies even though the host supplies current runtimes.

## 4. Repository and state topology

The repository should contain source, migrations, tests and deployment templates, but no live owner state or secrets.

Suggested source layout:

```text
backend/atlas/{api,runtime,providers,transcript,artifacts,registry,actions,workspace,memory,schedules,control}
frontend/
migrations/
tests/
docs/
```
Suggested live-state layout:

```text
/var/lib/atlas-v5/
  artifacts/
  runtime/
  backups/
/etc/atlas-v5/
  config/
  secrets/
```

Journald is the default runtime log sink; durable effect/action evidence belongs in PostgreSQL rather than relying on log retention.

## 5. Service identity and isolation

V5 should use a dedicated system account such as `atlas-v5`, not the existing V4 `atlas` identity.

That cleanly separates V5 state and credentials from V4 and prevents V5 from inheriting old Google/API access merely because it runs as the same Unix user.

The core runtime may read V5 secrets. Model-facing shell/code/filesystem execution must later run through a less-privileged execution boundary that cannot read those secrets or protected runtime configuration.

A system-level `atlas-v5.service` is preferred over a user service because systemd hardening, dependency ordering and service identity are clearer. The unit runs the application as `User=atlas-v5` and binds only to localhost.

The existing V4 package broker remains untouched. If V5 later needs privileged host operations, it receives its own narrow broker or explicit compatible extension rather than inheriting UID-995 authority by accident.
## 6. Development and production ports

During development V4 keeps `127.0.0.1:8080` and the live DNS/Caddy route.

V5 should use a separate localhost port, proposed as `127.0.0.1:8086`.

Developer access can use SSH/VS Code port forwarding while V5 is not yet public. A temporary staging hostname is optional, not required by the architecture.

At cutover, Caddy changes only this upstream:

```text
atlas-agentic.co.za
127.0.0.1:8080  ->  127.0.0.1:8086
```

V5 does not need to reclaim port 8080. Keeping separate ports makes rollback and historical inspection simpler.

## 7. PostgreSQL foundation

Install native PostgreSQL 18 plus pgvector and keep the database on the host system service rather than adding a Docker dependency solely for Atlas persistence.

Initial database concerns are identity, transcripts/content blocks, artifact metadata, Environment Registry state, runs/actions/evidence and migrations. Memory tables/indexes can arrive when the memory orbit begins.

Use separate database roles where boundaries differ. In particular, model-facing memory retrieval must not expose operational tables or secrets simply because the application database contains them.

Exact DDL is implementation detail and should evolve through migrations.
## 8. Phase 0 — centre skeleton

Create the source layout, dependency manifests, migration framework, test harness, frontend shell and systemd/Caddy deployment templates.

Implement the minimum Runtime-Constitution spine before any consequential external tool is enabled:

- Atlas identity and seat bootstrap;
- transcript/session and multimodal content-block model;
- artifact identity and filesystem storage;
- minimal Environment Registry;
- run/action/evidence records;
- protected configuration/secret boundary;
- health/error state and owner-attention projection;
- PostgreSQL connection/migration discipline.

Phase 0 is foundation, not a workflow. The model should become reachable as soon as the minimum centre is coherent enough to support Phase 1.

## 9. Phase 1 — the working Atlas centre

Implement the OpenAI adapter and one real Atlas conversation path.

The adapter should be thin: Atlas supplies bootstrap/context and available capabilities; the provider performs inference; Atlas owns transcript, artifacts and continuity.

The UI should support streaming responses, conversation reload, error display and multimodal attachment/output blocks from the start.

Provider conversation/response continuation may be used as an optimization, but a service restart must not destroy Atlas-owned conversational continuity.
Phase 1 is complete when the owner can:

- open Atlas in a browser;
- hold a normal streamed conversation;
- attach an image or document and have it represented as a first-class content block;
- receive provider-generated artifacts when supported;
- refresh/reopen the browser without losing the conversation;
- restart the V5 service and continue from Atlas-owned state;
- see basic model/runtime/database health and any blocking error.

This is the first point at which V5 is a real Atlas rather than scaffolding.

## 10. Phase 2 — first orbit: provider-native capability

Survey the exact capabilities of the selected OpenAI model/API at implementation time and expose useful native/provider-hosted abilities through the Environment Registry.

Prioritize capabilities that expand perception and output without introducing owner-system mutation, such as vision, document/file understanding, image generation, research/web tools and sandboxed computation where appropriate.

Each capability must have:

- clear semantic registry metadata;
- UI/content-block support for its inputs and outputs;
- provenance for returned material;
- truthful provider/tool errors;
- no hard-coded task workflow around it.

Provider-native consequential effects are not enabled unless they can satisfy the Runtime Constitution's Atlas-owned authority/evidence/recovery guarantees.
## 11. Phase 3 — external tool boundary

Add Atlas-owned tool execution and MCP only after the minimum effect spine is working.

Start with a small integration that exercises discovery, enablement, progressive disclosure, exact results and hard isolation. Filesystem/Git is a better boundary test than immediately rebuilding every connected service.

Capability families remain descriptive Control/registry groupings. The executable identity remains the real MCP/provider/local operation.

Dynamic capability exposure is a runtime invariant: the full Environment Registry is not dumped into model context. The model receives a compact family index plus a stable capability-search/call control surface. Relevant operation descriptors and schemas are disclosed on demand, so adding integrations does not require editing the conversation loop or flooding the provider request with hundreds of tools. Provider-native filtering such as OpenAI `allowed_tools` may further narrow exposure, but Atlas remains the canonical registry and authority owner.

Execution authority is separate from availability and exposure. Read-only operations may execute automatically; consequential operations remain visible and callable but become durable prepared actions when owner approval is required. The right-rail widget surface presents these prepared actions as observable operational intent with approve/cancel controls. Approval resumes the exact prepared operation rather than asking the model to re-plan it. Hard denial is reserved for unavailable or forbidden authority, not ordinary owner-gated writes.

Resource acquisition is also generic. Storage, Drive, repositories, mail attachments and future sources should acquire a typed resource with provenance rather than each inventing semantic readers. The provider adapter routes that resource into the model's native perception surface (for example image input or file input) while keeping raw bytes out of ordinary tool-result context. Source-specific tools fetch resources; the model performs the semantic understanding.

Before a mutating tool is enabled, prove:

- ON/OFF actually removes/restores callable exposure;
- shell/code execution cannot read runtime secrets;
- consequential effects receive durable action identity;
- success/failure/uncertain outcomes persist across restart;
- an uncertain action is reconciled rather than blindly replayed;
- tool results carry provenance/trust information.

After that boundary is proven, add useful integrations such as Google Workspace, GitHub, local office software and independent utilities like the normalizer without changing the agent cycle.

GitHub is the first true generic MCP exercise: Atlas launches GitHub's official MCP server over stdio, discovers its enabled tools dynamically, namespaces them into the Environment Registry, and exposes them only through capability search. The initial server is pinned and read-only, with repository/code/commit/issue/pull-request reads enabled. Adding or removing GitHub MCP tools therefore changes the discovered catalog rather than the conversation runtime.

## 12. Phase 4 — workspace maturity

Give workspaces stable identity and preserve situated state without turning them into task objects.

Add repository/directory awareness, applicable `AGENTS.md` discovery, artifact/resource references, working-directory state and the contextual projections needed by the main Atlas page.
## 13. Phase 5 — memory orbit

Implement transcript rollover, context capsules, indexed short-term memory, hybrid retrieval and long-term memory processing on the PostgreSQL/pgvector foundation.

The memory processor remains outside active conversational inference. If classification or summarization requires semantic judgment, it may use separate model inference while deterministic runtime owns queuing, persistence, precedence and write application.

Explicit `remember`, `correct` and `forget` instructions receive durable command state and precedence over stale derived memory.

Retrieval must apply source authority, supersession, tombstones and freshness constraints before relevance ranking can present something as current truth.

Chunk sizes, TTLs, ranking weights, embedding model and rollover thresholds remain configurable and should be tuned from actual use.

## 14. Phase 6 — schedules and passive automation

Add persisted intent + deterministic trigger/window after ordinary interactive execution is stable.

Each scheduled wake creates a distinct run/transcript. Current capability enablement and authority apply at execution time.

Default same-schedule overlap should be conservative until a use case proves parallel execution is safe. Missed triggers, cancellation, restart catch-up, uncertain effects and waiting-for-owner state must remain durable and visible.

Semantic conditions are evaluated by inference; deterministic event sources may wake Atlas but must not pretend to interpret meaning themselves.
## 15. Phase 7 — Control and operational maturity

Control begins small and grows only as real runtime state exists to inspect.

Initial Control should expose provider/model state, PostgreSQL/runtime health, current V5 version, recent errors and the enabled capability projection.

Later it expands to connections/credentials health, memory commands and retention, schedules/runs, workspace/storage state, usage/cost, action/effect evidence and deeper diagnostics.

The main Atlas page keeps a compact `Needs You` projection for blocked, failed, uncertain or waiting-for-owner work. Control remains the engineering surface, not the normal task path.

## 16. systemd deployment shape

Proposed units:

- `postgresql.service` — native PostgreSQL 18 managed by Ubuntu;
- `atlas-v5.service` — core FastAPI/runtime/static frontend service running as `atlas-v5`;
- optional later workers/brokers only when an actual boundary or workload justifies a separate process.

`atlas-v5.service` should start after the network and PostgreSQL are available, bind `127.0.0.1:8086`, restart on recoverable failure, use a restrictive umask and apply appropriate systemd hardening without blocking required artifact/state access.

Do not split the runtime into many daemons merely because the architecture has many conceptual faculties.
## 17. Caddy and DNS cutover

Development leaves the existing `atlas-agentic.co.za -> 127.0.0.1:8080` route untouched.

When the V5 centre has passed its deployment checks:

1. confirm V5 is healthy on `127.0.0.1:8086`;
2. stop the V4 Atlas API service but preserve its files/state/unit;
3. back up the active Caddyfile;
4. change only the Atlas reverse-proxy upstream from `127.0.0.1:8080` to `127.0.0.1:8086`;
5. validate the Caddy configuration and reload Caddy;
6. verify HTTPS, streaming, attachments, refresh/reconnect and mobile access through the existing hostname.

Rollback is the inverse: restore the Caddy upstream to 8080, reload, and restart V4. V4 should not be deleted merely because V5 becomes primary.

No DNS change is required for the final cutover because the existing hostname already terminates at this Caddy instance.

## 18. Deployment acceptance for the centre

Before the Caddy cutover, prove that a clean server/service restart results in PostgreSQL and Atlas returning to healthy state without manual repair.

Verify that conversation state survives restart, artifacts remain resolvable, secrets do not appear in logs/transcripts/tool output, database migrations are reproducible, and the UI reports a provider/database failure truthfully rather than hanging.

Backups for PostgreSQL and artifact state must have a documented restore path before V5 is treated as the primary Atlas.
## 19. Implementation discipline

Coding should follow the accepted architecture rather than use the implementation to rediscover product semantics.

For each new capability, ask:

- does this extend what the model can perceive, reason about or do?
- is semantic choice still with inference?
- does runtime preserve the Constitution's isolation, evidence and recovery rules?
- are we using an existing provider/tool/software capability before inventing Atlas machinery?
- is the new state canonical, derived, temporary or an artifact, and is it stored accordingly?

Tests should validate the implementation contract and failure modes of capabilities that exist. They should not become a hidden workflow specification.

## 20. First coding sequence after this plan is accepted

1. Create backend/frontend package skeletons and dependency manifests.
2. Install/configure PostgreSQL 18 + pgvector and establish migrations.
3. Create V5 service/state identities and protected directories.
4. Implement transcript/content-block/artifact/run foundations.
5. Implement the minimal Environment Registry and seat bootstrap.
6. Implement the OpenAI streaming adapter.
7. Build the first Atlas chat UI and minimal Control health view.
8. Prove refresh/restart continuity and multimodal attachment handling.
9. Add provider-native capabilities outward from the working centre.

No V4 shutdown, Caddy change, DNS change or production-state migration occurs during these first coding steps.
