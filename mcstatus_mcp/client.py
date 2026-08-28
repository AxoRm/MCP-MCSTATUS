from __future__ import annotations

import csv
import ipaddress
import io
import json
import os
import re
import shutil
import socket
import struct
import tarfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    import maxminddb
except ImportError:  # pragma: no cover - handled at runtime
    maxminddb = None

DEFAULT_API_BASE_URL = "https://mcstatus.xyz/api"
DEFAULT_KUMA_API_BASE_URL = "http://status.dsts.cloud:3001/api"
SIMPLE_VOICE_CHAT_DEFAULT_PORT = 24454
SIMPLE_VOICE_CHAT_PING_CHECK_ID = uuid.UUID("58bc9ae9-c7a8-45e4-a11c-efbb67199425")
SIMPLE_VOICE_CHAT_PING_PAYLOAD_LENGTH = 24
DEFAULT_TIMEOUT_MS = 4000
JAVA_DEFAULT_PORT = 25565
BEDROCK_DEFAULT_PORT = 19132
ALLOWED_EDITIONS = {"java", "bedrock"}
ALLOWED_MODES = {"auto", "legacy", "fe", "fe01", "fe01fa"}
DEFAULT_MAXMIND_DB_PATH = "data/GeoLite2-City.mmdb"
DEFAULT_MAXMIND_EDITION_ID = "GeoLite2-City"
DEFAULT_MAXMIND_REFRESH_HOURS = 24
MAXMIND_DOWNLOAD_URL = "https://download.maxmind.com/app/geoip_download"
MAXMIND_SOURCE_NAME = "maxmind-geolite2-city"
DEFAULT_BGPTOOLS_ASN_DB_URL = "https://bgp.tools/asns.csv"
DEFAULT_BGPTOOLS_ASN_DB_PATH = "data/bgp_tools_asns.csv"
DEFAULT_BGPTOOLS_ASN_REFRESH_HOURS = 24
DEFAULT_BGPTOOLS_WHOIS_HOST = "bgp.tools"
DEFAULT_BGPTOOLS_WHOIS_PORT = 43
GENERIC_NODE_QUERY_TOKENS = frozenset(
    {
        "node",
        "nodes",
        "server",
        "servers",
        "srv",
        "host",
        "hostname",
        "machine",
        "нода",
        "ноды",
        "узел",
        "узлы",
        "сервер",
        "сервера",
        "серверы",
        "хост",
        "машина",
    }
)
KNOWN_ANYCAST_PLAYER_NODES: dict[str, str] = {
    "169.150.255.56": "Германия",
    "143.244.45.11": "Украина",
    "185.9.145.68": "Москва DDoSGuard",
    "194.39.67.137": "Алматы",
    "79.127.249.68": "Стокгольм",
    "143.20.155.0": "Польша",
    "185.17.10.91": "Москва Селектел",
    "213.152.43.117": "Москва"
}


class MCStatusApiError(RuntimeError):
    """Error returned when upstream API request fails."""


class MCStatusApiClient:
    """Typed client wrapper around mcstatus.xyz and Kuma endpoints."""

    def __init__(
        self,
        base_url: str = DEFAULT_API_BASE_URL,
        kuma_api_base_url: str = DEFAULT_KUMA_API_BASE_URL,
        default_timeout_ms: int = DEFAULT_TIMEOUT_MS,
        maxmind_db_path: str = DEFAULT_MAXMIND_DB_PATH,
        maxmind_license_key: str | None = None,
        maxmind_edition_id: str = DEFAULT_MAXMIND_EDITION_ID,
        maxmind_refresh_hours: int = DEFAULT_MAXMIND_REFRESH_HOURS,
        bgptools_asn_db_url: str = DEFAULT_BGPTOOLS_ASN_DB_URL,
        bgptools_asn_db_path: str = DEFAULT_BGPTOOLS_ASN_DB_PATH,
        bgptools_asn_refresh_hours: int = DEFAULT_BGPTOOLS_ASN_REFRESH_HOURS,
        bgptools_user_agent: str | None = None,
        bgptools_whois_host: str = DEFAULT_BGPTOOLS_WHOIS_HOST,
        bgptools_whois_port: int = DEFAULT_BGPTOOLS_WHOIS_PORT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.kuma_api_base_url = kuma_api_base_url.rstrip("/")
        self.default_timeout_ms = self.validate_timeout_ms(default_timeout_ms)
        self.maxmind_db_path = Path(maxmind_db_path).expanduser()
        self.maxmind_license_key = (maxmind_license_key or "").strip()
        self.maxmind_edition_id = maxmind_edition_id.strip() or DEFAULT_MAXMIND_EDITION_ID
        self.maxmind_refresh_hours = self.validate_refresh_hours(maxmind_refresh_hours, field_name="maxmind_refresh_hours")
        self.bgptools_asn_db_url = bgptools_asn_db_url.strip() or DEFAULT_BGPTOOLS_ASN_DB_URL
        self.bgptools_asn_db_path = Path(bgptools_asn_db_path).expanduser()
        self.bgptools_asn_refresh_hours = self.validate_refresh_hours(
            bgptools_asn_refresh_hours, field_name="bgptools_asn_refresh_hours"
        )
        self.bgptools_user_agent = (bgptools_user_agent or "").strip()
        self.bgptools_whois_host = bgptools_whois_host.strip() or DEFAULT_BGPTOOLS_WHOIS_HOST
        self.bgptools_whois_port = self.validate_port(bgptools_whois_port)
        self._kuma_cache_lock = threading.Lock()
        self._kuma_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    @classmethod
    def from_environment(cls) -> MCStatusApiClient:
        base_url = os.getenv("MCSTATUS_API_BASE_URL", DEFAULT_API_BASE_URL)
        kuma_api_base_url = os.getenv("KUMA_API_BASE_URL", DEFAULT_KUMA_API_BASE_URL)
        timeout_raw = os.getenv("MCSTATUS_TIMEOUT_MS", str(DEFAULT_TIMEOUT_MS))
        try:
            timeout_ms = int(timeout_raw)
        except ValueError as exc:
            raise ValueError("MCSTATUS_TIMEOUT_MS must be an integer.") from exc
        maxmind_db_path = os.getenv("MAXMIND_DB_PATH", DEFAULT_MAXMIND_DB_PATH)
        maxmind_license_key = os.getenv("MAXMIND_LICENSE_KEY")
        maxmind_edition_id = os.getenv("MAXMIND_EDITION_ID", DEFAULT_MAXMIND_EDITION_ID)
        refresh_raw = os.getenv("MAXMIND_REFRESH_HOURS", str(DEFAULT_MAXMIND_REFRESH_HOURS))
        try:
            maxmind_refresh_hours = int(refresh_raw)
        except ValueError as exc:
            raise ValueError("MAXMIND_REFRESH_HOURS must be an integer.") from exc
        bgptools_asn_db_url = os.getenv("BGPTOOLS_ASN_DB_URL", DEFAULT_BGPTOOLS_ASN_DB_URL)
        bgptools_asn_db_path = os.getenv("BGPTOOLS_ASN_DB_PATH", DEFAULT_BGPTOOLS_ASN_DB_PATH)
        bgptools_refresh_raw = os.getenv("BGPTOOLS_ASN_REFRESH_HOURS", str(DEFAULT_BGPTOOLS_ASN_REFRESH_HOURS))
        try:
            bgptools_asn_refresh_hours = int(bgptools_refresh_raw)
        except ValueError as exc:
            raise ValueError("BGPTOOLS_ASN_REFRESH_HOURS must be an integer.") from exc
        bgptools_user_agent = os.getenv("BGPTOOLS_USER_AGENT")
        bgptools_whois_host = os.getenv("BGPTOOLS_WHOIS_HOST", DEFAULT_BGPTOOLS_WHOIS_HOST)
        bgptools_whois_port_raw = os.getenv("BGPTOOLS_WHOIS_PORT", str(DEFAULT_BGPTOOLS_WHOIS_PORT))
        try:
            bgptools_whois_port = int(bgptools_whois_port_raw)
        except ValueError as exc:
            raise ValueError("BGPTOOLS_WHOIS_PORT must be an integer.") from exc
        return cls(
            base_url=base_url,
            kuma_api_base_url=kuma_api_base_url,
            default_timeout_ms=timeout_ms,
            maxmind_db_path=maxmind_db_path,
            maxmind_license_key=maxmind_license_key,
            maxmind_edition_id=maxmind_edition_id,
            maxmind_refresh_hours=maxmind_refresh_hours,
            bgptools_asn_db_url=bgptools_asn_db_url,
            bgptools_asn_db_path=bgptools_asn_db_path,
            bgptools_asn_refresh_hours=bgptools_asn_refresh_hours,
            bgptools_user_agent=bgptools_user_agent,
            bgptools_whois_host=bgptools_whois_host,
            bgptools_whois_port=bgptools_whois_port,
        )

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
    def validate_attempts(attempts: int) -> int:
        value = int(attempts)
        if not (1 <= value <= 10):
            raise ValueError("`attempts` must be in range 1..10.")
        return value

    @staticmethod
    def _encode_simple_voice_chat_ping(request_id: uuid.UUID, timestamp_ms: int) -> bytes:
        payload = request_id.bytes + struct.pack(">q", int(timestamp_ms))
        return (
            bytes((0xFF,))
            + SIMPLE_VOICE_CHAT_PING_CHECK_ID.bytes
            + bytes((SIMPLE_VOICE_CHAT_PING_PAYLOAD_LENGTH,))
            + payload
        )

    @staticmethod
    def _is_matching_simple_voice_chat_pong(
        data: bytes,
        *,
        request_id: uuid.UUID,
        timestamp_ms: int,
    ) -> bool:
        if len(data) != SIMPLE_VOICE_CHAT_PING_PAYLOAD_LENGTH:
            return False
        return data[:16] == request_id.bytes and data[16:] == struct.pack(">q", int(timestamp_ms))

    @staticmethod
    def _resolve_udp_endpoint(host: str, port: int) -> tuple[int, tuple[Any, ...]]:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
        if not addresses:
            raise socket.gaierror(f"No UDP address found for {host!r}.")
        selected = next((address for address in addresses if address[0] == socket.AF_INET), addresses[0])
        return selected[0], selected[4]

    @staticmethod
    def validate_refresh_hours(refresh_hours: int, *, field_name: str = "refresh_hours") -> int:
        value = int(refresh_hours)
        if value < 0:
            raise ValueError(f"`{field_name}` must be >= 0.")
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

    @staticmethod
    def validate_node_name(node_name: str) -> str:
        value = node_name.strip()
        if not value:
            raise ValueError("`node_name` must be a non-empty string.")
        return value

    def _request_json_with_base(
        self,
        *,
        base_url: str,
        source_name: str,
        path: str,
        params: dict[str, Any],
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        safe_timeout_ms = self.default_timeout_ms if timeout_ms is None else self.validate_timeout_ms(timeout_ms)
        query = urlencode(params)
        url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{query}"
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
            # mcstatus.xyz intentionally returns a non-2xx status for some useful
            # negative observations (for example connection_refused). Treat a
            # valid JSON object as a tool result instead of losing its diagnostics.
            try:
                error_result = json.loads(body)
            except json.JSONDecodeError:
                error_result = None
            if isinstance(error_result, dict) and source_name == "mcstatus API":
                error_result.setdefault("upstream_http_status", exc.code)
                error_result.setdefault("tool_execution", "completed")
                return error_result
            raise MCStatusApiError(
                f"{source_name} returned HTTP {exc.code} for {path}: {body}"
            ) from exc
        except URLError as exc:
            raise MCStatusApiError(f"Unable to reach {source_name} for {path}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise MCStatusApiError(f"Request to {source_name} timed out for {path}.") from exc

        try:
            result = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MCStatusApiError(f"{source_name} returned invalid JSON.") from exc

        if not isinstance(result, dict):
            raise MCStatusApiError(f"{source_name} returned unexpected payload type.")
        return result

    def _request_json(self, path: str, params: dict[str, Any], timeout_ms: int | None = None) -> dict[str, Any]:
        return self._request_json_with_base(
            base_url=self.base_url,
            source_name="mcstatus API",
            path=path,
            params=params,
            timeout_ms=timeout_ms,
        )

    @staticmethod
    def _sanitize_status_payload(payload: dict[str, Any]) -> dict[str, Any]:
        data = payload.get("data")
        if isinstance(data, dict):
            data.pop("favicon", None)
        return payload

    @staticmethod
    def _invalid_public_target(host: str) -> str | None:
        value = host.strip()
        if "://" in value or any(character in value for character in "/?#@"):
            return "Use a hostname or IP only, without a URL, path, query, or credentials."
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            if "_" in value:
                return "Internal service IDs and underscore labels are not public hostnames."
            if len(value) > 253:
                return "Hostname is longer than the DNS limit."
            labels = value.rstrip(".").split(".")
            hostname_label = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
            if not labels or any(not hostname_label.fullmatch(label) for label in labels):
                return "Value is not a valid DNS hostname or IP address."
            return None
        if address.is_unspecified or address.is_loopback:
            return "Unspecified and loopback addresses cannot be checked from the public MCP service."
        return None

    @staticmethod
    def _diagnostic_text(payload: dict[str, Any]) -> str:
        parts = [
            payload.get("error"),
            payload.get("error_detail"),
            payload.get("detail"),
            payload.get("message"),
        ]
        return " ".join(str(part) for part in parts if part).strip()

    @classmethod
    def _normalize_minecraft_status_failure(
        cls,
        payload: dict[str, Any],
        *,
        host: str,
        port: int,
        edition: str,
    ) -> dict[str, Any]:
        result = cls._sanitize_status_payload(dict(payload))
        result.setdefault("host", host)
        result.setdefault("port", port)
        result.setdefault("edition", edition)
        result.setdefault("tool_execution", "completed")
        diagnostic = cls._diagnostic_text(result)
        lowered = diagnostic.casefold()

        if any(token in lowered for token in ("connection refused", "actively refused", "no route to host")):
            status = "offline"
            diagnostic_code = "connection_refused"
            conclusive = True
            reachable: bool | None = False
        elif any(token in lowered for token in ("name or service not known", "getaddrinfo", "nxdomain", "dns")):
            status = "unresolved"
            diagnostic_code = "dns_resolution_failed"
            conclusive = True
            reachable = None
        elif any(token in lowered for token in ("timed out", "timeout", "deadline exceeded")):
            status = "unconfirmed"
            diagnostic_code = "timeout"
            conclusive = False
            reachable = None
        else:
            status = "unavailable"
            diagnostic_code = "upstream_error"
            conclusive = False
            reachable = None

        original_error = result.pop("error", None)
        if original_error and "detail" not in result:
            result["detail"] = original_error
        result.update(
            {
                "ok": status == "online",
                "status": status,
                "reachable": reachable,
                "observation_conclusive": conclusive,
                "diagnostic": diagnostic_code,
            }
        )
        return result

    def _request_kuma_cached(
        self,
        *,
        path: str,
        timeout_ms: int,
        fresh_seconds: int,
        stale_seconds: int = 300,
    ) -> tuple[dict[str, Any], str, str | None]:
        now = time.monotonic()
        with self._kuma_cache_lock:
            cached = self._kuma_cache.get(path)
            if cached and now - cached[0] <= fresh_seconds:
                return cached[1], "cache_fresh", None

        try:
            payload = self._request_json_with_base(
                base_url=self.kuma_api_base_url,
                source_name="Kuma status API",
                path=path,
                params={},
                timeout_ms=max(timeout_ms, 8_000),
            )
        except MCStatusApiError as exc:
            with self._kuma_cache_lock:
                cached = self._kuma_cache.get(path)
            if cached and now - cached[0] <= stale_seconds:
                return cached[1], "cache_stale", str(exc)
            raise

        with self._kuma_cache_lock:
            self._kuma_cache[path] = (now, payload)
        return payload, "live", None

    def _build_maxmind_download_url(self) -> str:
        if not self.maxmind_license_key:
            raise MCStatusApiError("MAXMIND_LICENSE_KEY is required to download MaxMind database.")
        query = urlencode(
            {
                "edition_id": self.maxmind_edition_id,
                "license_key": self.maxmind_license_key,
                "suffix": "tar.gz",
            }
        )
        return f"{MAXMIND_DOWNLOAD_URL}?{query}"

    def _maxmind_should_refresh(self) -> bool:
        if not self.maxmind_db_path.exists():
            return True
        if self.maxmind_refresh_hours == 0:
            return False
        max_age_seconds = self.maxmind_refresh_hours * 3600
        file_age_seconds = max(0.0, time.time() - self.maxmind_db_path.stat().st_mtime)
        return file_age_seconds >= max_age_seconds

    def _download_maxmind_database(self, timeout_ms: int) -> None:
        url = self._build_maxmind_download_url()
        timeout_s = timeout_ms / 1000.0
        request = Request(
            url,
            headers={
                "User-Agent": "mcstatus-mcp-server/1.1",
                "Accept": "application/gzip, application/x-gzip, application/octet-stream",
            },
        )
        try:
            with urlopen(request, timeout=timeout_s) as response:
                archive_bytes = response.read()
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise MCStatusApiError(
                f"MaxMind download failed with HTTP {exc.code}: {body}"
            ) from exc
        except URLError as exc:
            raise MCStatusApiError(f"Unable to reach MaxMind download endpoint: {exc.reason}") from exc
        except TimeoutError as exc:
            raise MCStatusApiError("MaxMind database download timed out.") from exc

        try:
            with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
                mmdb_member = next(
                    (
                        member
                        for member in archive.getmembers()
                        if member.isfile() and member.name.lower().endswith(".mmdb")
                    ),
                    None,
                )
                if mmdb_member is None:
                    raise MCStatusApiError("MaxMind archive does not contain an .mmdb file.")

                extracted = archive.extractfile(mmdb_member)
                if extracted is None:
                    raise MCStatusApiError("Unable to extract MaxMind database file from archive.")

                self.maxmind_db_path.parent.mkdir(parents=True, exist_ok=True)
                temp_path = self.maxmind_db_path.with_suffix(f"{self.maxmind_db_path.suffix}.tmp")
                try:
                    with extracted:
                        with temp_path.open("wb") as target:
                            shutil.copyfileobj(extracted, target)
                    temp_path.replace(self.maxmind_db_path)
                finally:
                    if temp_path.exists():
                        temp_path.unlink(missing_ok=True)
        except tarfile.TarError as exc:
            raise MCStatusApiError("Downloaded MaxMind archive is invalid or corrupted.") from exc

    def _ensure_maxmind_database(self, timeout_ms: int) -> tuple[Path, bool]:
        if self.maxmind_db_path.exists():
            if not self._maxmind_should_refresh():
                return self.maxmind_db_path, False
            if not self.maxmind_license_key:
                # Keep serving stale DB when refresh is requested but credentials are absent.
                return self.maxmind_db_path, False
        self._download_maxmind_database(timeout_ms=timeout_ms)
        if not self.maxmind_db_path.exists():
            raise MCStatusApiError("MaxMind database download did not produce a usable file.")
        return self.maxmind_db_path, True

    @staticmethod
    def _extract_name(obj: Any) -> str | None:
        if not isinstance(obj, dict):
            return None
        names = obj.get("names")
        if isinstance(names, dict):
            english = names.get("en")
            if isinstance(english, str):
                return english
        name = obj.get("name")
        if isinstance(name, str):
            return name
        return None

    def _bgptools_asn_db_should_refresh(self) -> bool:
        if not self.bgptools_asn_db_path.exists():
            return True
        if self.bgptools_asn_refresh_hours == 0:
            return False
        max_age_seconds = self.bgptools_asn_refresh_hours * 3600
        file_age_seconds = max(0.0, time.time() - self.bgptools_asn_db_path.stat().st_mtime)
        return file_age_seconds >= max_age_seconds

    def _download_bgptools_asn_database(self, timeout_ms: int) -> None:
        user_agent = self.bgptools_user_agent
        if not user_agent:
            raise MCStatusApiError(
                "BGPTOOLS_USER_AGENT is required to download ASN database from bgp.tools."
            )

        request = Request(
            self.bgptools_asn_db_url,
            headers={
                "User-Agent": user_agent,
                "Accept": "text/csv, text/plain, */*",
            },
        )
        timeout_s = timeout_ms / 1000.0
        try:
            with urlopen(request, timeout=timeout_s) as response:
                payload = response.read()
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise MCStatusApiError(
                f"bgp.tools ASN database download failed with HTTP {exc.code}: {body}"
            ) from exc
        except URLError as exc:
            raise MCStatusApiError(f"Unable to reach bgp.tools ASN database endpoint: {exc.reason}") from exc
        except TimeoutError as exc:
            raise MCStatusApiError("bgp.tools ASN database download timed out.") from exc

        text = payload.decode("utf-8", errors="replace")
        if "Requests from default user agents are not allowed" in text:
            raise MCStatusApiError(
                "bgp.tools rejected User-Agent. Set BGPTOOLS_USER_AGENT to a descriptive value with contact."
            )
        if "asn,name,class,cc" not in text:
            raise MCStatusApiError("bgp.tools ASN database payload has unexpected format.")

        self.bgptools_asn_db_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.bgptools_asn_db_path.with_suffix(f"{self.bgptools_asn_db_path.suffix}.tmp")
        try:
            with temp_path.open("wb") as target:
                target.write(payload)
            temp_path.replace(self.bgptools_asn_db_path)
        finally:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)

    def _ensure_bgptools_asn_database(self, timeout_ms: int) -> tuple[Path, bool]:
        if self.bgptools_asn_db_path.exists():
            if not self._bgptools_asn_db_should_refresh():
                return self.bgptools_asn_db_path, False
            if not self.bgptools_user_agent:
                # Keep serving stale DB when refresh is requested but User-Agent is absent.
                return self.bgptools_asn_db_path, False
        self._download_bgptools_asn_database(timeout_ms=timeout_ms)
        if not self.bgptools_asn_db_path.exists():
            raise MCStatusApiError("bgp.tools ASN database download did not produce a usable file.")
        return self.bgptools_asn_db_path, True

    def _query_bgptools_whois(self, query: str, timeout_ms: int) -> str:
        timeout_s = timeout_ms / 1000.0
        try:
            with socket.create_connection((self.bgptools_whois_host, self.bgptools_whois_port), timeout=timeout_s) as conn:
                conn.settimeout(timeout_s)
                conn.sendall((query.strip() + "\n").encode("utf-8"))
                chunks: list[bytes] = []
                while True:
                    try:
                        chunk = conn.recv(4096)
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    chunks.append(chunk)
        except TimeoutError as exc:
            raise MCStatusApiError("bgp.tools whois request timed out.") from exc
        except OSError as exc:
            raise MCStatusApiError(f"Unable to query bgp.tools whois: {exc}") from exc

        result = b"".join(chunks).decode("utf-8", errors="replace").strip()
        if not result:
            raise MCStatusApiError("bgp.tools whois returned empty response.")
        return result

    @staticmethod
    def _parse_bgptools_whois_ip_row(whois_text: str) -> dict[str, Any] | None:
        lines = [line.strip() for line in whois_text.splitlines() if line.strip()]
        for line in lines:
            if "|" not in line:
                continue
            if line.lower().startswith("as"):
                continue

            parts = [part.strip() for part in line.split("|")]
            if len(parts) < 7:
                continue

            asn_raw = parts[0].upper().replace("AS", "").strip()
            asn: int | None = None
            if asn_raw.isdigit():
                asn = int(asn_raw)

            return {
                "asn": asn,
                "ip": parts[1] or None,
                "bgp_prefix": parts[2] or None,
                "cc": parts[3] or None,
                "registry": parts[4] or None,
                "allocated": parts[5] or None,
                "as_name": "|".join(parts[6:]).strip() or None,
                "raw_row": line,
            }
        return None

    def _lookup_bgptools_asn_record(self, asn: int, timeout_ms: int) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        details: dict[str, Any] = {
            "url": self.bgptools_asn_db_url,
            "path": str(self.bgptools_asn_db_path),
            "downloaded_now": False,
            "mtime_epoch": None,
            "error": None,
        }
        try:
            db_path, downloaded = self._ensure_bgptools_asn_database(timeout_ms=timeout_ms)
            details["path"] = str(db_path)
            details["downloaded_now"] = downloaded
            details["mtime_epoch"] = int(db_path.stat().st_mtime)
        except MCStatusApiError as exc:
            details["error"] = str(exc)
            return None, details

        target = f"AS{asn}"
        try:
            with db_path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    row_asn = (row.get("asn") or "").strip().upper()
                    if row_asn != target:
                        continue
                    return {
                        "asn": asn,
                        "as_number": target,
                        "name": (row.get("name") or "").strip() or None,
                        "class": (row.get("class") or "").strip() or None,
                        "cc": (row.get("cc") or "").strip() or None,
                    }, details
        except OSError as exc:
            details["error"] = f"Failed to read ASN database: {exc}"
            return None, details

        return None, details

    @staticmethod
    def _map_kuma_status(status_code: Any) -> str:
        if status_code == 1:
            return "UP"
        if status_code == 0:
            return "DOWN"
        if status_code == 2:
            return "PENDING"
        return "MAINTENANCE"

    @staticmethod
    def _normalize_alias(value: str) -> str:
        return "".join(char for char in value.strip().lower() if char.isalnum())

    @classmethod
    def _strip_generic_node_prefix(cls, value: str) -> str:
        normalized = value
        while normalized:
            if normalized in GENERIC_NODE_QUERY_TOKENS:
                return ""
            stripped = normalized
            for generic_token in GENERIC_NODE_QUERY_TOKENS:
                if normalized.startswith(generic_token) and len(normalized) > len(generic_token):
                    suffix = normalized[len(generic_token):]
                    if suffix and any(char.isdigit() for char in suffix):
                        stripped = suffix
                        break
            if stripped == normalized:
                return normalized
            normalized = stripped
        return normalized

    @staticmethod
    def _normalize_alias_part(value: str) -> str:
        if value.isdigit():
            return value.lstrip("0") or "0"
        return value

    @classmethod
    def _split_alias_parts(cls, value: str) -> list[str]:
        return [cls._normalize_alias_part(part) for part in re.findall(r"[^\W\d_]+|\d+", value) if part]

    @classmethod
    def _core_alias_parts(cls, value: str) -> list[str]:
        head = value.strip().lower().split(".", 1)[0]
        raw_tokens = re.findall(r"[^\W_]+", head)
        if not raw_tokens:
            normalized = cls._normalize_alias(head)
            normalized = cls._strip_generic_node_prefix(normalized)
            return cls._split_alias_parts(normalized) if normalized else []

        parts: list[str] = []
        for token in raw_tokens:
            normalized = cls._normalize_alias(token)
            if not normalized:
                continue
            normalized = cls._strip_generic_node_prefix(normalized)
            if not normalized:
                continue
            parts.extend(cls._split_alias_parts(normalized))
        return parts

    @classmethod
    def _build_alias_signature(cls, value: str) -> dict[str, Any]:
        raw = value.strip()
        lower = raw.lower()
        head = lower.split(".", 1)[0]
        normalized = cls._normalize_alias(lower)
        head_normalized = cls._normalize_alias(head)
        core_parts = cls._core_alias_parts(lower)
        return {
            "raw": raw,
            "lower": lower,
            "head": head,
            "normalized": normalized,
            "head_normalized": head_normalized,
            "core_parts": core_parts,
            "core_fingerprint": "".join(core_parts),
            "core_terms": set(core_parts),
        }

    @staticmethod
    def _serialize_alias_signature(signature: dict[str, Any]) -> dict[str, Any]:
        core_fingerprint = signature.get("core_fingerprint") or None
        return {
            "normalized": core_fingerprint or signature.get("head_normalized") or signature.get("normalized") or None,
            "core_parts": list(signature.get("core_parts") or []),
            "core_fingerprint": core_fingerprint,
        }

    @classmethod
    def _match_kuma_node_name(cls, query_name: str, monitor_name: str) -> tuple[int, str] | None:
        query_signature = cls._build_alias_signature(query_name)
        monitor_signature = cls._build_alias_signature(monitor_name)

        if monitor_name == query_signature["raw"]:
            return 0, "exact_name"
        if monitor_signature["lower"] == query_signature["lower"]:
            return 1, "case_insensitive_name"
        if monitor_signature["head"] == query_signature["lower"]:
            return 2, "short_hostname"
        if query_signature["head_normalized"] and query_signature["head_normalized"] == monitor_signature["head_normalized"]:
            return 3, "short_hostname_normalized"
        if query_signature["normalized"] and query_signature["normalized"] == monitor_signature["normalized"]:
            return 4, "full_name_normalized"
        if (
            query_signature["core_fingerprint"]
            and query_signature["core_fingerprint"] == monitor_signature["core_fingerprint"]
        ):
            return 5, "core_fingerprint"
        if query_signature["core_terms"] and query_signature["core_terms"].issubset(monitor_signature["core_terms"]):
            return 6, "core_terms_subset"

        return None

    @staticmethod
    def _extract_latest_kuma_heartbeat(
        heartbeat_list: dict[str, Any], monitor_id: int | str
    ) -> dict[str, Any] | None:
        heartbeat_entries = heartbeat_list.get(str(monitor_id))
        if not isinstance(heartbeat_entries, list) or not heartbeat_entries:
            return None
        candidate = heartbeat_entries[0]
        if isinstance(candidate, dict):
            return candidate
        return None

    @classmethod
    def _build_kuma_status_match(
        cls, monitor: dict[str, Any], heartbeat_list: dict[str, Any]
    ) -> dict[str, Any]:
        latest_heartbeat = cls._extract_latest_kuma_heartbeat(heartbeat_list, monitor["id"])
        status_code = latest_heartbeat.get("status") if latest_heartbeat else None
        status_label = cls._map_kuma_status(status_code)
        return {
            "node_name": monitor["name"],
            "node_id": monitor["id"],
            "matched_by": monitor["matched_by"],
            "match_priority": monitor["match_priority"],
            "status": status_label,
            "status_code": status_code,
            "heartbeat_time": latest_heartbeat.get("time") if latest_heartbeat else None,
            "message": latest_heartbeat.get("msg") if latest_heartbeat else None,
            "ping": latest_heartbeat.get("ping") if latest_heartbeat else None,
            "has_heartbeat": latest_heartbeat is not None,
            "matched_by_case_insensitive_name": monitor["matched_by"] != "exact_name",
        }

    def check_node_status(self, node_name: str, timeout_ms: int | None = None) -> dict[str, Any]:
        safe_node_name = self.validate_node_name(node_name)
        safe_timeout_ms = self.default_timeout_ms if timeout_ms is None else self.validate_timeout_ms(timeout_ms)
        query_signature = self._build_alias_signature(safe_node_name)

        try:
            nodes_payload, nodes_source, nodes_warning = self._request_kuma_cached(
                path="status-page/nodes",
                timeout_ms=safe_timeout_ms,
                fresh_seconds=300,
            )
        except MCStatusApiError as exc:
            return {
                "ok": False,
                "tool_execution": "completed",
                "status": "unavailable",
                "observation_conclusive": False,
                "input_node_name": safe_node_name,
                "error_code": "kuma_unavailable",
                "detail": str(exc),
            }
        groups = nodes_payload.get("publicGroupList")
        if not isinstance(groups, list):
            return {
                "ok": False,
                "tool_execution": "completed",
                "status": "unavailable",
                "observation_conclusive": False,
                "input_node_name": safe_node_name,
                "error_code": "invalid_kuma_nodes_payload",
            }

        ranked_matches: list[dict[str, Any]] = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            monitors = group.get("monitorList")
            if not isinstance(monitors, list):
                continue
            for monitor in monitors:
                if not isinstance(monitor, dict):
                    continue
                monitor_name = monitor.get("name")
                monitor_id = monitor.get("id")
                if not isinstance(monitor_name, str) or not isinstance(monitor_id, (int, str)):
                    continue
                name_match = self._match_kuma_node_name(safe_node_name, monitor_name)
                if name_match is None:
                    continue
                match_priority, match_mode = name_match
                ranked_matches.append(
                    {
                        "id": monitor_id,
                        "name": monitor_name,
                        "match_priority": match_priority,
                        "matched_by": match_mode,
                    }
                )

        if not ranked_matches:
            return {
                "ok": True,
                "tool_execution": "completed",
                "status": "unknown_node",
                "found": False,
                "observation_conclusive": False,
                "input_node_name": safe_node_name,
                "interpreted_query": self._serialize_alias_signature(query_signature),
                "detail": "Node with this name/alias was not found on Kuma status page.",
                "source": nodes_source,
            }

        best_match_priority = min(match["match_priority"] for match in ranked_matches)
        matches = sorted(
            (match for match in ranked_matches if match["match_priority"] == best_match_priority),
            key=lambda match: (str(match["name"]).lower(), str(match["id"])),
        )
        try:
            heartbeat_payload, heartbeat_source, heartbeat_warning = self._request_kuma_cached(
                path="status-page/heartbeat/nodes",
                timeout_ms=safe_timeout_ms,
                fresh_seconds=10,
            )
        except MCStatusApiError as exc:
            return {
                "ok": False,
                "tool_execution": "completed",
                "status": "unavailable",
                "observation_conclusive": False,
                "input_node_name": safe_node_name,
                "error_code": "kuma_unavailable",
                "detail": str(exc),
                "source": nodes_source,
            }
        heartbeat_list = heartbeat_payload.get("heartbeatList")
        if not isinstance(heartbeat_list, dict):
            return {
                "ok": False,
                "tool_execution": "completed",
                "status": "unavailable",
                "observation_conclusive": False,
                "input_node_name": safe_node_name,
                "error_code": "invalid_kuma_heartbeat_payload",
            }

        resolved_matches = [self._build_kuma_status_match(monitor=match, heartbeat_list=heartbeat_list) for match in matches]

        if len(resolved_matches) > 1:
            return {
                "ok": True,
                "input_node_name": safe_node_name,
                "interpreted_query": self._serialize_alias_signature(query_signature),
                "ambiguous": True,
                "match_count": len(resolved_matches),
                "match_priority": best_match_priority,
                "matched_by_modes": sorted({match["matched_by"] for match in resolved_matches}),
                "matches": resolved_matches,
                "tool_execution": "completed",
                "observation_conclusive": all(match["has_heartbeat"] for match in resolved_matches),
                "source": {"nodes": nodes_source, "heartbeats": heartbeat_source},
                "warning": heartbeat_warning or nodes_warning,
            }

        match = resolved_matches[0]
        return {
            "ok": True,
            "input_node_name": safe_node_name,
            "interpreted_query": self._serialize_alias_signature(query_signature),
            **match,
            "tool_execution": "completed",
            "observation_conclusive": bool(match["has_heartbeat"]),
            "source": {"nodes": nodes_source, "heartbeats": heartbeat_source},
            "warning": heartbeat_warning or nodes_warning,
        }

    def check_voice_chat_status(
        self,
        host: str,
        port: int = SIMPLE_VOICE_CHAT_DEFAULT_PORT,
        timeout_ms: int = 1000,
        attempts: int = 3,
    ) -> dict[str, Any]:
        safe_host = self.validate_host(host)
        safe_port = self.validate_port(port)
        safe_timeout_ms = self.validate_timeout_ms(timeout_ms)
        safe_attempts = self.validate_attempts(attempts)

        base_result: dict[str, Any] = {
            "software": "simple_voice_chat",
            "host": safe_host,
            "port": safe_port,
            "transport": "udp",
            "timeout_ms": safe_timeout_ms,
            "attempts_requested": safe_attempts,
        }

        try:
            family, socket_address = self._resolve_udp_endpoint(safe_host, safe_port)
        except OSError as exc:
            return {
                **base_result,
                "ok": False,
                "status": "error",
                "reachable": None,
                "probe_supported": True,
                "attempts_sent": 0,
                "responses_received": 0,
                "error": "dns_resolution_failed",
                "error_detail": str(exc),
            }

        results: list[dict[str, Any]] = []
        latencies_ms: list[float] = []
        valid_responses = 0
        invalid_responses = 0
        attempts_sent = 0

        for attempt in range(1, safe_attempts + 1):
            request_id = uuid.uuid4()
            timestamp_ms = int(time.time() * 1000)
            ping = self._encode_simple_voice_chat_ping(request_id, timestamp_ms)
            started = time.perf_counter()
            attempt_result: dict[str, Any] = {"attempt": attempt, "status": "timeout"}
            udp_socket: socket.socket | None = None

            try:
                udp_socket = socket.socket(family, socket.SOCK_DGRAM)
                udp_socket.settimeout(safe_timeout_ms / 1000)
                udp_socket.connect(socket_address)
                udp_socket.send(ping)
                attempts_sent += 1

                deadline = time.monotonic() + safe_timeout_ms / 1000
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise socket.timeout()
                    udp_socket.settimeout(remaining)
                    response = udp_socket.recv(1024)
                    if not self._is_matching_simple_voice_chat_pong(
                        response,
                        request_id=request_id,
                        timestamp_ms=timestamp_ms,
                    ):
                        invalid_responses += 1
                        continue

                    latency_ms = round((time.perf_counter() - started) * 1000, 2)
                    valid_responses += 1
                    latencies_ms.append(latency_ms)
                    attempt_result = {
                        "attempt": attempt,
                        "status": "reply",
                        "latency_ms": latency_ms,
                        "response_bytes": len(response),
                    }
                    break
            except (socket.timeout, TimeoutError):
                attempt_result = {"attempt": attempt, "status": "timeout"}
            except OSError as exc:
                attempt_result = {
                    "attempt": attempt,
                    "status": "socket_error",
                    "error": str(exc),
                }
            finally:
                if udp_socket is not None:
                    udp_socket.close()

            results.append(attempt_result)

        resolved_address = str(socket_address[0]) if socket_address else safe_host
        response_summary: dict[str, Any] = {
            **base_result,
            "probe_supported": True,
            "probe": "simple_voice_chat_external_ping_v1",
            "resolved_address": resolved_address,
            "attempts_sent": attempts_sent,
            "responses_received": valid_responses,
            "invalid_responses_received": invalid_responses,
            "packet_loss_percent": (
                round((attempts_sent - valid_responses) * 100 / attempts_sent, 2)
                if attempts_sent > 0
                else None
            ),
            "attempts": results,
        }

        if latencies_ms:
            return {
                **response_summary,
                "ok": True,
                "status": "online",
                "reachable": True,
                "latency_ms": round(sum(latencies_ms) / len(latencies_ms), 2),
                "latency_min_ms": min(latencies_ms),
                "latency_max_ms": max(latencies_ms),
            }

        return {
            **response_summary,
            "ok": False,
            "status": "unconfirmed",
            "reachable": None,
            "error": "no_valid_ping_response",
            "explanation": (
                "No valid Simple Voice Chat pong was received. This can mean the service is offline, the UDP "
                "route/firewall/proxy is wrong, the port is wrong, or the server has allow_pings disabled."
            ),
        }

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

        invalid_target = self._invalid_public_target(safe_host)
        if invalid_target:
            return {
                "ok": False,
                "tool_execution": "completed",
                "status": "invalid_input",
                "observation_conclusive": False,
                "host": safe_host,
                "port": safe_port,
                "edition": safe_edition,
                "error_code": "invalid_public_endpoint",
                "detail": invalid_target,
            }

        params: dict[str, Any] = {
            "host": safe_host,
            "port": safe_port,
            "edition": safe_edition,
            "timeout_ms": safe_timeout_ms,
        }

        if safe_edition == "java":
            params["mode"] = self.validate_mode(mode)
            params["proto"] = self.validate_proto(proto)

        try:
            response = self._request_json(path="status", params=params, timeout_ms=safe_timeout_ms)
        except MCStatusApiError as exc:
            return {
                "ok": False,
                "tool_execution": "completed",
                "status": "unavailable",
                "reachable": None,
                "observation_conclusive": False,
                "host": safe_host,
                "port": safe_port,
                "edition": safe_edition,
                "error_code": "mcstatus_upstream_unavailable",
                "detail": str(exc),
            }
        sanitized = self._sanitize_status_payload(response)
        if sanitized.get("ok") is False or sanitized.get("error"):
            return self._normalize_minecraft_status_failure(
                sanitized,
                host=safe_host,
                port=safe_port,
                edition=safe_edition,
            )
        sanitized.setdefault("tool_execution", "completed")
        sanitized.setdefault("status", "online")
        sanitized.setdefault("reachable", True)
        sanitized.setdefault("observation_conclusive", True)
        return sanitized

    def get_srv_records(self, host: str, port: int = JAVA_DEFAULT_PORT, timeout_ms: int | None = None) -> dict[str, Any]:
        safe_host = self.validate_host(host)
        safe_port = self.validate_port(port)
        safe_timeout_ms = self.default_timeout_ms if timeout_ms is None else self.validate_timeout_ms(timeout_ms)
        invalid_target = self._invalid_public_target(safe_host)
        if invalid_target:
            return {
                "ok": False,
                "tool_execution": "completed",
                "status": "invalid_input",
                "observation_conclusive": False,
                "host": safe_host,
                "error_code": "invalid_public_hostname",
                "detail": invalid_target,
            }
        try:
            result = self._request_json(
                path="srv",
                params={"host": safe_host, "port": safe_port},
                timeout_ms=safe_timeout_ms,
            )
        except MCStatusApiError as exc:
            return {
                "ok": False,
                "tool_execution": "completed",
                "status": "unavailable",
                "observation_conclusive": False,
                "host": safe_host,
                "error_code": "srv_upstream_unavailable",
                "detail": str(exc),
            }
        if result.get("ok") is False or result.get("error"):
            detail = self._diagnostic_text(result)
            lowered = detail.casefold()
            if any(token in lowered for token in ("nxdomain", "not found", "no srv", "no record")):
                return {
                    **result,
                    "ok": True,
                    "tool_execution": "completed",
                    "status": "nxdomain",
                    "records": [],
                    "observation_conclusive": True,
                    "host": safe_host,
                }
            return {
                **result,
                "ok": False,
                "tool_execution": "completed",
                "status": "unavailable",
                "observation_conclusive": False,
                "host": safe_host,
                "error_code": "srv_upstream_unavailable",
                "detail": detail or "SRV lookup did not return a usable result.",
            }
        result.setdefault("tool_execution", "completed")
        result.setdefault("observation_conclusive", True)
        return result

    def resolve_dns(self, host: str, timeout_ms: int | None = None) -> dict[str, Any]:
        safe_host = self.validate_host(host)
        safe_timeout_ms = self.default_timeout_ms if timeout_ms is None else self.validate_timeout_ms(timeout_ms)
        invalid_target = self._invalid_public_target(safe_host)
        if invalid_target:
            return {
                "ok": False,
                "tool_execution": "completed",
                "status": "invalid_input",
                "resolved": None,
                "observation_conclusive": False,
                "host": safe_host,
                "error_code": "invalid_public_hostname",
                "detail": invalid_target,
            }
        try:
            result = self._request_json(
                path="dns",
                params={"host": safe_host},
                timeout_ms=safe_timeout_ms,
            )
            if result.get("ok") is False or result.get("error"):
                raise MCStatusApiError(self._diagnostic_text(result) or "DNS lookup did not return a usable result.")
            result.setdefault("tool_execution", "completed")
            result.setdefault("observation_conclusive", True)
            return result
        except MCStatusApiError as upstream_error:
            try:
                addresses = sorted({item[4][0] for item in socket.getaddrinfo(safe_host, None)})
            except socket.gaierror as exc:
                return {
                    "ok": True,
                    "tool_execution": "completed",
                    "status": "nxdomain",
                    "resolved": False,
                    "observation_conclusive": True,
                    "host": safe_host,
                    "source": "local_dns_fallback",
                    "detail": str(exc),
                }
            except OSError as exc:
                return {
                    "ok": False,
                    "tool_execution": "completed",
                    "status": "unavailable",
                    "resolved": None,
                    "observation_conclusive": False,
                    "host": safe_host,
                    "error_code": "dns_lookup_unavailable",
                    "detail": f"{upstream_error}; local fallback: {exc}",
                }
            return {
                "ok": True,
                "tool_execution": "completed",
                "status": "resolved",
                "resolved": True,
                "observation_conclusive": True,
                "host": safe_host,
                "addresses": addresses,
                "source": "local_dns_fallback",
                "warning": str(upstream_error),
            }

    def get_bgp_info(self, ip: str, timeout_ms: int | None = None) -> dict[str, Any]:
        safe_ip = self.validate_ip(ip)
        safe_timeout_ms = self.default_timeout_ms if timeout_ms is None else self.validate_timeout_ms(timeout_ms)
        return self._request_json(
            path="bgp",
            params={"ip": safe_ip},
            timeout_ms=safe_timeout_ms,
        )

    def get_ip_provider_info(self, ip: str, timeout_ms: int | None = None) -> dict[str, Any]:
        safe_ip = self.validate_ip(ip)
        safe_timeout_ms = self.default_timeout_ms if timeout_ms is None else self.validate_timeout_ms(timeout_ms)
        payload: dict[str, Any] = {
            "ok": False,
            "ip": safe_ip,
            "source": "bgp.tools",
            "provider": None,
            "asn": None,
        }

        try:
            whois_text = self._query_bgptools_whois(query=safe_ip, timeout_ms=safe_timeout_ms)
        except MCStatusApiError as exc:
            payload["error"] = str(exc)
            return payload

        whois_row = self._parse_bgptools_whois_ip_row(whois_text)
        if whois_row is None:
            payload["error"] = "bgp.tools whois returned an unexpected payload format."
            payload["whois_raw"] = whois_text
            return payload

        asn = whois_row.get("asn")
        asn_db_record: dict[str, Any] | None = None
        asn_db_details: dict[str, Any] = {
            "url": self.bgptools_asn_db_url,
            "path": str(self.bgptools_asn_db_path),
            "downloaded_now": False,
            "mtime_epoch": None,
            "error": "ASN not present in bgp.tools whois response.",
        }
        if isinstance(asn, int):
            asn_db_record, asn_db_details = self._lookup_bgptools_asn_record(asn=asn, timeout_ms=safe_timeout_ms)

        provider = None
        if asn_db_record is not None:
            provider = asn_db_record.get("name")
        if not provider:
            provider = whois_row.get("as_name")

        payload["ok"] = True
        payload["provider"] = provider
        payload["asn"] = asn
        payload["as_name"] = whois_row.get("as_name")
        payload["bgp_prefix"] = whois_row.get("bgp_prefix")
        payload["cc"] = whois_row.get("cc")
        payload["registry"] = whois_row.get("registry")
        payload["allocated"] = whois_row.get("allocated")
        payload["whois_host"] = self.bgptools_whois_host
        payload["whois_port"] = self.bgptools_whois_port
        payload["whois_row"] = whois_row.get("raw_row")
        payload["asn_database"] = asn_db_details
        payload["asn_database_record"] = asn_db_record
        return payload

    def is_ip_anycast(self, ip: str, timeout_ms: int | None = None) -> dict[str, Any]:
        safe_ip = self.validate_ip(ip)
        _ = self.default_timeout_ms if timeout_ms is None else self.validate_timeout_ms(timeout_ms)

        known_node_label = KNOWN_ANYCAST_PLAYER_NODES.get(safe_ip)
        matched_known_list = known_node_label is not None

        payload: dict[str, Any] = {
            "ok": True,
            "ip": safe_ip,
            "is_anycast": matched_known_list,
            "matched_known_anycast_list": matched_known_list,
            "known_anycast_label": known_node_label,
            "detection_sources": ["known_anycast_list"] if matched_known_list else [],
            "detection_mode": "known_list_only",
            "bgp_anycast_by_upstreams_used_for_detection": False,
        }

        return payload

    def get_reverse_dns(self, ip: str, timeout_ms: int | None = None) -> dict[str, Any]:
        safe_ip = self.validate_ip(ip)
        safe_timeout_ms = self.default_timeout_ms if timeout_ms is None else self.validate_timeout_ms(timeout_ms)
        timeout_s = safe_timeout_ms / 1000.0

        payload: dict[str, Any] = {
            "ok": False,
            "ip": safe_ip,
            "ptr": None,
            "aliases": [],
            "addresses": [],
        }
        done = threading.Event()
        result: tuple[str, list[str], list[str]] | None = None
        error: Exception | None = None

        def worker() -> None:
            nonlocal result, error
            try:
                host, aliases, addresses = socket.gethostbyaddr(safe_ip)
                result = (host, aliases, addresses)
            except Exception as exc:  # noqa: BLE001
                error = exc
            finally:
                done.set()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        if not done.wait(timeout=timeout_s):
            payload["error"] = "Reverse DNS lookup timed out."
            return payload

        if error is not None:
            payload["error"] = str(error)
            return payload

        if result is None:
            payload["error"] = "Reverse DNS lookup returned no result."
            return payload

        host, aliases, addresses = result
        payload["ok"] = True
        payload["ptr"] = host
        payload["aliases"] = aliases
        payload["addresses"] = addresses
        return payload

    def get_geoip_maxmind(self, ip: str, timeout_ms: int | None = None) -> dict[str, Any]:
        safe_ip = self.validate_ip(ip)
        safe_timeout_ms = self.default_timeout_ms if timeout_ms is None else self.validate_timeout_ms(timeout_ms)

        payload: dict[str, Any] = {
            "ok": False,
            "source": MAXMIND_SOURCE_NAME,
            "ip": safe_ip,
        }

        if maxminddb is None:
            payload["error"] = "`maxminddb` package is not installed."
            return payload

        try:
            db_path, downloaded = self._ensure_maxmind_database(timeout_ms=safe_timeout_ms)
        except MCStatusApiError as exc:
            payload["error"] = str(exc)
            return payload

        try:
            with maxminddb.open_database(str(db_path)) as reader:
                record = reader.get(safe_ip)
        except Exception as exc:  # noqa: BLE001
            payload["error"] = f"Failed to read MaxMind database: {exc}"
            return payload

        payload["database_path"] = str(db_path)
        payload["database_mtime_epoch"] = int(db_path.stat().st_mtime)
        payload["database_downloaded_now"] = downloaded

        if not isinstance(record, dict):
            payload["error"] = "IP is not present in MaxMind database."
            return payload

        country = record.get("country")
        city = record.get("city")
        location = record.get("location")
        subdivisions = record.get("subdivisions")
        first_subdivision = subdivisions[0] if isinstance(subdivisions, list) and subdivisions else None
        postal = record.get("postal")

        payload["ok"] = True
        payload["country_iso_code"] = country.get("iso_code") if isinstance(country, dict) else None
        payload["country_name"] = self._extract_name(country)
        payload["city_name"] = self._extract_name(city)
        payload["subdivision_name"] = self._extract_name(first_subdivision)
        payload["postal_code"] = postal.get("code") if isinstance(postal, dict) else None
        payload["time_zone"] = location.get("time_zone") if isinstance(location, dict) else None
        payload["latitude"] = location.get("latitude") if isinstance(location, dict) else None
        payload["longitude"] = location.get("longitude") if isinstance(location, dict) else None
        payload["raw"] = record
        return payload
