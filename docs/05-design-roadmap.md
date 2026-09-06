# Atlas V5 Design Roadmap

## Status

The architecture discovery, review, and implementation-planning phases are complete. The project has passed both the **architecture gate** and the **implementation-plan gate**. Phase 0 has also been implemented, deployed, restart-verified, and cut over behind the production Caddy route.

The accepted design authorities remain `15-pre-implementation-baseline.md`, `17-runtime-constitution.md`, and `18-implementation-plan.md`. Phase 1 now builds the working Atlas model centre from that foundation.

## Completed design areas

- provider and tool capability survey;
- model-led provider posture with OpenAI as primary/reference provider;
- seat bootstrap and Environment Registry direction;
- thin agent cycle and model/runtime decision boundary;
- workspace as maintained field of action;
- transcript, indexed short-term memory, PostgreSQL/pgvector long-term memory, and asynchronous memory processing;
- enabled-capability authority model and progressive tool disclosure;
- schedules as persisted intent plus deterministic trigger;
- two-surface product shape: Atlas and Control;
- first-class multimodal conversation/artifact handling;
- heliocentric implementation philosophy.

## Accepted design

The owner accepted the baseline, Runtime Constitution, and implementation plan on 2026-09-05. Future changes to those documents are deliberate design amendments, not incidental implementation choices.

Operational values such as TTLs, chunk sizes, embedding choice, ranking weights, indexing windows, panel geometry, and model-routing heuristics remain deliberately unfrozen.

## Implementation next

Phase 1 now follows the accepted heliocentric sequence in `18-implementation-plan.md` rather than a use-case vertical slice.

The centre is a working model and direct multimodal interface, with the minimum runtime execution spine required to preserve identity, transcript/artifact continuity, secrets, capability enablement, effect truth/recovery, and owner-visible failure state. The first orbit is the primary provider's native capabilities. The next orbit is MCP, connected services, and useful local software. Runtime evolves in parallel as each capability needs execution, persistence, authority, or observability support.

Validation is derived from the capabilities actually present. Test scenarios prove architecture; they do not define Atlas's product semantics.