# Save as: providers/meraki_provider.py

import ipaddress
import logging
import os
import re
import time
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import meraki
from meraki.exceptions import APIError
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout as RequestsTimeout

logger = logging.getLogger("Meraki_Provider")

MAC_REGEX = re.compile(r"^(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$|^[0-9A-Fa-f]{12}$")
TRANSIENT_MERAKI_STATUSES = {500, 502, 503, 504}

class MerakiProviderError(Exception):
    """Base exception for Meraki provider failures."""

class MerakiConfigurationError(MerakiProviderError):
    """Raised when required Meraki provider configuration is missing or invalid."""

class MerakiLookupError(MerakiProviderError):
    """Raised when a Meraki lookup fails unexpectedly."""

@dataclass
class MerakiClientContext:
    meraki_found: bool
    mac_address: str
    ip_address: str
    network_id: str
    network_name: str
    switch_ap_name: str
    switch_ap_serial: str
    switch_ap_mac: str
    port_ssid: str
    vlan: str
    status: str
    manufacturer: str
    os: str
    user: str
    description: str
    first_seen: str
    last_seen: str
    lookup_source: str
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

class TTLCache:
    def __init__(self, ttl_seconds: int = 300) -> None:
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be greater than zero.")
        self.ttl_seconds = ttl_seconds
        self._store: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            item = self._store.get(key)
            if not item:
                return None
            expires_at = float(item.get("expires_at", 0))
            if time.time() > expires_at:
                self._store.pop(key, None)
                return None
            return item.get("value")

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._store[key] = {
                "value": value,
                "expires_at": time.time() + self.ttl_seconds,
            }

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


class MerakiProvider:
    def __init__(
        self,
        api_key: str,
        organization_id: str,
        lookback_days: int = 31,
        cache_ttl_seconds: int = 300,
        suppress_sdk_logging: bool = True,
        max_retries: int = 3,
        request_timeout_seconds: int = 30,
    ) -> None:
        if not api_key or not api_key.strip():
            raise MerakiConfigurationError("MERAKI_API_KEY is missing or empty.")
        if not organization_id or not organization_id.strip():
            raise MerakiConfigurationError("MERAKI_ORG_ID is missing or empty.")
        if lookback_days < 1 or lookback_days > 31:
            raise MerakiConfigurationError("MERAKI_LOOKBACK_DAYS must be between 1 and 31.")

        self.organization_id = organization_id.strip()
        self.lookback_days = lookback_days
        self.timespan_seconds = lookback_days * 24 * 60 * 60
        self.cache = TTLCache(ttl_seconds=cache_ttl_seconds)
        
        self.enrich_device_names = os.getenv("MERAKI_ENRICH_DEVICE_NAMES", "false").strip().lower() in {"1", "true", "yes", "y"}

        # rate-limit handler successfully configured here
        self.dashboard = meraki.DashboardAPI(
            api_key=api_key.strip(),
            suppress_logging=suppress_sdk_logging,
            print_console=False,
            output_log=False,
            wait_on_rate_limit=True,
            maximum_retries=max_retries,
            single_request_timeout=request_timeout_seconds,
        )

        logger.info("MerakiProvider initialized org_id=%s ttl=%s", self.organization_id, cache_ttl_seconds)

    @staticmethod
    def normalize_mac(mac_address: str) -> str:
        if not mac_address or not isinstance(mac_address, str):
            raise ValueError("MAC address is required.")
        candidate = mac_address.strip().replace("-", ":").upper()
        if re.fullmatch(r"[0-9A-F]{12}", candidate):
            candidate = ":".join(candidate[i : i + 2] for i in range(0, 12, 2))
        if not MAC_REGEX.match(candidate):
            raise ValueError(f"Invalid MAC address format: {mac_address}")
        return candidate

    @staticmethod
    def normalize_mac_compact(mac_address: str) -> str:
        normalized = MerakiProvider.normalize_mac(mac_address)
        return normalized.replace(":", "").upper()

    @staticmethod
    def validate_ipv4(ip_address: str) -> str:
        if not ip_address or not isinstance(ip_address, str):
            raise ValueError("IP address is required.")
        candidate = ip_address.strip()
        try:
            ip_obj = ipaddress.ip_address(candidate)
        except ValueError as exc:
            raise ValueError(f"Invalid IP address format: {ip_address}") from exc
        if ip_obj.version != 4:
            raise ValueError(f"Only IPv4 lookup is currently supported: {ip_address}")
        return str(ip_obj)

    @staticmethod
    def _safe_string(value: Any, default: str = "N/A") -> str:
        if value is None: return default
        if isinstance(value, str):
            cleaned = value.strip()
            return cleaned if cleaned else default
        return str(value)

    @staticmethod
    def _epoch_to_utc_string(value: Any) -> str:
        if value is None or value == "": return "N/A"
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped: return "N/A"
            if stripped.isdigit():
                try:
                    return datetime.fromtimestamp(int(stripped), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                except Exception: return stripped
            return stripped
        if isinstance(value, (int, float)):
            try: return datetime.fromtimestamp(int(value), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            except Exception: return str(value)
        return str(value)

    def _call_sdk(self, sdk_callable: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return sdk_callable(*args, **kwargs)
        except TypeError as exc:
            message = str(exc)
            pagination_keys = {"total_pages", "perPage"}
            if any(key in kwargs for key in pagination_keys):
                fallback_kwargs = {k: v for k, v in kwargs.items() if k not in pagination_keys}
                logger.warning("SDK rejected pagination kwargs. Retrying without. method=%s", getattr(sdk_callable, "__name__", "unknown"))
                return sdk_callable(*args, **fallback_kwargs)
            raise

    @staticmethod
    def _ensure_list(value: Any) -> List[Dict[str, Any]]:
        if value is None: return []
        if isinstance(value, list): return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            records = value.get("records")
            if isinstance(records, list): return [item for item in records if isinstance(item, dict)]
            return [value]
        return []

    def _empty_context(self, mac_address: str = "", ip_address: str = "", lookup_source: str = "none", error: str = "") -> MerakiClientContext:
        return MerakiClientContext(
            meraki_found=False, mac_address=mac_address or "N/A", ip_address=ip_address or "N/A",
            network_id="N/A", network_name="N/A", switch_ap_name="N/A", switch_ap_serial="N/A", switch_ap_mac="N/A",
            port_ssid="N/A", vlan="N/A", status="Not Found", manufacturer="N/A", os="N/A", user="N/A",
            description="N/A", first_seen="N/A", last_seen="N/A", lookup_source=lookup_source, error=error,
        )

    # ---------------------------------------------------------------------
    # Inventory Cache Wrappers
    # ---------------------------------------------------------------------

    def get_organization_networks(self) -> List[Dict[str, Any]]:
        cache_key = "organization_networks"
        cached = self.cache.get(cache_key)
        if cached is not None: return cached

        try:
            networks = self._call_sdk(self.dashboard.organizations.getOrganizationNetworks, self.organization_id, total_pages="all")
            normalized = self._ensure_list(networks)
            self.cache.set(cache_key, normalized)
            return normalized
        except Exception as exc:
            logger.exception("Meraki error fetching org networks.")
            raise MerakiLookupError(str(exc)) from exc

    def get_network_details(self, network_id: str) -> Dict[str, Any]:
        cache_key = f"network_details_{network_id}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        for attempt in range(2):
            try:
                details = self._call_sdk(self.dashboard.networks.getNetwork, network_id)
                normalized = details if isinstance(details, dict) else {}
                self.cache.set(cache_key, normalized)
                return normalized
            except Exception as exc:
                if attempt == 0 and self._is_transient_meraki_error(exc):
                    logger.warning(
                        "Transient Meraki network detail failure for %s; retrying once: %s",
                        network_id,
                        exc,
                    )
                    time.sleep(0.5)
                    continue
                logger.warning("Meraki error fetching network details for %s: %s", network_id, exc)
                raise MerakiLookupError(str(exc)) from exc

        raise MerakiLookupError(f"Could not fetch Meraki network details for {network_id}.")

    @staticmethod
    def _is_transient_meraki_error(exc: Exception) -> bool:
        return (
            getattr(exc, "status", None) in TRANSIENT_MERAKI_STATUSES
            or isinstance(exc, (RequestsConnectionError, RequestsTimeout, ConnectionError, TimeoutError))
        )

    def get_organization_devices(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        cache_key = "organization_devices"
        if not force_refresh:
            cached = self.cache.get(cache_key)
            if cached is not None: return cached

        try:
            devices = self._call_sdk(self.dashboard.organizations.getOrganizationDevices, self.organization_id, total_pages="all")
            normalized = self._ensure_list(devices)
            self.cache.set(cache_key, normalized)
            return normalized
        except Exception as exc:
            logger.warning("Meraki error fetching org devices: %s", exc)
            self.cache.set(cache_key, [])
            return []

    def get_appliance_location_inventory(self) -> List[Dict[str, Any]]:
        """Returns one location record per network containing an MX or Z4 appliance."""
        networks: Dict[str, Dict[str, Any]] = {}
        for device in self.get_organization_devices(force_refresh=True):
            model = self._safe_string(device.get("model"), "")
            if not model.upper().startswith(("MX", "Z4")):
                continue

            network_id = self._safe_string(device.get("networkId"), "")
            if not network_id:
                continue

            record = networks.setdefault(network_id, {
                "network_id": network_id,
                "network_name": self._safe_string(device.get("networkName"), ""),
                "address": "",
                "address_source": "",
                "address_conflict": False,
                "time_zone": "",
                "device_count": 0,
                "appliance_models": set(),
                "serial_numbers": set(),
                "lookup_error": "",
            })
            record["device_count"] += 1
            record["appliance_models"].add(model)
            device_address = self._safe_string(device.get("address"), "")
            if device_address:
                if record["address"] and record["address"] != device_address:
                    record["address_conflict"] = True
                elif not record["address"]:
                    record["address"] = device_address
                    record["address_source"] = "device"
            serial = self._safe_string(device.get("serial"), "")
            if serial:
                record["serial_numbers"].add(serial)

        network_metadata: Dict[str, Dict[str, Any]] = {}
        try:
            network_metadata = {
                str(network.get("id")): network
                for network in self.get_organization_networks()
                if network.get("id")
            }
        except Exception as exc:
            logger.warning("Could not fetch Meraki organization network metadata: %s", exc)

        inventory = []
        for network_id, record in networks.items():
            metadata = network_metadata.get(network_id, {})
            if metadata:
                record["network_name"] = self._safe_string(
                    metadata.get("name"), record["network_name"]
                )
                record["time_zone"] = self._safe_string(metadata.get("timeZone"), "")
                if not record["address"]:
                    record["address"] = self._safe_string(metadata.get("address"), "")
                    if record["address"]:
                        record["address_source"] = "network"

            if not record["address"] or not record["time_zone"]:
                try:
                    details = self.get_network_details(network_id)
                    record["network_name"] = self._safe_string(
                        details.get("name"), record["network_name"]
                    )
                    record["time_zone"] = self._safe_string(
                        details.get("timeZone"), record["time_zone"]
                    )
                    if not record["address"]:
                        record["address"] = self._safe_string(details.get("address"), "")
                        if record["address"]:
                            record["address_source"] = "network"
                except Exception as exc:
                    record["lookup_error"] = str(exc)
                    logger.warning("Could not fetch Meraki network details for %s: %s", network_id, exc)

            record["appliance_models"] = ", ".join(sorted(record["appliance_models"]))
            record["serial_numbers"] = ", ".join(sorted(record["serial_numbers"]))
            inventory.append(record)

        return sorted(inventory, key=lambda row: row["network_name"].lower())

    def get_organization_templates(self) -> List[Dict[str, Any]]:
        cache_key = "organization_config_templates"
        cached = self.cache.get(cache_key)
        if cached is not None: return cached

        try:
            templates = self._call_sdk(self.dashboard.organizations.getOrganizationConfigTemplates, self.organization_id)
            normalized = self._ensure_list(templates)
            self.cache.set(cache_key, normalized)
            return normalized
        except Exception as exc:
            logger.error("Failed to fetch organization templates: %s", exc)
            return []

    # FIX 1: Completed the missing dictionary logic for MAC mapping
    def _build_device_index_by_mac(self) -> Dict[str, Dict[str, Any]]:
        cache_key = "organization_devices_by_mac"
        cached = self.cache.get(cache_key)
        if cached is not None: return cached

        devices = self.get_organization_devices()
        index = {}
        for d in devices:
            mac = self._safe_string(d.get("mac"))
            if mac and mac != "N/A":
                try:
                    compact_mac = self.normalize_mac_compact(mac)
                    index[compact_mac] = d
                except ValueError:
                    pass

        self.cache.set(cache_key, index)
        return index

    def _get_device_by_mac(self, device_mac: str) -> Optional[Dict[str, Any]]:
        if not device_mac or device_mac == "N/A": return None
        try: compact_mac = self.normalize_mac_compact(device_mac)
        except ValueError: return None
        device_index = self._build_device_index_by_mac()
        return device_index.get(compact_mac)

    # FIX 2: Added missing wrapper to cache heavy VLAN lookups across the org
    def get_network_appliance_vlans(self, network_id: str) -> List[Dict[str, Any]]:
        cache_key = f"appliance_vlans_{network_id}"
        cached = self.cache.get(cache_key)
        if cached is not None: return cached

        try:
            vlans = self._call_sdk(self.dashboard.appliance.getNetworkApplianceVlans, network_id)
            normalized = self._ensure_list(vlans)
            self.cache.set(cache_key, normalized)
            return normalized
        except Exception as exc:
            # Often returns 400/404 if network has no appliance or doesn't support VLANs
            self.cache.set(cache_key, [])
            return []

    # ---------------------------------------------------------------------
    # Normalization & Lookups
    # ---------------------------------------------------------------------

    @staticmethod
    def _extract_network(record: Dict[str, Any]) -> Tuple[str, str]:
        network = record.get("network")
        if isinstance(network, dict):
            return MerakiProvider._safe_string(network.get("id")), MerakiProvider._safe_string(network.get("name"))
        return MerakiProvider._safe_string(record.get("networkId")), MerakiProvider._safe_string(record.get("networkName"))

    def _choose_best_record(self, records: List[Dict[str, Any]], preferred_ip: str = "") -> Optional[Dict[str, Any]]:
        if not records: return None
        if preferred_ip:
            exact = [r for r in records if self._safe_string(r.get("ip"), "") == preferred_ip]
            if exact: records = exact

        def score(r: Dict[str, Any]) -> Tuple[int, int]:
            online_score = 1 if self._safe_string(r.get("status"), "").lower() == "online" else 0
            try: last_seen_score = int(r.get("lastSeen") or 0)
            except Exception: last_seen_score = 0
            return online_score, last_seen_score

        return sorted(records, key=score, reverse=True)[0]

    def _normalize_record(self, record: Dict[str, Any], fallback_mac: str, lookup_source: str) -> MerakiClientContext:
        network_id, network_name = self._extract_network(record)
        raw_mac = self._safe_string(record.get("mac"), fallback_mac)
        try: mac_address = self.normalize_mac(raw_mac)
        except ValueError: mac_address = raw_mac.upper()

        ip_address = self._safe_string(record.get("ip"))
        ssid = self._safe_string(record.get("ssid"))
        switchport = self._safe_string(record.get("switchport"))
        port_ssid = switchport if switchport != "N/A" else ssid

        recent_device_mac = self._safe_string(record.get("recentDeviceMac"))
        recent_device_serial = self._safe_string(record.get("recentDeviceSerial"))
        recent_device_name = self._safe_string(record.get("recentDeviceName"))

        if self.enrich_device_names and recent_device_name == "N/A" and recent_device_mac != "N/A":
            device = self._get_device_by_mac(recent_device_mac)
            if device:
                recent_device_name = self._safe_string(device.get("name"), self._safe_string(device.get("model"), recent_device_mac))
                recent_device_serial = self._safe_string(device.get("serial"), recent_device_serial)

        if recent_device_name == "N/A": recent_device_name = recent_device_mac

        return MerakiClientContext(
            meraki_found=True, mac_address=mac_address, ip_address=ip_address, network_id=network_id,
            network_name=network_name, switch_ap_name=recent_device_name, switch_ap_serial=recent_device_serial,
            switch_ap_mac=recent_device_mac, port_ssid=port_ssid, vlan=self._safe_string(record.get("vlan")),
            status=self._safe_string(record.get("status")), manufacturer=self._safe_string(record.get("manufacturer")),
            os=self._safe_string(record.get("os")), user=self._safe_string(record.get("user")),
            description=self._safe_string(record.get("description")), first_seen=self._epoch_to_utc_string(record.get("firstSeen")),
            last_seen=self._epoch_to_utc_string(record.get("lastSeen")), lookup_source=lookup_source, error="",
        )

    def get_client_by_mac(self, mac_address: str, preferred_ip: str = "") -> MerakiClientContext:
        normalized_mac = self.normalize_mac(mac_address)
        normalized_ip = self.validate_ipv4(preferred_ip) if preferred_ip and preferred_ip.strip() and preferred_ip != "No IP Assigned" else ""

        cache_key = f"client_by_mac:{normalized_mac}:preferred_ip:{normalized_ip}"
        cached = self.cache.get(cache_key)
        if cached is not None: return cached

        try:
            response = self._call_sdk(self.dashboard.organizations.getOrganizationClientsSearch, self.organization_id, mac=normalized_mac, total_pages="all")
            records = self._ensure_list(response)
            best_record = self._choose_best_record(records=records, preferred_ip=normalized_ip)

            if not best_record:
                context = self._empty_context(mac_address=normalized_mac, ip_address=normalized_ip, lookup_source="org_search", error="Not found.")
                self.cache.set(cache_key, context)
                return context

            context = self._normalize_record(record=best_record, fallback_mac=normalized_mac, lookup_source="org_search")
            self.cache.set(cache_key, context)
            return context
        except Exception as exc:
            context = self._empty_context(mac_address=normalized_mac, ip_address=normalized_ip, lookup_source="org_search", error=str(exc))
            self.cache.set(cache_key, context)
            return context

    def get_network_clients_paginated(self, network_id: str, ip_address: str = "", mac_address: str = "", statuses: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"timespan": self.timespan_seconds, "perPage": 5000, "total_pages": "all"}
        if ip_address: params["ip"] = self.validate_ipv4(ip_address)
        if mac_address: params["mac"] = self.normalize_mac(mac_address)
        if statuses: params["statuses"] = statuses

        try:
            response = self._call_sdk(self.dashboard.networks.getNetworkClients, network_id.strip(), **params)
            return self._ensure_list(response)
        except Exception: return []

    def get_client_by_ip(self, ip_address: str) -> MerakiClientContext:
        normalized_ip = self.validate_ipv4(ip_address)
        cache_key = f"client_by_ip:{normalized_ip}"
        cached = self.cache.get(cache_key)
        if cached is not None: return cached

        try:
            networks = self.get_organization_networks()
            for network in networks:
                network_id = self._safe_string(network.get("id"), "")
                if not network_id: continue

                clients = self.get_network_clients_paginated(network_id=network_id, ip_address=normalized_ip)
                if not clients: continue

                best_record = self._choose_best_record(records=clients, preferred_ip=normalized_ip)
                if not best_record: continue

                best_record["network"] = {"id": network_id, "name": self._safe_string(network.get("name"))}
                context = self._normalize_record(record=best_record, fallback_mac=self._safe_string(best_record.get("mac")), lookup_source="net_search")
                self.cache.set(cache_key, context)
                return context

            context = self._empty_context(ip_address=normalized_ip, lookup_source="net_search", error="Not found.")
            self.cache.set(cache_key, context)
            return context
        except Exception as exc:
            context = self._empty_context(ip_address=normalized_ip, lookup_source="net_search", error=str(exc))
            self.cache.set(cache_key, context)
            return context

    def get_client_by_mac_or_ip(self, mac_address: str = "", ip_address: str = "") -> MerakiClientContext:
        if mac_address and mac_address.strip(): return self.get_client_by_mac(mac_address=mac_address, preferred_ip=ip_address)
        if ip_address and ip_address.strip() and ip_address != "No IP Assigned": return self.get_client_by_ip(ip_address=ip_address)
        return self._empty_context(mac_address=mac_address or "N/A", ip_address=ip_address or "N/A", lookup_source="mac_or_ip", error="Neither MAC nor IP provided.")

    def enrich_adapter_record(self, adapter_record: Dict[str, Any]) -> Dict[str, Any]:
        adapter_name = self._safe_string(adapter_record.get("Name"), "Unknown")
        adapter_type = self._safe_string(adapter_record.get("Type"), "Unknown")
        mac_address = self._safe_string(adapter_record.get("MacAddress"), "")
        ip_address = self._safe_string(adapter_record.get("IPAddress"), "No IP Assigned")
        os_status = self._safe_string(adapter_record.get("Status"), "Unknown")

        context = self.get_client_by_mac_or_ip(mac_address=mac_address, ip_address="" if ip_address == "No IP Assigned" else ip_address)

        return {
            "adapter_name": adapter_name, "adapter_type": adapter_type, "mac_address": mac_address or "N/A",
            "ip_address": ip_address or "No IP Assigned", "os_status": os_status, "meraki_found": context.meraki_found,
            "network_name": context.network_name, "switch_ap_name": context.switch_ap_name, "switch_ap_serial": context.switch_ap_serial,
            "switch_ap_mac": context.switch_ap_mac, "port_ssid": context.port_ssid, "vlan": context.vlan,
            "meraki_status": context.status, "manufacturer": context.manufacturer, "os": context.os,
            "user": context.user, "description": context.description, "first_seen": context.first_seen,
            "last_seen": context.last_seen, "lookup_source": context.lookup_source, "meraki_error": context.error,
        }

    def health_check(self) -> Dict[str, Any]:
        try:
            networks = self.get_organization_networks()
            return {"status": "OK", "organization_id": self.organization_id, "network_count": len(networks), "lookback_days": self.lookback_days}
        except Exception as exc:
            return {"status": "ERROR", "organization_id": self.organization_id, "network_count": 0, "lookback_days": self.lookback_days, "error": str(exc)}