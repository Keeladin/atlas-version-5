# 25. Foreground temporal context

## Purpose

Atlas needs enough temporal orientation to understand continuity without turning time itself into a running commentary. The design is intentionally small: runtime supplies trustworthy temporal facts; the foreground model interprets their significance in context.

The feature is called **Passage of Time (POT)** in design discussion. It is not a separate state machine, freshness engine, or elapsed-time classifier.

## Design rule

**Runtime measures time; the model interprets temporal significance.**

The same elapsed duration can mean very different things. Three hours since coffee may be unimportant; three hours spent waiting may be central. Atlas therefore does not use hard-coded elapsed-time bands such as “one hour is short” or “one day is stale.”

Relevance determines whether time matters. Context, expected cadence, volatility, urgency and the subject under discussion determine what the elapsed time means.

## Model-visible temporal facts

On each foreground inference Atlas receives:

1. a current owner-local date/time derived at request time using the configured `owner_timezone`;
2. full owner-local timestamps on projected **owner turns**.

Atlas and tool turns are not timestamped in the conversational projection. They inherit the conversational moment established by the surrounding owner turn, which keeps the transcript readable and avoids turning model context into telemetry.

Timestamps include the date as well as time-of-day so the model can reason across midnight, weekends, weeks and longer gaps.

## Timezone boundary

Canonical timestamps remain timezone-aware and UTC-based at rest. Owner-facing and foreground-model temporal context is rendered in the configured IANA timezone, currently `Africa/Johannesburg`.

The Ubuntu host is also configured for `Africa/Johannesburg` so shell and host-local displays match the owner. This host setting does not change canonical database timestamp semantics. Chrony/NTP remains infrastructure below the Atlas reasoning boundary; Atlas sees owner-local time, not NTP details.

## Behavioral intent

Time should normally remain **latent context**. Atlas should let elapsed time influence continuity, urgency, freshness and expectations, then mention time explicitly only when doing so materially improves the response.

Examples of intended effects:

- resume naturally when a gap does not alter the situation;
- re-orient when elapsed time could materially change task state or expectations;
- treat volatile operational facts as candidates for re-checking when enough time has passed for change to matter;
- avoid treating age alone as evidence that stable information has become false;
- avoid narrating “it has been X hours/days” unless that elapsed time is itself relevant.

## Implementation

The foreground instruction builder accepts an owner timezone and inserts the current owner-local datetime plus the temporal interpretation rule. Working-context projection converts persisted owner-turn `created_at` timestamps into the same timezone and prefixes the owner turn with a compact timestamp marker.

The scheduler uses the same timezone-aware instruction path for scheduled inference. Schedule storage remains canonical UTC while scheduled intent retains its named timezone.

The implementation is in release `9ab22de` (`Add foreground temporal context`). No schema migration was required.

## Deliberate non-features

This change does **not** add:

- fixed elapsed-time thresholds;
- a temporal importance score;
- per-memory staleness rules;
- timestamps on every projected Atlas/tool turn;
- a second POT database or worker;
- automatic claims that older context is stale.

If later observation shows that summarized context needs stronger dating, capsule or memory timestamps can be exposed selectively. That should be driven by an observed failure, not added pre-emptively.

## Validation and deployment receipt

Before commit, the focused conversation-context tests passed 9/9. After the two locally exposed regression expectations were aligned with timestamped owner turns and the larger instruction payload, the local backend suite passed **190 tests with 217 skipped**.

Release `9ab22de` was pushed to `memory-state-machine-v1` and `main` and deployed successfully. `/opt/atlas-v5/app/RELEASE` reports `9ab22de`; `atlas-v5.service` is active.

GitHub CI run 30 later exposed one unrelated stale wording assertion in `tests/test_memory_conflicts.py`: production code now says `Memory attention (runtime state)` while that test still expected `Atlas pending owner memory attention`. CI otherwise reported **406 passed, 1 failed** in the backend job and the frontend job passed. Commit `eb2d37b` corrected that assertion, and CI run 31 passed both backend and frontend jobs.

A point-in-time operational receipt is stored at `/home/jaco/Workspace/Exports/atlas-v5-pot-receipt-2026-09-13.txt`.
