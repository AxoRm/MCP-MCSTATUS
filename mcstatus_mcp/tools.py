from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from mcp.server.fastmcp import FastMCP

from mcstatus_mcp.client import (
    BEDROCK_DEFAULT_PORT,
    DEFAULT_TIMEOUT_MS,
    JAVA_DEFAULT_PORT,
    SIMPLE_VOICE_CHAT_DEFAULT_PORT,
    MCStatusApiClient,
)


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
    def _structured(payload: dict[str, Any]) -> dict[str, Any]:
        # Let FastMCP build both text content and structuredContent for broad client compatibility.
        return payload

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
        "Check an exact public Minecraft server endpoint. Use only when the ticket contains the actual public "
        "Minecraft hostname/IP and port and its current reachability matters. Never pass a website URL, hosting "
        "service ID, panel ID, node alias, bind address such as 0.0.0.0, or an unrelated domain. Supports Java and "
        "Bedrock. `offline` is a conclusive negative observation; `unconfirmed`/`unavailable` is not proof of outage."
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
    description = (
        "Check an exact public Java Minecraft hostname/IP and port. Use only for a Java server reachability problem "
        "with an endpoint explicitly present in ticket/service data. Do not pass service IDs, node names, URLs, "
        "website domains, or 0.0.0.0. Prefer get_minecraft_status when the edition is uncertain."
    )

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
    description = (
        "Check an exact public Bedrock Minecraft hostname/IP and UDP port (often 19132). Use only when Bedrock and "
        "the endpoint are explicit. Do not pass service IDs, node names, URLs, website domains, or 0.0.0.0."
    )

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
    description = (
        "Read Minecraft SRV records for an exact real DNS hostname when connection through a domain without a port "
        "is relevant. Never pass an internal service/panel ID, URL, arbitrary text, or raw IP."
    )

    def invoke(
        self,
        host: str,
        port: int = JAVA_DEFAULT_PORT,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> dict[str, Any]:
        return self._structured(self.api_client.get_srv_records(host=host, port=port, timeout_ms=timeout_ms))


class ResolveDnsTool(BaseMCStatusTool):
    name = "resolve_dns"
    description = (
        "Resolve an exact real DNS hostname when the ticket is specifically about DNS or domain resolution. Never "
        "use it merely because a domain appears in service data, and never pass an internal service ID, URL, "
        "arbitrary text, or 0.0.0.0. NXDOMAIN is a valid negative result; unavailable is inconclusive."
    )

    def invoke(self, host: str, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> dict[str, Any]:
        return self._structured(self.api_client.resolve_dns(host=host, timeout_ms=timeout_ms))


class ReverseDnsTool(BaseMCStatusTool):
    name = "rdns"
    description = "Reverse DNS (PTR) lookup for an IP address."

    def invoke(self, ip: str, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> dict[str, Any]:
        return self._structured(self.api_client.get_reverse_dns(ip=ip, timeout_ms=timeout_ms))


class GeoIpMaxMindTool(BaseMCStatusTool):
    name = "geoip_maxmind"
    description = ( "GeoIP lookup by IP using local MaxMind GeoLite2 database."
                    "Helps when need player GEO to check RKN / GOV internet blocks" )

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
        "Use this when proxy protocol and bungee modes are disabled and see client ip in logs."
        "if client ip != not anycast => player dont connect via anycast"
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
        "Check a Hosting-Minecraft infrastructure node in Kuma by its exact node name or smart alias "
        "(e.g., s3, br4, x 21, Node-x21). Use only for an actual hosting-node incident. Never pass a customer "
        "service ID, public game address, website domain, IP address, or location without a node identifier. "
        "Args: node_name (str, required), timeout_ms (int, optional). "
        "Returns UP/DOWN/PENDING/MAINTENANCE with match metadata; unknown_node means the alias was not in Kuma and "
        "unavailable means the check was inconclusive."
    )

    def invoke(self, node_name: str, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> dict[str, Any]:
        return self._structured(self.api_client.check_node_status(node_name=node_name, timeout_ms=timeout_ms))


class CheckVoiceChatStatusTool(BaseMCStatusTool):
    name = "check_voice_chat_status"
    description = (
        "Actively test a Simple Voice Chat UDP endpoint with its official external ping protocol. "
        "Args: host, UDP port, timeout_ms, and attempts. A valid pong proves that the "
        "Simple Voice Chat application answered through the tested UDP path."
    )

    def invoke(
        self,
        host: str,
        port: int = SIMPLE_VOICE_CHAT_DEFAULT_PORT,
        timeout_ms: int = 1000,
        attempts: int = 3,
    ) -> dict[str, Any]:
        return self._structured(
            self.api_client.check_voice_chat_status(
                host=host,
                port=port,
                timeout_ms=timeout_ms,
                attempts=attempts,
            )
        )


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
        CheckVoiceChatStatusTool(api_client),
    ]
