from pathlib import Path
from typing import Any

from atlas.capabilities import AuthorityMode, EffectKind, OperationDescriptor

from .mcp_stdio import MCPStdioClient


class GitHubMCPService:
    def __init__(self, command: Path, token_file: Path, toolsets: str) -> None:
        token = token_file.read_text().strip()
        if not token:
            raise RuntimeError("GitHub credential is empty")
        self.client = MCPStdioClient(
            command,
            ["stdio", "--read-only", f"--toolsets={toolsets}"],
            environment={"GITHUB_PERSONAL_ACCESS_TOKEN": token},
        )

    def discover_operations(self) -> list[OperationDescriptor]:
        return descriptors_from_mcp_tools(self.client.list_tools())

    def call(self, operation_id: str, arguments: dict[str, Any]) -> Any:
        prefix = "github."
        if not operation_id.startswith(prefix):
            raise ValueError("Not a GitHub operation")
        return self.client.call_tool(operation_id[len(prefix):], arguments)


def descriptors_from_mcp_tools(tools: list[dict[str, Any]]) -> list[OperationDescriptor]:
    descriptors: list[OperationDescriptor] = []
    for tool in tools:
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            continue
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        description = str(tool.get("description") or f"GitHub MCP operation {name}.")
        descriptors.append(OperationDescriptor(
            id=f"github.{name}",
            capability_id="github.mcp",
            family="GitHub",
            description=description,
            input_schema=schema,
            effect=EffectKind.READ,
            authority=AuthorityMode.AUTO,
            trust="external",
        ))
    return sorted(descriptors, key=lambda item: item.id)
