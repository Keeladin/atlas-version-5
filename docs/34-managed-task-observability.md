# Managed task observability

Each managed task keeps controller state alongside the protected task checkpoint: current progress, checkpoint, pending actions, coding-session identity, retry count, next wake time, and the most recent background worker run. Background iterations also use the ordinary run-event stream and evidence store.

The intended owner-facing Workspace view should read this persisted state rather than infer progress from chat text. It should distinguish at least `ready`, `running`, `waiting_for_owner`, `stalled`, `complete`, and `cancelled`, and expose acceptance criteria separately from conversational summaries.
