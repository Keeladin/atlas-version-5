# Atlas V5 Design Roadmap

## Status

The initial architecture discovery list has been completed. The project is now at the **pre-implementation architecture review** gate.

The consolidated review target is `15-pre-implementation-baseline.md`. Implementation remains prohibited until that baseline is accepted.

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

## Review phase

The review should look for contradictions, missing ownership boundaries, hidden workflow engines, provider lock-in, and places where implementation detail has been mistaken for architecture.

Operational values such as TTLs, chunk sizes, embedding choice, ranking weights, indexing windows, panel geometry, and model-routing heuristics are deliberately not frozen.

## After architecture acceptance

Implementation planning should translate the accepted architecture into a heliocentric build sequence rather than a use-case vertical slice.

The centre is a working model and direct multimodal interface. The first orbit is the primary provider's native capabilities. The next orbit is MCP, connected services, and useful local software. Runtime evolves in parallel as each capability needs execution, persistence, authority, or observability support.

Validation is derived from the capabilities actually present. Test scenarios prove architecture; they do not define Atlas's product semantics.