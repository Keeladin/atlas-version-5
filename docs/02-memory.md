# Atlas V5 Memory Model

## 1. Principle

Atlas memory is divided conceptually into **contextual memory** and **embedded memory**.

The computer analogy is deliberate:

- contextual memory is RAM;
- embedded memory is long-term storage.

The active model should not perform memory housekeeping while it is trying to answer or work. Memory write-back is an asynchronous side process outside the active context frame.

## 2. Contextual memory

Contextual memory is the current seat state presented to the model. It is small enough to read in one sweep and rich enough for a model to enter an ongoing task without reconstructing the world.

It may contain:

- current conversation and immediate intent;
- active objective and workspace state;
- recent tool results;
- unresolved questions or decisions;
- relevant owner preferences already known to matter;
- current capability and authority summaries;
- selected material retrieved from embedded memory.

It changes continuously and is not automatically permanent.

## 3. Embedded memory

Embedded memory is durable, large, semantically retrievable memory. It is not injected wholesale into prompts. Relevant material is retrieved when needed and promoted into contextual memory.
## 4. Memory write-back

When information is no longer needed in active context, it can enter a memory outbox. A background processor then decides what happens to it.

Possible outcomes include:

- embed/index for future semantic recall;
- preserve as a canonical durable fact, preference, decision, or record;
- merge or supersede an existing memory;
- retain only as ordinary conversation or execution history;
- discard as temporary conversational debris.

This process should be asynchronous and batchable. A reply should not wait while Atlas decides whether every sentence deserves long-term memory.

The original source should remain traceable. Derived memory should retain provenance to the conversation, document, tool result, or owner statement from which it came.

## 5. Three similar statements, three different states

"Send an email to Daniel" is immediate intent. It belongs in current context while Atlas performs the task. The resulting send receipt may be durable evidence, but the instruction itself is not a long-term owner memory.

"What was the last email Daniel sent me?" is a retrieval request. Gmail is the authoritative source. The retrieved email may enter contextual memory temporarily, then expire.

"Emails from Daniel are important to me" is a durable owner preference. It should influence future behavior and therefore belongs in long-term memory.

The memory system must understand the role information plays, not merely whether a sentence contains a fact.

## 6. External truth is not automatically memory

Atlas should not copy every external fact it encounters into long-term memory. Gmail, Drive, GitHub, filesystems, databases, and other connected systems remain authoritative for their own current state.

Memory should preserve what improves future reasoning, continuity, or personalization without turning Atlas into an uncontrolled duplicate of every source it can read.
