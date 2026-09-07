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

## 2026-09-06 — Durable execution and transcript truth pass

The first post-review hardening pass implements the five owner-approved runtime fixes without adding a new integration.

Consequential approval proposals now bind the exact operation, canonical arguments, owner principal, capability identity, creation time and expiry into the stored target hash. Approval revalidates that payload and fails closed on mismatch or expiry. Approved actions transition atomically from `prepared` to durable `executing` and commit before external dispatch. Completed effects land as `succeeded`, known pre-dispatch failures as `failed`, and ambiguous dispatch failures as `uncertain`; per-action idempotency identities are persisted for future replay/reconciliation work, but they are not yet provider-enforced; externally returned identifiers are retained when available.

Tool activity is now durable structured transcript evidence. Capability searches, prepared calls, automatic calls, results and approved consequential outcomes are stored as `tool_observation` blocks and projected back into later model context as runtime evidence rather than owner-authored chat.

Scheduled executions now create `scheduled` transcripts rather than borrowing the active owner transcript. Owner-chat selection filters to `owner` transcripts, so background runs cannot become or contaminate the foreground conversation. Scheduled results remain available through task state and activity surfaces.

Context rollover is now pressure-driven rather than merely measured. At 70% of the configured provider context window, Atlas summarizes the older active prefix into a durable context capsule, records the exact summarized-through turn, retains the most recent 40 turns verbatim, and uses the capsule plus unsummarized tail for subsequent provider requests. The canonical transcript itself remains intact.

Validation after this pass: Ruff clean, 40 backend tests passing, frontend production build passing, frontend lint 0 warnings / 0 errors. A new Alembic head adds transcript kind and rollover state.

## 2026-09-06 — Project folders gained bounded write access

The owner-approved `/home/jaco/Projects` surface is no longer conceptually read-only. Atlas can now preview and apply targeted single-file creates/updates, rename or move a file within one project, inspect Git status/diff, and request deletion. Shell execution remains a separate capability and is not implied by project write access.

Project writes enforce canonical root confinement and symlink validation, block normal writes to `.git`, environment files, credentials/tokens, secret/key directories and private-key formats, and limit one text edit to 4 MB. Every update is previewed as an exact unified diff and returns a change token; applying it requires the same proposed content plus the expected pre-edit SHA-256. If VS Code or another process changes the file after Atlas inspected/previewed it, the write fails closed instead of overwriting the newer file.

Writes use same-directory temporary files, fsync and atomic replacement while preserving the existing file mode. Before mutation, a clean Git tree records the current HEAD as the baseline. A dirty tree creates a private checkpoint under `/var/lib/atlas-v5/project-checkpoints` containing the tracked binary diff plus copies of non-ignored untracked files, bounded by file-count and size limits. Non-Git projects receive a direct filesystem checkpoint of the affected file.

Targeted preview/apply/move operations remain automatic within those safeguards. Deletes remain owner-approval-required. Automatic project mutations now enter the durable action spine as `executing` before filesystem dispatch and land in the normal action/tool evidence trail afterward, so safe write access does not reintroduce the post-facto execution-recording gap fixed earlier in the day.

Production systemd changes the Projects bind from read-only to read/write. Project filesystem access is provisioned explicitly by `deployment/reconcile-project-access.sh`, which grants `atlas-v5` bounded ACL write access while retaining Jaco's access and default inheritance for future files. Normal deployments do not recursively rewrite `/home/jaco/Projects`. Protected/generated trees such as `.git`, `node_modules`, `.venv` and `__pycache__` are excluded from reconciliation.

## 2026-09-06 — Uncertain execution made durable and visible

`UNCERTAIN` is now a first-class run outcome rather than being collapsed back into `FAILED`. An uncertain action retains or creates unresolved owner attention, marks its run `uncertain`, and remains visible in Needs You with an explicit message that Atlas cannot confirm whether the external effect completed. The UI does not present approval/cancel controls for this state.

Consequential execution now records `execution_started_at` when entering `EXECUTING`. A reconciler runs once at startup and then periodically. With the default five-minute stale threshold it compare-and-set transitions abandoned `EXECUTING` actions to `UNCERTAIN`, raises durable owner attention, and prevents the runtime from silently treating the action as failed or replayable. The poll interval and stale threshold are runtime-configurable.

## 2026-09-06 — Runtime invariant test hardening

The test suite was expanded around runtime failure modes rather than an arbitrary coverage target. New regression coverage now directly defends action outcome separation, uncertain-action visibility, stale-execution CAS reconciliation, scheduled recurrence and owner-transcript isolation, rollover fail-open behavior, bounded tool evidence, project preview/stale/delete/move safeguards, passkey session/challenge lifecycle, and Alembic-chain integrity. The maintained matrix is `docs/21-runtime-invariant-test-matrix.md`.

## 2026-09-07 — Runtime controls and protected project reads

Mobile owner-session ergonomics now keep Log out visible beside the compact transcript token counter. Control exposes an owner-authenticated Restart API action: the API accepts the request, terminates itself gracefully, and the systemd unit uses `Restart=always` to restore service without granting the web process sudo or systemctl authority. The Control UI waits for the service to disappear and return before reloading.

The bounded project capability now applies the existing protected-material boundary to reads as well as writes. Protected directories and credential/key/token material are hidden from listings and rejected on acquisition, and resolved symlink targets are checked so a benign-looking path cannot bypass the boundary.

Validation at this checkpoint: Ruff clean, 90 backend tests passing, frontend lint 0 warnings / 0 errors, and the production frontend build passing.

## 2026-09-07 — Owner-readable uncertain actions

Needs You now enriches uncertain actions from their durable action evidence with a safe owner-readable label and target, rather than exposing only internal capability names. Project apply/move/delete actions show paths, while sensitive payloads such as file contents, hashes, tokens, and mail bodies are not added to uncertain-action summaries. Existing unresolved items gain this context dynamically.

Uncertain actions now have an explicit Acknowledge control. Acknowledgement resolves the owner-attention item without rewriting the durable action or run outcome from `uncertain`, preserving the audit truth while allowing Needs You to return to zero once the owner has reviewed the ambiguity.

## 2026-09-07 — Context statistics instrumentation

Control now exposes owner-authenticated context statistics calculated from the canonical PostgreSQL transcript without displaying transcript contents. The view compares the current model-visible input with bounded recent windows of 5, 10, 15, and 20 owner exchanges, includes intervening tool evidence, separates fixed seat/tool-definition overhead from the recent-window load, and reports transcript message/tool counts. This instrumentation is intentionally observational: it does not trim, purge, summarize, or otherwise mutate the canonical transcript and exists to tune a future bounded working-context policy from real Atlas usage.

## 2026-09-07 — Tool context footprint analysis

Control context instrumentation now measures the model-visible footprint of structured tool evidence over the most recent 20 owner exchanges. It groups observations by operation, reports exact provider-tokenizer marginal token counts with the fixed Atlas seat removed, separates observation counts across exchanges 1–10, 11–15, and 16–20, and identifies the heaviest individual observations without exposing their raw payloads. Canonical tool evidence in PostgreSQL remains unchanged. This is diagnostic input for the bounded context assembler and future evidence-compaction policy, not a retention mutation.

## 2026-09-07 — Bounded working context activated

Atlas now separates canonical transcript growth from provider-visible working context. Foreground inference selects at most the latest 10 owner exchanges and targets a 32,000-token total model-input budget, both runtime-configurable. The existing one-million-token provider context remains capacity rather than the normal operating target. PostgreSQL transcript rows are neither deleted nor rewritten by this policy.

The assembler is token-aware using the provider's input-token counter. When the 10-exchange projection exceeds budget, it first compacts successful/read-heavy tool observations older than the two most recent exchanges, then other compactable successful tool evidence, and only then removes the oldest exchange(s). Compacted evidence preserves operation, phase and safe structural metadata while the complete observation remains canonical. Failed, uncertain, prepared, executing, forbidden and unavailable evidence is excluded from normal compaction. If even the current owner turn cannot fit the target, Atlas fails open rather than silently truncating owner input.

The top-rail context indicator now reports the bounded working-context budget instead of provider capacity, while Control separately reports current model input and full-transcript input. The previous 70%-of-provider-window rollover path no longer drives ordinary foreground context assembly; transcript-era rollover and retrieval remain separate lifecycle concerns.

## 2026-09-07 — Continuous integration baseline

Atlas now has a GitHub Actions CI gate for `main` pushes, pull requests, and manual runs. The backend job uses the repository-pinned Python 3.14.7 with the locked `uv` dependency graph and a disposable PostgreSQL 17 service, runs Ruff, proves a blank database migrates through the complete Alembic chain to `head`, and runs the full backend suite. The frontend job uses Node 22 with `npm ci`, oxlint, TypeScript compilation, and the Vite production build. Workflow permissions are read-only and stale runs for the same ref are cancelled.

The CI path was exercised locally before publishing against a disposable PostgreSQL 17 container: the full migration chain completed, 100 backend tests passed, Ruff was clean, frontend lint reported zero warnings/errors, and the production frontend build completed.

The first hosted clean-checkout run exposed a repository-integrity defect that the long-lived server worktree had masked: the broad `artifacts/` ignore rule also ignored the source package `backend/atlas/artifacts/`. The ignore rules are now root-anchored to live state only, the artifact package is tracked, and ignored-source inspection reports no Python source hidden under backend, tests, or migrations.

## 2026-09-07 — Pinned Python runtime and ACL mask hardening

Atlas now pins its application interpreter through the repository `.python-version` file. CI consumes that pin directly, and host deployment installs the same uv-managed interpreter under `/opt/atlas-v5/python` before synchronizing the production virtual environment. Ubuntu's system Python remains distribution-managed and is not replaced. The initial pin is Python 3.14.7.

Project ACL reconciliation uses `setfacl -n` with explicit access masks: `m::rwx` for directories and `m::rw-` for regular files. This fixes the earlier bug where `setfacl -m` recalculated an ACL mask from inherited `group::rwx` state and could turn ordinary source files from mode 0664 into 0674, causing Ruff EXE002 failures. Reconciliation is now an explicit bootstrap/maintenance action rather than something every deployment repeats.


## 2026-09-07 — Maintenance permissions separated from deployment

The dedicated `atlas-v5` runtime identity remains in place for this trial, but routine deployment no longer walks or rewrites the owner project tree. Existing/default project ACLs continue to provide runtime access; a moved or newly introduced pre-existing project tree can be reconciled deliberately with `sudo bash deployment/reconcile-project-access.sh <project-path>`. Bootstrap performs that reconciliation once when `/home/jaco/Projects` already exists.

Production inspection is now a separate read-only concern. `deployment/grant-maintenance-access.sh` grants Jaco read/traverse ACLs only under `/opt/atlas-v5`; deployment and the Google/GitHub integration bootstrap scripts call it after writing production files. `/etc/atlas-v5` configuration and secrets are deliberately outside that grant. This keeps the service identity and systemd sandbox while removing elevation from ordinary deployed-code inspection.

Atomic project replacement is intentionally unchanged in this trial. Because the service account creates the replacement inode, an Atlas-edited file can still become owned by `atlas-v5`; changing that would require either weakening the atomic-write design or granting a narrowly scoped ownership privilege, so it remains an explicit point to evaluate rather than being hidden behind more permission repair.

## 2026-09-07 — Working-context budget trial raised to 64k

The bounded foreground history remains capped at the latest 10 owner exchanges, but the normal working-context token target is raised from 32,000 to 64,000. The change is deliberately limited to the operating budget: canonical transcript retention, exchange selection, evidence-compaction ordering, protected unresolved evidence, and owner-turn fail-open behaviour are unchanged. The higher target is a trial intended to preserve continuity during real multi-step work while tool-evidence normalization is improved, rather than forcing premature compaction during ordinary workflows.
