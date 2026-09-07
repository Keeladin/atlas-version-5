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
| Scheduled recurrence advances before execution and one-time tasks disable first | `test_schedules.py`, `test_runtime_invariants.py` | Covered |
| Scheduled transcripts cannot become the foreground owner transcript | `test_transcript_repository.py` | Covered |
| Rollover removes only the summarized prefix from provider projection | `test_conversation_context.py`, `test_runtime_invariants.py` | Covered |
| Missing rollover marker fails open to canonical history rather than dropping turns | `test_runtime_invariants.py` | Covered |
| Tool evidence projects as bounded developer/runtime evidence | `test_conversation_context.py`, `test_runtime_invariants.py` | Covered |
| Same-turn model-facing tool evidence is bounded while canonical evidence remains intact | `test_provider_resources.py`, `test_conversation_context.py` | Covered |
| Active-task checkpoint survives ordinary history trimming | `test_task_state.py`, `test_api.py` | Covered |
| Model task-state deltas cannot overwrite runtime-owned facts | `test_task_state.py` | Covered |
| Large UTF-8 resources can be bounded and reacquired by line range | `test_local_storage.py`, `test_provider_resources.py` | Covered |
| Capability discovery uses compact operation cards and reuses identical same-turn searches | `test_capabilities.py`, `test_provider_resources.py` | Covered |
| Project edits are preview-bound and stale-owner changes refuse overwrite | `test_local_storage.py`, `test_runtime_invariants.py` | Covered |
| Project writes checkpoint dirty state before mutation | `test_local_storage.py` | Covered |
| Project writes cannot cross project/path/symlink/protected-material boundaries | `test_local_storage.py`, `test_runtime_invariants.py` | Covered |
| Project move/delete refuse stale hashes | `test_local_storage.py`, `test_runtime_invariants.py` | Covered |
| Passkey bearer token is stored only as a hash and expired sessions fail closed | `test_auth.py`, `test_auth_sessions.py` | Covered |
| Registration challenge/code are one-time bounded enrollment state | `test_auth.py`, `test_auth_sessions.py` | Covered |
| Alembic history is one connected linear chain | `test_migrations.py` | Covered |
| Google mail/calendar consequential payloads preserve prepared arguments | `test_google_workspace.py` | Covered |
| GitHub integration remains namespaced/read-oriented at the current boundary | `test_github_mcp.py`, `test_registry.py` | Covered |

## Still intentionally thin

The suite does not yet simulate a real process kill between provider dispatch and database commit or exercise WebAuthn against a browser authenticator. Hosted CI does run the complete Alembic chain against disposable PostgreSQL. The remaining gaps are integration/system-test layers rather than missing unit invariants.

The acceptance rule for future runtime work is: any new hard invariant must arrive with a direct regression test, and consequential cross-layer behavior should also receive an integration test when the necessary harness exists.
