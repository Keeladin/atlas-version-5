# Managed workspace tasks

Managed workspace tasks turn an owner-approved objective into a durable execution contract that Atlas can carry across multiple model turns without asking the owner to press Continue after every ordinary runtime boundary.

## Authority model

The task contract is continuity, not permission. Atlas Control remains the only authority source for capability execution. Each delegated operation still passes through the normal capability registry, operation-authority lookup, action ledger, evidence store, and approval flow. A managed task cannot promote an Ask me or Deny operation into Auto.

The MCP transports deliberately contain no independent permission policy. `workspace.tasks` persists task contracts. `coding.agent` manages Codex process and thread lifecycle. Host administration remains in the structured `host.operations` broker.

## Durable task contract

A managed task is stored in a dedicated `managed_task` transcript. Its active task state records a task ID, project ID, objective, scope, acceptance criteria, checkpoints, progress, next step, controller state, pending actions, and the current coding-agent session when one exists.

`workspace.tasks.create` requires at least one acceptance criterion. The runtime refuses managed completion until every criterion is marked passed with a runtime evidence reference and no action remains pending. This keeps “done” tied to evidence instead of model narration.

The owner-facing task operations are:

- `workspace.tasks.create`
- `workspace.tasks.list`
- `workspace.tasks.get`
- `workspace.tasks.cancel`
- `workspace.tasks.resume`

## Automatic continuation

`atlas-v5-managed-tasks.service` polls active managed-task transcripts. It claims one background run under a row lock, rehydrates the protected task checkpoint, and executes a normal Atlas inference turn. If the task remains active, the worker schedules another bounded continuation turn. A pending owner decision moves the task to `waiting_for_owner`; a cancelled or completed task is not claimed again.

Provider or process interruption does not create an ordinary “press Continue” interruption for a managed task. The exact checkpoint and effect evidence are retained and the controller retries automatically. Already-dispatched effects are never blindly replayed: uncertain effects continue to use the existing action reconciliation and owner-attention path.

The controller keeps two retry budgets deliberately separate. A successful model turn that makes no material task progress consumes the small no-progress budget (`ATLAS_MANAGED_TASK_MAX_NO_PROGRESS`, default 3) and may eventually create a genuine `stalled` owner-attention item. Transient provider/runtime failures instead use a much larger automatic retry budget (`ATLAS_MANAGED_TASK_MAX_TRANSIENT_FAILURES`, default 48) with exponential backoff capped at 15 minutes. They do not consume the no-progress budget. This means rate limits, temporary provider outages, database connection interruptions, worker restarts, or equivalent availability failures do not silently turn into a request for the owner to press Continue.

If the transient retry budget is genuinely exhausted, the task is marked `stalled` with an execution-availability explanation. `workspace.tasks.resume` clears both retry counters and re-enters automatic continuation.

## Coding agent boundary

The coding MCP is exposed over `/run/atlas-v5/mcp/coding-agent.sock`. The Atlas runtime remains the isolated `atlas-v5` identity; each coding MCP connection runs as `jaco` so Codex can work in owner-controlled project directories without granting the whole Atlas service owner permissions.

The coding service is constrained by systemd. `ProtectSystem=strict`, `ProtectHome=read-only`, `NoNewPrivileges=yes`, and explicit `ReadWritePaths` confine writable owner paths. The Python bridge additionally resolves symlinks and rejects work directories outside `ATLAS_CODING_ROOTS` (normally `/home/jaco/Projects` and `/home/jaco/Workspace`).

Coding operations are:

- `coding.agent.start_session`
- `coding.agent.send_turn`
- `coding.agent.get_status`
- `coding.agent.get_result`
- `coding.agent.resume_session`
- `coding.agent.cancel_session`

One Codex turn may run at a time per coding session. Session state records the Codex thread ID, PID/process group, JSONL event log, result, and timestamps so a later Atlas turn can reconnect rather than invent a new worker.

## Deployment

`deployment/install-host-mcp.sh` installs the host broker, coding-agent socket, and managed-task worker units. It smoke-tests the host and coding MCP sockets as the `atlas-v5` runtime identity. The managed-task worker is enabled but is started with the main Atlas service so deployment does not accidentally execute background work during the maintenance window.

No managed-task implementation, Codex session, commit, push, merge, deployment, package change, service change, or host change bypasses the current Control authority for the operation that actually performs it.
