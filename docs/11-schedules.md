# Atlas V5 Schedules

## Principle

Scheduling is a passive Atlas faculty. The owner expresses intent in ordinary language; Atlas translates that intent into durable timing or trigger state behind the scenes.

The owner may inspect, edit, pause, run, or delete schedules through Control when useful, but ordinary use should not require interacting with a scheduling subsystem.

## Examples

"Every morning at 07:00 summarize my important emails" should create a durable scheduled trigger and enough task intent for a future model to perform the job.

"When new normalized manuals appear in this folder, index the documents I mark as worth remembering" may create an event-driven trigger plus a model-led task when the condition occurs.

"Remind me tomorrow afternoon" is a simple deferred event and should not require the machinery of a general workflow graph.

## Separation of concerns

The scheduler decides that a trigger is due. It does not decide how the objective should be achieved.

When a trigger fires, Atlas prepares the relevant contextual state, capabilities, workspace/resource references, and authority, then gives the objective to the selected model. The model works out the workflow at execution time.

This preserves the owner's durable intent without freezing a reasoning path months in advance.

## Schedule state

A schedule may need durable metadata such as owner intent, trigger definition, enabled state, next run, last run, model-routing preference if overridden, relevant resource references, and recent outcome.

The exact schema is an implementation concern for later design. The architecture should keep it substantially smaller than V4 Cadence/Work machinery unless real use cases prove otherwise.
