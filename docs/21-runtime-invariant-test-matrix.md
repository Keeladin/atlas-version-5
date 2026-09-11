# Runtime invariant test matrix

Date: 2026-09-06

This matrix treats tests as protection for runtime truth, not as a raw test-count target. The highest-value invariants are those whose regression could make Atlas misstate an external effect, lose owner attention, contaminate transcript continuity, overwrite owner work, or bypass an authority boundary.

| Invariant | Direct regression coverage | Status |
| --- | --- | --- |
| Exact consequential proposal cannot change after owner review | `test_authority_integrity.py`, `test_runtime_invariants.py` | Covered |
| Prepared/executing/succeeded/failed/uncertain remain distinct | `test_authority_integrity.py`, `test_runtime_invariants.py` | Covered |
| Uncertain action remains visible and cannot be collapsed into failure | `test_authority_integrity.py`, `test_runtime_invariants.py` | Covered |
| Stale executing action reconciles by CAS and raises durable attention | `test_authority_integrity.py`, `test_runtime_invariants.py` | Covered |
| A later run finalizer cannot overwrite an uncertain run | `test_runtime_invariants.py` | Covered |
| Capability authority gates automatic vs approval-required effects | `test_capabilities.py`, `test_registry.py` | Covered |
| Capability discovery is bounded separately from dispatched operations | `test_provider_resources.py` | Covered |
| Scheduled recurrence advancement and queued occurrence creation are atomic | `test_schedules.py`, `test_runtime_invariants.py` | Covered |
| Scheduled transcripts cannot become the foreground owner transcript | `test_transcript_repository.py` | Covered |
| Owner chats retain separate canonical transcripts and can be switched without mixing turns | `test_owner_chats.py` | Real PostgreSQL |
| Deleting a chat cascades transcript history/index state but preserves independent durable memory | `test_owner_chats.py` | Real PostgreSQL |
| Memory mutation provenance follows the executing transcript rather than mutable UI selection | `test_durable_memory.py`, `test_owner_chats.py` | Real PostgreSQL |
| Rollover removes only the summarized prefix from provider projection | `test_conversation_context.py`, `test_runtime_invariants.py` | Covered |
| Missing rollover marker fails open to canonical history rather than dropping turns | `test_runtime_invariants.py` | Covered |
| Tool evidence projects as bounded, explicitly untrusted source evidence | `test_conversation_context.py`, `test_runtime_invariants.py` | Covered |
| Same-turn model-facing tool evidence is bounded while canonical evidence remains intact | `test_provider_resources.py`, `test_conversation_context.py` | Covered |
| Active-task checkpoint survives ordinary history trimming | `test_task_state.py`, `test_api.py` | Covered |
| Model task-state deltas cannot overwrite runtime-owned facts | `test_task_state.py` | Covered |
| Large UTF-8 resources can be bounded and reacquired by line range | `test_local_storage.py`, `test_provider_resources.py` | Covered |
| Capability discovery uses compact operation cards and rechecks current owner policy | `test_capabilities.py`, `test_provider_resources.py` | Covered |
| Legacy isolated-tree edits are preview-bound and stale-owner changes refuse overwrite | `test_local_storage.py`, `test_runtime_invariants.py` | Covered |
| Legacy isolated-tree writes checkpoint dirty state before mutation | `test_local_storage.py` | Covered |
| Project writes cannot cross project/path/symlink/protected-material boundaries | `test_local_storage.py`, `test_runtime_invariants.py` | Covered |
| Project move/delete refuse stale hashes | `test_local_storage.py`, `test_runtime_invariants.py` | Covered |
| Passkey bearer token is stored only as a hash and expired sessions fail closed | `test_auth.py`, `test_auth_sessions.py` | Covered |
| Registration challenge/code are one-time bounded enrollment state | `test_auth.py`, `test_auth_sessions.py` | Covered |
| Alembic history is one connected linear chain | `test_migrations.py` | Covered |
| Google mail/calendar consequential payloads preserve prepared arguments | `test_google_workspace.py` | Covered |
| GitHub integration remains namespaced/read-oriented at the current boundary | `test_github_mcp.py`, `test_registry.py` | Covered |

| Task updates preserve all semantic fields, runtime facts and unresolved references with revision/CAS | `test_task_state.py`, `test_persistence_invariants.py`, `test_evidence_recovery.py` | Covered |
| Concurrent owner creation and foreground requests have one winner; turns have unique sequence | `test_persistence_invariants.py`, `test_evidence_recovery.py` | Real PostgreSQL |
| Full schema validation precedes dispatch; ValueError after effects stays ambiguous | `test_capabilities.py`, `test_evidence_recovery.py` | Covered |
| Resource bytes are not duplicated in transcript/action JSON; exact HTML/Unicode replay survives source changes | `test_evidence_recovery.py`, `test_provider_resources.py` | Real PostgreSQL + artifacts |
| Public provider web/citation events retain provenance without extra inference | `test_provider_resources.py` | Provider double |
| Protected aliases/path swaps fail closed; moves never overwrite; permitted snapshot patches remain applicable | `test_local_storage.py` | Real filesystem + Git |
| Cancelled/abandoned foreground inference releases its slot without losing task/effect truth | `test_evidence_recovery.py` | Real PostgreSQL |
| Interrupted attention can be dismissed without changing task/run truth; uncertain attention cannot be hidden by that control | `test_evidence_recovery.py`, `frontend/tests/conversation.test.mjs` | Real PostgreSQL + frontend API |
| Durable queued schedules execute original intent once after interruption | `test_evidence_recovery.py` | Real PostgreSQL + provider double |
| Clean and historical database upgrades preserve conversation/task/completed-run history | `test_migrations.py` | Real PostgreSQL |

| Approval shows complete canonical arguments and binds dispatch to the displayed hash | `test_remaining_hardening.py`, `frontend/tests/approval.test.mjs` | PostgreSQL + frontend field projection |
| Owner enablement survives startup and revokes discovery, direct calls and native web | `test_remaining_hardening.py` | PostgreSQL + provider double |
| Production project operations stage changes and preserve a concurrent owner save | `test_remaining_hardening.py`, `test_deployment_permissions.py` | Real filesystem + factory + unit configuration |
| Recent history is SQL-bounded and tool continuations stay within budget with task/evidence retained | `test_remaining_hardening.py` | PostgreSQL + provider double |
| Queued occurrences pause while scheduling capability is disabled | `test_evidence_recovery.py` | PostgreSQL + provider double |
| Offline state restores exactly into empty targets and rejects corrupt/populated targets | `test_backup_restore.py` | Two disposable PostgreSQL databases + artifact directories |
| Closed owner chats become fully eligible for transcript indexing on the next maintenance pass, while the active chat tail remains protected | `test_owner_chats.py`, `test_memory.py`, `test_deployment_permissions.py` | Real PostgreSQL + systemd configuration |
| Cross-chat continuity is revisioned derived orientation, excludes the active chat, is dropped before current-chat history under budget pressure, and remains subordinate to retirement/correction guards | `test_continuity_capsules.py`, `test_api.py`, `test_durable_memory.py` | Real PostgreSQL + provider double |
| Deployment stops runtime before installed code/dependency replacement | `test_deployment_permissions.py` | Script ordering |
| Control-managed connection overrides persist privately across restart and never return submitted credentials | `test_control_connections.py`, `frontend/tests/control.test.mjs` | Filesystem modes + backend/frontend transport |
| Model setup verifies before save; GitHub/Google setup verify before restart-required activation | `test_control_connections.py`, `frontend/tests/control.test.mjs` | Provider/MCP boundaries with test doubles |
| Model selection is populated from the authenticated provider catalog; discovery never persists or returns the submitted key, and stored-key refresh stays server-side | `test_control_connections.py`, `frontend/tests/control.test.mjs` | Provider catalog double + frontend transport |
| Stale/reclaimed memory-candidate lease cannot publish even when the owner-memory revision is unchanged | `test_memory_reconciliation.py` | Real PostgreSQL |
| Owner memory/source changes during reconciliation reject stale semantic publication | `test_memory_reconciliation.py`, `test_durable_memory.py` | Real PostgreSQL |
| Derived reconciliation cannot supersede owner memory, restore lifecycle-restricted content, widen unresolved scope, or target unseen memory | `test_memory_reconciliation.py` | Real PostgreSQL |
| Equivalent claims add provenance only for a genuinely new source; repeated lineage is a true no-op | `test_memory_reconciliation.py` | Real PostgreSQL |
| Semantic memory identity follows the supported proposition rather than classification metadata; cross-kind/durability paraphrases deduplicate while genuine narrowing, distinct claims and temporal changes remain model-judged | `test_memory_semantic_identity.py` | Real PostgreSQL |
| Pre-redaction continuity/embedding work cannot publish stale copies after canonical source or lifecycle state changes | `test_memory_reconciliation.py`, `test_continuity_capsules.py`, `test_memory.py` | Real PostgreSQL + provider doubles |
| Memory observability remains read-only, excludes deleted source text, and its SSE change token advances with candidate/attempt state | `test_memory_observability.py`, `test_api.py`, `frontend/tests/memory-observability.test.mjs` | Real PostgreSQL + ASGI stream + frontend transport |
| Explicit remember/correct queue verified candidates; their obligations settle atomically with terminal outcomes, survive automatic retries, allow owner retry under state-version CAS, and terminate visibly after the budget | `test_memory_explicit_commands.py`, `test_memory_transition_matrix.py` | Real PostgreSQL |
| Explicit corrections keep their target and change type: guard linking, untimed corrections versus validity windows, target-drift blocking, pending-correction suppression; explicit corrections may supersede owner-directed rows while derived candidates still conflict | `test_memory_explicit_commands.py`, `test_memory_state_machine.py` | Real PostgreSQL |
| Identical publication replay is answered from the ledger without a second semantic mutation across create, lineage, supersede, historical predecessor and legacy grounding | `test_memory_publication_replay.py` | Real PostgreSQL |
| Discovery sweep cursor/window semantics: handle minting, disjoint windows, context visibility, horizon, dedupe, failure retry, soft-deleted turns, purge rewind, eligibility, rotation, in-flight purge/append | `test_memory_discovery.py` | Real PostgreSQL |
| Memory conflicts expose both wordings with a review-version CAS; keep/accept/restate resolve atomically, expired conflicts stay resolvable, attention is bounded, and a clarification round trip works through the capability runtime | `test_memory_conflicts.py` | Real PostgreSQL + capability runtime |
| Migration/restore rehearsal restores a pre-25a13 snapshot into disposable targets, migrates to head with a clean `alembic check`, verifies invariants, day-one review state, artifacts and empty recall, rehearses downgrade refusal, and refuses production targets | `test_migration_rehearsal.py`, `test_migration_history.py` | Disposable PostgreSQL databases + alembic subprocess |

## Still intentionally thin

The suite does not yet simulate a real process kill between provider dispatch and database commit or exercise WebAuthn against a browser authenticator. Hosted CI runs migrations and the PostgreSQL integration tests against disposable PostgreSQL. Locally, set `ATLAS_TEST_DATABASE_URL` explicitly to a disposable database; the fixture never falls back to the application database. Without it, those integration tests skip. The frontend has Node tests for conflict/error classification and streaming transport, plus lint/build checks; browser interaction automation remains a separate gap. The remaining gaps are integration/system-test layers rather than missing unit invariants.

The acceptance rule for future runtime work is: any new hard invariant must arrive with a direct regression test, and consequential cross-layer behavior should also receive an integration test when the necessary harness exists.

Memory lifecycle regression coverage in `test_durable_memory.py` and `test_migrations.py` includes retained deletion identities/null payloads, explicit historical retrieval, retirement/restoration, dependency scrubbing, rollback when source text cannot be isolated, shared owner revision advancement, and preservation of unrelated text after multi-turn chunk rebuilding. Provider/registry integration exercises retirement and deletion without a `memory.forget` capability.
