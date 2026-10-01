"""Async client for the EVE-NG REST API (Community and Pro).

EVE-NG wraps every response as {"code", "status", "message", "data"} and uses a
cookie session (unetlab_session) obtained from /api/auth/login. Expired sessions
come back as HTTP 401 or 412; the client logs in again once and retries.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlparse

import httpx


class EveError(RuntimeError):
    """Raised when EVE-NG returns an error or an unexpected response."""


@dataclass
class EveConfig:
    url: str
    username: str
    password: str
    verify_ssl: bool = False
    timeout: float = 30.0
    console_host: str | None = None  # override host used for telnet consoles
    read_only: bool = False

    @classmethod
    def from_env(cls) -> "EveConfig":
        url = os.environ.get("EVE_URL")
        if not url:
            raise EveError("EVE_URL is not set (e.g. https://eve.lab.local)")
        truthy = {"1", "true", "yes", "on"}
        return cls(
            url=url.rstrip("/"),
            username=os.environ.get("EVE_USER", "admin"),
            password=os.environ.get("EVE_PASSWORD", "eve"),
            verify_ssl=os.environ.get("EVE_VERIFY_SSL", "false").lower() in truthy,
            timeout=float(os.environ.get("EVE_TIMEOUT", "30")),
            console_host=os.environ.get("EVE_CONSOLE_HOST") or None,
            read_only=os.environ.get("EVE_READ_ONLY", "false").lower() in truthy,
        )


def lab_path(path: str) -> str:
    """Normalise a lab path to '/Folder/Lab Name.unl'."""
    p = "/" + path.strip().strip("/")
    if not p.endswith(".unl"):
        p += ".unl"
    return p


def encode_path(path: str) -> str:
    """URL-encode each segment of a path, keeping the slashes."""
    return "/".join(quote(seg, safe="") for seg in path.split("/"))


class EveClient:
    def __init__(self, config: EveConfig, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self._http = httpx.AsyncClient(
            base_url=config.url,
            verify=config.verify_ssl,
            timeout=config.timeout,
            transport=transport,
            follow_redirects=True,
        )
        self._logged_in = False

    async def aclose(self) -> None:
        if self._logged_in:
            try:
                await self._http.get("/api/auth/logout")
            except httpx.HTTPError:
                pass
        await self._http.aclose()

    # ---------------------------------------------------------------- core
    async def login(self) -> None:
        r = await self._http.post(
            "/api/auth/login",
            json={
                "username": self.config.username,
                "password": self.config.password,
                "html5": "-1",
            },
        )
        if r.status_code != 200:
            raise EveError(f"Login failed ({r.status_code}): {_message(r)}")
        self._logged_in = True

    async def request(self, method: str, path: str, json: Any = None) -> Any:
        if not self._logged_in:
            await self.login()
        r = await self._http.request(method, path, json=json)
        if r.status_code in (401, 412):  # session expired / not authenticated
            await self.login()
            r = await self._http.request(method, path, json=json)
        if r.status_code >= 400:
            raise EveError(f"{method} {path} -> {r.status_code}: {_message(r)}")
        try:
            body = r.json()
        except ValueError:
            return r.text
        if isinstance(body, dict) and "data" in body:
            return body["data"]
        return body

    def _lab(self, lab: str) -> str:
        return "/api/labs" + encode_path(lab_path(lab))

    # -------------------------------------------------------------- system
    async def status(self) -> Any:
        return await self.request("GET", "/api/status")

    async def list_folder(self, folder: str = "/") -> Any:
        f = "/" + folder.strip().strip("/")
        path = "/api/folders" + (encode_path(f) if f != "/" else "/")
        return await self.request("GET", path)

    async def list_templates(self) -> Any:
        return await self.request("GET", "/api/list/templates/")

    async def template(self, name: str) -> Any:
        return await self.request("GET", f"/api/list/templates/{quote(name, safe='')}")

    # ---------------------------------------------------------------- labs
    async def get_lab(self, lab: str) -> Any:
        return await self.request("GET", self._lab(lab))

    async def create_lab(self, folder: str, name: str, description: str = "",
                         author: str = "", version: str = "1") -> Any:
        return await self.request("POST", "/api/labs", json={
            "path": "/" + folder.strip().strip("/"),
            "name": name,
            "version": version,
            "author": author,
            "description": description,
            "body": "",
        })

    async def delete_lab(self, lab: str) -> Any:
        return await self.request("DELETE", self._lab(lab))

    async def topology(self, lab: str) -> Any:
        return await self.request("GET", self._lab(lab) + "/topology")

    # --------------------------------------------------------------- nodes
    async def nodes(self, lab: str) -> Any:
        return await self.request("GET", self._lab(lab) + "/nodes")

    async def node(self, lab: str, node_id: int) -> Any:
        return await self.request("GET", self._lab(lab) + f"/nodes/{node_id}")

    async def node_interfaces(self, lab: str, node_id: int) -> Any:
        return await self.request("GET", self._lab(lab) + f"/nodes/{node_id}/interfaces")

    async def add_node(self, lab: str, spec: dict[str, Any]) -> Any:
        return await self.request("POST", self._lab(lab) + "/nodes", json=spec)

    async def delete_node(self, lab: str, node_id: int) -> Any:
        return await self.request("DELETE", self._lab(lab) + f"/nodes/{node_id}")

    async def node_action(self, lab: str, node_id: int | None, action: str) -> Any:
        """action: start | stop | wipe. node_id None = all nodes."""
        base = self._lab(lab) + ("/nodes" if node_id is None else f"/nodes/{node_id}")
        try:
            return await self.request("GET", f"{base}/{action}")
        except EveError:
            if action != "stop":
                raise
            # EVE-NG Pro uses /stop/stopmode=3 (graceful + power off)
            return await self.request("GET", f"{base}/stop/stopmode=3")

    # ------------------------------------------------------------ networks
    async def networks(self, lab: str) -> Any:
        return await self.request("GET", self._lab(lab) + "/networks")

    async def add_network(self, lab: str, name: str, net_type: str = "bridge",
                          left: int = 100, top: int = 100) -> Any:
        return await self.request("POST", self._lab(lab) + "/networks", json={
            "type": net_type, "name": name, "left": left, "top": top, "visibility": 1,
        })

    async def delete_network(self, lab: str, network_id: int) -> Any:
        return await self.request("DELETE", self._lab(lab) + f"/networks/{network_id}")

    async def connect_interface(self, lab: str, node_id: int, interface_id: int,
                                network_id: int) -> Any:
        return await self.request(
            "PUT", self._lab(lab) + f"/nodes/{node_id}/interfaces",
            json={str(interface_id): network_id},
        )

    # ------------------------------------------------------------- console
    def console_endpoint(self, node: dict[str, Any]) -> tuple[str, int]:
        """Return (host, port) for a node's telnet console."""
        url = node.get("url") or ""
        parsed = urlparse(url)
        if parsed.scheme != "telnet" or not parsed.port:
            raise EveError(
                f"Node console is not telnet (url={url!r}). Set the node's console "
                "type to telnet, or use the HTML5 console in the EVE-NG UI."
            )
        host = self.config.console_host or parsed.hostname or urlparse(self.config.url).hostname
        return host, parsed.port


def _message(r: httpx.Response) -> str:
    try:
        return r.json().get("message", r.text[:200])
    except ValueError:
        return r.text[:200]
