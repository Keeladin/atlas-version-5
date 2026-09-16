# Workspace UI contract

The owner-facing Workspace page is a projection of durable managed-task state, not a second execution engine.

For each task it should show the project/title, objective, controller state, progress, current checkpoint, acceptance criteria, next step, current coding session, recent background run, and any pending owner action. Cancel and resume actions should call the existing managed-task operations rather than editing state directly.

The UI is intentionally downstream of the backend contract. The controller, coding bridge, evidence rules, and authority boundary must be green before the page is treated as production-ready.
