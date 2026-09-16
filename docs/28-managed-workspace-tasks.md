# Managed workspace tasks

Managed workspace tasks turn an owner-approved objective into a durable execution contract that Atlas can carry across multiple model turns without asking the owner to press Continue after every ordinary runtime boundary.

## Authority model

Atlas Control/runtime remains the single authority source. When the owner approves a managed implementation, Atlas discovers the exact operations it expects to need and records them as `authority_grants` in the immutable task contract. Task creation preflights those operation IDs against the current capability registry and Control policy before work starts. During that task only, a preflighted grant may promote an `Ask me` operation to `Auto`, so the same approved implementation does not stop later to ask the same permission again. A current Control `Deny`, a disabled capability, an unknown operation, filesystem scope, sandbox rule, or other technical constraint is never bypassed.

This keeps the distinction explicit: Control defines the hard boundary, the owner-approved task contract defines authority inside that boundary for this one workflow, and the runtime enforces both before dispatch. The model cannot add new grants through `task_state_delta`; scope expansion requires a new owner decision or Control change.

The MCP transports deliberately contain no independent permission policy. `workspace.tasks` persists task contracts. `coding.agent` manages Codex process and thread lifecycle. Host administration remains in the structured `host.operations` broker.

## Durable task contract

A managed task is stored in a dedicated `managed_task` transcript. Its active task state records a task ID, project ID, objective, scope, exact authority grants, acceptance criteria, checkpoints, progress, next step, controller state, pending actions, and the current coding-agent session when one exists.

`workspace.tasks.create` requires at least one acceptance criterion. Before Atlas creates the task it should discover the required operations and include them in `authority_grants`; the runtime preflights those grants before the MCP is dispatched. The runtime refuses managed completion until every criterion is marked passed with a runtime evidence reference and no action remains pending. This keeps “done” tied to evidence instead of model narration.

The owner-facing task operations are:

- `workspace.tasks.create`
- `workspace.tasks.list`
- `workspace.tasks.get`
- `workspace.tasks.cancel`
- `workspace.tasks.resume`

## Automatic continuation

`atlas-v5-managed-tasks.service` polls active managed-task transcripts. It claims one background run under a row lock, rehydrates the protected task checkpoint, and executes a normal Atlas inference turn. If the task remains active, the worker schedules another bounded continuation turn. A pending owner decision moves the task to `waiting_for_owner`; a cancelled or completed task is not claimed again.

Provider or process interruption does not create an ordinary “press Continue” interruption for a managed task. The exact checkpoint and effect evidence are retained and the controller retries automatically. Already-dispatched effects are never blindly replayed: uncertain effects continue to use the existing action reconciliation and owner-attention path.

The controller keeps two retry budgets deliberately separate. A successful model turn that makes no material task progress consumes the small no-progress budget (`ATLAS_MANAGED_TASK_MAX_NO_PROGRESS`, default 3) and may eventually create a genuine `stalled` owner-attention item. Fresh read-only polling evidence does not count as progress. Transient provider/runtime failures instead use a much larger automatic retry budget (`ATLAS_MANAGED_TASK_MAX_TRANSIENT_FAILURES`, default 48) with exponential backoff capped at 15 minutes. They do not consume the no-progress budget. This means rate limits, temporary provider outages, database connection interruptions, worker restarts, or equivalent availability failures do not silently turn into a request for the owner to press Continue.

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

One Codex turn may run at a time per coding session. Session state records the Codex thread ID, PID/process group, JSONL event log, result, and timestamps so a later Atlas turn can reconnect rather than invent a new worker. Cancellation first fences the Atlas task, then terminates the exact Codex process identity; if SIGTERM does not stop it within the bounded grace period the bridge escalates to SIGKILL and verifies termination before reporting cancellation.

## Deployment

`deployment/install-host-mcp.sh` installs the host broker, coding-agent socket, and managed-task worker units. It smoke-tests the host and coding MCP sockets as the `atlas-v5` runtime identity. The managed-task worker is enabled but is started with the main Atlas service so deployment does not accidentally execute background work during the maintenance window.

No task grant bypasses a current Control `Deny`, disabled capability, host scope, sandbox, or technical validation boundary. Merge and deployment remain ordinary operations: they run automatically inside a task only when the owner-approved task grant explicitly contains those exact operations and Control does not deny them.
