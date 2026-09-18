#!/usr/bin/env python3
"""Structured privileged host operations for Atlas V5.

This process runs as root behind a Unix socket. It contains no owner-policy logic: Atlas
Control is the authority source. The broker only exposes named, structured operations and
never accepts an arbitrary shell command.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "2025-06-18"
SCOPES = Path(os.environ.get("ATLAS_HOST_SCOPES_FILE", "/var/lib/atlas-v5/control/host-scopes.json"))


def run(argv: list[str], *, timeout: int = 120) -> dict[str, Any]:
    cp = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    out = {"argv": argv, "returncode": cp.returncode, "stdout": cp.stdout[-262144:], "stderr": cp.stderr[-65536:]}
    if cp.returncode != 0:
        raise RuntimeError(json.dumps(out, ensure_ascii=False))
    return out


def scopes() -> dict[str, list[str]]:
    try:
        raw = json.loads(SCOPES.read_text())
    except (OSError, json.JSONDecodeError):
        raw = {}
    return {k: [str(Path(v).expanduser().resolve()) for v in raw.get(k, []) if isinstance(v, str) and v.strip()]
            for k in ("read", "write", "delete")}


def allowed(path: str, kind: str) -> Path:
    target = Path(path).expanduser().resolve(strict=False)
    roots = scopes().get(kind, [])
    if not roots:
        raise PermissionError(f"No filesystem {kind} paths are configured in Atlas Control")
    for root in roots:
        base = Path(root)
        try:
            target.relative_to(base)
            return target
        except ValueError:
            continue
    raise PermissionError(f"{target} is outside the owner-configured filesystem {kind} paths")


TOKEN = re.compile(r"^[A-Za-z0-9_.@:+/-]+$")

def safe_token(value: Any, label: str) -> str:
    value = str(value)
    if not value or value.startswith("-") or not TOKEN.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def tool(name: str, description: str, schema: dict[str, Any], *, read=False, destructive=False) -> dict[str, Any]:
    return {"name": name, "description": description, "inputSchema": {"type":"object", "additionalProperties":False, **schema},
            "annotations": {"readOnlyHint": read, "destructiveHint": destructive}}

TOOLS = [
    tool("services_inspect", "Inspect system services and their current state.", {"properties":{"pattern":{"type":"string"}}}, read=True),
    tool("service_logs", "Read recent logs for a system service.", {"properties":{"unit":{"type":"string"},"count":{"type":"integer","minimum":1,"maximum":1000}},"required":["unit"]}, read=True),
    tool("service_start", "Start a system service.", {"properties":{"unit":{"type":"string"}},"required":["unit"]}),
    tool("service_restart", "Restart a system service.", {"properties":{"unit":{"type":"string"}},"required":["unit"]}),
    tool("service_stop", "Stop a system service.", {"properties":{"unit":{"type":"string"}},"required":["unit"]}, destructive=True),
    tool("service_enable_disable", "Enable or disable a system service at boot.", {"properties":{"unit":{"type":"string"},"enabled":{"type":"boolean"}},"required":["unit","enabled"]}, destructive=True),
    tool("docker_inspect", "Inspect Docker containers, or one named container.", {"properties":{"name":{"type":"string"}}}, read=True),
    tool("docker_start_restart", "Start or restart a Docker container.", {"properties":{"name":{"type":"string"},"action":{"type":"string","enum":["start","restart"]}},"required":["name","action"]}),
    tool("docker_stop", "Stop a Docker container.", {"properties":{"name":{"type":"string"}},"required":["name"]}, destructive=True),
    tool("docker_create_remove", "Create or remove a Docker container.", {"properties":{"action":{"type":"string","enum":["create","remove"]},"name":{"type":"string"},"image":{"type":"string"},"command":{"type":"array","items":{"type":"string"}}},"required":["action","name"]}, destructive=True),
    tool("filesystem_read", "Read a text file inside an owner-approved host path.", {"properties":{"path":{"type":"string"},"max_bytes":{"type":"integer","minimum":1,"maximum":1048576}},"required":["path"]}, read=True),
    tool("filesystem_write", "Write a text file inside an owner-approved host path.", {"properties":{"path":{"type":"string"},"content":{"type":"string"},"create_parents":{"type":"boolean"}},"required":["path","content"]}),
    tool("filesystem_delete", "Delete a file or directory inside an owner-approved delete path.", {"properties":{"path":{"type":"string"},"recursive":{"type":"boolean"}},"required":["path"]}, destructive=True),
    tool("packages_inspect", "Inspect installed Ubuntu packages, optionally filtered by name.", {"properties":{"package":{"type":"string"}}}, read=True),
    tool("packages_change", "Install, update, or remove Ubuntu packages.", {"properties":{"action":{"type":"string","enum":["install","update","remove"]},"packages":{"type":"array","items":{"type":"string"},"minItems":1}},"required":["action","packages"]}, destructive=True),
    tool("host_resources", "View host uptime, memory, disk, load, and listening sockets.", {"properties":{}}, read=True),
    tool("host_restart", "Restart the host operating system.", {"properties":{}}, destructive=True),
    tool("host_shutdown", "Shut down the host operating system.", {"properties":{}}, destructive=True),
]


def call(name: str, a: dict[str, Any]) -> Any:
    if name == "services_inspect":
        result = run(["systemctl","list-units","--type=service","--all","--no-pager","--plain"])
        pat = str(a.get("pattern") or "").casefold()
        if pat: result["stdout"] = "\n".join(line for line in result["stdout"].splitlines() if pat in line.casefold())
        return result
    if name == "service_logs": return run(["journalctl","-u",safe_token(a["unit"],"unit"),"-n",str(a.get("count",100)),"--no-pager"])
    if name in {"service_start","service_restart","service_stop"}: return run(["systemctl",name.split("_")[1],safe_token(a["unit"],"unit")])
    if name == "service_enable_disable": return run(["systemctl","enable" if a["enabled"] else "disable",safe_token(a["unit"],"unit")])
    if name == "docker_inspect":
        return run(["docker","inspect",safe_token(a["name"],"container")]) if a.get("name") else run(["docker","ps","-a","--no-trunc"])
    if name == "docker_start_restart": return run(["docker",a["action"],safe_token(a["name"],"container")])
    if name == "docker_stop": return run(["docker","stop",safe_token(a["name"],"container")])
    if name == "docker_create_remove":
        if a["action"] == "remove": return run(["docker","rm",safe_token(a["name"],"container")])
        if not a.get("image"): raise ValueError("image is required when action=create")
        return run(["docker","create","--name",safe_token(a["name"],"container"),safe_token(a["image"],"image"),*[str(x) for x in a.get("command",[])]])
    if name == "filesystem_read":
        target = allowed(a["path"], "read"); maxb=int(a.get("max_bytes",262144)); data=target.read_bytes()[:maxb]
        if b"\x00" in data:
            raise ValueError("filesystem_read supports UTF-8 text files only; binary content is not returned inline")
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("filesystem_read supports UTF-8 text files only; binary content is not returned inline") from exc
        return {"path":str(target),"content":content,"truncated":target.stat().st_size>len(data)}
    if name == "filesystem_write":
        target=allowed(a["path"],"write")
        if a.get("create_parents"): target.parent.mkdir(parents=True,exist_ok=True)
        fd,tmp=tempfile.mkstemp(prefix=f".{target.name}.",dir=str(target.parent)); os.close(fd)
        try:
            Path(tmp).write_text(a["content"]); os.replace(tmp,target)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)
        return {"path":str(target),"bytes":len(a["content"].encode())}
    if name == "filesystem_delete":
        target=allowed(a["path"],"delete")
        if target.is_dir():
            if not a.get("recursive"): target.rmdir()
            else: shutil.rmtree(target)
        else: target.unlink()
        return {"path":str(target),"deleted":True}
    if name == "packages_inspect":
        if a.get("package"): return run(["apt-cache","policy",safe_token(a["package"],"package")])
        return run(["dpkg-query","-W","-f=${binary:Package}\t${Version}\n"])
    if name == "packages_change":
        pkgs=[safe_token(x,"package") for x in a["packages"]]; action=a["action"]
        if action == "install": return run(["apt-get","install","-y","--",*pkgs],timeout=900)
        if action == "remove": return run(["apt-get","remove","-y","--",*pkgs],timeout=900)
        run(["apt-get","update"],timeout=900); return run(["apt-get","install","--only-upgrade","-y","--",*pkgs],timeout=900)
    if name == "host_resources":
        return {"uptime":run(["uptime"]),"memory":run(["free","-h"]),"disk":run(["df","-h","-x","tmpfs","-x","devtmpfs"]),"sockets":run(["ss","-lntup"])}
    if name == "host_restart": return run(["systemctl","reboot","--no-block"])
    if name == "host_shutdown": return run(["systemctl","poweroff","--no-block"])
    raise KeyError(name)


def send(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj,separators=(",",":"),ensure_ascii=False)+"\n"); sys.stdout.flush()

def serve() -> None:
    for line in sys.stdin:
        try:
            req=json.loads(line); mid=req.get("id"); method=req.get("method")
            if method == "initialize":
                send({"jsonrpc":"2.0","id":mid,"result":{"protocolVersion":PROTOCOL_VERSION,"capabilities":{"tools":{}},"serverInfo":{"name":"atlas-host-operations","version":"1"}}})
            elif method == "notifications/initialized":
                continue
            elif method == "tools/list":
                send({"jsonrpc":"2.0","id":mid,"result":{"tools":TOOLS}})
            elif method == "tools/call":
                params=req.get("params") or {}; name=params.get("name"); args=params.get("arguments") or {}
                try:
                    result=call(str(name),args)
                    send({"jsonrpc":"2.0","id":mid,"result":{"structuredContent":result if isinstance(result,dict) else {"result":result},"content":[{"type":"text","text":json.dumps(result,ensure_ascii=False)}]}})
                except Exception as exc:  # noqa: BLE001 - tool failures are returned as MCP errors
                    send({"jsonrpc":"2.0","id":mid,"result":{"isError":True,"content":[{"type":"text","text":str(exc)}]}})
            elif mid is not None:
                send({"jsonrpc":"2.0","id":mid,"error":{"code":-32601,"message":"method not found"}})
        except Exception as exc:  # noqa: BLE001 - malformed requests must not terminate the socket server
            send({"jsonrpc":"2.0","id":None,"error":{"code":-32603,"message":str(exc)}})


if __name__ == "__main__":
    serve()
