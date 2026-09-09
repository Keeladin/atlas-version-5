from sqlalchemy.exc import SQLAlchemyError

from atlas.artifacts.store import ArtifactStore
from atlas.capabilities import AuthorityMode, EffectKind, OperationDescriptor
from atlas.config import Settings
from atlas.db import get_session_factory
from atlas.integrations import GitHubMCPService, GoogleWorkspaceService
from atlas.memory import MemoryService
from atlas.memory.embeddings import OpenAIEmbeddingClient
from atlas.registry.repository import RegistryRepository
from atlas.registry.service import EnvironmentRegistry
from atlas.runtime.observations import EvidenceStore
from atlas.schedules import ScheduleService
from atlas.storage import LocalStorageService, ProjectFolderService
from atlas.storage.changes import ProjectChanges

from .service import CapabilityRuntime


def build_capability_runtime(settings: Settings, registry: EnvironmentRegistry) -> CapabilityRuntime:
    runtime = CapabilityRuntime(registry.operations())
    local = LocalStorageService(settings.workspace_root, settings.workspace_display_root)
    runtime.register_executor(
        "storage.local.list",
        lambda arguments: local.list_directory(str(arguments.get("path") or "")),
    )
    runtime.register_executor(
        "storage.local.acquire",
        lambda arguments: local.acquire_file(
            str(arguments.get("path") or ""),
            start_line=int(arguments["start_line"]) if arguments.get("start_line") is not None else None,
            max_lines=int(arguments["max_lines"]) if arguments.get("max_lines") is not None else None,
        ),
    )
    factory = get_session_factory()
    memory_embedder = (
        OpenAIEmbeddingClient(
            api_key=settings.openai_api_key,
            model=settings.memory_embedding_model,
            dimensions=settings.memory_embedding_dimensions,
        )
        if settings.openai_api_key is not None
        else None
    )
    memory = MemoryService(
        factory,
        chunk_chars=settings.memory_chunk_chars,
        embedder=memory_embedder,
        embedding_batch_size=settings.memory_embedding_batch_size,
        embedding_max_chunks_per_run=settings.memory_embedding_max_chunks_per_run,
    )
    runtime.register_executor("memory.search", memory.search)
    runtime.register_executor("memory.remember", memory.remember)
    runtime.register_executor("memory.correct", memory.correct)
    runtime.register_executor("memory.retire", memory.retire)
    runtime.register_executor("memory.restore", memory.restore)
    runtime.register_executor("memory.delete", memory.delete)
    runtime.register_executor("memory.commands.list", memory.commands)

    async def policy_reader():
        try:
            async with factory() as session:
                return await RegistryRepository(session).enabled_ids()
        except SQLAlchemyError:
            return set()  # Failure to verify owner permission denies all capability dispatch.
    runtime.policy_reader = policy_reader

    async def evidence_call(method, arguments):
        async with factory() as session:
            return await getattr(EvidenceStore(session, ArtifactStore(settings.artifact_dir)), method)(**arguments)

    runtime.register_executor("evidence.task.read", lambda arguments: evidence_call("task_read", arguments))
    runtime.register_executor("evidence.read", lambda arguments: evidence_call("read", arguments))
    runtime.register_executor("evidence.resource.acquire", lambda arguments: evidence_call("acquire", arguments))

    async def schedule_call(method: str, arguments):
        async with factory() as session:
            service = ScheduleService(session, settings.owner_timezone)
            result = await getattr(service, method)(arguments) if method != "list" else await service.list(include_disabled=bool(arguments.get("include_disabled", True)))
            await session.commit()
            return result

    runtime.register_executor("schedules.list", lambda arguments: schedule_call("list", arguments))
    runtime.register_executor("schedules.create", lambda arguments: schedule_call("create", arguments))
    runtime.register_executor("schedules.update", lambda arguments: schedule_call("update", arguments))
    runtime.register_executor("schedules.delete", lambda arguments: schedule_call("delete", arguments))

    projects = ProjectFolderService(settings.projects_root, settings.projects_display_root, settings.project_checkpoint_root)
    runtime.register_executor(
        "storage.projects.list",
        lambda arguments: projects.list_directory(str(arguments.get("path") or "")),
    )
    runtime.register_executor(
        "storage.projects.acquire",
        lambda arguments: projects.acquire_file(
            str(arguments.get("path") or ""),
            start_line=int(arguments["start_line"]) if arguments.get("start_line") is not None else None,
            max_lines=int(arguments["max_lines"]) if arguments.get("max_lines") is not None else None,
        ),
    )
    runtime.register_executor("storage.projects.status", lambda arguments: projects.git_status(str(arguments.get("project") or "")))
    runtime.register_executor("storage.projects.diff", lambda arguments: projects.git_diff(str(arguments.get("project") or "")))
    runtime.register_executor(
        "storage.projects.preview",
        lambda arguments: projects.preview_file(str(arguments.get("path") or ""), str(arguments.get("content") or "")),
    )
    changes = ProjectChanges(projects)
    runtime.register_executor(
        "storage.projects.apply",
        lambda arguments: changes.apply_file(
            str(arguments.get("path") or ""), str(arguments.get("content") or ""),
            str(arguments.get("expected_sha256") or ""), str(arguments.get("change_token") or ""),
        ),
    )
    runtime.register_executor(
        "storage.projects.move",
        lambda arguments: changes.move_file(
            str(arguments.get("source_path") or ""), str(arguments.get("target_path") or ""),
            str(arguments.get("expected_sha256") or ""),
        ),
    )
    runtime.register_executor(
        "storage.projects.delete",
        lambda arguments: changes.delete_file(str(arguments.get("path") or ""), str(arguments.get("expected_sha256") or "")),
    )
    if settings.github_configured and settings.github_token_file is not None:
        github = GitHubMCPService(
            settings.github_mcp_command,
            settings.github_token_file,
            settings.github_mcp_toolsets,
        )
        try:
            github_operations = github.discover_operations()
        except (RuntimeError, TypeError, OSError):
            github_operations = []
        for descriptor in github_operations:
            registry.register_operation(descriptor)
            runtime.register(
                descriptor,
                lambda arguments, operation_id=descriptor.id: github.call(operation_id, arguments),
            )
    if settings.gws_configured:
        drive = GoogleWorkspaceService(
            settings.gws_command,
            settings.gws_credentials_file,
            settings.gws_config_dir,
            settings.gws_workspace_dir,
        )
        runtime.register_executor(
            "drive.files.list",
            lambda arguments: drive.list_drive_folder(str(arguments.get("folder_id") or "root")),
        )
        try:
            auth_status = drive.auth_status()
            scopes = auth_status.get("scopes") if isinstance(auth_status, dict) else None
        except (RuntimeError, TypeError):
            scopes = []
        gmail_ready = isinstance(scopes, list) and any("gmail" in str(scope) or scope == "https://mail.google.com/" for scope in scopes)
        if gmail_ready:
            gmail_operations = [
                OperationDescriptor(
                    id="gmail.messages.search", capability_id="google.workspace", family="Gmail",
                    description="Search the owner's Gmail mailbox using Gmail search syntax and return matching message summaries.",
                    input_schema={"type":"object","properties":{"query":{"type":"string"},"max_results":{"type":"integer","minimum":1,"maximum":100}},"additionalProperties":False},
                    effect=EffectKind.READ, authority=AuthorityMode.AUTO, trust="external",
                ),
                OperationDescriptor(
                    id="gmail.message.read", capability_id="google.workspace", family="Gmail",
                    description="Read one Gmail message, including sender, recipients, subject, date, and body.",
                    input_schema={"type":"object","properties":{"message_id":{"type":"string"}},"required":["message_id"],"additionalProperties":False},
                    effect=EffectKind.READ, authority=AuthorityMode.AUTO, trust="external",
                ),
                OperationDescriptor(
                    id="gmail.message.send", capability_id="google.workspace", family="Gmail",
                    description="Send an email from the owner's connected Gmail account using the exact prepared recipients, subject, and body.",
                    input_schema={"type":"object","properties":{"to":{"type":"string"},"subject":{"type":"string"},"body":{"type":"string"},"cc":{"type":"string"},"bcc":{"type":"string"}},"required":["to","subject","body"],"additionalProperties":False},
                    effect=EffectKind.CREATE, authority=AuthorityMode.APPROVAL_REQUIRED, trust="external",
                ),
            ]
            executors = {
                "gmail.messages.search": lambda arguments: drive.gmail_search(str(arguments.get("query") or "is:unread"), int(arguments.get("max_results") or 20)),
                "gmail.message.read": lambda arguments: drive.gmail_read(str(arguments.get("message_id") or "")),
                "gmail.message.send": lambda arguments: drive.gmail_send(
                    to=str(arguments.get("to") or ""), subject=str(arguments.get("subject") or ""),
                    body=str(arguments.get("body") or ""), cc=str(arguments.get("cc") or ""), bcc=str(arguments.get("bcc") or ""),
                ),
            }
            for descriptor in gmail_operations:
                registry.register_operation(descriptor)
                runtime.register(descriptor, executors[descriptor.id])
        calendar_ready = isinstance(scopes, list) and any("calendar" in str(scope) for scope in scopes)
        if calendar_ready:
            calendar_operations = [
                OperationDescriptor(
                    id="calendar.agenda", capability_id="google.workspace", family="Google Calendar",
                    description="Read the owner's upcoming Google Calendar agenda across calendars for a bounded number of days.",
                    input_schema={"type":"object","properties":{"days":{"type":"integer","minimum":1,"maximum":31},"calendar":{"type":"string"},"timezone":{"type":"string"}},"additionalProperties":False},
                    effect=EffectKind.READ, authority=AuthorityMode.AUTO, trust="external",
                ),
                OperationDescriptor(
                    id="calendar.event.get", capability_id="google.workspace", family="Google Calendar",
                    description="Read one Google Calendar event by event ID.",
                    input_schema={"type":"object","properties":{"event_id":{"type":"string"},"calendar_id":{"type":"string"}},"required":["event_id"],"additionalProperties":False},
                    effect=EffectKind.READ, authority=AuthorityMode.AUTO, trust="external",
                ),
                OperationDescriptor(
                    id="calendar.freebusy", capability_id="google.workspace", family="Google Calendar",
                    description="Read busy periods for one or more Google Calendars within an exact time window.",
                    input_schema={"type":"object","properties":{"time_min":{"type":"string"},"time_max":{"type":"string"},"calendar_ids":{"type":"array","items":{"type":"string"}},"timezone":{"type":"string"}},"required":["time_min","time_max"],"additionalProperties":False},
                    effect=EffectKind.READ, authority=AuthorityMode.AUTO, trust="external",
                ),
                OperationDescriptor(
                    id="calendar.event.create", capability_id="google.workspace", family="Google Calendar",
                    description="Create a Google Calendar event using the exact prepared title, times, location, description, attendees, and meeting setting.",
                    input_schema={"type":"object","properties":{"summary":{"type":"string"},"start":{"type":"string"},"end":{"type":"string"},"calendar_id":{"type":"string"},"location":{"type":"string"},"description":{"type":"string"},"attendees":{"type":"array","items":{"type":"string"}},"meet":{"type":"boolean"}},"required":["summary","start","end"],"additionalProperties":False},
                    effect=EffectKind.CREATE, authority=AuthorityMode.APPROVAL_REQUIRED, trust="external",
                ),
                OperationDescriptor(
                    id="calendar.event.update", capability_id="google.workspace", family="Google Calendar",
                    description="Update an existing Google Calendar event with the exact prepared patch.",
                    input_schema={"type":"object","properties":{"event_id":{"type":"string"},"calendar_id":{"type":"string"},"changes":{"type":"object"}},"required":["event_id","changes"],"additionalProperties":False},
                    effect=EffectKind.UPDATE, authority=AuthorityMode.APPROVAL_REQUIRED, trust="external",
                ),
                OperationDescriptor(
                    id="calendar.event.delete", capability_id="google.workspace", family="Google Calendar",
                    description="Delete an existing Google Calendar event and notify attendees according to Google Calendar semantics.",
                    input_schema={"type":"object","properties":{"event_id":{"type":"string"},"calendar_id":{"type":"string"}},"required":["event_id"],"additionalProperties":False},
                    effect=EffectKind.DELETE, authority=AuthorityMode.APPROVAL_REQUIRED, trust="external",
                ),
            ]
            calendar_executors = {
                "calendar.agenda": lambda arguments: drive.calendar_agenda(days=int(arguments.get("days") or 7), calendar=str(arguments.get("calendar") or ""), timezone=str(arguments.get("timezone") or "")),
                "calendar.event.get": lambda arguments: drive.calendar_event_get(str(arguments.get("event_id") or ""), str(arguments.get("calendar_id") or "primary")),
                "calendar.freebusy": lambda arguments: drive.calendar_freebusy(time_min=str(arguments.get("time_min") or ""), time_max=str(arguments.get("time_max") or ""), calendar_ids=[str(item) for item in arguments.get("calendar_ids", [])] if isinstance(arguments.get("calendar_ids"), list) else None, timezone=str(arguments.get("timezone") or "")),
                "calendar.event.create": lambda arguments: drive.calendar_event_create(summary=str(arguments.get("summary") or ""), start=str(arguments.get("start") or ""), end=str(arguments.get("end") or ""), calendar_id=str(arguments.get("calendar_id") or "primary"), location=str(arguments.get("location") or ""), description=str(arguments.get("description") or ""), attendees=[str(item) for item in arguments.get("attendees", [])] if isinstance(arguments.get("attendees"), list) else None, meet=bool(arguments.get("meet", False))),
                "calendar.event.update": lambda arguments: drive.calendar_event_update(event_id=str(arguments.get("event_id") or ""), calendar_id=str(arguments.get("calendar_id") or "primary"), changes=arguments.get("changes") if isinstance(arguments.get("changes"), dict) else {}),
                "calendar.event.delete": lambda arguments: drive.calendar_event_delete(event_id=str(arguments.get("event_id") or ""), calendar_id=str(arguments.get("calendar_id") or "primary")),
            }
            for descriptor in calendar_operations:
                registry.register_operation(descriptor)
                runtime.register(descriptor, calendar_executors[descriptor.id])
    return runtime
