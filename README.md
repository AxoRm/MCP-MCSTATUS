# MCP-MCSTATUS

MCP server (Python) with tools for `https://mcstatus.xyz/api`.

## Implemented MCP Tools

- `get_minecraft_status` - Minecraft server status (Java/Bedrock), endpoint `/api/status`
- `get_java_status` - Java status shortcut, endpoint `/api/status`
- `get_bedrock_status` - Bedrock status shortcut, endpoint `/api/status`
- `get_srv_records` - SRV records, endpoint `/api/srv`
- `resolve_dns` - DNS resolution and provider info, endpoint `/api/dns`
- `get_bgp_info` - BGP/ASN details for an IP, endpoint `/api/bgp`

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
- `MCSTATUS_TIMEOUT_MS` - default timeout in milliseconds for tools (default: `4000`)
- `MCP_HOST` - host for HTTP transports (`sse` and `streamable-http`, default: `127.0.0.1`)
- `MCP_PORT` - port for HTTP transports (default: `8000`)
- `MCP_STREAMABLE_HTTP_PATH` - streamable HTTP path (default: `/mcp`)
- `MCP_SSE_PATH` - SSE path (default: `/sse`)

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
                "get_bgp_info",
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
