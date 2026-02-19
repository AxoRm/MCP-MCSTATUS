from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from mcstatus_mcp.client import MCStatusApiClient
from mcstatus_mcp.tools import build_default_tools


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer.") from exc


mcp = FastMCP(
    name="mcstatus-mcp",
    instructions=(
        "Tools for Minecraft server status, DNS/BGP, reverse DNS, and MaxMind GeoIP lookups. "
        "Use these tools whenever host status, DNS/SRV, BGP, PTR, or IP geolocation info is needed."
    ),
    host=os.getenv("MCP_HOST", "127.0.0.1"),
    port=_env_int("MCP_PORT", 8000),
    streamable_http_path=os.getenv("MCP_STREAMABLE_HTTP_PATH", "/mcp"),
    sse_path=os.getenv("MCP_SSE_PATH", "/sse"),
)

api_client = MCStatusApiClient.from_environment()
registered_tools = build_default_tools(api_client=api_client)

for tool in registered_tools:
    tool.register(mcp)
