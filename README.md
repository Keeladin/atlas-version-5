# Atlas Version 5

Atlas V5 is a clean architectural restart of Atlas.

The project is intentionally documentation-only during the design phase. No runtime, UI, database, provider adapter, tool host, or migration code should be added until the architecture is explicitly accepted.

## Product direction

Atlas is a persistent local agent environment in which the model is the primary decision-maker.

The model enters inference already aware of its useful native abilities, available tools, current workspace, relevant context, and effective authority. It decides how to achieve the owner's request. The runtime provides state, tools, execution, boundaries, persistence, scheduling mechanics, and observability; it does not act as the planner.

## Design shorthand

- The model is the agent.
- Tools are abilities, not workflows.
- Workspaces are where the agent works, mostly behind the scenes.
- Authority is enforced at the effect boundary.
- Contextual memory is RAM; embedded memory is long-term storage.
- Memory housekeeping happens asynchronously outside the active context frame.
- Schedules are passive Atlas machinery, exposed only when useful to inspect or control.
- Existing software should be used rather than reimplemented.
- Deterministic adjacent utilities are "second cousins", not Atlas subsystems.

## Planning documents

See `docs/` for the architecture, memory model, provider/tool survey, V4 lessons, and design roadmap.

Status: **architecture and planning only**.
