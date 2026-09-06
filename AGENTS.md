# Atlas Version 5

Atlas V5 is model-led. The selected model owns semantic decisions; runtime governs deterministic execution and reality.

## Authoritative design

Read these before changing runtime semantics:
- `docs/15-pre-implementation-baseline.md`
- `docs/17-runtime-constitution.md`
- `docs/18-implementation-plan.md`

## Repository rules

- Do not import V4 architecture by default.
- Do not introduce a runtime planner, mandatory Work objects, or general CONFIRM state.
- Keep provider-specific features behind adapters; OpenAI is first, not canonical Atlas state.
- Treat multimodal content and artifacts as first-class from the start.
- Keep secrets out of source, transcripts, ordinary artifacts, logs, and model-readable state.
- Persistent owner state lives outside the Git checkout.
- V4 remains separate and untouched unless explicitly requested.
- Live Caddy/systemd cutover is a deliberate deployment action, not a development side effect.

## Verification

Run backend tests, frontend tests/build, and migration/static checks appropriate to the files changed before committing.