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
