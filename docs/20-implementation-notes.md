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

## 2026-09-07 — Active-task continuity and same-turn evidence bounds

Foreground Atlas now maintains a small durable active-task checkpoint on the owner transcript. Runtime-owned operational facts are derived directly from structured tool/action events and reference canonical evidence rather than copying bodies, diffs or provider payloads. The model may optionally emit a hidden `task_state_delta` at the end of its normal response for semantic state only: objective, constraints, decisions, findings, open questions and next step. Runtime-owned fields are rejected from that envelope. This is a by-product of the existing inference cycle; no checkpoint-only model call was added.

The working-context assembler always projects an active checkpoint ahead of ordinary transcript history, so normal successful-tool compaction and exchange trimming cannot evict the task's objective/current state. The dedicated checkpoint projection retains semantic state until a valid model delta explicitly marks it complete; missing or invalid deltas leave it unchanged. It is operational continuity, not durable memory; background memory processing remains a later, separate orbit.

Tool-result hygiene now applies before same-turn reinference as well as to later transcript projection. Canonical tool evidence remains complete through PostgreSQL records and immutable artifact snapshots, while the provider-facing copy strips binary payloads, cleans HTML, bounds large strings/lists and marks compaction with omission metadata. UTF-8 file acquisition supports bounded line ranges, and automatic text-resource injection is capped with an explicit instruction to reacquire another range when needed.

Capability discovery now returns compact operation cards containing executable identity, effect/authority and a minimal argument contract instead of full registry descriptors. Identical discovery requests within one model turn reuse the first search rather than dispatching another registry lookup. Scheduled runs use the same compact discovery cards but do not emit foreground active-task semantics.


## 2026-09-07 — Architecture review remediation

The five priority fixes are implemented together with three linear migrations (`25a01`–`25a03`). Task updates now use a dedicated lossless semantic projector, task UUID, and revision/CAS; replacement cannot erase runtime facts. Per-transcript sequence allocation establishes canonical ordering. A rejected second-tab request preserves its unsent draft and attachments and reloads canonical history. Partial unique indexes enforce one open owner transcript and one active foreground inference. Historical turns retain their previous `(created_at, id)` display order because earlier causal order cannot be reconstructed; duplicate open transcripts are archived without deleting their history or checkpoint. Existing completed runs stay completed during migration.

Foreground and scheduled calls share `RunExecutor`. Action identity, canonical observation and finalization share transactions. Action transitions are conditional under a run lock, and run outcome is recomputed from every sibling action and inference status. Cancellation cannot rewrite an executing action. Late results append evidence without silently rewriting uncertainty. Full JSON Schema validation runs before proposal/dispatch; executor exceptions conservatively retain ambiguous dispatch status.

Protected-file policy now covers acquire, previews, writes, move/delete, Git diffs and checkpoints. Validated path aliases resolve into pinned directory handles; reads reject a newly substituted symlink. Moves use Linux `renameat2(RENAME_NOREPLACE)`. Git renders permitted file snapshots with external diff/text conversion disabled; protected tracked/untracked content is excluded from checkpoints. Ordinary UTF-8 edits, executable modes, binary diffs and permitted symlink changes remain supported. A directory changed during an operation produces an ambiguous outcome for verification rather than a false success. Independent processes that directly alter project files do not participate in Atlas's project mutation lock.

Owner attachment bytes are frozen at submission. Acquired bytes are stored once as an artifact and passed transiently to provider-native perception; PostgreSQL carries references, hashes and provenance. Observations larger than 32 KB are artifact-backed too. `evidence.read` returns exact character pages/JSON pointers; `evidence.resource.acquire` replays the original snapshot instead of reading a changed source. The task projector keeps all semantic fields plus a bounded first page of pending actions; `evidence.task.read` exposes the complete durable checkpoint at a pinned revision. Public web calls/citations and accepted/rejected deltas retain response provenance.

Schedule advancement and queued occurrence/run creation are atomic. Queued occurrences retain immutable intent, survive interruption, and are uniquely identified by schedule/time. Heartbeats and conditional recovery fence abandoned producers. An interrupted foreground inference releases its slot, retains task/action evidence, and presents a continuation notice. A new owner message resolves that notice, while uncertain effect attention remains separate. Slow synchronous capability executors, attachment snapshots and storage/control calls use worker threads.

Verification uses disposable PostgreSQL schemas only. Tests cover concurrent owner creation, canonical ordering, competing foreground runs, task CAS, interleaved updates, sibling uncertainty, stale cancellation, exact evidence replay, interruption, schedule restart and immutable intent. Migration tests exercise both empty databases and historical upgrades. Synthetic protected-file fixtures and deterministic path swaps reproduce the original boundaries; permitted Git patches are also checked with `git apply --reverse --check`.

No code deployment, production migration, or live provider effect is part of this change. Before deploying the new runtime, stop the old runtime during migration. `deployment/deploy-host.sh` now enforces this order: it stops `atlas-v5.service` before `alembic upgrade head`, leaves Atlas stopped if migration fails, and starts the service only after a successful migration. Historical raw payload rows are preserved; the new artifact representation applies to new observations, without rewriting old evidence. Real power-loss/provider-boundary crash testing and browser automation remain outside the current harness.

Verification outcome: **fixed**, validated in the V5 working tree on 2026-09-07. The final hardening run passed **166 backend tests** with the PostgreSQL integration fixture enabled, plus **4 frontend transport tests**. One existing Starlette/AnyIO deprecation warning remains.

| Verification gate | Commands / evidence | Result |
| --- | --- | --- |
| Static/import checks | `uv run ruff check backend tests migrations`; `uv run python -m compileall -q backend tests migrations`; `bash -n deployment/*.sh`; `git diff --check` | Pass |
| Security triggers and ordinary controls | Protected aliases, substituted final symlinks, racing move parents/destinations, filtered dirty checkpoints, ordinary reads/edits, and executable/binary/symlink patch round trips in `tests/test_local_storage.py` | Pass; original protected-content and overwrite reproductions no longer succeed |
| Runtime and persistence checks | `ATLAS_TEST_DATABASE_URL=<disposable PostgreSQL URL> uv run pytest -q` | 166 pass, no skipped integration tests |
| Migration compatibility | `tests/test_migrations.py` runs `alembic upgrade` from empty and historical schemas, followed by `alembic check` | Pass; no schema drift |
| Frontend checks | In `frontend/`: `npm run lint && npm run test && npm run build` | Pass; 4 transport tests |

Changed boundaries are `runtime/task_state.py`, `transcript/repository.py`, persistence models and three migrations; `actions/authority.py`, `runtime/execution.py`, capability validation and API decisions; `storage/projects.py`, `storage/local.py`, artifact storage, `runtime/observations.py` and provider projection; `runtime/recovery.py`, action reconciliation and schedule execution; and the frontend conflict/interruption surfaces. Tests and CI now exercise these contracts. Validation uses synthetic resources, real filesystem/Git operations and disposable PostgreSQL; it does not claim live-provider effects, browser automation or power-loss fault injection.

## 2026-09-07 — Direct controls for interrupted work

Interrupted foreground work in Needs You now exposes `Continue` and `Dismiss` in both desktop and mobile through one shared renderer. `Continue` submits an ordinary owner turn containing `continue`; it does not replay an earlier action or bypass the foreground execution path. A successful new owner turn continues to resolve the prior interruption notice through the existing recovery contract.

`Dismiss` resolves only the durable `interrupted` owner-attention row. It does not change the active-task checkpoint, run outcome or action evidence, and the backend rejects attempts to use this endpoint to hide uncertain-action attention. The Continue control is disabled while another foreground send/upload is active or the model is unavailable.

Validation after this patch: **168 backend tests** against disposable PostgreSQL, **5 frontend transport/API tests**, Ruff, frontend lint/build, Python compilation and whitespace checks.


## 2026-09-07 — Remaining review issues

This entry supersedes the earlier direct-project-mutation and owner-turn fail-open descriptions above.

Approval now reads verified canonical proposal data rather than the duplicated attention payload. Every argument is visible, including full mail recipients/body, Calendar patches and unknown nested fields. Approval sends the reviewed hash and the locked transition checks it again; missing/invalid/expired/changed payloads cannot execute through that approval.

Shared-file changes now use `storage/changes.py`: create/update, move and approved delete operations save a downloadable ZIP containing baseline bytes, proposed bytes where applicable, mode, expected hash and diff. A `staged_change` notice exposes the bundle in Needs You. Live owner files stay untouched, including a save at the last staging boundary. Owner integration happens through their editor/VCS. The production Projects mount is read-only. Legacy low-level mutation primitives remain for existing isolated-tree tests; no runtime capability calls them and they make no concurrency promise against independent editors.

Registry startup preserves existing owner enablement and initializes newly discovered capabilities disabled. Control exposes persistent switches. Discovery, operation lookup/call, prepared approvals, storage entrypoints and each provider-native web request consult current policy; unavailable policy fails closed. Disabling schedules pauses queued occurrences. Runtime authentication, canonical evidence and owner recovery remain available independently of model capability switches.

Working-context database reads now fetch bounded recent history (20 exchanges, at most 500 rows, retaining the latest owner request); UI history loads in 200-turn pages. Initial assembly reserves 25% of the input budget for continuation. The provider recounts every tool-loop request, compacts whole tool rounds with exact canonical evidence locators, and refreshes the protected foreground checkpoint. Oversized protected input interrupts safely rather than evicting it or dispatching over budget. Control statistics describe a bounded recent window, not the entire transcript.

`maintenance/backup.py` and `deployment/backup-host.sh` define offline PostgreSQL+artifact+checkpoint/bundle snapshots and restoration into empty targets. Files and directories are flushed before completion is marked, manifests detect damaged/incomplete snapshots, and restoration verifies database artifact references. Credentials/configuration need a separate protected backup. Deployment stops the runtime before changing installed code/interpreter/dependencies. See `22-backup-and-recovery.md` for the operational procedure and recovery limitations.

Regression coverage includes canonical approval binding, persistent revocation, native web revocation during token counting, an actual bounded provider continuation, the production staging factory and owner-save race, bounded PostgreSQL history, paused schedules, and an actual disposable PostgreSQL backup/restore. No production state or live provider effect is used by these tests.

Verification outcome: **fixed** for these five review items. Enforcement stays at the existing canonical proposal/action, capability dispatch, provider request and storage/deployment boundaries. Project staging is necessary because Atlas cannot enforce a filesystem CAS against arbitrary owner editors. Existing inspection, exact evidence replay, permitted dispatch, owner uploads, task continuity and scheduled execution after re-enablement remain covered.

A fresh read-only candidate review identified an OFF→ON automatic-action race and an unguarded artifact-upload route. Both reproduced as failing regressions before correction. Automatic effect classification now uses the stable descriptor before the executing action commits; current owner policy still decides dispatch. Explicit artifact uploads now check their capability, while internal canonical evidence persistence remains runtime infrastructure. The same two regressions pass after correction, and enabled owner uploads still preserve exact bytes.

Changed implementation areas: `actions/authority.py`, `api/app.py`, `capabilities/{factory,service}.py`, `registry/{repository,service}.py`, `runtime/execution.py`, `providers/openai.py`, `schedules/runner.py`, `transcript/repository.py`, `storage/{projects,changes}.py`, `maintenance/backup.py`, frontend approval/history/control code, deployment script/unit, and CI PostgreSQL client setup. New focused coverage is in `test_remaining_hardening.py`, `test_backup_restore.py`, and `frontend/tests/approval.test.mjs`; existing API, recovery and deployment tests were extended.

Validation gates, in order:

1. Syntax/static checks: `uv run ruff check backend tests migrations`, `uv run python -m compileall -q backend tests migrations`, `bash -n deployment/*.sh`, and `git diff --check` passed.
2. Focused regressions: canonical approval integrity/review hash, full field/transport projection, late owner saves at the staging boundary, disabled dispatch/native web/artifact upload, automatic-action identity across enablement changes, bounded history/continuations and exact backup/restore passed. The two independently reported candidate gaps failed before correction and passed afterward. These tests show the reported triggers no longer reproduce through the runtime boundaries.
3. Integration/package checks: `ATLAS_TEST_DATABASE_URL=<disposable PostgreSQL URL> uv run pytest -q --tb=short` passed **183 tests** with no skips or warnings. This includes clean/historical `alembic upgrade head` and `alembic check`, and backup/restore into two disposable databases. A subsequent focused upload test also verified the enabled success path.
4. Frontend: `npm run lint`, `npm run test` (**9 passed**) and `npm run build` passed after the final UI change. Loading older pages does not trigger the new-message scroll-to-bottom behavior.

The remaining Starlette/AnyIO test warning was removed without suppressing it or downgrading dependencies: the two API test modules that used `fastapi.testclient.TestClient` now use HTTPX `AsyncClient` with `ASGITransport`. This avoids Starlette's currently deprecated `anyio.abc.BlockingPortal` alias while continuing to exercise the ASGI application directly. A fresh PostgreSQL-backed full-suite run passed **183 tests with zero warnings**.

Production state, services and provider effects were not touched. This change has not been committed or deployed. Hosted CI, real browser interaction, live provider dispatch and power-loss injection were not run; local integration used PostgreSQL 18 and provider doubles. The disposable database was stopped after validation. The backup rehearsal proves restoration of captured state, not reconciliation of effects performed after a historical snapshot.

## 2026-09-07 — Control became an operational setup console

Control is reorganized as a compact dashboard rather than a vertical status dump. Runtime, system health and working-context pressure occupy the first row; Model API, Google Workspace and GitHub are first-class connection cards; capability authority is a compact switch grid; detailed context, credential-protection and registry information moved behind an Advanced disclosure.

Model API setup accepts a replacement API credential, verifies it by requesting the provider's live model catalog, and only then exposes those returned model IDs in a selector. The discovery request does not persist the submitted key. Selecting a model triggers a second provider check before the credential/model pair is saved and applied immediately. Existing configured connections can refresh the catalog with the protected server-side key, so Control never returns that secret to the browser. GitHub setup accepts an owner and token, proves the token through the official read-only MCP discovery path before saving, and marks the connection for restart so the runtime can rebuild its discovered operation table. Google Workspace setup imports a standard `authorized_user` OAuth credential, verifies it through the local `gws` bridge, and also requires restart before the runtime adopts a newly imported credential. Existing connections have a credential-free Test action.

Owner-managed connection overrides are written atomically below `ATLAS_STATE_DIR/control` with private directory/file modes. Credential values are never returned by Control APIs after submission. The normal provider/capability dispatch boundaries remain unchanged: configuring a connection does not enable its capability, and the existing persistent owner capability switches still decide whether Atlas may use Google Workspace, GitHub, web, storage, scheduling and other optional surfaces.

Google's interactive `gws auth login` remains a browser-local OAuth flow with a localhost callback, so headless/server Control uses the supported exported-credential path rather than pretending it can proxy that callback safely. The helper authorization script now requests Drive, Gmail and Calendar together. Control-managed connection secrets/settings are excluded from the ordinary PostgreSQL/artifact snapshot and must be included with the separate encrypted operator credential/configuration backup described in `22-backup-and-recovery.md`.

Validation for this pass: **188 backend tests** against disposable PostgreSQL 18 with zero warnings; frontend lint **0 warnings / 0 errors**, **12 frontend tests**, and a successful Vite production build. Production was not changed by this implementation pass.


## 2026-09-07 — Provider-backed model selection

Control no longer accepts a free-text model identifier during normal Model API setup. The owner selects a provider (OpenAI is the currently implemented provider), enters an API credential, and asks Atlas to verify it. Atlas calls the provider's model-list API with that credential and returns only the model identifiers actually reported for that key. The credential is not persisted by discovery and is never echoed to the browser.

After successful discovery, the model field becomes a dropdown populated from that provider response. Changing the key or provider invalidates the previous catalog so a model cannot be saved against stale credential discovery. A configured connection can refresh its model list with the protected stored key without re-entering or exposing it. Saving still verifies the selected model through Atlas's normal OpenAI provider token-count path before atomically persisting the key/model pair.

Regression coverage checks provider-sourced (not hard-coded) catalog projection, submitted-key discovery without persistence, stored-key refresh, selected-model save without secret disclosure, and the frontend request contracts for both new-key and protected-key discovery.

Validation for this pass: **191 backend tests** against disposable PostgreSQL 18 with zero warnings; frontend lint **0 warnings / 0 errors**, **14 frontend tests**, and a successful Vite production build. Ruff, Python compilation, deployment shell syntax and `git diff --check` also passed. The disposable PostgreSQL instance was removed after validation.


## 2026-09-08 — First memory-orbit slice: indexed transcript recall

Atlas now has a deterministic transcript-indexing boundary before any semantic memory worker is introduced. `TranscriptIndexer` reads canonical turns incrementally, never mutates transcript history, preserves exact source turn identities, and checkpoints the highest processed turn per transcript/index version so restart or rerun does not duplicate chunks. The active owner transcript keeps the latest configured owner exchanges out of the background index; closed transcripts may be indexed completely.

The first retrieval layer is deliberately lexical rather than semantic. PostgreSQL stores derived transcript chunks with a generated `tsvector` and GIN index using the `simple` dictionary so technical identifiers are not stemmed away. `memory.search` returns bounded provenance-backed chunks and accepts transcript, sequence and exclusion constraints so the conversational model can refine a failed lookup without repeatedly receiving the same wrong results. Tool observations contribute only their bounded operation/phase/summary, not raw detail payloads.

Indexing is not on the owner-facing inference path. `python -m atlas.memory` provides one deterministic maintenance pass; scheduling, embeddings/pgvector, context-capsule generation and the separate semantic memory-reasoning worker remain subsequent work. The memory capability follows the existing authority model: it is provisioned by the runtime but a newly discovered capability starts disabled until the owner enables it in Control.

Validation used a disposable PostgreSQL 17 container and exercised empty/historical Alembic upgrades, generated full-text indexes, incremental checkpoints, active-tail exclusion and lexical recall. The full backend suite passed **196 tests** with PostgreSQL integration enabled. Ruff also passed; the disposable database was removed after validation. No production state, service or provider call was touched.
