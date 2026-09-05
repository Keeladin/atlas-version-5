# Atlas V5 Product Definition

## Product promise

Atlas is a persistent local agent environment that lets a capable model work naturally with the owner's machine, connected services, files, artifacts, memory, and schedules without making the owner operate the underlying machinery.

The model is the agent. Atlas is the environment around it.

## Owner experience

Normal use happens on one main Atlas page centred on multimodal chat. The owner can type, attach images/documents and other supported artifacts, receive generated artifacts, and see useful workspace projections such as mail, files, previews, research, runtime health, or a compact Needs You view for blocked/uncertain work around the conversation.

A separate Control page exists for deliberate configuration, observability, diagnostics, and capability enablement. Ordinary work should not require navigating runtime subsystems.

## Behind the scenes

Atlas maintains the Environment Registry, transcript continuity, workspace state, artifact references/storage, provider/tool execution, credentials and authority boundaries, asynchronous memory processing, schedules, persistence, usage information, and diagnostics.

These are supporting faculties, not a menu of workflows the owner must drive.

## Product boundary

Atlas becomes more capable by exploiting the selected model/provider's native abilities first, then by adding MCPs, connected services, and useful local software.

Independent deterministic utilities can remain separate products. Atlas may know they exist and use them without owning their implementation or lifecycle.

**Atlas should know its environment, not absorb its environment.**