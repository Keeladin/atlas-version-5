# Atlas V5 Capability Projection

## Purpose

Capabilities are one projection of the Environment Registry. They describe what the agent can meaningfully do without dumping every raw tool definition into the model's context.

The capability layer is not a workflow registry and does not decide what step comes next.

## Capability sources

Capabilities may originate from:

- the selected model itself;
- provider-hosted tools;
- Atlas-owned MCP/client integrations;
- connected external services;
- installed local software or CLIs;
- a small Atlas-native function only where no better existing boundary exists.

Origin and transport are metadata. The model primarily needs a clear semantic description of what the capability accomplishes.

## Meaningful grouping

Raw functions should be grouped into capabilities humans and models can understand. For example, a mail-read capability may contain search, get-message, read-thread, attachment retrieval, or related raw operations.

The owner enables the meaningful capability rather than choosing which raw function Atlas must use. The model may choose any enabled underlying operation or combination that achieves the objective.

Atlas may replace vague upstream descriptions with clearer local descriptions while retaining the authoritative upstream schemas underneath. Capability families are descriptive/control metadata, not a second executable tool protocol; the actual operation retains its real schema, transport, provenance, and effect identity.
## Enabled projection

Control may know that a capability is provisioned while disabled. The agent-facing Environment Registry exposes only capabilities the owner has enabled. Availability is tracked separately from enablement, and hard runtime boundaries remain authoritative at execution.

Enabled but temporarily unhealthy capabilities may remain visible with precise availability state so the model can reason about authentication or service failure. Disabled capabilities are absent and not callable.

## Progressive detail

Large tool catalogs should use progressive disclosure. The registry may say that Google Workspace Mail is enabled without loading every Gmail schema.

When inference decides mail is relevant, Atlas can expose the applicable tool family or use provider-native tool search/deferred-loading features where available.

## Model changes

Changing the selected model can change model-native and provider-hosted capabilities. Atlas-owned MCP, services, local software, workspace, memory, and schedules remain part of the surrounding Atlas environment.

The Environment Registry updates the capability projection without redefining the owner's task.

## Tool priority

Before creating an Atlas-specific implementation, prefer native model capability, provider-native capability, existing MCP/service, then ordinary local software.

A capability being usable by Atlas does not make its implementation part of Atlas.

## Provider image generation

Image input and image output are separate boundaries. Atlas already accepts owner image attachments through the ordinary artifact/evidence path and lets the selected conversational model inspect them. Image generation does not replace or duplicate that path.

When an OpenAI key is configured, the Environment Registry provisions the provider capability `openai.images` with two operations:

- `image.generate` creates one new image from a prompt;
- `image.edit` creates a new image from an existing Atlas image artifact plus an edit/reference prompt.

Both operations are `create` effects with `Automatic` default authority. The owner's per-operation Control setting still wins. As with every newly provisioned capability, the persistent registry row starts disabled and must be enabled by the owner before the model can discover or dispatch it.

Provider image bytes are transient. Successful output is persisted by the existing Artifact Store as `kind=image`; capability evidence and the transcript receive only artifact identity and metadata. A generated artifact is attached to the final Atlas turn and rendered inline by the conversation UI. Editing reuses the exact frozen `artifact_id` from an owner attachment or acquired evidence resource rather than reconstructing the image from a textual description.

The default provider model is configurable through `ATLAS_OPENAI_IMAGE_MODEL` and is currently `gpt-image-2.5-sunburst`. The operation boundary deliberately remains provider-facing rather than adding a second Atlas-native image pipeline.
