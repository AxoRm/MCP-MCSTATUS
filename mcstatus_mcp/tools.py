from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult

from mcstatus_mcp.client import BEDROCK_DEFAULT_PORT, DEFAULT_TIMEOUT_MS, JAVA_DEFAULT_PORT, MCStatusApiClient


class BaseMCStatusTool(ABC):
    """Abstract base class for all MCP tools in this server."""

    name: str
    description: str

    def __init__(self, api_client: MCStatusApiClient) -> None:
        self.api_client = api_client

    def register(self, mcp: FastMCP) -> None:
        if not self.name or not self.description:
            raise ValueError(f"{self.__class__.__name__} must define non-empty `name` and `description`.")
        mcp.tool(name=self.name, description=self.description)(self.invoke)

    @staticmethod
    def _structured(payload: dict[str, Any]) -> CallToolResult:
        # Keep result machine-readable only; avoid duplicated JSON text content.
        return CallToolResult(content=[], structuredContent=payload, isError=False)

    @abstractmethod
    def invoke(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Execute the tool and return a JSON-serializable dict."""


class StatusToolBase(BaseMCStatusTool, ABC):
    """Base class for tools backed by /api/status."""

    def _status(
        self,
        *,
        host: str,
        edition: str,
        port: int | None,
        mode: str = "auto",
        proto: int = 762,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> dict[str, Any]:
        return self.api_client.get_minecraft_status(
            host=host,
            edition=edition,
            port=port,
            mode=mode,
            proto=proto,
            timeout_ms=timeout_ms,
        )


class MinecraftStatusTool(StatusToolBase):
    name = "get_minecraft_status"
    description = (
        "Get Minecraft server status from mcstatus.xyz /api/status. "
        "Supports Java and Bedrock editions."
    )

    def invoke(
        self,
        host: str,
        edition: str = "java",
        port: int | None = None,
        mode: str = "auto",
        proto: int = 762,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> dict[str, Any]:
        return self._structured(
            self._status(
            host=host,
            edition=edition,
            port=port,
            mode=mode,
            proto=proto,
            timeout_ms=timeout_ms,
            )
        )


class JavaStatusTool(StatusToolBase):
    name = "get_java_status"
    description = "Convenience wrapper for Java edition status lookup."

    def invoke(
        self,
        host: str,
        port: int = JAVA_DEFAULT_PORT,
        mode: str = "auto",
        proto: int = 762,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> dict[str, Any]:
        return self._structured(
            self._status(
            host=host,
            edition="java",
            port=port,
            mode=mode,
            proto=proto,
            timeout_ms=timeout_ms,
            )
        )


class BedrockStatusTool(StatusToolBase):
    name = "get_bedrock_status"
    description = "Convenience wrapper for Bedrock edition status lookup."

    def invoke(
        self,
        host: str,
        port: int = BEDROCK_DEFAULT_PORT,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> dict[str, Any]:
        return self._structured(
            self._status(
            host=host,
            edition="bedrock",
            port=port,
            timeout_ms=timeout_ms,
            )
        )


class SrvRecordsTool(BaseMCStatusTool):
    name = "get_srv_records"
    description = "Get SRV records via mcstatus.xyz /api/srv."

    def invoke(
        self,
        host: str,
        port: int = JAVA_DEFAULT_PORT,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> dict[str, Any]:
        return self._structured(self.api_client.get_srv_records(host=host, port=port, timeout_ms=timeout_ms))


class ResolveDnsTool(BaseMCStatusTool):
    name = "resolve_dns"
    description = "Resolve DNS and provider info via mcstatus.xyz /api/dns."

    def invoke(self, host: str, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> dict[str, Any]:
        return self._structured(self.api_client.resolve_dns(host=host, timeout_ms=timeout_ms))


class ReverseDnsTool(BaseMCStatusTool):
    name = "rdns"
    description = "Reverse DNS (PTR) lookup for an IP address."

    def invoke(self, ip: str, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> dict[str, Any]:
        return self._structured(self.api_client.get_reverse_dns(ip=ip, timeout_ms=timeout_ms))


class GeoIpMaxMindTool(BaseMCStatusTool):
    name = "geoip_maxmind"
    description = "GeoIP lookup by IP using local MaxMind GeoLite2 database."

    def invoke(self, ip: str, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> dict[str, Any]:
        return self._structured(self.api_client.get_geoip_maxmind(ip=ip, timeout_ms=timeout_ms))


class IpProviderInfoTool(BaseMCStatusTool):
    name = "get_ip_provider_info"
    description = "Get IP provider info via bgp.tools whois and ASN database."

    def invoke(self, ip: str, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> dict[str, Any]:
        return self._structured(self.api_client.get_ip_provider_info(ip=ip, timeout_ms=timeout_ms))


class IsIpAnycastTool(BaseMCStatusTool):
    name = "is_ip_anycast"
    description = (
        "Check whether an IP is an Anycast node using the curated known-node list. "
        "Use this when proxy protocol and bungee modes are disabled."
    )

    def invoke(self, ip: str, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> dict[str, Any]:
        return self._structured(self.api_client.is_ip_anycast(ip=ip, timeout_ms=timeout_ms))


class BgpInfoTool(BaseMCStatusTool):
    name = "get_bgp_info"
    description = "Get BGP details for an IP via mcstatus.xyz /api/bgp."

    def invoke(self, ip: str, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> dict[str, Any]:
        return self._structured(self.api_client.get_bgp_info(ip=ip, timeout_ms=timeout_ms))


class CheckNodeStatusTool(BaseMCStatusTool):
    name = "check_node_status"
    description = (
        "Check node status in Kuma by node name or short alias (e.g., s3, br4). "
        "Args: node_name (str, required), timeout_ms (int, optional). "
        "Returns UP/DOWN/PENDING/MAINTENANCE with match metadata; on ambiguous alias returns matches."
    )

    def invoke(self, node_name: str, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> dict[str, Any]:
        return self._structured(self.api_client.check_node_status(node_name=node_name, timeout_ms=timeout_ms))


def build_default_tools(api_client: MCStatusApiClient) -> list[BaseMCStatusTool]:
    return [
        MinecraftStatusTool(api_client),
        JavaStatusTool(api_client),
        BedrockStatusTool(api_client),
        SrvRecordsTool(api_client),
        ResolveDnsTool(api_client),
        ReverseDnsTool(api_client),
        GeoIpMaxMindTool(api_client),
        IpProviderInfoTool(api_client),
        IsIpAnycastTool(api_client),
        BgpInfoTool(api_client),
        CheckNodeStatusTool(api_client),
    ]
