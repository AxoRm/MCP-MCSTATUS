from __future__ import annotations

import ipaddress
import io
import json
import os
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


class MCStatusApiError(RuntimeError):
    """Error returned when mcstatus.xyz request fails."""


class MCStatusApiClient:
    """Typed client wrapper around mcstatus.xyz endpoints."""

    def __init__(
        self,
        base_url: str = DEFAULT_API_BASE_URL,
        default_timeout_ms: int = DEFAULT_TIMEOUT_MS,
        maxmind_db_path: str = DEFAULT_MAXMIND_DB_PATH,
        maxmind_license_key: str | None = None,
        maxmind_edition_id: str = DEFAULT_MAXMIND_EDITION_ID,
        maxmind_refresh_hours: int = DEFAULT_MAXMIND_REFRESH_HOURS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.default_timeout_ms = self.validate_timeout_ms(default_timeout_ms)
        self.maxmind_db_path = Path(maxmind_db_path).expanduser()
        self.maxmind_license_key = (maxmind_license_key or "").strip()
        self.maxmind_edition_id = maxmind_edition_id.strip() or DEFAULT_MAXMIND_EDITION_ID
        self.maxmind_refresh_hours = self.validate_maxmind_refresh_hours(maxmind_refresh_hours)

    @classmethod
    def from_environment(cls) -> MCStatusApiClient:
        base_url = os.getenv("MCSTATUS_API_BASE_URL", DEFAULT_API_BASE_URL)
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
        return cls(
            base_url=base_url,
            default_timeout_ms=timeout_ms,
            maxmind_db_path=maxmind_db_path,
            maxmind_license_key=maxmind_license_key,
            maxmind_edition_id=maxmind_edition_id,
            maxmind_refresh_hours=maxmind_refresh_hours,
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
    def validate_maxmind_refresh_hours(refresh_hours: int) -> int:
        value = int(refresh_hours)
        if value < 0:
            raise ValueError("`maxmind_refresh_hours` must be >= 0.")
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
