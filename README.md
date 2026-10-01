# eveng-mcp

An [MCP](https://modelcontextprotocol.io) server that lets Claude drive an
EVE-NG server: browse labs, build topologies, start and stop nodes, and run CLI
commands on node consoles.

Tested against **EVE-NG Pro 7.2.0-4** (Ubuntu 24.04, Python 3.12) with Claude
Desktop on Windows. It also works with EVE-NG Community; the Pro-only stop
endpoint is used automatically when the Community one isn't there.

```
 Windows laptop                         EVE-NG server
┌──────────────────┐   ssh (stdio)   ┌──────────────────────────────────────┐
│ Claude Desktop   │ ──────────────▶ │ /opt/eveng-mcp/run.sh                │
│  claude_desktop_ │                 │   └─ eveng-mcp (Python venv)         │
│  config.json     │ ◀────────────── │        ├─ REST API  https://127.0.0.1│
└──────────────────┘                 │        └─ telnet consoles 127.0.0.1  │
                                     └──────────────────────────────────────┘
```

Running the server on the EVE host means the API and every node console are
reached on localhost, so no console ports need to be opened through a firewall.
Claude Desktop starts it over SSH, so nothing listens on the network and your
EVE password never leaves the EVE box.

---

## Tools

| Area | Tools |
|---|---|
| Server | `eve_status`, `list_labs`, `list_templates`, `get_template` |
| Labs | `get_lab`, `get_topology`, `create_lab`, `delete_lab`* |
| Nodes | `list_nodes`, `get_node`, `add_node`, `delete_node`*, `start_nodes`, `stop_nodes`, `wipe_nodes`* |
| Networks | `list_networks`, `add_network`, `connect_interface`, `connect_nodes`, `delete_network` |
| Console | `run_commands`, `read_console` |

\* Requires `confirm=true`, so Claude has to be explicit before destroying anything.

- `add_node` pulls defaults (type, image, RAM, NIC count) from the EVE template,
  so `add_node(lab, "fortinet", "FGT-2")` is enough.
- `connect_nodes` creates a hidden bridge and cables both interfaces, so the
  link draws as a direct line in the EVE UI.
- `run_commands` connects to the node's telnet console, waits for the CLI
  prompt, sends each command, and returns per-command output with `--More--`
  pages expanded and ANSI codes stripped.
- `EVE_READ_ONLY=true` blocks every change and all console input; reads still work.

---

## Setup

### 1. Install on the EVE-NG server

EVE-NG Pro 7.x runs Ubuntu 24.04, which ships Python 3.12. Install into a
dedicated venv: Ubuntu 24.04 blocks `pip install` into the system Python, and a
venv keeps the server separate from EVE's own packages.

Copy `eveng-mcp.zip` from this repo to the server (for example with
`scp eveng-mcp.zip root@<eve-ip>:~`), then as root:

```bash
apt install -y python3-venv unzip
cd /opt && unzip ~/eveng-mcp.zip          # unpacks to /opt/eve-ng-mcp (source)
python3 -m venv /opt/eveng-mcp/.venv      # venv lives in /opt/eveng-mcp
/opt/eveng-mcp/.venv/bin/pip install /opt/eve-ng-mcp
```

Or clone instead of copying the zip (needs GitHub access from the EVE box):

```bash
git clone https://github.com/jmtech-pro/eveng-mcp.git /opt/eve-ng-mcp
```

> **Note the two folders.** The source unpacks to `/opt/eve-ng-mcp`
> (hyphens) and the venv lives in `/opt/eveng-mcp`. Point `pip install` at the
> source folder; pointing it at the venv folder gives
> `Neither 'setup.py' nor 'pyproject.toml' found`.

The package pins `mcp<2`. MCP Python SDK 2.x renamed `FastMCP` and the server
won't start on it. If an older install pulled in 2.x, fix it with:

```bash
/opt/eveng-mcp/.venv/bin/pip install "mcp>=1.10,<2"
```

### 2. Run the live check

A read-only smoke test. It logs in, calls the GET endpoints the tools use, and
prints OK or FAIL per step:

```bash
EVE_URL=https://127.0.0.1 EVE_USER=admin EVE_PASSWORD='yourpass' EVE_CONSOLE_HOST=127.0.0.1 \
  /opt/eveng-mcp/.venv/bin/python -m eveng_mcp.livecheck

# a specific lab:
...livecheck "/Labs/My Lab.unl"
# plus a console command on node 1:
...livecheck "/Labs/My Lab.unl" 1 "get system status"
```

Add `-v` for tracebacks.

### 3. Create the wrapper script

The wrapper holds the connection settings on the EVE box, so your password
never appears in the Claude Desktop config:

```bash
cat > /opt/eveng-mcp/run.sh <<'EOF'
#!/bin/sh
export EVE_URL=https://127.0.0.1 EVE_USER=admin EVE_PASSWORD='yourpass' EVE_CONSOLE_HOST=127.0.0.1
exec /opt/eveng-mcp/.venv/bin/eveng-mcp
EOF
chmod 700 /opt/eveng-mcp/run.sh
```

Use `https://` because EVE Pro may redirect HTTP to HTTPS. Certificate checking
is off by default (`EVE_VERIFY_SSL=false`), so the self-signed certificate is fine.

### 4. Set up SSH key login from Windows

Claude Desktop can't answer a password or host-key prompt, so key-based login
must already work. `ssh-copy-id` doesn't exist on Windows; in PowerShell:

```powershell
ssh-keygen -t ed25519      # skip if you already have a key
type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh root@<eve-ip> "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
ssh -o BatchMode=yes root@<eve-ip> echo ok     # must print "ok" with no prompt
```

Accept the host key once during the interactive step.

### 5. Add the server to Claude Desktop

Where the config file lives depends on how Claude Desktop was installed:

| Install | Config file |
|---|---|
| Installer from claude.ai | `%APPDATA%\Claude\claude_desktop_config.json` |
| Microsoft Store (MSIX) | `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json` |

**Claude menu → Settings… → Developer → Edit Config** always opens the right one.

The file already holds Claude Desktop's own settings. Add `mcpServers` *inside*
the existing top-level `{ }`; don't paste a second `{ }` block after it:

```json
{
  "...existing settings...": "...",
  "mcpServers": {
    "eve-ng": {
      "command": "C:\\Windows\\System32\\OpenSSH\\ssh.exe",
      "args": ["-o", "BatchMode=yes", "root@<eve-ip>", "/opt/eveng-mcp/run.sh"]
    }
  }
}
```

Pasting a second block gives this on restart:

> Couldn't load app settings — Unexpected non-whitespace character after JSON at position … (line N column 1)

This PowerShell snippet repairs that, or adds the entry cleanly in the first
place. Quit Claude Desktop from the tray first, and set `$p` to your config path:

```powershell
$p = "$env:LOCALAPPDATA\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json"
Copy-Item $p "$p.bak" -Force

# keep only the first complete JSON object (the original settings)
$raw = Get-Content $p -Raw
$depth = 0; $end = -1
for ($i = 0; $i -lt $raw.Length; $i++) {
  if ($raw[$i] -eq '{') { $depth++ }
  elseif ($raw[$i] -eq '}') { $depth--; if ($depth -eq 0) { $end = $i; break } }
}
$cfg = $raw.Substring(0, $end + 1) | ConvertFrom-Json

if (-not $cfg.mcpServers) { $cfg | Add-Member mcpServers ([pscustomobject]@{}) }
$eve = [pscustomobject]@{
  command = 'C:\Windows\System32\OpenSSH\ssh.exe'
  args    = @('-o', 'BatchMode=yes', 'root@<eve-ip>', '/opt/eveng-mcp/run.sh')
}
$cfg.mcpServers | Add-Member 'eve-ng' $eve -Force

# UTF-8 without BOM (Claude Desktop can't parse a BOM)
[IO.File]::WriteAllText($p, ($cfg | ConvertTo-Json -Depth 20), (New-Object Text.UTF8Encoding $false))
(Get-Content $p -Raw | ConvertFrom-Json).mcpServers     # should list eve-ng
```

Fully quit Claude Desktop (tray icon → Quit) and reopen it.

### 6. Use it

Ask Claude things like:

- "Using the eve-ng MCP server, list my EVE labs."
- "Show the nodes in FGSP Lab v1 and which ones are running."
- "On FGT1 through FGT3, run `get system ha status`."
- "Build a lab with two FortiGates and a Linux host, cable port1 to port1, and start everything."

---

## Configuration reference

| Variable | Default | Notes |
|---|---|---|
| `EVE_URL` | (required) | `https://127.0.0.1` when running on the EVE host |
| `EVE_USER` / `EVE_PASSWORD` | `admin` / `eve` | Web UI credentials |
| `EVE_VERIFY_SSL` | `false` | EVE ships a self-signed certificate |
| `EVE_CONSOLE_HOST` | host from the node's console URL | Host used for telnet consoles |
| `EVE_READ_ONLY` | `false` | Blocks changes and console input |
| `EVE_TIMEOUT` | `30` | HTTP timeout in seconds |

### Running it on your laptop instead

It also runs anywhere with Python 3.10+ that can reach EVE's web UI and console
ports (32768 and up). Install with `pip install .`, set
`EVE_URL=https://<eve-ip>` (leave `EVE_CONSOLE_HOST` unset), and use
`"command": "eveng-mcp"` with an `env` block in the Claude Desktop config.
For Claude Code: `claude mcp add eve-ng -e EVE_URL=... -e EVE_PASSWORD=... -- eveng-mcp`.

---

## Console notes

- Only **telnet** consoles are supported. Nodes using VNC or HTML5 consoles
  (common for Linux templates) return an error from `run_commands`; set the
  node's console type to telnet if Claude should drive it.
- A freshly booted node may be at a login or setup prompt. Use `read_console`
  to look, then log in through `run_commands` with a `prompt_regex` that matches
  the login prompts, e.g. `(?i)(login:|password:|# ?$)`.
- On FortiGate, `config system console` / `set output standard` avoids paging.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `No module named 'mcp.server.fastmcp'` | MCP SDK 2.x installed. `pip install "mcp>=1.10,<2"` in the venv. |
| `Neither 'setup.py' nor 'pyproject.toml' found` | pip pointed at the venv folder. Install from `/opt/eve-ng-mcp`. |
| "Couldn't load app settings … Unexpected non-whitespace character" | Two JSON objects in the config. Use the repair snippet in step 5. |
| Server missing after restart | Check `mcp-server-eve-ng.log` in the `logs` folder next to the config file (Store installs keep logs under the Packages path, not `%APPDATA%`). |
| `Permission denied (publickey)` in the log | SSH key not installed; redo step 4 and test with `BatchMode=yes`. |
| `Node console is not telnet` | Node uses VNC/HTML5; change its console type. |

---

## How this was built

This server was built and debugged in a Claude session:

1. **Built** with the MCP Python SDK (FastMCP) and `httpx`, plus a minimal
   asyncio telnet client (Python 3.13 removed `telnetlib`).
2. **Tested offline** with a mocked EVE REST API and a fake FortiGate-style
   telnet console that does IAC negotiation and `--More--` paging, plus a stdio
   smoke test that launched the server the way Claude Desktop does.
3. **Couldn't test live from the cloud**: the cloud workspace refuses private
   addresses, so `livecheck` was added for testing from inside the network.
4. **Deployed on the EVE box** (Pro 7.2.0-4 / Ubuntu 24.04) in a venv. Fixed
   two issues on the way: the source vs. venv folder mix-up, and pip pulling MCP
   SDK 2.2.0, which led to the `mcp<2` pin.
5. **Wired into Claude Desktop** (Microsoft Store install) over SSH. Fixed a
   config file with two JSON objects using the repair snippet above.
6. **Verified live**: `eve_status`, `list_labs` and `list_nodes` returned the
   real server version, all 10 labs and the running FGSP lab's 8 nodes. The
   console tools (`run_commands`, `read_console`) haven't been exercised against
   the live server yet.

---

## Development

```bash
pip install -e '.[dev]'
pytest
```

```
src/eveng_mcp/
  server.py     MCP tools (FastMCP)
  client.py     async EVE-NG REST client: login, re-login on 401/412, path encoding
  telnet.py     asyncio telnet: IAC negotiation, prompt detection, pager handling
  livecheck.py  read-only smoke test against a real server
tests/          mocked API + fake telnet console
```
