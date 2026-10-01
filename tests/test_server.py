import asyncio
import json

import httpx
import pytest

from eveng_mcp import server, telnet
from eveng_mcp.client import EveClient, EveConfig, lab_path, encode_path


# ------------------------------------------------------------ fake EVE API
class FakeEve:
    def __init__(self):
        self.calls = []
        self.logged_in = False
        self.logins = 0
        self.expire_once = False
        self.nodes = {"1": {"id": 1, "name": "FGT-1", "type": "qemu", "template": "fortinet",
                            "image": "fortinet-FGT-v7.4.4", "status": 2,
                            "url": "telnet://10.0.0.5:32769", "console": "telnet"}}
        self.nets = {}

    def ok(self, data=None, code=200):
        return httpx.Response(code, json={"code": code, "status": "success",
                                          "message": "ok", "data": data})

    def __call__(self, req: httpx.Request):
        path = req.url.raw_path.decode()
        self.calls.append((req.method, path))
        if path == "/api/auth/login":
            body = json.loads(req.content)
            if body["password"] != "eve":
                return httpx.Response(400, json={"message": "bad creds"})
            self.logged_in, self.logins = True, self.logins + 1
            return self.ok()
        if not self.logged_in:
            return httpx.Response(412, json={"message": "unauthorized"})
        if self.expire_once:
            self.expire_once, self.logged_in = False, False
            return httpx.Response(412, json={"message": "session expired"})
        lab = "/api/labs/Fortinet/SD-WAN%20Lab.unl"
        routes = {
            ("GET", "/api/status"): {"version": "6.2.0-4"},
            ("GET", "/api/folders/"): {"folders": [{"name": "..", "path": "/"},
                                                   {"name": "Fortinet", "path": "/Fortinet"}],
                                       "labs": [{"file": "a.unl", "path": "/a.unl"}]},
            ("GET", lab): {"name": "SD-WAN Lab"},
            ("GET", lab + "/nodes"): self.nodes,
            ("GET", lab + "/nodes/1"): self.nodes["1"],
            ("GET", lab + "/nodes/1/interfaces"): {"ethernet": {"0": {"name": "port1", "network_id": 0}}},
            ("GET", lab + "/networks"): self.nets,
            ("GET", "/api/list/templates/fortinet"): {
                "description": "Fortinet FortiGate",
                "options": {"type": {"value": "qemu"}, "image": {"value": "fortinet-FGT-v7.4.4",
                            "list": {"fortinet-FGT-v7.4.4": "x"}}, "ram": {"value": 2048},
                            "ethernet": {"value": 10}}},
        }
        if (req.method, path) in routes:
            return self.ok(routes[(req.method, path)])
        if req.method == "POST" and path == lab + "/nodes":
            spec = json.loads(req.content)
            self.last_node_spec = spec
            return self.ok({"id": 2}, 201)
        if req.method == "POST" and path == lab + "/networks":
            nid = len(self.nets) + 1
            self.nets[str(nid)] = {"id": nid, **json.loads(req.content)}
            return self.ok({"id": nid}, 201)
        if req.method == "PUT" and "/interfaces" in path:
            return self.ok(None, 201)
        if req.method == "PUT" and "/networks/" in path:
            return self.ok(None, 201)
        if path == lab + "/nodes/1/stop":
            return httpx.Response(404, json={"message": "not found"})  # force Pro fallback
        if path.endswith(("/start", "/stop/stopmode=3", "/wipe")):
            return self.ok()
        if req.method == "DELETE":
            return self.ok()
        return httpx.Response(404, json={"message": f"no route {req.method} {path}"})


@pytest.fixture
def eve(monkeypatch):
    fake = FakeEve()
    cfg = EveConfig(url="https://eve.test", username="admin", password="eve")
    monkeypatch.setattr(server, "_client", EveClient(cfg, transport=httpx.MockTransport(fake)))
    return fake


LAB = "Fortinet/SD-WAN Lab"


def test_paths():
    assert lab_path("Fortinet/SD-WAN Lab") == "/Fortinet/SD-WAN Lab.unl"
    assert lab_path("/x.unl") == "/x.unl"
    assert encode_path("/Fortinet/SD-WAN Lab.unl") == "/Fortinet/SD-WAN%20Lab.unl"


async def test_status_and_folders(eve):
    assert (await server.eve_status())["version"] == "6.2.0-4"
    labs = await server.list_labs()
    assert labs["folders"] == ["/Fortinet"] and labs["labs"] == ["/a.unl"]


async def test_relogin_on_expired_session(eve):
    await server.eve_status()
    eve.expire_once = True
    await server.eve_status()
    assert eve.logins == 2


async def test_list_nodes_and_get_node(eve):
    nodes = await server.list_nodes(LAB)
    assert nodes[0]["status"] == "running" and nodes[0]["name"] == "FGT-1"
    node = await server.get_node(LAB, 1)
    assert node["interfaces"]["ethernet"][0] == {"index": 0, "name": "port1", "network_id": 0}


async def test_add_node_uses_template_defaults(eve):
    res = await server.add_node(LAB, "fortinet", "FGT-2", ram=4096)
    assert res["node_id"] == 2
    spec = eve.last_node_spec
    assert spec["type"] == "qemu" and spec["ram"] == 4096 and spec["ethernet"] == 10
    assert spec["image"] == "fortinet-FGT-v7.4.4"


async def test_connect_nodes(eve):
    res = await server.connect_nodes(LAB, 1, 0, 2, 0)
    assert res["network_id"] == 1
    puts = [c for c in eve.calls if c[0] == "PUT" and "interfaces" in c[1]]
    assert len(puts) == 2


async def test_stop_falls_back_to_pro_endpoint(eve):
    res = await server.stop_nodes(LAB, [1])
    assert res["results"][1] == "ok"
    assert ("GET", "/api/labs/Fortinet/SD-WAN%20Lab.unl/nodes/1/stop/stopmode=3") in eve.calls


async def test_destructive_needs_confirm(eve):
    assert (await server.delete_lab(LAB))["deleted"] is False
    assert (await server.delete_lab(LAB, confirm=True))["deleted"] is True


async def test_read_only_mode(eve):
    server._client.config.read_only = True
    with pytest.raises(Exception, match="read-only"):
        await server.start_nodes(LAB)
    assert await server.list_nodes(LAB)  # reads still work


# ------------------------------------------------------- fake telnet node
async def fake_console(reader, writer):
    IAC, WILL, DO = 255, 251, 253
    writer.write(bytes([IAC, WILL, 1, IAC, WILL, 3, IAC, DO, 24]))  # negotiate
    writer.write(b"FortiGate-VM64 login: ")
    await writer.drain()
    prompt = b"\r\nFGT-1 # "
    while data := await reader.read(1024):
        data = bytes(b for b in data if b < 240)  # ignore our IAC replies
        line = data.decode(errors="ignore").strip()
        if not line and b"\r" in data:
            writer.write(prompt)
        elif line == "get system status":
            writer.write(b"get system status\r\nVersion: FortiGate-VM64 v7.4.4\r\n"
                         b"Serial-Number: FGVMEV000\r\n--More-- ")
            await writer.drain()
            await reader.read(10)  # the space
            writer.write(b"\rHostname: FGT-1" + prompt)
        elif line:
            writer.write(data.strip() + b"\r\n\x1b[1mdone\x1b[0m" + prompt)
        await writer.drain()


async def test_telnet_run_commands():
    srv = await asyncio.start_server(fake_console, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    async with srv:
        res = await telnet.run_commands("127.0.0.1", port, ["get system status", "show"],
                                        command_timeout=5)
    assert res[0]["prompt_seen"]
    out = res[0]["output"]
    assert "v7.4.4" in out and "Hostname: FGT-1" in out and "More" not in out
    assert "get system status" not in out and "FGT-1 #" not in out
    assert res[1]["output"] == "done"  # ANSI stripped


async def test_console_endpoint_override(eve):
    c = server._client
    assert c.console_endpoint(eve.nodes["1"]) == ("10.0.0.5", 32769)
    c.config.console_host = "eve.test"
    assert c.console_endpoint(eve.nodes["1"]) == ("eve.test", 32769)
