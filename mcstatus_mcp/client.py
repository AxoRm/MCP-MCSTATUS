from __future__ import annotations

import ipaddress
import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_API_BASE_URL = "https://mcstatus.xyz/api"
DEFAULT_TIMEOUT_MS = 4000
JAVA_DEFAULT_PORT = 25565
BEDROCK_DEFAULT_PORT = 19132
ALLOWED_EDITIONS = {"java", "bedrock"}
ALLOWED_MODES = {"auto", "legacy", "fe", "fe01", "fe01fa"}


class MCStatusApiError(RuntimeError):
    """Error returned when mcstatus.xyz request fails."""


class MCStatusApiClient:
    """Typed client wrapper around mcstatus.xyz endpoints."""

    def __init__(self, base_url: str = DEFAULT_API_BASE_URL, default_timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
        self.base_url = base_url.rstrip("/")
        self.default_timeout_ms = self.validate_timeout_ms(default_timeout_ms)

    @classmethod
    def from_environment(cls) -> MCStatusApiClient:
        base_url = os.getenv("MCSTATUS_API_BASE_URL", DEFAULT_API_BASE_URL)
        timeout_raw = os.getenv("MCSTATUS_TIMEOUT_MS", str(DEFAULT_TIMEOUT_MS))
        try:
            timeout_ms = int(timeout_raw)
        except ValueError as exc:
            raise ValueError("MCSTATUS_TIMEOUT_MS must be an integer.") from exc
        return cls(base_url=base_url, default_timeout_ms=timeout_ms)

    @staticmethod
    def validate_host(host: str) -> str:
        value = host.strip()
        if not value:
            raise ValueError("`host` must be a non-empty domain or IP.")
        return value

    @staticmethod
    def validate_port(port: int) -> int:
        value = int(port)
        if not (1 <= value <= 65535):
            raise ValueError("`port` must be in range 1..65535.")
        return value

    @staticmethod
    def validate_timeout_ms(timeout_ms: int) -> int:
        value = int(timeout_ms)
        if value <= 0:
            raise ValueError("`timeout_ms` must be > 0.")
        return value

    @staticmethod
    def validate_edition(edition: str) -> str:
        value = edition.strip().lower()
        if value not in ALLOWED_EDITIONS:
            raise ValueError(f"`edition` must be one of: {', '.join(sorted(ALLOWED_EDITIONS))}.")
        return value

    @staticmethod
    def validate_mode(mode: str) -> str:
        value = mode.strip().lower()
        if value not in ALLOWED_MODES:
            raise ValueError(f"`mode` must be one of: {', '.join(sorted(ALLOWED_MODES))}.")
        return value

    @staticmethod
    def validate_proto(proto: int) -> int:
        value = int(proto)
        if value < 0:
            raise ValueError("`proto` must be >= 0.")
        return value

    @staticmethod
    def validate_ip(ip: str) -> str:
        try:
            return str(ipaddress.ip_address(ip.strip()))
        except ValueError as exc:
            raise ValueError("`ip` must be a valid IPv4 or IPv6 address.") from exc

    def _request_json(self, path: str, params: dict[str, Any], timeout_ms: int | None = None) -> dict[str, Any]:
        safe_timeout_ms = self.default_timeout_ms if timeout_ms is None else self.validate_timeout_ms(timeout_ms)
        query = urlencode(params)
        url = f"{self.base_url}/{path.lstrip('/')}?{query}"
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "mcstatus-mcp-server/1.1",
            },
        )
        timeout_s = safe_timeout_ms / 1000.0

        try:
            with urlopen(request, timeout=timeout_s) as response:
                payload = response.read().decode("utf-8")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise MCStatusApiError(
                f"mcstatus API returned HTTP {exc.code} for {path}: {body}"
            ) from exc
        except URLError as exc:
            raise MCStatusApiError(f"Unable to reach mcstatus API for {path}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise MCStatusApiError(f"Request to mcstatus API timed out for {path}.") from exc

        try:
            result = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MCStatusApiError("mcstatus API returned invalid JSON.") from exc

        if not isinstance(result, dict):
            raise MCStatusApiError("mcstatus API returned unexpected payload type.")
        return result

    @staticmethod
    def _sanitize_status_payload(payload: dict[str, Any]) -> dict[str, Any]:
        data = payload.get("data")
        if isinstance(data, dict):
            data.pop("favicon", None)
        return payload

    def get_minecraft_status(
        self,
        host: str,
        edition: str = "java",
        port: int | None = None,
        mode: str = "auto",
        proto: int = 762,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        safe_host = self.validate_host(host)
        safe_edition = self.validate_edition(edition)
        safe_timeout_ms = self.default_timeout_ms if timeout_ms is None else self.validate_timeout_ms(timeout_ms)

        effective_port = port
        if effective_port is None:
            effective_port = BEDROCK_DEFAULT_PORT if safe_edition == "bedrock" else JAVA_DEFAULT_PORT
        safe_port = self.validate_port(effective_port)

        params: dict[str, Any] = {
            "host": safe_host,
            "port": safe_port,
            "edition": safe_edition,
            "timeout_ms": safe_timeout_ms,
        }

        if safe_edition == "java":
            params["mode"] = self.validate_mode(mode)
            params["proto"] = self.validate_proto(proto)

        response = self._request_json(path="status", params=params, timeout_ms=safe_timeout_ms)
        return self._sanitize_status_payload(response)

    def get_srv_records(self, host: str, port: int = JAVA_DEFAULT_PORT, timeout_ms: int | None = None) -> dict[str, Any]:
        safe_host = self.validate_host(host)
        safe_port = self.validate_port(port)
        safe_timeout_ms = self.default_timeout_ms if timeout_ms is None else self.validate_timeout_ms(timeout_ms)
        return self._request_json(
            path="srv",
            params={"host": safe_host, "port": safe_port},
            timeout_ms=safe_timeout_ms,
        )

    def resolve_dns(self, host: str, timeout_ms: int | None = None) -> dict[str, Any]:
        safe_host = self.validate_host(host)
        safe_timeout_ms = self.default_timeout_ms if timeout_ms is None else self.validate_timeout_ms(timeout_ms)
        return self._request_json(
            path="dns",
            params={"host": safe_host},
            timeout_ms=safe_timeout_ms,
        )

    def get_bgp_info(self, ip: str, timeout_ms: int | None = None) -> dict[str, Any]:
        safe_ip = self.validate_ip(ip)
        safe_timeout_ms = self.default_timeout_ms if timeout_ms is None else self.validate_timeout_ms(timeout_ms)
        return self._request_json(
            path="bgp",
            params={"ip": safe_ip},
            timeout_ms=safe_timeout_ms,
        )
