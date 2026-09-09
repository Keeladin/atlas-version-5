# Atlas V5 Shared-State Write Contract

Status: **Accepted implementation contract**
Date: 2026-09-09

## 1. Purpose

Atlas may have multiple independent conversation and background streams active at the same time. Those streams can read shared durable state, but they do not own it.

The rule is:

> **Conversations may propose. Runtime commits.**

This contract defines the smallest common concurrency boundary for mutable Atlas-owned state. It gives each write canonical provenance, idempotent identity, an optimistic version fence, an explicit outcome, and one atomic commit boundary.

It is deliberately not a workflow engine, distributed lock service, or semantic conflict resolver.

## 2. Scope

Use this contract for Atlas-owned shared mutable resources when two independent streams could otherwise overwrite each other silently.

Examples include derived durable memory, mutable project/runtime metadata, schedule definitions, and similar canonical state.

Do not force unrelated persistence through it. Canonical transcript turns already have transactional sequence allocation. Consequential external effects use the action/evidence/reconciliation contract because a database rollback cannot undo an email, calendar mutation, or remote API effect.

## 3. Canonical write envelope

Every shared-state mutation begins with a runtime-owned envelope:

```json
{
  "operation_id": "uuid",
  "resource_type": "durable_memory",
  "resource_id": "stable-resource-id",
  "operation": "upsert",
  "expected_version": 17,
  "payload": {},
  "actor": "memory_processor",
  "source_transcript_id": "uuid-or-null",
  "source_turn_id": "uuid-or-null",
  "source_run_id": "uuid-or-null"
}
```

`operation_id` identifies this exact attempted mutation across retries. `resource_type` plus `resource_id` identifies the shared resource being fenced. `operation` names the resource-specific mutation. `expected_version` is the caller's concurrency expectation. Provenance fields are bound by runtime from canonical state; model-authored content cannot replace them.

The payload is available to the resource-specific mutation code, but the generic operation ledger stores only a canonical SHA-256 payload hash. Canonical resource data remains in its proper resource table rather than being duplicated into a generic write log.

## 4. Version semantics

`shared_resource_versions` holds one monotonic integer per `(resource_type, resource_id)`.

Version `0` means no successful mutating commit has yet passed through this fence. Each successful mutation increments the version exactly once. A committed no-op does not increment it.

`expected_version` has three meanings:

- `0` — apply only if this resource has not changed through the shared-state boundary yet;
- `N > 0` — apply only if the current resource version is exactly `N`;
- `null` — no optimistic comparison. This is allowed only when the resource-specific operation is deliberately safe as a blind/commutative/idempotent write. It is not the default escape hatch for stale callers.

If the expected version does not match, the mutation callback is not executed. Runtime records a `conflict/version_conflict` receipt containing the observed version.

The version fence is about write ordering, not semantic truth. A memory processor still decides whether two facts should merge or supersede; the runtime only ensures that decision is not applied to a resource version different from the one it evaluated.

## 5. Idempotency semantics

An operation ID must be created before the write is attempted and reused for every retry of that exact write.

If the same operation ID is seen again with the same resource identity, operation, expected version, payload hash, actor, and provenance, Atlas returns the already durable receipt and does not run the mutation again.

If an operation ID is reused with any different identity or payload, Atlas raises an idempotency conflict and performs no mutation. Operation IDs are therefore identities, not convenient request labels.

Two different operation IDs may legitimately target the same resource. The resource version fence decides which stale/parallel mutations may commit.

## 6. Outcomes

The minimal durable outcomes are:

- `committed/applied` — mutation executed and the resource version incremented;
- `committed/no_change` — resource-specific code determined that no canonical change was needed, so the version remained unchanged;
- `conflict/version_conflict` — caller evaluated an older or otherwise different resource version; mutation did not execute.

An unexpected exception inside the local mutation aborts the database transaction. No successful receipt or version increment survives that rollback. Callers may retry the same operation ID after fixing/recovering the local failure.

This contract must not wrap non-transactional external effects inside its mutation callback. Those remain governed by Atlas action identity, uncertain-state handling, and reconciliation.

## 7. What counts as a commit

A shared-state write is committed only when one PostgreSQL transaction durably contains all of the following:

1. the claimed operation identity and canonical provenance;
2. the resource-specific mutation, if any;
3. the corresponding resource-version increment, if mutated;
4. the final operation receipt.

There is no successful state in which the resource changed but its version/receipt did not, or the version advanced without the resource mutation. A process crash before transaction commit leaves none of those changes visible.

A deterministic version conflict is itself committed as an operation outcome, but it does not mutate the resource or advance its version.

## 8. Locking and concurrency

For one-resource writes, the implementation claims the operation ID and then serializes on the target resource-version row with a PostgreSQL row lock.

This makes two parallel writes to the same resource deterministic: after one commits, the other observes the new version and conflicts if its expectation is stale. Writes to unrelated resources remain independent.

The minimal contract covers one fenced resource per envelope. A future operation that must atomically mutate several independently fenced resources must define a deterministic multi-resource lock order rather than acquiring them ad hoc.

## 9. Memory integration

Foreground `memory_candidates` remain non-authoritative proposals. Candidate intake may deduplicate and queue them without pretending that they already changed durable owner memory.

When the asynchronous memory processor later evaluates a candidate against valid current memory, its create/merge/supersede decision must be applied through this shared-state boundary. The processor therefore evaluates a known memory resource version and commits only against that same version. If another chat, explicit owner command, or worker changes the target first, the stale processor write conflicts and must re-read/reason rather than overwrite newer state.

Explicit owner remember/correct/forget commands keep their existing precedence and command ledger. They already use transactional resource-specific mutation. Retrofitting them onto this generic fence is optional hardening, not a prerequisite for derived-memory reconciliation.

## 10. Deliberate non-goals

This primitive does not decide:

- whether a memory candidate is important;
- whether two facts are contradictory;
- which fact is semantically newer or more authoritative;
- whether the model should retry after a conflict;
- how external APIs reconcile uncertain effects;
- how owner files modified outside Atlas are locked.

Those decisions remain at their existing semantic or resource boundary. The shared-write layer only prevents hidden lost updates and duplicate local mutations.

## 11. Implementation mapping

The concrete implementation lives in `backend/atlas/persistence/shared_writes.py` with persistent rows in `shared_resource_versions` and `shared_write_operations`.

Migration `25a10` creates those ledgers. Regression coverage proves atomic commit/version advancement, stale-write rejection without callback execution, idempotent retry, operation-ID misuse rejection, and the one-winner/one-conflict result for concurrent writes to one resource.
