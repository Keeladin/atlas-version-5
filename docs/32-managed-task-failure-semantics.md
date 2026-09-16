# Managed task failure semantics

Managed work is allowed to continue through ordinary model/runtime boundaries, but not through ambiguous external effects.

A provider interruption before an effect is dispatched is retryable from the durable checkpoint. A capability failure with a definite result is recorded and can be reasoned about on the next turn. A dispatched effect whose outcome cannot be confirmed remains `uncertain`; Atlas retains it as pending owner attention and the managed controller must not replay it automatically.

A task that repeatedly fails to make durable progress is marked `stalled` after the bounded retry threshold. That state surfaces to the owner instead of spinning forever. `workspace.tasks.resume` explicitly re-arms an active stalled task after the blocker is corrected.
