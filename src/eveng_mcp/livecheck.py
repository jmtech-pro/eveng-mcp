"""Read-only smoke test against a real EVE-NG server.

    EVE_URL=https://10.255.5.21 EVE_USER=admin EVE_PASSWORD=... python -m eveng_mcp.livecheck
    ... python -m eveng_mcp.livecheck "/Folder/Lab.unl"            # also inspect one lab
    ... python -m eveng_mcp.livecheck "/Folder/Lab.unl" 3 "get system status"  # + console

Makes no changes: it only logs in and calls GET endpoints (plus the optional
console command you give it). Each step prints OK/FAIL so a mismatch between
this server's API and the MCP tools is easy to spot.
"""
from __future__ import annotations

import asyncio
import json
import sys
import traceback

from . import server
from .client import EveConfig


def show(label: str, data) -> None:
    text = json.dumps(data, indent=2, default=str)
    if len(text) > 1500:
        text = text[:1500] + "\n  ... (truncated)"
    print(f"\n[OK]   {label}\n{text}")


async def step(label: str, coro):
    try:
        data = await coro
        show(label, data)
        return data
    except Exception as e:  # noqa: BLE001
        print(f"\n[FAIL] {label}: {type(e).__name__}: {e}")
        if "-v" in sys.argv:
            traceback.print_exc()
        return None


async def main() -> int:
    args = [a for a in sys.argv[1:] if a != "-v"]
    cfg = EveConfig.from_env()
    cfg.read_only = not (len(args) >= 3)  # console step needs writes enabled
    print(f"EVE-NG live check against {cfg.url} as {cfg.username}")

    await step("eve_status", server.eve_status())
    root = await step("list_labs /", server.list_labs("/"))
    await step("list_templates (available)", server.list_templates())

    lab = args[0] if args else None
    if not lab and root:
        lab = (root.get("labs") or [None])[0]
        if lab:
            print(f"\n(no lab given; using first lab in root: {lab})")
    if lab:
        await step(f"get_lab {lab}", server.get_lab(lab))
        await step("get_topology", server.get_topology(lab))
        nodes = await step("list_nodes", server.list_nodes(lab))
        if nodes:
            nid = int(args[1]) if len(args) >= 2 else nodes[0]["id"]
            await step(f"get_node {nid}", server.get_node(lab, nid))
            if len(args) >= 3:
                await step(f"run_commands node {nid}",
                           server.run_commands(lab, nid, args[2:]))
    else:
        print("\n(no labs in root folder; pass a lab path to test lab/node tools)")

    await server.client().aclose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
