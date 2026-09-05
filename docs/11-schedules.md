# Atlas V5 Schedules and Passive Automation

## Principle

A schedule is persisted intent bound to a deterministic trigger or execution window.

The owner expresses the intent naturally. Runtime stores and triggers it. When due, Atlas wakes a model with the stored intent and normal current environment; inference decides what the intent means and how to achieve it.

The scheduler does not encode the future workflow.

## Trigger forms

The architecture should support simple forms such as:

- one-off date/time;
- recurring time/date rule;
- execution window;
- event-driven trigger where a deterministic future event creates the wake-up; semantic conditions are evaluated by inference after a wake, not by the scheduler itself.

The exact representation is an implementation choice.

## Example

"Every morning at 07:00 summarize important mail" stores the intent and recurrence. At execution time, the model decides what counts as important, what mail capability to use, what period to inspect, and how to present the result.

A heavy indexing job may instead be allowed only within a configured overnight window. Window times, batch size, throttling, and similar operational policy remain tunable rather than architectural constants.

## Control

Schedules remain passive. The normal user creates them through conversation; Control may list, enable/disable, edit, run, inspect, or delete them when needed.

Each firing receives its own durable run identity and conversational/execution context. Current capability enablement, credentials, containment, and effect rules apply at execution time.

Runtime owns due-time/event detection, duplicate/missed-trigger handling, cancellation, overlap policy, and persistence. Model inference owns semantic execution. Conflicting foreground/background mutations must be coordinated deterministically rather than allowed to race silently.

Blocked, failed, uncertain, or waiting-for-owner runs remain observable after the model turn ends. The runtime rules are defined in `17-runtime-constitution.md`.