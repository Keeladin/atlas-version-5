from atlas.capabilities import AuthorityMode, EffectKind, OperationDescriptor
from atlas.config import Settings

from .models import (
    CapabilityAvailability,
    CapabilityEntry,
    CapabilitySource,
)


class EnvironmentRegistry:
    def __init__(
        self,
        entries: list[CapabilityEntry] | None = None,
        operations: list[OperationDescriptor] | None = None,
    ) -> None:
        self._entries = {entry.id: entry for entry in entries or []}
        self._operations = {operation.id: operation for operation in operations or []}

    def upsert(self, entry: CapabilityEntry) -> None:
        self._entries[entry.id] = entry

    def register_operation(self, operation: OperationDescriptor) -> None:
        entry = self._entries.get(operation.capability_id)
        if entry is None or not entry.enabled or entry.availability != CapabilityAvailability.AVAILABLE:
            return
        self._operations[operation.id] = operation
        if operation.id not in entry.executable_operations:
            entry.executable_operations.append(operation.id)

    def all_entries(self) -> list[CapabilityEntry]:
        return sorted(self._entries.values(), key=lambda item: item.id)

    def enabled_projection(self) -> list[CapabilityEntry]:
        return [entry for entry in self.all_entries() if entry.enabled]

    def operations(self) -> list[OperationDescriptor]:
        return sorted(self._operations.values(), key=lambda item: item.id)


def build_phase0_registry(settings: Settings | None = None) -> EnvironmentRegistry:
    gws_ready = bool(settings and settings.gws_configured)
    entries = [
        CapabilityEntry(id="atlas.evidence", family="Evidence",
            description="Read exact historical observations and resource snapshots by evidence identity.",
            source=CapabilitySource.ATLAS, enabled=True, availability=CapabilityAvailability.AVAILABLE),
        CapabilityEntry(
            id="atlas.memory",
            family="Memory",
            description="Search canonical history and manage explicit owner-directed memory lifecycle: remember, correct, retire, restore, and delete.",
            source=CapabilitySource.ATLAS,
            enabled=True,
            availability=CapabilityAvailability.AVAILABLE,
            executable_operations=[
                "memory.search", "memory.remember", "memory.correct",
                "memory.retire", "memory.restore", "memory.delete", "memory.commands.list",
                "memory.obligations.list", "memory.obligations.resolve",
            ],
        ),
        CapabilityEntry(
            id="atlas.artifacts",
            family="Artifacts",
            description="Store and reference first-class conversation artifacts.",
            source=CapabilitySource.ATLAS,
            enabled=True,
            availability=CapabilityAvailability.AVAILABLE,
            executable_operations=[],
        ),
        CapabilityEntry(
            id="openai.web",
            family="Web",
            description="Search and read the public web using the active model provider's native web capability.",
            source=CapabilitySource.PROVIDER,
            enabled=bool(settings and settings.openai_api_key is not None),
            availability=(CapabilityAvailability.AVAILABLE if settings and settings.openai_api_key is not None else CapabilityAvailability.AUTHENTICATION_REQUIRED),
            executable_operations=[],
            trust="external",
        ),
        CapabilityEntry(
            id="atlas.local_storage",
            family="Storage",
            description="Browse the owner-approved local workspace root.",
            source=CapabilitySource.ATLAS,
            enabled=True,
            availability=CapabilityAvailability.AVAILABLE,
            executable_operations=["storage.local.list", "storage.local.acquire"],
        ),
        CapabilityEntry(
            id="atlas.project_folders",
            family="Project folders",
            description="Inspect owner project files and stage downloadable changes for owner integration.",
            source=CapabilitySource.ATLAS,
            enabled=True,
            availability=CapabilityAvailability.AVAILABLE,
            executable_operations=[
                "storage.projects.list", "storage.projects.acquire", "storage.projects.status",
                "storage.projects.diff", "storage.projects.preview", "storage.projects.apply",
                "storage.projects.move", "storage.projects.delete",
            ],
        ),
        CapabilityEntry(
            id="atlas.schedules",
            family="Scheduled tasks",
            description="Persist and run owner-approved scheduled Atlas tasks.",
            source=CapabilitySource.ATLAS,
            enabled=True,
            availability=CapabilityAvailability.AVAILABLE,
            executable_operations=["schedules.list", "schedules.create", "schedules.update", "schedules.delete"],
        ),
        CapabilityEntry(
            id="github.mcp",
            family="GitHub",
            description="Access owner-authorized GitHub repositories through GitHub's official MCP server.",
            source=CapabilitySource.MCP,
            enabled=bool(settings and settings.github_configured),
            availability=(
                CapabilityAvailability.AVAILABLE
                if settings and settings.github_configured
                else CapabilityAvailability.AUTHENTICATION_REQUIRED
            ),
            executable_operations=[],
            trust="external",
        ),
        CapabilityEntry(
            id="google.workspace",
            family="Google Workspace",
            description="Access owner-authorized Google Workspace services through the Google Workspace MCP boundary.",
            source=CapabilitySource.MCP,
            enabled=gws_ready,
            availability=(
                CapabilityAvailability.AVAILABLE
                if gws_ready
                else CapabilityAvailability.AUTHENTICATION_REQUIRED
            ),
            executable_operations=["drive.files.list"] if gws_ready else [],
            trust="external",
        ),
    ]
    registry = EnvironmentRegistry(entries)
    registry.register_operation(OperationDescriptor(
        id="evidence.task.read", capability_id="atlas.evidence", family="Evidence",
        description="Page the exact current task checkpoint, including all unresolved action references. Pin expected_revision across pages; restart if it changes.",
        input_schema={"type": "object", "properties": {
            "task_id": {"type": "string", "format": "uuid"}, "expected_revision": {"type": "integer", "minimum": 0},
            "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 8000}},
            "required": ["task_id", "expected_revision"], "additionalProperties": False}, trust="internal"))
    registry.register_operation(OperationDescriptor(
        id="evidence.read", capability_id="atlas.evidence", family="Evidence",
        description="Read exact canonical evidence by evidence_id with actor/source metadata; use JSON pointer and character offset/limit for bounded reads. For a referenced text artifact supply artifact_id.",
        input_schema={"type": "object", "properties": {
            "evidence_id": {"type": "string", "format": "uuid"},
            "artifact_id": {"type": "string", "format": "uuid"}, "pointer": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 8000}},
            "required": ["evidence_id"], "additionalProperties": False}, trust="external"))
    registry.register_operation(OperationDescriptor(
        id="evidence.resource.acquire", capability_id="atlas.evidence", family="Evidence",
        description="Reacquire the exact previously observed resource snapshot, including images/documents, using its evidence_id and artifact_id.",
        input_schema={"type": "object", "properties": {"evidence_id": {"type": "string", "format": "uuid"},
            "artifact_id": {"type": "string", "format": "uuid"}},
            "required": ["evidence_id", "artifact_id"], "additionalProperties": False}, trust="external"))
    registry.register_operation(OperationDescriptor(
        id="memory.search", capability_id="atlas.memory", family="Memory",
        description="Search applicable active durable memory plus indexed canonical transcript history. Lifecycle, authority, scope and temporal applicability constrain results before relevance ranking. Set include_historical only when the owner explicitly asks about prior/superseded state; retired and deleted memory are never returned.",
        input_schema={"type": "object", "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            "transcript_id": {"type": "string", "format": "uuid"},
            "before_sequence": {"type": "integer", "minimum": 1},
            "include_historical": {"type": "boolean", "description": "Include superseded historical memory only for explicitly historical questions."},
            "exclude_chunk_ids": {"type": "array", "items": {"type": "string", "format": "uuid"}, "maxItems": 50}},
            "required": ["query"],
            "dependentRequired": {"before_sequence": ["transcript_id"]},
            "additionalProperties": False},
        effect=EffectKind.READ, authority=AuthorityMode.AUTO, trust="internal"))
    registry.register_operation(OperationDescriptor(
        id="memory.remember", capability_id="atlas.memory", family="Memory",
        description="Persist or explicitly restore one concise owner-directed memory. Deleted identities are terminal; reintroduced information receives a new memory ID.",
        input_schema={"type":"object","properties":{
            "content":{"type":"string","minLength":1,"maxLength":4000},
            "kind":{"type":"string","enum":["identity","preference","fact","decision","relationship","procedure","project_state","intent"]},
            "scope":{"type":"string","enum":["chat","cross_chat"]},
            "durability":{"type":"string","enum":["short_term","long_term"]},
            "subject":{"type":"string","maxLength":160},
            "namespace":{"type":"string","maxLength":160}},
            "required":["content"],"additionalProperties":False},
        effect=EffectKind.CREATE, authority=AuthorityMode.AUTO, trust="internal"))
    registry.register_operation(OperationDescriptor(
        id="memory.correct", capability_id="atlas.memory", family="Memory",
        description="Apply an explicit owner correction or change-over-time. The previous claim remains historical/superseded rather than deleted.",
        input_schema={"type":"object","properties":{
            "memory_id":{"type":"string","format":"uuid"},
            "old_content":{"type":"string","minLength":1,"maxLength":4000},
            "content":{"type":"string","minLength":1,"maxLength":4000},
            "change_type":{"type":"string","enum":["correction","change_over_time"]}},
            "required":["content"],"anyOf":[{"required":["memory_id"]},{"required":["old_content"]}],
            "additionalProperties":False},
        effect=EffectKind.UPDATE, authority=AuthorityMode.AUTO, trust="internal"))
    registry.register_operation(OperationDescriptor(
        id="memory.retire", capability_id="atlas.memory", family="Memory",
        description="Retain selected information for owner inspection/restoration but exclude it from model recall and automatic re-promotion from the same sources.",
        input_schema={"type":"object","properties":{
            "memory_id":{"type":"string","format":"uuid"},
            "content":{"type":"string","minLength":1,"maxLength":4000}},
            "anyOf":[{"required":["memory_id"]},{"required":["content"]}],
            "additionalProperties":False},
        effect=EffectKind.UPDATE, authority=AuthorityMode.AUTO, trust="internal"))
    registry.register_operation(OperationDescriptor(
        id="memory.restore", capability_id="atlas.memory", family="Memory",
        description="Restore one retired memory to active recall. Deleted memory identities cannot be restored.",
        input_schema={"type":"object","properties":{
            "memory_id":{"type":"string","format":"uuid"}},
            "required":["memory_id"],"additionalProperties":False},
        effect=EffectKind.UPDATE, authority=AuthorityMode.AUTO, trust="internal"))
    registry.register_operation(OperationDescriptor(
        id="memory.delete", capability_id="atlas.memory", family="Memory",
        description="Remove selected information from Atlas live memory records and, when requested, safely isolated supporting transcript passages and dependent derived copies. Stable IDs remain as content-free deletion identities; backups have separate retention policy.",
        input_schema={"type":"object","properties":{
            "memory_id":{"type":"string","format":"uuid"},
            "content":{"type":"string","minLength":1,"maxLength":4000},
            "scope":{"type":"string","enum":["memory_only","memory_and_sources"]}},
            "anyOf":[{"required":["memory_id"]},{"required":["content"]}],
            "additionalProperties":False},
        effect=EffectKind.DELETE, authority=AuthorityMode.AUTO, trust="internal"))
    registry.register_operation(OperationDescriptor(
        id="memory.commands.list", capability_id="atlas.memory", family="Memory",
        description="Inspect the content-free owner memory command ledger and pending/applied/failed operation lifecycle.",
        input_schema={"type":"object","properties":{
            "status":{"type":"string","enum":["pending","applied","failed"]},
            "limit":{"type":"integer","minimum":1,"maximum":100}},
            "additionalProperties":False},
        effect=EffectKind.READ, authority=AuthorityMode.AUTO, trust="internal"))
    registry.register_operation(OperationDescriptor(
        id="memory.obligations.list", capability_id="atlas.memory", family="Memory",
        description="List pending or resolved memory confirmation/conflict obligations without exposing hidden model reasoning.",
        input_schema={"type":"object","properties":{
            "status":{"type":"string","enum":["pending","resolved"]},
            "kind":{"type":"string","enum":["memory_confirmation","memory_review","memory_conflict","explicit_remember"]},
            "limit":{"type":"integer","minimum":1,"maximum":100}},
            "additionalProperties":False},
        effect=EffectKind.READ, authority=AuthorityMode.AUTO, trust="internal"))
    registry.register_operation(OperationDescriptor(
        id="memory.obligations.resolve", capability_id="atlas.memory", family="Memory",
        description="Resolve exactly one owner memory review/confirmation. Use the review_version returned by memory.obligations.list; stale versions are rejected and must be re-rendered. Optional content is edited assertion text and becomes fresh canonical owner evidence. Conflict obligations require substantive clarification instead.",
        input_schema={"type":"object","properties":{
            "obligation_id":{"type":"string","format":"uuid"},
            "decision":{"type":"string","enum":["confirm","reject","retry"]},
            "review_version":{"type":"string","minLength":1,"maxLength":128},
            "content":{"type":"string","minLength":1,"maxLength":4000}},
            "required":["obligation_id","decision"],"additionalProperties":False},
        effect=EffectKind.UPDATE, authority=AuthorityMode.AUTO, trust="internal"))
    registry.register_operation(OperationDescriptor(
        id="storage.local.list",
        capability_id="atlas.local_storage",
        family="Local storage",
        description="List files and folders inside the owner-approved local Atlas workspace.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path relative to the approved workspace root."}},
            "additionalProperties": False,
        },
        effect=EffectKind.READ,
        authority=AuthorityMode.AUTO,
    ))
    registry.register_operation(OperationDescriptor(
        id="storage.projects.list",
        capability_id="atlas.project_folders",
        family="Project folders",
        description="List files and folders inside the owner's approved local development project root.",
        input_schema={"type":"object","properties":{"path":{"type":"string"}},"additionalProperties":False},
        effect=EffectKind.READ,
        authority=AuthorityMode.AUTO,
    ))
    registry.register_operation(OperationDescriptor(
        id="storage.local.acquire",
        capability_id="atlas.local_storage",
        family="Local storage",
        description="Acquire one local workspace file as a model-readable resource; use optional line ranges for large UTF-8 text files.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to the approved workspace root."},
                "start_line": {"type": "integer", "minimum": 1, "description": "Optional first UTF-8 text line to acquire."},
                "max_lines": {"type": "integer", "minimum": 1, "maximum": 2000, "description": "Optional bounded UTF-8 text line count."},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        effect=EffectKind.READ,
        authority=AuthorityMode.AUTO,
    ))
    if gws_ready:
        registry.register_operation(OperationDescriptor(
            id="drive.files.list",
            capability_id="google.workspace",
            family="Google Drive",
            description="List files and folders in an owner-authorized Google Drive folder.",
            input_schema={
                "type": "object",
                "properties": {"folder_id": {"type": "string", "description": "Google Drive folder ID; use root for My Drive."}},
                "additionalProperties": False,
            },
            effect=EffectKind.READ,
            authority=AuthorityMode.AUTO,
            trust="external",
        ))

    registry.register_operation(OperationDescriptor(
        id="schedules.list", capability_id="atlas.schedules", family="Scheduled tasks",
        description="List the owner's scheduled Atlas tasks and their next-run state.",
        input_schema={"type":"object","properties":{"include_disabled":{"type":"boolean"}},"additionalProperties":False},
        effect=EffectKind.READ, authority=AuthorityMode.AUTO,
    ))
    registry.register_operation(OperationDescriptor(
        id="schedules.create", capability_id="atlas.schedules", family="Scheduled tasks",
        description="Create a future Atlas task. Use once with an ISO timestamp, interval with minutes, or cron with a five-field cron expression.",
        input_schema={"type":"object","properties":{"title":{"type":"string"},"prompt":{"type":"string"},"schedule_kind":{"type":"string","enum":["once","interval","cron"]},"schedule_value":{"type":"string"},"timezone":{"type":"string"}},"required":["title","prompt","schedule_kind","schedule_value"],"additionalProperties":False},
        effect=EffectKind.CREATE, authority=AuthorityMode.APPROVAL_REQUIRED,
    ))
    registry.register_operation(OperationDescriptor(
        id="schedules.update", capability_id="atlas.schedules", family="Scheduled tasks",
        description="Change, pause, or resume an existing scheduled Atlas task.",
        input_schema={"type":"object","properties":{"task_id":{"type":"string"},"title":{"type":"string"},"prompt":{"type":"string"},"schedule_kind":{"type":"string","enum":["once","interval","cron"]},"schedule_value":{"type":"string"},"timezone":{"type":"string"},"enabled":{"type":"boolean"}},"required":["task_id"],"additionalProperties":False},
        effect=EffectKind.UPDATE, authority=AuthorityMode.APPROVAL_REQUIRED,
    ))
    registry.register_operation(OperationDescriptor(
        id="schedules.delete", capability_id="atlas.schedules", family="Scheduled tasks",
        description="Permanently delete a scheduled Atlas task.",
        input_schema={"type":"object","properties":{"task_id":{"type":"string"}},"required":["task_id"],"additionalProperties":False},
        effect=EffectKind.DELETE, authority=AuthorityMode.APPROVAL_REQUIRED,
    ))

    registry.register_operation(OperationDescriptor(
        id="storage.projects.acquire",
        capability_id="atlas.project_folders",
        family="Project folders",
        description="Acquire one project file as a model-readable resource; use optional line ranges for large UTF-8 text files.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "minimum": 1},
                "max_lines": {"type": "integer", "minimum": 1, "maximum": 2000},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        effect=EffectKind.READ,
        authority=AuthorityMode.AUTO,
    ))
    registry.register_operation(OperationDescriptor(
        id="storage.projects.status", capability_id="atlas.project_folders", family="Project folders",
        description="Inspect Git baseline and working-tree status for one local project before making changes.",
        input_schema={"type":"object","properties":{"project":{"type":"string"}},"required":["project"],"additionalProperties":False},
        effect=EffectKind.READ, authority=AuthorityMode.AUTO,
    ))
    registry.register_operation(OperationDescriptor(
        id="storage.projects.diff", capability_id="atlas.project_folders", family="Project folders",
        description="Inspect the current Git diff for one local project.",
        input_schema={"type":"object","properties":{"project":{"type":"string"}},"required":["project"],"additionalProperties":False},
        effect=EffectKind.READ, authority=AuthorityMode.AUTO,
    ))
    registry.register_operation(OperationDescriptor(
        id="storage.projects.preview", capability_id="atlas.project_folders", family="Project folders",
        description="Preview an exact single-file create or update as a unified diff. Returns the file hash and change token required to apply it.",
        input_schema={"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"}},"required":["path","content"],"additionalProperties":False},
        effect=EffectKind.READ, authority=AuthorityMode.AUTO,
    ))
    registry.register_operation(OperationDescriptor(
        id="storage.projects.apply", capability_id="atlas.project_folders", family="Project folders",
        description="Stage one previewed UTF-8 project-file create/update as a downloadable change bundle. Live owner files stay untouched; the owner integrates the change.",
        input_schema={"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"},"expected_sha256":{"type":"string"},"change_token":{"type":"string"}},"required":["path","content","expected_sha256","change_token"],"additionalProperties":False},
        effect=EffectKind.UPDATE, authority=AuthorityMode.AUTO,
    ))
    registry.register_operation(OperationDescriptor(
        id="storage.projects.move", capability_id="atlas.project_folders", family="Project folders",
        description="Stage a same-project file move as a downloadable change bundle. Verify the source hash and absent destination; do not move the live owner file.",
        input_schema={"type":"object","properties":{"source_path":{"type":"string"},"target_path":{"type":"string"},"expected_sha256":{"type":"string"}},"required":["source_path","target_path","expected_sha256"],"additionalProperties":False},
        effect=EffectKind.UPDATE, authority=AuthorityMode.AUTO,
    ))
    registry.register_operation(OperationDescriptor(
        id="storage.projects.delete", capability_id="atlas.project_folders", family="Project folders",
        description="Stage a project-file deletion after owner approval and hash verification. Live files remain untouched; the owner integrates the deletion.",
        input_schema={"type":"object","properties":{"path":{"type":"string"},"expected_sha256":{"type":"string"}},"required":["path","expected_sha256"],"additionalProperties":False},
        effect=EffectKind.DELETE, authority=AuthorityMode.APPROVAL_REQUIRED,
    ))
    return registry
