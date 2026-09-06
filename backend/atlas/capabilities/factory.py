from atlas.capabilities import AuthorityMode, EffectKind, OperationDescriptor
from atlas.config import Settings
from atlas.integrations import GitHubMCPService, GoogleWorkspaceService
from atlas.registry.service import EnvironmentRegistry
from atlas.storage import LocalStorageService

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
        lambda arguments: local.acquire_file(str(arguments.get("path") or "")),
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
    return runtime
