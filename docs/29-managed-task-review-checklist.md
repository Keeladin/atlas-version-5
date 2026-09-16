# Managed task review checklist

Before this branch is merged or deployed, verify the managed-task path as one system rather than as separate features.

- Control remains authoritative for every capability operation. A managed task carries continuity and scope, never extra permission.
- A task cannot complete until every acceptance criterion has runtime evidence and no action is pending.
- A background task with an Ask me operation becomes `waiting_for_owner` rather than repeatedly dispatching or inventing approval.
- A provider/runtime interruption preserves the task checkpoint and automatically retries without creating a routine Continue prompt.
- An uncertain already-dispatched effect is never replayed automatically.
- Only one background Atlas run can own a task at a time.
- Only one Codex turn can own a coding session at a time.
- The coding MCP persists long enough for an asynchronously launched Codex child to finish; socket activation must not tear down the worker process after each request.
- Codex work directories are resolved through symlinks and must stay under `ATLAS_CODING_ROOTS`.
- The Atlas API/managed-task controller remains the `atlas-v5` identity; only the narrow coding worker runs as `jaco`.
- The coding worker has no sudo/root path. Privileged host effects continue through `host.operations`.
- Deployment smoke-tests the host and coding sockets before Atlas is restarted.
- Existing custom MCP authority defaults remain parse-compatible, but owner Control overrides remain the final runtime decision.
- Full backend tests, Ruff, deployment shell syntax, frontend lint/tests/build, and an independent post-fix review are green before deployment.
