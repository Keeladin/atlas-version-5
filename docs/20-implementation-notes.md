# Atlas V5 Implementation Notes

## 2026-09-05 — Phase 0 started

Implementation began from the accepted baseline, Runtime Constitution and implementation plan.

### Port adjustment

The proposed V5 port `8085` was found to be occupied by the existing `ac-worldserver` Docker container. V5 therefore uses `127.0.0.1:8086` for development and the planned systemd/Caddy cutover.

This is an operational tuning change allowed by the implementation plan, not an architecture amendment. V4 remains on `127.0.0.1:8080` and its live Caddy route is untouched during development.

## 2026-09-06 — Phase 0 host foundation deployed

The privileged host bootstrap completed successfully with native PostgreSQL 18.6 and pgvector 0.8.1, a dedicated `atlas-v5` system identity, protected state/config paths, and a generated database credential stored only at `/etc/atlas-v5/secrets/database-url`.

Security checks confirmed that the normal `jaco` account cannot read the database credential or traverse the production artifact store. V5 therefore keeps the runtime secret boundary intact instead of relying on developer-user access.

The first production deployment then copied the verified build into `/opt/atlas-v5/app`, created a production virtual environment, applied the Phase 0 Alembic migration, and enabled `atlas-v5.service` on `127.0.0.1:8086`.

The initial deploy script exposed one filesystem-ordering bug: `frontend/dist` was copied before its parent existed. The script was corrected to create all deployment subdirectories first and was re-tested against a clean temporary deployment layout before rerun.

## 2026-09-06 — Phase 0 verified and cut over

The final Phase 0 deployment verified the production service restart path, the PostgreSQL-backed health endpoint, the Atlas frontend, and the direct `/control` SPA route. The deployment script waits for `/api/health` after restarting systemd so the normal Uvicorn startup window cannot be mistaken for a failed deployment.

The production Caddy route for `atlas-agentic.co.za` was backed up, changed only from `127.0.0.1:8080` to `127.0.0.1:8086`, validated, and reloaded. Public HTTPS checks returned HTTP 200 for `/` and `/control`, and `/api/health` reported production status `ok` with PostgreSQL healthy. `www.atlas-agentic.co.za` continues to redirect to the canonical hostname.

V4 remains running on `127.0.0.1:8080` as an immediate rollback target. Morning, Construction, n8n, and Spares Caddy routes were inspected after cutover and remained unchanged.

Phase 0 is therefore complete as the deployed runtime foundation. Phase 1 begins with the model/request boundary and should incorporate the current OpenAI implementation guidance gathered before adapter work: minimal seat context, structured request hints outside prompt text, capability discovery separated from executable tool exposure, provider-native continuation semantics, and deterministic runtime/UI actions that do not require inference.

## 2026-09-06 — Phase 0 interface shell refined

Before Phase 1 inference work, the Atlas shell was reshaped around the accepted product surfaces rather than extending the original three-card prototype.

The main Atlas view now uses a thin persistent status rail, a structural left navigator, a full-height chat canvas, and a floating right activity surface for `Needs You` and `Latest`. The centre canvas uses a subtle top-left-to-bottom-right dark gradient while the left navigator and floating activity surface use lighter muted dark surfaces to preserve depth.

The frontend design tokens were consolidated around the dashboard reference supplied during review: compact 12–14 px interface typography, Mona Sans-compatible system fallbacks, restrained semibold emphasis, `#010409` / `#0d1117` / `#151b23` depth layers, and `#3d444d` structural borders. No remote font dependency was introduced so the local-first boundary remains intact.

The left navigator now reserves truthful Phase 0 locations for Projects, local and Drive storage, project folders, repositories, artifacts, and a Normalization utility with unset Input and Output folders. These are presentation affordances only; no fake integrations or Phase 1 capability behavior was added.

## 2026-09-06 — Phase 1 centre became operational

The OpenAI Responses adapter is now live with Atlas-owned transcript continuity. Owner turns are persisted before inference, the complete active canonical transcript is supplied on each turn, and Atlas responses are persisted after completion. Provider-side storage remains disabled so the provider is replaceable and Atlas remains the continuity owner.

Capability execution now uses progressive disclosure rather than exposing the full environment to the model. Atlas receives a compact capability-family index plus stable `atlas_capability_search` and `atlas_capability_call` controls. The runtime resolves discovered operations generically, separates availability/exposure from authority, and records automatic executions or durable approval proposals.

The first resource path is also generic: local files can be acquired as typed resources with provenance, raw bytes are kept out of ordinary tool-result context, and supported images/documents are routed into provider-native perception. This was verified end-to-end with an owner image in the shared workspace.

Google Workspace is provisioned through an encrypted OAuth bundle and the local `gws` bridge. Drive browsing is active. Gmail search/read operations are exposed as automatic reads, while `gmail.message.send` is approval-required and resumes the exact prepared recipient/subject/body after owner approval.

GitHub is the first dynamic MCP integration. Atlas launches the official GitHub MCP server over stdio, discovers its enabled operations at runtime, namespaces them into the Environment Registry, and exposes them through progressive capability search. The initial MCP server is deliberately read-only. Live verification successfully listed public/private repositories and read repository structure/content.

Provider-native web search is enabled alongside Atlas capability controls. Current-information requests can therefore be resolved by the model without a separate scripted browsing workflow. Web use is model-selected rather than owner-commanded.

The main shell now includes the owner-approved shared `/home/jaco/Workspace` surface, Drive browsing, a floating activity/approval rail, `Home` and `Control` navigation in the top rail, and discreet runtime/database/model status. A transcript context-pressure indicator uses OpenAI's own input-token counting endpoint against the configured one-million-token context budget, with green/amber/red state for healthy, pressure and rollover-near conditions.

The active design rule for continuity is: **transcript rollover is context-pressure driven, with semantic handoff, not date driven**. Capability availability is also explicitly dynamic: before declaring an environment capability unavailable, Atlas must consult the live capability registry rather than relying on prior turns or repository state.

At this checkpoint Ruff passes, the frontend production build passes, and the backend suite contains 23 passing tests.
