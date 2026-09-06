from pathlib import Path

from atlas.capabilities import AuthorityMode, EffectKind
from atlas.integrations.github import descriptors_from_mcp_tools
from atlas.integrations.mcp_stdio import MCPStdioClient


def test_github_tools_become_namespaced_read_operations() -> None:
    tools = [{
        "name": "search_repositories",
        "description": "Search repositories.",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
    }]

    descriptor = descriptors_from_mcp_tools(tools)[0]
    assert descriptor.id == "github.search_repositories"
    assert descriptor.family == "GitHub"
    assert descriptor.effect == EffectKind.READ
    assert descriptor.authority == AuthorityMode.AUTO
    assert descriptor.input_schema["properties"]["query"]["type"] == "string"


def test_mcp_stdio_client_initializes_lists_and_calls(tmp_path: Path) -> None:
    server = tmp_path / "fake-mcp.py"
    server.write_text("""#!/usr/bin/env python3
import json
import sys
for line in sys.stdin:
    request = json.loads(line)
    if request.get('id') == 1:
        print(json.dumps({'jsonrpc':'2.0','id':1,'result':{'protocolVersion':'2025-06-18','capabilities':{},'serverInfo':{'name':'fake','version':'1'}}}), flush=True)
    elif request.get('id') == 2 and request.get('method') == 'tools/list':
        print(json.dumps({'jsonrpc':'2.0','id':2,'result':{'tools':[{'name':'ping','description':'Ping','inputSchema':{'type':'object','properties':{}}}]}}), flush=True)
    elif request.get('id') == 2 and request.get('method') == 'tools/call':
        print(json.dumps({'jsonrpc':'2.0','id':2,'result':{'content':[{'type':'text','text':json.dumps({'pong':True})}]}}), flush=True)
""")
    server.chmod(0o755)

    client = MCPStdioClient(server)
    assert client.list_tools()[0]["name"] == "ping"
    assert client.call_tool("ping", {}) == {"pong": True}
