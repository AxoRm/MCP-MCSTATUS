from __future__ import annotations

import csv
import ipaddress
import io
import json
import os
import re
import shutil
import socket
import tarfile
import threading
import time
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
    def _match_kuma_node_name(cls, query_name: str, monitor_name: str) -> tuple[int, str] | None:
        query_raw = query_name.strip()
        query_lower = query_raw.lower()
        query_normalized = cls._normalize_alias(query_raw)
        monitor_lower = monitor_name.strip().lower()
        monitor_head = monitor_lower.split(".", 1)[0]
        monitor_head_tokens = [token for token in re.split(r"[\s_-]+", monitor_head) if token]
        monitor_head_normalized = cls._normalize_alias(monitor_head)
        monitor_head_token_normalized: set[str] = set()
        for token in monitor_head_tokens:
            normalized = cls._normalize_alias(token)
            if normalized:
                monitor_head_token_normalized.add(normalized)
        monitor_full_normalized = cls._normalize_alias(monitor_lower)

        if monitor_name == query_raw:
            return 0, "exact_name"
        if monitor_lower == query_lower:
            return 1, "case_insensitive_name"
        if monitor_head == query_lower:
            return 2, "short_hostname"
        if query_lower in monitor_head_tokens:
            return 3, "short_hostname_token"
        if query_normalized and query_normalized == monitor_head_normalized:
            return 4, "short_hostname_normalized"
        if query_normalized and query_normalized in monitor_head_token_normalized:
            return 5, "short_hostname_token_normalized"
        if query_normalized and query_normalized == monitor_full_normalized:
            return 6, "full_name_normalized"

        return None

    def check_node_status(self, node_name: str, timeout_ms: int | None = None) -> dict[str, Any]:
        safe_node_name = self.validate_node_name(node_name)
        safe_timeout_ms = self.default_timeout_ms if timeout_ms is None else self.validate_timeout_ms(timeout_ms)

        nodes_payload = self._request_json_with_base(
            base_url=self.kuma_api_base_url,
            source_name="Kuma status API",
            path="status-page/nodes",
            params={},
            timeout_ms=safe_timeout_ms,
        )
        groups = nodes_payload.get("publicGroupList")
        if not isinstance(groups, list):
            raise MCStatusApiError("Kuma status API returned unexpected payload for status-page/nodes.")

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
                "ok": False,
                "input_node_name": safe_node_name,
                "error": "Node with this name/alias was not found on Kuma status page.",
            }

        best_match_priority = min(match["match_priority"] for match in ranked_matches)
        matches = [match for match in ranked_matches if match["match_priority"] == best_match_priority]
        if len(matches) > 1:
            return {
                "ok": False,
                "input_node_name": safe_node_name,
                "error": (
                    "Multiple nodes matched this name/alias at the same confidence level. "
                    "Use a more specific node name."
                ),
                "matches": matches,
            }

        monitor = matches[0]
        monitor_id_str = str(monitor["id"])
        heartbeat_payload = self._request_json_with_base(
            base_url=self.kuma_api_base_url,
            source_name="Kuma status API",
            path="status-page/heartbeat/nodes",
            params={},
            timeout_ms=safe_timeout_ms,
        )
        heartbeat_list = heartbeat_payload.get("heartbeatList")
        if not isinstance(heartbeat_list, dict):
            raise MCStatusApiError("Kuma status API returned unexpected payload for status-page/heartbeat/nodes.")

        heartbeat_entries = heartbeat_list.get(monitor_id_str)
        latest_heartbeat: dict[str, Any] | None = None
        if isinstance(heartbeat_entries, list) and heartbeat_entries:
            candidate = heartbeat_entries[0]
            if isinstance(candidate, dict):
                latest_heartbeat = candidate

        status_code = latest_heartbeat.get("status") if latest_heartbeat else None
        status_label = self._map_kuma_status(status_code)
        return {
            "ok": True,
            "input_node_name": safe_node_name,
            "node_name": monitor["name"],
            "node_id": monitor["id"],
            "matched_by": monitor["matched_by"],
            "status": status_label,
            "status_code": status_code,
            "heartbeat_time": latest_heartbeat.get("time") if latest_heartbeat else None,
            "message": latest_heartbeat.get("msg") if latest_heartbeat else None,
            "ping": latest_heartbeat.get("ping") if latest_heartbeat else None,
            "has_heartbeat": latest_heartbeat is not None,
            "matched_by_case_insensitive_name": monitor["matched_by"] != "exact_name",
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
