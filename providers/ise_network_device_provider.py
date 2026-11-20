"""
ise_network_device_provider.py

Cisco ISE ERS Network Device provider.

Purpose:
- Centralize Cisco ISE ERS NetworkDevice and NetworkDeviceGroup API access.
- Keep FastAPI routes and audit services free of raw requests/ERS plumbing.
- Provide safe pagination, defensive error handling, and payload sanitization.

This provider is intended to be consumed by ndg_audit_service.py and any future
admin workflow that governs ISE Network Devices / NDGs.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from requests.auth import HTTPBasicAuth
from requests.exceptions import RequestException


logger = logging.getLogger("ISE_Network_Device_Provider")


ISE_SECRET_SCRUB_FIELDS = {
    "previousSharedSecret",
    "previousSharedSecretExpiry",
    "isPreviousSharedSecretEnabled",
}


class ISENetworkDeviceProviderError(Exception):
    """Base exception for ISE Network Device provider failures."""


class ISEProviderConfigurationError(ISENetworkDeviceProviderError):
    """Raised when required ISE provider configuration is missing or invalid."""


class ISEProviderRequestError(ISENetworkDeviceProviderError):
    """Raised when an ISE ERS API request fails."""


@dataclass
class ISENetworkDeviceProviderConfig:
    """Runtime configuration for Cisco ISE ERS NetworkDevice APIs."""

    ise_host: str
    username: str
    password: str
    verify_ssl: bool = False
    request_timeout_seconds: int = 30
    page_size: int = 100

    @classmethod
    def from_env(cls) -> "ISENetworkDeviceProviderConfig":
        """Build config from project environment variables."""

        ise_host = os.getenv("ISE_PAN_IP", "").strip()
        username = os.getenv("ISE_ERS_USER", "").strip()
        password = os.getenv("ISE_ERS_PASS", "").strip()

        missing = []
        if not ise_host:
            missing.append("ISE_PAN_IP")
        if not username:
            missing.append("ISE_ERS_USER")
        if not password:
            missing.append("ISE_ERS_PASS")

        if missing:
            raise ISEProviderConfigurationError(
                f"Missing required ISE environment variables: {', '.join(missing)}"
            )

        verify_ssl = os.getenv("ISE_VERIFY_SSL", "false").strip().lower() in {
            "1",
            "true",
            "yes",
            "y",
        }

        return cls(
            ise_host=ise_host,
            username=username,
            password=password,
            verify_ssl=verify_ssl,
            request_timeout_seconds=int(os.getenv("ISE_REQUEST_TIMEOUT_SECONDS", "30")),
            page_size=int(os.getenv("ISE_ERS_PAGE_SIZE", "100")),
        )


def safe_string(value: Any, default: str = "") -> str:
    """Return a stripped string or a default."""

    if value is None:
        return default

    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned if cleaned else default

    return str(value)


def scrub_previous_secret_fields(obj: Any) -> None:
    """
    Remove ISE previous-shared-secret metadata from nested objects in-place.

    ISE can return previous secret metadata fields on network-device responses.
    Those fields should not be sent back during update operations.
    """

    if isinstance(obj, dict):
        for key in list(obj.keys()):
            if key in ISE_SECRET_SCRUB_FIELDS:
                obj.pop(key, None)
            else:
                scrub_previous_secret_fields(obj[key])
    elif isinstance(obj, list):
        for item in obj:
            scrub_previous_secret_fields(item)


def is_ipv4_address(value: str) -> bool:
    """Validate basic IPv4 dotted decimal syntax and octet ranges."""

    candidate = safe_string(value)

    if not re.match(r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$", candidate):
        return False

    try:
        return all(0 <= int(octet) <= 255 for octet in candidate.split("."))
    except Exception:
        return False


def normalize_mask(mask: Any) -> int:
    """Normalize ISE network device mask value to 0-32 range."""

    try:
        parsed = int(mask)
        if 0 <= parsed <= 32:
            return parsed
    except Exception:
        pass

    return 32


class ISENetworkDeviceProvider:
    """
    Cisco ISE ERS NetworkDevice / NetworkDeviceGroup provider.

    This class owns all raw REST interaction with ISE ERS for NetworkDevice
    governance workflows. It intentionally avoids FastAPI imports.
    """

    def __init__(self, config: ISENetworkDeviceProviderConfig) -> None:
        if not config.ise_host:
            raise ISEProviderConfigurationError("ISE host is required.")
        if not config.username:
            raise ISEProviderConfigurationError("ISE ERS username is required.")
        if not config.password:
            raise ISEProviderConfigurationError("ISE ERS password is required.")
        if config.page_size < 1:
            raise ISEProviderConfigurationError("ERS page_size must be greater than zero.")

        self.config = config
        self.base_ers_url = f"https://{config.ise_host}:9060/ers/config"
        self.network_device_url = f"{self.base_ers_url}/networkdevice"
        self.network_device_group_url = f"{self.base_ers_url}/networkdevicegroup"

        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(config.username, config.password)
        self.session.verify = config.verify_ssl
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

        logger.info(
            "Initialized ISENetworkDeviceProvider host=%s verify_ssl=%s page_size=%s",
            config.ise_host,
            config.verify_ssl,
            config.page_size,
        )

    @classmethod
    def from_env(cls) -> "ISENetworkDeviceProvider":
        """Create provider directly from .env/environment variables."""

        return cls(ISENetworkDeviceProviderConfig.from_env())

    # ------------------------------------------------------------------
    # Low-level request helpers
    # ------------------------------------------------------------------

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Execute an ISE ERS request with consistent error handling."""

        try:
            response = self.session.request(
                method=method,
                url=url,
                timeout=self.config.request_timeout_seconds,
                **kwargs,
            )
        except RequestException as exc:
            logger.exception("ISE ERS request transport failure method=%s url=%s", method, url)
            raise ISEProviderRequestError(
                f"ISE ERS request transport failure method={method} url={url} error={exc}"
            ) from exc

        if response.status_code >= 400:
            logger.error(
                "ISE ERS request failed method=%s url=%s status=%s body=%s",
                method,
                url,
                response.status_code,
                response.text,
            )
            raise ISEProviderRequestError(
                f"ISE ERS request failed method={method} url={url} "
                f"status={response.status_code} body={response.text}"
            )

        return response

    @staticmethod
    def _json_or_empty(response: requests.Response) -> Dict[str, Any]:
        if not response.text:
            return {}
        try:
            data = response.json()
            return data if isinstance(data, dict) else {}
        except ValueError as exc:
            raise ISEProviderRequestError(f"ISE ERS returned invalid JSON: {exc}") from exc

    def _get_search_result_resources(self, url: str, page_size: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Fetch all SearchResult resources from a paginated ISE ERS endpoint.

        ISE ERS list endpoints commonly use page/size pagination.
        """

        resources: List[Dict[str, Any]] = []
        page = 1
        size = page_size or self.config.page_size
        total: Optional[int] = None

        while True:
            separator = "&" if "?" in url else "?"
            paged_url = f"{url}{separator}page={page}&size={size}"

            response = self._request("GET", paged_url)
            data = self._json_or_empty(response)
            search_result = data.get("SearchResult", {}) if isinstance(data, dict) else {}
            page_resources = search_result.get("resources", []) or []

            if not isinstance(page_resources, list):
                page_resources = []

            resources.extend([item for item in page_resources if isinstance(item, dict)])
            total = search_result.get("total") if total is None else total

            logger.info(
                "Fetched ISE ERS page url=%s page=%s page_count=%s total=%s accumulated=%s",
                url,
                page,
                len(page_resources),
                total,
                len(resources),
            )

            if not page_resources:
                break
            if isinstance(total, int) and len(resources) >= total:
                break
            if len(page_resources) < size:
                break

            page += 1

        return resources

    # ------------------------------------------------------------------
    # NetworkDevice methods
    # ------------------------------------------------------------------

    def list_network_devices(self) -> List[Dict[str, Any]]:
        """Return ISE NetworkDevice SearchResult summaries."""

        return self._get_search_result_resources(self.network_device_url)

    def list_network_devices_full(self) -> List[Dict[str, Any]]:
        """Return full ISE NetworkDevice objects for all NetworkDevice records."""

        summaries = self.list_network_devices()
        full_devices: List[Dict[str, Any]] = []

        for summary in summaries:
            device_id = safe_string(summary.get("id"))
            if not device_id:
                continue

            try:
                full_devices.append(self.get_network_device_by_id(device_id))
            except Exception as exc:
                logger.warning("Failed loading full ISE NetworkDevice id=%s error=%s", device_id, exc)

        return full_devices

    def get_network_device_by_id(self, device_id: str) -> Dict[str, Any]:
        """Return one full ISE NetworkDevice object by ID."""

        cleaned_id = safe_string(device_id)
        if not cleaned_id:
            raise ValueError("device_id is required.")

        response = self._request("GET", f"{self.network_device_url}/{cleaned_id}")
        data = self._json_or_empty(response)
        network_device = data.get("NetworkDevice", data)

        if not isinstance(network_device, dict):
            raise ISEProviderRequestError(f"Unexpected NetworkDevice response for id={cleaned_id}")

        return network_device

    def get_network_device_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Return one full NetworkDevice by exact name match, if found."""

        cleaned_name = safe_string(name)
        if not cleaned_name:
            raise ValueError("name is required.")

        filter_url = f"{self.network_device_url}?filter=name.EQ.{cleaned_name}"
        resources = self._get_search_result_resources(filter_url)

        if not resources:
            return None

        device_id = safe_string(resources[0].get("id"))
        return self.get_network_device_by_id(device_id) if device_id else None

    def find_network_device_by_ip(self, ip_address: str) -> Optional[Dict[str, Any]]:
        """
        Find a full ISE NetworkDevice containing the given IP address.

        ISE ERS filtering support for nested NetworkDeviceIPList can vary, so this
        method intentionally builds a full inventory and checks locally.
        """

        cleaned_ip = safe_string(ip_address)
        if not is_ipv4_address(cleaned_ip):
            raise ValueError(f"Invalid IPv4 address: {ip_address}")

        for device in self.list_network_devices_full():
            for ip_entry in device.get("NetworkDeviceIPList", []) or []:
                if safe_string(ip_entry.get("ipaddress")) == cleaned_ip:
                    return device

        return None

    def index_network_devices(self) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
        """
        Return full NetworkDevice indexes by IP and lowercase name.

        Returns:
            (by_ip, by_name)
        """

        by_ip: Dict[str, Dict[str, Any]] = {}
        by_name: Dict[str, Dict[str, Any]] = {}

        for device in self.list_network_devices_full():
            name = safe_string(device.get("name"))
            if name:
                by_name[name.lower()] = device

            for ip_entry in device.get("NetworkDeviceIPList", []) or []:
                ip_address = safe_string(ip_entry.get("ipaddress"))
                if ip_address:
                    by_ip[ip_address] = device

        return by_ip, by_name

    def create_network_device(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Create an ISE NetworkDevice."""

        if not isinstance(payload, dict) or "NetworkDevice" not in payload:
            raise ValueError("payload must be a dict containing a NetworkDevice key.")

        scrub_previous_secret_fields(payload)
        response = self._request("POST", self.network_device_url, json=payload)

        return {
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "body": response.text,
        }

    def update_network_device(self, device_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Update an ISE NetworkDevice by ID."""

        cleaned_id = safe_string(device_id)
        if not cleaned_id:
            raise ValueError("device_id is required.")
        if not isinstance(payload, dict) or "NetworkDevice" not in payload:
            raise ValueError("payload must be a dict containing a NetworkDevice key.")

        scrub_previous_secret_fields(payload)
        response = self._request("PUT", f"{self.network_device_url}/{cleaned_id}", json=payload)

        return {
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "body": response.text,
        }

    def delete_network_device(self, device_id: str) -> Dict[str, Any]:
        """
        Delete an ISE NetworkDevice by ID.

        This method is intentionally available for completeness, but current audit
        workflows should not call it automatically.
        """

        cleaned_id = safe_string(device_id)
        if not cleaned_id:
            raise ValueError("device_id is required.")

        response = self._request("DELETE", f"{self.network_device_url}/{cleaned_id}")

        return {
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "body": response.text,
        }

    # ------------------------------------------------------------------
    # NetworkDeviceGroup methods
    # ------------------------------------------------------------------

    def list_network_device_groups(self) -> List[Dict[str, Any]]:
        """Return ISE NetworkDeviceGroup SearchResult summaries."""

        return self._get_search_result_resources(self.network_device_group_url)

    def get_network_device_group_by_id(self, group_id: str) -> Dict[str, Any]:
        """Return one full ISE NetworkDeviceGroup by ID."""

        cleaned_id = safe_string(group_id)
        if not cleaned_id:
            raise ValueError("group_id is required.")

        response = self._request("GET", f"{self.network_device_group_url}/{cleaned_id}")
        data = self._json_or_empty(response)
        group = data.get("NetworkDeviceGroup", data)

        if not isinstance(group, dict):
            raise ISEProviderRequestError(f"Unexpected NetworkDeviceGroup response for id={cleaned_id}")

        return group

    def get_network_device_group_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Return one ISE NetworkDeviceGroup by exact name match, if found."""

        cleaned_name = safe_string(name)
        if not cleaned_name:
            raise ValueError("name is required.")

        filter_url = f"{self.network_device_group_url}?filter=name.EQ.{cleaned_name}"
        resources = self._get_search_result_resources(filter_url)

        if not resources:
            return None

        group_id = safe_string(resources[0].get("id"))
        return self.get_network_device_group_by_id(group_id) if group_id else None

    def get_existing_group_names(self) -> List[str]:
        """Return all visible ISE NetworkDeviceGroup names from SearchResult summaries."""

        groups = self.list_network_device_groups()
        names = [safe_string(group.get("name")) for group in groups]
        return sorted(name for name in names if name)

    def validate_group_names(self, desired_group_names: Iterable[str]) -> Dict[str, List[str]]:
        """
        Validate desired NDG names against current ISE NDG inventory.

        Returns:
            {
                "existing": [...],
                "missing": [...]
            }
        """

        existing_names = set(self.get_existing_group_names())
        desired_names = [safe_string(group) for group in desired_group_names if safe_string(group)]

        existing = sorted(group for group in desired_names if group in existing_names)
        missing = sorted(group for group in desired_names if group not in existing_names)

        return {
            "existing": existing,
            "missing": missing,
        }

    # ------------------------------------------------------------------
    # Payload builders
    # ------------------------------------------------------------------

    @staticmethod
    def build_network_device_payload(
        name: str,
        ip_address: str,
        mask: int = 32,
        description: str = "",
        network_device_groups: Optional[List[str]] = None,
        radius_shared_secret: str = "",
    ) -> Dict[str, Any]:
        """Build a minimal valid ISE NetworkDevice payload."""

        cleaned_name = safe_string(name)
        cleaned_ip = safe_string(ip_address)

        if not cleaned_name:
            raise ValueError("name is required.")
        if not is_ipv4_address(cleaned_ip):
            raise ValueError(f"Invalid IPv4 address: {ip_address}")

        network_device: Dict[str, Any] = {
            "name": cleaned_name,
            "description": safe_string(description),
            "NetworkDeviceIPList": [
                {
                    "ipaddress": cleaned_ip,
                    "mask": normalize_mask(mask),
                }
            ],
            "NetworkDeviceGroupList": list(network_device_groups or []),
            "authenticationSettings": {
                "networkProtocol": "RADIUS",
            },
        }

        if radius_shared_secret:
            network_device["authenticationSettings"]["radiusSharedSecret"] = radius_shared_secret

        return {"NetworkDevice": network_device}

    @staticmethod
    def merge_network_device_payload(
        existing_network_device: Dict[str, Any],
        name: Optional[str] = None,
        ip_address: Optional[str] = None,
        mask: int = 32,
        description: Optional[str] = None,
        network_device_groups: Optional[List[str]] = None,
        radius_shared_secret: str = "",
        update_radius_secret: bool = False,
    ) -> Dict[str, Any]:
        """
        Merge desired values into an existing ISE NetworkDevice object.

        Used for safe PUT updates while preserving ISE fields not owned by the
        audit service.
        """

        if not isinstance(existing_network_device, dict):
            raise ValueError("existing_network_device must be a dict.")

        network_device = dict(existing_network_device)
        scrub_previous_secret_fields(network_device)

        if name is not None:
            cleaned_name = safe_string(name)
            if not cleaned_name:
                raise ValueError("name cannot be empty when provided.")
            network_device["name"] = cleaned_name

        if description is not None:
            network_device["description"] = safe_string(description)

        if ip_address is not None:
            cleaned_ip = safe_string(ip_address)
            if not is_ipv4_address(cleaned_ip):
                raise ValueError(f"Invalid IPv4 address: {ip_address}")
            network_device["NetworkDeviceIPList"] = [
                {
                    "ipaddress": cleaned_ip,
                    "mask": normalize_mask(mask),
                }
            ]

        if network_device_groups is not None:
            network_device["NetworkDeviceGroupList"] = list(network_device_groups)

        auth_settings = network_device.get("authenticationSettings")
        if not isinstance(auth_settings, dict):
            auth_settings = {}
        auth_settings["networkProtocol"] = auth_settings.get("networkProtocol", "RADIUS")

        if update_radius_secret and radius_shared_secret:
            auth_settings["radiusSharedSecret"] = radius_shared_secret

        network_device["authenticationSettings"] = auth_settings

        return {"NetworkDevice": network_device}

    # ------------------------------------------------------------------
    # Health / diagnostics
    # ------------------------------------------------------------------

    def health_check(self) -> Dict[str, Any]:
        """Lightweight ERS health check using NetworkDevice list endpoint."""

        try:
            resources = self._get_search_result_resources(self.network_device_url, page_size=1)
            return {
                "status": "OK",
                "ise_host": self.config.ise_host,
                "network_device_query_ok": True,
                "sample_count": len(resources),
            }
        except Exception as exc:
            logger.exception("ISE NetworkDevice provider health check failed.")
            return {
                "status": "ERROR",
                "ise_host": self.config.ise_host,
                "network_device_query_ok": False,
                "error": str(exc),
            }
