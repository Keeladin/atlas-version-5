# Remaining Open Questions

Most first-pass architecture questions are now resolved. The remaining items are either review questions or implementation choices that should not be frozen prematurely.

## Architecture review questions

- Is the Environment Registry boundary small enough that it remains a map rather than another loaded context bundle?
- Can OpenAI and the first compatibility provider occupy the same Atlas-owned seat without moving continuity or memory into provider state?
- Are any supporting faculties still making semantic decisions that belong to inference?
- Does any proposed UI surface expose an internal subsystem merely because it exists?
- Are any enabled-capability controls too granular for a human owner to understand naturally?
- Does the memory design preserve provenance and correction without making the active model a database writer?

## Implementation choices to defer

- exact provider-adapter API shapes;
- database table/index layout;
- embedding provider/model and dimensions;
- transcript rollover thresholds and short-term retention TTL;
- chunking and retrieval ranking parameters;
- artifact-store implementation and retention policy;
- exact model-routing and cost/latency heuristics;
- schedule/indexing windows and resource throttling;
- main-page panel geometry and responsive behavior;
- Control information architecture;
- health-check frequencies and observability retention.

These choices may be tested and tuned during implementation as long as they do not violate the accepted architecture baseline.