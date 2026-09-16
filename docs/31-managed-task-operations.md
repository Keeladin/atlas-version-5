# Managed task operation surface

`workspace.tasks` is the durable control surface for jobs. It creates, lists, reads, cancels, and resumes task contracts. It does not perform project edits itself.

`coding.agent` is the implementation/review worker surface. It starts and resumes Codex threads, returns their status/results, and can terminate an active coding session. It does not decide whether Atlas may call those operations.

`host.operations` remains the privileged structured host surface. Managed tasks do not give Codex a root shell and do not convert host administration into coding-agent work.

The managed-task controller runs ordinary Atlas capability dispatch in background runs, so action records, evidence, owner attention, uncertain-effect handling, and operation authority are shared with foreground conversation rather than reimplemented for jobs.
