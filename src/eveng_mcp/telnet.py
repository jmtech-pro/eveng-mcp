"""Minimal asyncio telnet client for EVE-NG node consoles.

telnetlib was removed in Python 3.13, so this handles just enough of the
protocol (IAC option negotiation) to drive a serial console: send commands,
read until a CLI prompt, and page through --More-- output.
"""
from __future__ import annotations

import asyncio
import re

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
ECHO, SGA = 1, 3

# Matches common prompts: Router#, switch>, FGT-1 #, user@host:~$, [admin@x] >, PA-VM>
DEFAULT_PROMPT = r"(?m)^[^\r\n]{0,80}?[\w\)\]~][\s]?[#>$%]\s*$"
PAGER = re.compile(r"(--\s?More\s?--|<--- More --->|Press any key to continue|--More--)", re.I)
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][AB012]")


class TelnetConsole:
    def __init__(self, host: str, port: int, timeout: float = 15.0):
        self.host, self.port, self.timeout = host, port, timeout
        self._r: asyncio.StreamReader | None = None
        self._w: asyncio.StreamWriter | None = None
        self._buf = ""

    async def __aenter__(self) -> "TelnetConsole":
        self._r, self._w = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), self.timeout
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._w:
            self._w.close()
            try:
                await self._w.wait_closed()
            except Exception:
                pass

    # ----------------------------------------------------------- protocol
    def _negotiate(self, data: bytes) -> bytes:
        """Strip IAC sequences from data, answering option requests."""
        out, i, reply = bytearray(), 0, bytearray()
        while i < len(data):
            b = data[i]
            if b != IAC:
                out.append(b); i += 1; continue
            if i + 1 >= len(data):
                break
            cmd = data[i + 1]
            if cmd == IAC:
                out.append(IAC); i += 2; continue
            if cmd in (DO, DONT, WILL, WONT) and i + 2 < len(data):
                opt = data[i + 2]
                if cmd == WILL:
                    reply += bytes([IAC, DO if opt in (ECHO, SGA) else DONT, opt])
                elif cmd == DO:
                    reply += bytes([IAC, WILL if opt == SGA else WONT, opt])
                i += 3; continue
            if cmd == SB:
                end = data.find(bytes([IAC, SE]), i)
                i = len(data) if end == -1 else end + 2; continue
            i += 2
        if reply and self._w:
            self._w.write(bytes(reply))
        return bytes(out)

    async def _read_some(self, wait: float) -> str:
        assert self._r
        try:
            data = await asyncio.wait_for(self._r.read(4096), wait)
        except asyncio.TimeoutError:
            return ""
        if not data:
            raise ConnectionError("console closed the connection")
        text = self._negotiate(data).decode("utf-8", "replace")
        return ANSI.sub("", text).replace("\r\n", "\n").replace("\r", "")

    async def write(self, text: str) -> None:
        assert self._w
        self._w.write(text.encode())
        await self._w.drain()

    # ---------------------------------------------------------------- api
    async def read_until_prompt(self, prompt: re.Pattern, timeout: float) -> tuple[str, bool]:
        """Read until the prompt appears or the timeout passes.

        Returns (output, prompt_seen). Pager prompts are answered with a space.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        out = ""
        while (left := deadline - loop.time()) > 0:
            chunk = await self._read_some(min(left, 1.0))
            if not chunk:
                if prompt.search(out[-200:]):
                    return out, True
                continue
            out += chunk
            if PAGER.search(out[-80:]):
                out = PAGER.sub("", out)
                await self.write(" ")
                continue
            if prompt.search(out[-200:]):
                # small grace period in case more output is still arriving
                more = await self._read_some(0.3)
                if not more:
                    return out, True
                out += more
        return out, False

    async def drain(self, idle: float = 1.0, max_time: float = 5.0) -> str:
        """Read whatever the console is printing until it goes quiet."""
        loop = asyncio.get_running_loop()
        deadline, out = loop.time() + max_time, ""
        while loop.time() < deadline:
            chunk = await self._read_some(idle)
            if not chunk:
                break
            out += chunk
        return out


async def run_commands(host: str, port: int, commands: list[str], *,
                       prompt_regex: str | None = None,
                       command_timeout: float = 30.0,
                       connect_timeout: float = 10.0) -> list[dict]:
    """Run CLI commands on a console and return per-command output."""
    prompt = re.compile(prompt_regex or DEFAULT_PROMPT)
    results = []
    async with TelnetConsole(host, port, connect_timeout) as con:
        await con.drain(idle=0.5, max_time=2.0)  # discard backlog
        await con.write("\r")
        banner, ok = await con.read_until_prompt(prompt, 10.0)
        if not ok:
            results.append({
                "command": "<wake console>",
                "output": banner[-2000:],
                "prompt_seen": False,
                "note": "No CLI prompt detected. The node may still be booting, be at a "
                        "login/setup prompt, or use a prompt this regex misses; pass "
                        "prompt_regex or read the console with read_console.",
            })
            return results
        for cmd in commands:
            await con.write(cmd + "\r")
            out, seen = await con.read_until_prompt(prompt, command_timeout)
            lines = out.split("\n")
            if lines and lines[0].strip().endswith(cmd.strip()):
                lines = lines[1:]  # drop echoed command
            if seen and lines:
                lines = lines[:-1]  # drop trailing prompt
            results.append({"command": cmd, "output": "\n".join(lines).strip("\n"),
                            "prompt_seen": seen})
    return results


async def read_console(host: str, port: int, send: str | None = None,
                       wait: float = 3.0) -> str:
    """Optionally send raw text (\\r appended if it lacks one), then return output."""
    async with TelnetConsole(host, port) as con:
        if send is not None:
            await con.write(send if send.endswith(("\r", "\n")) else send + "\r")
        return await con.drain(idle=1.0, max_time=wait)
