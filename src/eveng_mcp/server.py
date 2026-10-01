"""EVE-NG MCP server.

Exposes EVE-NG lab management (folders, labs, nodes, networks, links, templates)
and node console access (telnet) as MCP tools. Configure with environment
variables; see README.md.
"""
from __future__ import annotations

import asyncio
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import telnet
from .client import EveClient, EveConfig, EveError, lab_path

mcp = FastMCP(
    "eve-ng",
    instructions=(
        "Tools for an EVE-NG network emulation server. Lab paths look like "
        "'/Folder/Lab Name.unl' (the .unl suffix and leading slash are optional). "
        "Node IDs are integers from list_nodes. Before running console commands, "
        "make sure the node is running; freshly started nodes can take minutes to "
        "boot, so use read_console to check for a prompt first. Destructive tools "
        "(delete_*, wipe_nodes) should only be used when the user asked for them."
    ),
)

_client: EveClient | None = None

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)

STATUS = {0: "stopped", 1: "starting", 2: "running", 3: "stopping"}
NODE_TYPES = {"iol": "iol", "vpcs": "vpcs", "dynamips": "dynamips",
              "c1710": "dynamips", "c3725": "dynamips", "c7200": "dynamips"}


def client() -> EveClient:
    global _client
    if _client is None:
        _client = EveClient(EveConfig.from_env())
    return _client


def _guard_write() -> None:
    if client().config.read_only:
        raise EveError("Server is in read-only mode (EVE_READ_ONLY=true).")


def _summarise_node(n: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "name", "type", "template", "image", "cpu", "ram",
            "ethernet", "console", "url", "left", "top")
    out = {k: n[k] for k in keys if k in n}
    s = n.get("status")
    out["status"] = STATUS.get(s, f"status {s}") if isinstance(s, int) else s
    return out


def _as_list(data: Any) -> list[dict[str, Any]]:
    """EVE returns collections as {"1": {...}, "2": {...}} or as a list."""
    if isinstance(data, dict):
        return list(data.values())
    return list(data or [])


# ================================================================== system
@mcp.tool(annotations=READ)
async def eve_status() -> dict:
    """EVE-NG server version, CPU/RAM/disk usage and running node counts."""
    return await client().status()


@mcp.tool(annotations=READ)
async def list_labs(folder: str = "/") -> dict:
    """List sub-folders and labs in an EVE-NG folder (default: root)."""
    data = await client().list_folder(folder)
    return {
        "folder": folder,
        "folders": [f.get("path") for f in data.get("folders", []) if f.get("name") != ".."],
        "labs": [l.get("path") for l in data.get("labs", [])],
    }


@mcp.tool(annotations=READ)
async def list_templates(only_available: bool = True) -> dict:
    """List node templates (e.g. 'fortinet', 'vios', 'linux').

    With only_available, returns only templates that have at least one image
    installed on the server (EVE marks the rest '.missing').
    """
    data = await client().list_templates()
    if only_available:
        data = {k: v for k, v in data.items() if ".missing" not in str(v)}
    return data


@mcp.tool(annotations=READ)
async def get_template(template: str) -> dict:
    """Show a template's defaults and installed images (use before add_node)."""
    data = await client().template(template)
    opts = data.get("options", {})
    return {
        "description": data.get("description"),
        "images": list((opts.get("image") or {}).get("list", {}).keys()),
        "defaults": {k: v.get("value") for k, v in opts.items() if isinstance(v, dict)},
    }


# ==================================================================== labs
@mcp.tool(annotations=READ)
async def get_lab(lab: str) -> dict:
    """Lab metadata plus a summary of its nodes and networks."""
    c = client()
    meta, nodes, nets = await asyncio.gather(c.get_lab(lab), c.nodes(lab), c.networks(lab))
    return {
        "lab": meta,
        "nodes": [_summarise_node(n) for n in _as_list(nodes)],
        "networks": _as_list(nets),
    }


@mcp.tool(annotations=READ)
async def get_topology(lab: str) -> list:
    """Point-to-point links in the lab (source/destination node and interface)."""
    return _as_list(await client().topology(lab))


@mcp.tool(annotations=WRITE)
async def create_lab(folder: str, name: str, description: str = "", author: str = "") -> dict:
    """Create an empty lab in a folder. Returns the new lab path."""
    _guard_write()
    await client().create_lab(folder, name, description, author)
    return {"lab": lab_path(f"{folder.strip('/')}/{name}")}


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_lab(lab: str, confirm: bool = False) -> dict:
    """Permanently delete a lab and its nodes' disks. Requires confirm=true."""
    _guard_write()
    if not confirm:
        return {"deleted": False, "reason": "Pass confirm=true to delete " + lab_path(lab)}
    await client().delete_lab(lab)
    return {"deleted": True, "lab": lab_path(lab)}


# =================================================================== nodes
@mcp.tool(annotations=READ)
async def list_nodes(lab: str) -> list:
    """Nodes in a lab with status (stopped/running), template, image and console URL."""
    return [_summarise_node(n) for n in _as_list(await client().nodes(lab))]


@mcp.tool(annotations=READ)
async def get_node(lab: str, node_id: int) -> dict:
    """Full node details plus its interfaces (index, name, attached network id)."""
    c = client()
    node, ifaces = await asyncio.gather(c.node(lab, node_id), c.node_interfaces(lab, node_id))
    for kind in ("ethernet", "serial"):
        items = ifaces.get(kind) if isinstance(ifaces, dict) else None
        if isinstance(items, dict):
            ifaces[kind] = [{"index": int(k), **v} for k, v in items.items()]
        elif isinstance(items, list):
            ifaces[kind] = [{"index": i, **v} for i, v in enumerate(items)]
    return {"node": _summarise_node(node) | {"raw": node}, "interfaces": ifaces}


@mcp.tool(annotations=WRITE)
async def add_node(lab: str, template: str, name: str, image: str | None = None,
                   cpu: int | None = None, ram: int | None = None,
                   ethernet: int | None = None, left: int = 200, top: int = 200,
                   extra: dict[str, Any] | None = None) -> dict:
    """Add a node from a template. Unset values use the template defaults.

    ram is in MB. `extra` passes any other EVE node option (e.g. {"console": "telnet"}).
    """
    _guard_write()
    c = client()
    tpl = await c.template(template)
    spec: dict[str, Any] = {
        k: v["value"] for k, v in tpl.get("options", {}).items()
        if isinstance(v, dict) and "value" in v
    }
    spec.update({"template": template, "name": name, "left": left, "top": top,
                 "count": 1, "postfix": 0})
    # EVE-NG Pro 7 template responses omit "type"; without it the API rejects
    # the node with error 20022, so infer it from the template.
    if not spec.get("type"):
        spec["type"] = NODE_TYPES.get(template, "docker" if template == "docker"
                                      or template.startswith("eve-") else "qemu")
    for key, val in (("image", image), ("cpu", cpu), ("ram", ram), ("ethernet", ethernet)):
        if val is not None:
            spec[key] = val
    spec.update(extra or {})
    data = await c.add_node(lab, spec)
    node_id = data.get("id") if isinstance(data, dict) else data
    return {"node_id": node_id, "name": name, "template": template, "image": spec.get("image")}


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_node(lab: str, node_id: int, confirm: bool = False) -> dict:
    """Delete a node from a lab. Requires confirm=true."""
    _guard_write()
    if not confirm:
        return {"deleted": False, "reason": f"Pass confirm=true to delete node {node_id}"}
    await client().delete_node(lab, node_id)
    return {"deleted": True, "node_id": node_id}


async def _bulk(lab: str, node_ids: list[int] | None, action: str) -> dict:
    c = client()
    if not node_ids:
        await c.node_action(lab, None, action)
        return {"action": action, "nodes": "all"}
    results = {}
    for nid in node_ids:  # sequential: EVE handles concurrent starts poorly
        try:
            await c.node_action(lab, nid, action)
            results[nid] = "ok"
        except EveError as e:
            results[nid] = str(e)
    return {"action": action, "results": results}


@mcp.tool(annotations=WRITE)
async def start_nodes(lab: str, node_ids: list[int] | None = None) -> dict:
    """Start the given nodes, or every node in the lab if node_ids is omitted."""
    _guard_write()
    return await _bulk(lab, node_ids, "start")


@mcp.tool(annotations=WRITE)
async def stop_nodes(lab: str, node_ids: list[int] | None = None) -> dict:
    """Stop the given nodes, or every node in the lab if node_ids is omitted."""
    _guard_write()
    return await _bulk(lab, node_ids, "stop")


@mcp.tool(annotations=DESTRUCTIVE)
async def wipe_nodes(lab: str, node_ids: list[int] | None = None, confirm: bool = False) -> dict:
    """Reset nodes to their base image, erasing saved config. Requires confirm=true."""
    _guard_write()
    if not confirm:
        return {"wiped": False, "reason": "Pass confirm=true; this erases node configuration."}
    return await _bulk(lab, node_ids, "wipe")


# ================================================================ networks
@mcp.tool(annotations=READ)
async def list_networks(lab: str) -> list:
    """Networks (bridges, cloud/pnet interfaces) in a lab."""
    return _as_list(await client().networks(lab))


@mcp.tool(annotations=WRITE)
async def add_network(lab: str, name: str, network_type: str = "bridge",
                      left: int = 300, top: int = 300) -> dict:
    """Add a network. network_type: 'bridge', or 'pnet0'..'pnet9' for a cloud
    that bridges to the EVE host's physical/management interfaces."""
    _guard_write()
    data = await client().add_network(lab, name, network_type, left, top)
    return {"network_id": data.get("id") if isinstance(data, dict) else data, "name": name}


@mcp.tool(annotations=WRITE)
async def connect_interface(lab: str, node_id: int, interface_index: int,
                            network_id: int) -> dict:
    """Attach one node interface (index from get_node) to a network."""
    _guard_write()
    await client().connect_interface(lab, node_id, interface_index, network_id)
    return {"node_id": node_id, "interface_index": interface_index, "network_id": network_id}


@mcp.tool(annotations=WRITE)
async def connect_nodes(lab: str, node_a: int, interface_a: int, node_b: int,
                        interface_b: int, link_name: str | None = None) -> dict:
    """Cable two node interfaces together via a new bridge network.

    Interface indexes come from get_node (e.g. port1 on a FortiGate is usually 0).
    Nodes generally need to be stopped for the change to take effect.
    """
    _guard_write()
    c = client()
    name = link_name or f"link-{node_a}.{interface_a}-{node_b}.{interface_b}"
    net = await c.add_network(lab, name, "bridge")
    net_id = net.get("id") if isinstance(net, dict) else net
    await c.connect_interface(lab, node_a, interface_a, net_id)
    await c.connect_interface(lab, node_b, interface_b, net_id)
    # hide the helper bridge so it draws as a direct link in the UI
    try:
        await c.request("PUT", c._lab(lab) + f"/networks/{net_id}", json={"visibility": 0})
    except EveError:
        pass
    return {"network_id": net_id, "a": [node_a, interface_a], "b": [node_b, interface_b]}


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_network(lab: str, network_id: int) -> dict:
    """Delete a network (disconnects every interface attached to it)."""
    _guard_write()
    await client().delete_network(lab, network_id)
    return {"deleted": True, "network_id": network_id}


# ================================================================= console
async def _endpoint(lab: str, node_id: int) -> tuple[str, int]:
    c = client()
    node = await c.node(lab, node_id)
    if node.get("status") not in (2, 3) and node.get("status") != "running":
        raise EveError(f"Node {node_id} is not running (status={node.get('status')}).")
    return c.console_endpoint(node)


@mcp.tool(annotations=WRITE)
async def run_commands(lab: str, node_id: int, commands: list[str],
                       prompt_regex: str | None = None,
                       command_timeout: float = 30.0) -> list:
    """Run CLI commands on a running node's telnet console, one after another.

    Works for show/diagnose commands and config mode alike (send 'config system
    interface', 'edit port1', ... as separate commands). The node must already
    be logged in at a CLI prompt; pass the username/password as the first
    commands with a prompt_regex matching the login prompts if it is not.
    Pagers (--More--) are handled automatically.
    """
    if client().config.read_only:
        raise EveError("Console commands are disabled in read-only mode.")
    host, port = await _endpoint(lab, node_id)
    return await telnet.run_commands(host, port, commands, prompt_regex=prompt_regex,
                                     command_timeout=command_timeout)


@mcp.tool(annotations=READ)
async def read_console(lab: str, node_id: int, send: str | None = None,
                       wait: float = 3.0) -> str:
    """Peek at a node's console: optionally send one line, then return output.

    Useful to see whether a node has finished booting or is at a login prompt.
    Send an empty string to just press Enter.
    """
    if send and client().config.read_only:
        raise EveError("Sending console input is disabled in read-only mode.")
    host, port = await _endpoint(lab, node_id)
    return await telnet.read_console(host, port, send, wait)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
