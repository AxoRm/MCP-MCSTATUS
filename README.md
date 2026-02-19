# MCP-MCSTATUS

MCP server (Python) with tools for `https://mcstatus.xyz/api` and Kuma status-page API.

## Implemented MCP Tools

- `get_minecraft_status` - Minecraft server status (Java/Bedrock), endpoint `/api/status`
- `get_java_status` - Java status shortcut, endpoint `/api/status`
- `get_bedrock_status` - Bedrock status shortcut, endpoint `/api/status`
- `get_srv_records` - SRV records, endpoint `/api/srv`
- `resolve_dns` - DNS resolution and provider info, endpoint `/api/dns`
- `rdns` - reverse DNS (PTR) lookup for IP
- `geoip_maxmind` - GeoIP lookup using local MaxMind GeoLite2 database
- `get_ip_provider_info` - provider/operator info for IP via `bgp.tools` whois + ASN database
- `is_ip_anycast` - check if player IP is Anycast by curated known-node list
- `get_bgp_info` - BGP/ASN details for an IP, endpoint `/api/bgp`
- `check_node_status` - find Kuma node by name or short alias (e.g., `s3`, `br4`) and return `UP/DOWN/PENDING/MAINTENANCE`

## `check_node_status` For GPT

Use this tool when you need node state from Kuma by human-friendly alias.

Input parameters:

- `node_name` (`string`, required) - full node name or short alias.
- `timeout_ms` (`integer`, optional, default `4000`, must be `> 0`).

Supported alias patterns:

- full name: `s3.joinserver.xyz`
- short hostname before first dot: `s3` for `s3.joinserver.xyz`
- token from short name split by `-`, `_`, or space: `br4`
- case-insensitive variants: `BR4`
- normalized alias (non-alphanumeric chars ignored in fallback matching)

Status mapping:

- `1` -> `UP`
- `0` -> `DOWN`
- `2` -> `PENDING`
- any other/unknown -> `MAINTENANCE`

Result format (`ok = true`):

```json
{
  "ok": true,
  "input_node_name": "s3",
  "node_name": "s3.joinserver.xyz",
  "node_id": 124,
  "matched_by": "short_hostname",
  "status": "DOWN",
  "status_code": 0,
  "heartbeat_time": "2026-02-19 11:48:48",
  "message": "",
  "ping": null,
  "has_heartbeat": true,
  "matched_by_case_insensitive_name": true
}
```

Result format (`ok = false`):

- not found:
```json
{"ok": false, "input_node_name": "unknown", "error": "Node with this name/alias was not found on Kuma status page."}
```
- ambiguous alias:
```json
{
  "ok": false,
  "input_node_name": "hmfra1",
  "error": "Multiple nodes matched this name/alias at the same confidence level. Use a more specific node name.",
  "matches": [{"id": 305, "name": "HMFRA1-7950"}, {"id": 304, "name": "HMFRA1-R9"}]
}
```

GPT usage flow:

1. Try short alias first (`s3`, `br4`, `fra28`).
2. If ambiguous, retry with a more specific name from `matches`.
3. If not found, retry with full node name.

## Architecture

- `mcstatus_mcp/client.py` - typed API client (`MCStatusApiClient`)
- `mcstatus_mcp/tools.py` - abstract `BaseMCStatusTool` + one class per MCP tool
- `mcstatus_mcp/server.py` - MCP app bootstrap and tool registration

Each tool is implemented as a class that inherits from `BaseMCStatusTool`.
All tools depend on a shared `MCStatusApiClient` instance.

## Install

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Run With Docker Compose

```powershell
docker compose up -d --build
```

Stop:

```powershell
docker compose down
```

Default MCP endpoint from compose:

`http://localhost:8000/mcp`

## Run MCP Server

Default transport is `stdio`:

```powershell
.\.venv\Scripts\python.exe main.py
```

Optional transport override:

```powershell
$env:MCP_TRANSPORT="sse"
.\.venv\Scripts\python.exe main.py
```

Allowed `MCP_TRANSPORT` values: `stdio`, `sse`, `streamable-http`.

## Local Stdio Config Example

```json
{
  "mcpServers": {
    "mcstatus": {
      "command": "C:\\Users\\rakse\\PycharmProjects\\MCP-MCSTATUS\\.venv\\Scripts\\python.exe",
      "args": ["C:\\Users\\rakse\\PycharmProjects\\MCP-MCSTATUS\\main.py"]
    }
  }
}
```

## Environment Variables

- `MCP_TRANSPORT` - MCP transport (default: `stdio`)
- `MCSTATUS_API_BASE_URL` - API base URL (default: `https://mcstatus.xyz/api`)
- `KUMA_API_BASE_URL` - Kuma API base URL for `check_node_status` (default: `http://status.dsts.cloud:3001/api`)
- `MCSTATUS_TIMEOUT_MS` - default timeout in milliseconds for tools (default: `4000`)
- `MCP_HOST` - host for HTTP transports (`sse` and `streamable-http`, default: `127.0.0.1`)
- `MCP_PORT` - port for HTTP transports (default: `8000`)
- `MCP_STREAMABLE_HTTP_PATH` - streamable HTTP path (default: `/mcp`)
- `MCP_SSE_PATH` - SSE path (default: `/sse`)
- `MAXMIND_LICENSE_KEY` - MaxMind license key for GeoLite2 download (required for auto-download if DB is missing/outdated)
- `MAXMIND_DB_PATH` - local path to `.mmdb` file (default: `data/GeoLite2-City.mmdb`)
- `MAXMIND_EDITION_ID` - MaxMind edition ID (default: `GeoLite2-City`)
- `MAXMIND_REFRESH_HOURS` - database refresh interval in hours; `0` disables periodic refresh (default: `24`)
- `BGPTOOLS_USER_AGENT` - descriptive user-agent with contact for downloading `https://bgp.tools/asns.csv` (recommended)
- `BGPTOOLS_ASN_DB_URL` - ASN CSV source URL (default: `https://bgp.tools/asns.csv`)
- `BGPTOOLS_ASN_DB_PATH` - local path to ASN CSV cache (default: `data/bgp_tools_asns.csv`)
- `BGPTOOLS_ASN_REFRESH_HOURS` - ASN CSV refresh interval in hours; `0` disables periodic refresh (default: `24`)
- `BGPTOOLS_WHOIS_HOST` - bgp.tools whois host (default: `bgp.tools`)
- `BGPTOOLS_WHOIS_PORT` - bgp.tools whois port (default: `43`)

## Use With OpenAI

Important: OpenAI MCP integration uses remote MCP servers.
Local `stdio` servers are good for local dev/testing, but for OpenAI binding you should expose a public HTTPS endpoint.

### 1) Run as streamable HTTP for deployment

```powershell
$env:MCP_TRANSPORT="streamable-http"
$env:MCP_HOST="0.0.0.0"
$env:MCP_PORT="8000"
.\.venv\Scripts\python.exe main.py
```

After deployment your MCP URL will look like:

`https://your-domain.example/mcp`

### 2) Bind MCP server in OpenAI Responses API

```python
from openai import OpenAI

client = OpenAI()

response = client.responses.create(
    model="gpt-4.1",
    tools=[
        {
            "type": "mcp",
            "server_label": "mcstatus",
            "server_url": "https://your-domain.example/mcp",
            "require_approval": "never",
            "allowed_tools": [
                "get_minecraft_status",
                "get_java_status",
                "get_bedrock_status",
                "get_srv_records",
                "resolve_dns",
                "rdns",
                "geoip_maxmind",
                "get_ip_provider_info",
                "is_ip_anycast",
                "get_bgp_info",
                "check_node_status",
            ],
        }
    ],
    input="Check DNS and Java status for mc.hypixel.net",
)

print(response.output_text)
```

### 3) Bind in ChatGPT (Connectors)

- Open ChatGPT connector settings.
- Add a custom connector using your remote MCP URL.
- Select tools and permissions.

Reference docs:
- https://platform.openai.com/docs/guides/tools-remote-mcp
- https://platform.openai.com/docs/guides/mcp
- https://help.openai.com/en/articles/11487775-connectors-in-chatgpt/
