"""
ndg_audit_service.py

Meraki -> Cisco ISE Network Device audit/sync service.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple, Optional

import requests
from requests.auth import HTTPBasicAuth
from requests.exceptions import RequestException

from core.config import get_settings

logger = logging.getLogger("NDG_Audit_Service")


def _get_default_location_ndg() -> str:
    settings = get_settings()
    return (os.getenv("ISE_DEFAULT_LOCATION_NDG") or settings.ISE_DEFAULT_LOCATION_NDG).strip()


DEFAULT_BASE_NEW_DEVICE_GROUPS = [
    get_settings().ISE_DEFAULT_LOCATION_NDG,
    "IPSEC#Is IPSEC Device#No",
]

ISE_SECRET_SCRUB_FIELDS = {
    "previousSharedSecret",
    "previousSharedSecretExpiry",
    "isPreviousSharedSecretEnabled",
}

class NDGAuditError(Exception):
    """Base exception for NDG audit failures."""

class NDGConfigurationError(NDGAuditError):
    """Raised when the audit service is missing required configuration."""

class ISENetworkDeviceError(NDGAuditError):
    """Raised when ISE NetworkDevice API interaction fails."""

@dataclass
class NDGAuditConfig:
    ise_host: str
    ise_username: str
    ise_password: str
    radius_shared_secret: str = ""
    target_vlan_id: int = 21
    default_location_ndg: str = field(default_factory=_get_default_location_ndg)
    base_new_device_groups: List[str] = field(
        default_factory=lambda: [_get_default_location_ndg(), "IPSEC#Is IPSEC Device#No"]
    )
    report_dir: str = "reports"
    verify_ssl: bool = False
    request_timeout_seconds: int = 30
    ers_page_size: int = 100
    update_radius_secret: bool = False
    target_template_id: str = ""
    hardware_filter: int = 1  # 1 = All, 2 = Z-Series Only, 3 = MX-Series Only
    gateway_type: str = "Z3"  # Default fallback values updated
    gateway_designation: str = "IOP"
    deployment_stage: str = "Limited Access"  # <-- Set the default stage

    @classmethod
    def from_env(cls) -> "NDGAuditConfig":
        settings = get_settings()
        required = {
            "ISE_PAN_IP": (os.getenv("ISE_PAN_IP") or settings.ISE_PAN_IP).strip(),
            "ISE_ERS_USER": (os.getenv("ISE_ERS_USER") or settings.ISE_ERS_USER).strip(),
            "ISE_ERS_PASS": (os.getenv("ISE_ERS_PASS") or settings.ISE_ERS_PASS).strip(),
        }

        missing = [name for name, value in required.items() if not value]
        if missing:
            raise NDGConfigurationError(f"Missing required environment variables: {', '.join(missing)}")

        raw_vlan = os.getenv("ISE_NDG_TARGET_VLAN_ID")
        target_vlan = int(raw_vlan) if raw_vlan is not None else settings.ISE_NDG_TARGET_VLAN_ID

        raw_update_secret = os.getenv("ISE_NDG_UPDATE_RADIUS_SECRET")
        update_secret = (
            raw_update_secret.strip().lower() in {"1", "true", "yes", "y"}
            if raw_update_secret is not None
            else settings.ISE_NDG_UPDATE_RADIUS_SECRET
        )

        raw_verify = os.getenv("ISE_VERIFY_SSL")
        verify_ssl = (
            raw_verify.strip().lower() in {"1", "true", "yes", "y"}
            if raw_verify is not None
            else settings.ISE_VERIFY_SSL
        )

        raw_timeout = os.getenv("ISE_NDG_REQUEST_TIMEOUT_SECONDS")
        timeout_seconds = (
            int(raw_timeout) if raw_timeout is not None else settings.ISE_NDG_REQUEST_TIMEOUT_SECONDS
        )

        raw_page_size = os.getenv("ISE_NDG_ERS_PAGE_SIZE")
        ers_page_size = (
            int(raw_page_size) if raw_page_size is not None else settings.ISE_NDG_ERS_PAGE_SIZE
        )

        default_location_ndg = (
            os.getenv("ISE_DEFAULT_LOCATION_NDG") or settings.ISE_DEFAULT_LOCATION_NDG
        ).strip()

        return cls(
            ise_host=required["ISE_PAN_IP"],
            ise_username=required["ISE_ERS_USER"],
            ise_password=required["ISE_ERS_PASS"],
            radius_shared_secret=(os.getenv("RADIUS_SHARED_SECRET") or settings.RADIUS_SHARED_SECRET).strip(),
            target_vlan_id=target_vlan,
            default_location_ndg=default_location_ndg,
            base_new_device_groups=[default_location_ndg, "IPSEC#Is IPSEC Device#No"],
            report_dir=(os.getenv("ISE_NDG_REPORT_DIR") or settings.ISE_NDG_REPORT_DIR).strip(),
            verify_ssl=verify_ssl,
            request_timeout_seconds=timeout_seconds,
            ers_page_size=ers_page_size,
            update_radius_secret=update_secret,
        )

@dataclass
class DesiredNetworkDevice:
    name: str
    ip_address: str
    mask: int
    description: str
    network_device_groups: List[str]
    meraki_serial: str
    meraki_model: str
    meraki_mac: str
    meraki_network_id: str
    meraki_network_name: str
    source: str = "meraki"

@dataclass
class AuditItem:
    action: str
    name: str
    ip_address: str
    reason: str
    ise_id: str = ""
    meraki_serial: str = ""
    meraki_model: str = ""
    dry_run: bool = True
    changed: bool = False
    error: str = ""
    diff: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

@dataclass
class AuditResult:
    status: str
    dry_run: bool
    started_at: str
    completed_at: str
    summary: Dict[str, int]
    items: List[AuditItem]
    json_report_path: str = ""
    csv_report_path: str = ""
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["items"] = [item.to_dict() for item in self.items]
        return data

def utc_now_string() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def timestamp_for_filename() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

def scrub_previous_secret_fields(obj: Any) -> None:
    if isinstance(obj, dict):
        for key in list(obj.keys()):
            if key in ISE_SECRET_SCRUB_FIELDS:
                obj.pop(key, None)
            else:
                scrub_previous_secret_fields(obj[key])
    elif isinstance(obj, list):
        for item in obj:
            scrub_previous_secret_fields(item)

def safe_string(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned if cleaned else default
    return str(value)

def normalize_ip_mask(mask: Any) -> int:
    try:
        parsed = int(mask)
        if parsed < 0 or parsed > 32:
            return 32
        return parsed
    except Exception:
        return 32


class ISENetworkDeviceClient:
    def __init__(self, config: NDGAuditConfig) -> None:
        self.config = config
        self.base_url = f"https://{config.ise_host}:9060/ers/config/networkdevice"
        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(config.ise_username, config.ise_password)
        self.session.verify = config.verify_ssl
        self.session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        try:
            response = self.session.request(method=method, url=url, timeout=self.config.request_timeout_seconds, **kwargs)
        except RequestException as exc:
            raise ISENetworkDeviceError(f"ISE request failed method={method} url={url} error={exc}") from exc

        if response.status_code >= 400:
            raise ISENetworkDeviceError(f"ISE request failed method={method} url={url} status={response.status_code} body={response.text}")
        return response

    def list_network_devices(self) -> List[Dict[str, Any]]:
        resources: List[Dict[str, Any]] = []
        page = 1
        size = self.config.ers_page_size
        total = None

        while True:
            url = f"{self.base_url}?page={page}&size={size}"
            response = self._request("GET", url)
            data = response.json() if response.text else {}
            search_result = data.get("SearchResult", {})
            page_resources = search_result.get("resources", []) or []

            resources.extend(page_resources)
            total = search_result.get("total") if total is None else total

            if not page_resources or (isinstance(total, int) and len(resources) >= total) or len(page_resources) < size:
                break
            page += 1
        return resources

    def get_network_device_by_id(self, device_id: str) -> Dict[str, Any]:
        response = self._request("GET", f"{self.base_url}/{device_id}")
        data = response.json() if response.text else {}
        return data.get("NetworkDevice", data)

    def create_network_device(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        scrub_previous_secret_fields(payload)
        response = self._request("POST", self.base_url, json=payload)
        return {"status_code": response.status_code, "body": response.text}

    def update_network_device(self, device_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        scrub_previous_secret_fields(payload)
        response = self._request("PUT", f"{self.base_url}/{device_id}", json=payload)
        return {"status_code": response.status_code, "body": response.text}


class NDGAuditService:
    # Item 2 Fix: Class-level persistent cache across endpoint instances
    _ise_inventory_cache: Optional[List[Dict[str, Any]]] = None

    def __init__(self, meraki_provider: Any, config: Optional[NDGAuditConfig] = None) -> None:
        if meraki_provider is None:
            raise NDGConfigurationError("meraki_provider is required.")
        self.meraki_provider = meraki_provider
        self.config = config or NDGAuditConfig.from_env()
        self.ise_client = ISENetworkDeviceClient(self.config)

    def run_audit(self, dry_run: bool = True, apply_updates: bool = False, progress_callback=None) -> AuditResult:
        self.progress_callback = progress_callback
        started_at = utc_now_string()
        items: List[AuditItem] = []
        errors: List[str] = []

        logger.info("Starting NDG audit dry_run=%s apply_updates=%s", dry_run, apply_updates)

        try:
            # 0% - 45%: Meraki Data Gathering
            self._update_progress(5, "Targeting Meraki Templates & Hardware...")
            desired_devices = self.build_desired_devices_from_meraki()
            desired_by_ip = {device.ip_address: device for device in desired_devices}
            desired_by_name = {device.name.lower(): device for device in desired_devices}

            # 45% - 80%: ISE Data Gathering with Item 2 Cache Interception
            existing_full = []
            if NDGAuditService._ise_inventory_cache is not None:
                self._update_progress(45, "Loading cached ISE inventory from previous step...")
                existing_full = NDGAuditService._ise_inventory_cache
            else:
                self._update_progress(45, "Fetching ISE Base Inventory... (This may take a moment)")
                existing_summaries = self.ise_client.list_network_devices()
                
                total_ise = len(existing_summaries)
                for idx, summary in enumerate(existing_summaries):
                    device_id = safe_string(summary.get("id"))
                    if device_id:
                        try:
                            existing_full.append(self.ise_client.get_network_device_by_id(device_id))
                        except Exception:
                            pass
                    
                    if idx % 25 == 0:
                        pct = 50 + int((idx / max(total_ise, 1)) * 30)
                        self._update_progress(pct, f"Downloading ISE Configuration {idx}/{total_ise}...")
                
                # Save into memory so the Live Sync can use it instantly
                NDGAuditService._ise_inventory_cache = existing_full

            # 80% - 95%: Drift Calculation
            self._update_progress(80, "Cross-referencing configuration diffs...")
            existing_by_ip, existing_by_name = self._index_existing_devices(existing_full)

            for desired in desired_devices:
                existing = existing_by_ip.get(desired.ip_address) or existing_by_name.get(desired.name.lower())

                if not existing:
                    items.append(self._handle_create_candidate(desired, dry_run=dry_run))
                    continue

                items.append(self._handle_existing_candidate(desired, existing, dry_run, apply_updates))

            summary = self._build_summary(items, desired_count=len(desired_by_ip), existing_count=len(existing_full))
            status = "COMPLETED_WITH_ERRORS" if any(item.error for item in items) or errors else "COMPLETED"

            # If Live Sync finishes successfully, flush the cache to prepare for future cycles
            if apply_updates and status == "COMPLETED":
                NDGAuditService._ise_inventory_cache = None

        except Exception as exc:
            logger.exception("NDG audit failed unexpectedly.")
            errors.append(str(exc))
            summary = self._build_summary(items, desired_count=0, existing_count=0)
            status = "FAILED"

        completed_at = utc_now_string()
        result = AuditResult(status=status, dry_run=dry_run, started_at=started_at, completed_at=completed_at, summary=summary, items=items, errors=errors)
        
        # 95% - 100%: Reporting
        self._update_progress(95, "Writing localized compliance reports...")
        self._write_reports(result)
        
        return result

    def _update_progress(self, pct: int, msg: str):
        if hasattr(self, 'progress_callback') and self.progress_callback:
            self.progress_callback(pct, msg)
        logging.getLogger("NDG_Audit_Service").info(msg)
        
    def build_desired_devices_from_meraki(self) -> List[DesiredNetworkDevice]:
        cache_key = f"meraki_ndg_targets_{self.config.target_template_id}"
        if hasattr(self.meraki_provider, 'cache'):
            cached_devices = self.meraki_provider.cache.get(cache_key)
            if cached_devices:
                self._update_progress(10, "Found cached Meraki data from recent Dry Run. Skipping API pull...")
                return cached_devices

        self._update_progress(10, "Fetching live organization devices and networks...")

        all_devices = self.meraki_provider.get_organization_devices(force_refresh=True)
        networks = self.meraki_provider.get_organization_networks()

        target_template_id = self.config.target_template_id
        bound_networks = [n for n in networks if n.get('configTemplateId') == target_template_id]
        
        devices_by_network = {}
        for d in all_devices:
            net_id = d.get('networkId')
            if net_id:
                devices_by_network.setdefault(net_id, []).append(d)

        desired_devices: List[DesiredNetworkDevice] = []
        target_vlan = self.config.target_vlan_id
        total_nets = len(bound_networks)
        
        self._update_progress(15, f"Found {total_nets} bound networks. Extracting Gateway IPs...")

        for idx, net in enumerate(bound_networks):
            net_id = net['id']
            net_name = net.get('name', 'Unknown')
            
            if idx % 10 == 0:
                pct = 15 + int((idx / max(total_nets, 1)) * 70)
                self._update_progress(pct, f"Scanning VLANs for Network {idx}/{total_nets}...")

            gateway_ip = None
            try:
                vlans = self.meraki_provider.dashboard.appliance.getNetworkApplianceVlans(net_id)
                for vlan in vlans:
                    if vlan['id'] == target_vlan and vlan.get('applianceIp'):
                        gateway_ip = vlan['applianceIp']
                        break
            except Exception:
                pass 

            hardware_gateway_found = False
            net_devices = devices_by_network.get(net_id, [])
            
            for dev in net_devices:
                model = str(dev.get('model', '')).upper()
                raw_name = dev.get('name', 'Unknown_Name')
                serial = dev.get('serial', '')
                mac = dev.get('mac', '')
                
                # --- NEW: Sanitize device name to comply with Cisco ISE ERS strict requirements ---
                # 1. Replace all spaces and sequential space-dashes with a single underscore
                sanitized_name = re.sub(r'[\s\-]+', '_', raw_name.strip())
                # 2. Strip out anything that isn't alphanumeric, a period, a hyphen, or an underscore
                sanitized_name = re.sub(r'[^A-Za-z0-9\.\-_]', '', sanitized_name)
                # 3. Collapse double underscores if any were generated
                name = re.sub(r'_{2,}', '_', sanitized_name)
                
                desc = f"Managed by Meraki -> ISE NDG Audit | Network={net_name} | Model={model} | Serial={serial}"
                
                # Item 1 Fix: Map strictly to target designations supplied via UI parameters
                ndg_list = [
                    self.config.default_location_ndg,
                    "IPSEC#Is IPSEC Device#No",
                    f"Device Type#All Device Types#Meraki#{self.config.gateway_type}",
                    f"Designation#Designation#{self.config.gateway_designation}",
                    f"Deployment Stages#Deployment Stages#{self.config.deployment_stage}"  # <-- EXACT MATCH TO ISE EXPORT
                ]
                
                if 'MX' in model or 'Z' in model:
                    hardware_gateway_found = True
                    if self.config.hardware_filter == 2 and 'Z' not in model: continue 
                    if self.config.hardware_filter == 3 and 'MX' not in model: continue 
                    
                    if gateway_ip:
                        desired_devices.append(DesiredNetworkDevice(
                            name=name, ip_address=gateway_ip, mask=32, description=desc,
                            network_device_groups=ndg_list, meraki_serial=serial, meraki_model=model,
                            meraki_mac=mac, meraki_network_id=net_id, meraki_network_name=net_name
                        ))
                        
                elif 'MS' in model or 'MR' in model:
                    lan_ip = dev.get('lanIp')
                    if lan_ip:
                        desired_devices.append(DesiredNetworkDevice(
                            name=name, ip_address=lan_ip, mask=32, description=desc,
                            network_device_groups=ndg_list, meraki_serial=serial, meraki_model=model,
                            meraki_mac=mac, meraki_network_id=net_id, meraki_network_name=net_name
                        ))

            if gateway_ip and not hardware_gateway_found and self.config.hardware_filter == 1:
                unclaimed_ndgs = [
                    self.config.default_location_ndg,
                    "IPSEC#Is IPSEC Device#No",
                    f"Device Type#All Device Types#Meraki#{self.config.gateway_type}",
                    f"Designation#Designation#{self.config.gateway_designation}"
                ]
                desired_devices.append(DesiredNetworkDevice(
                    name=f"Net_Gateway_{gateway_ip}", ip_address=gateway_ip, mask=32,
                    description=f"Managed by Meraki -> ISE NDG Audit | Network={net_name} | Model=Unclaimed_Gateway | Serial=UNKNOWN",
                    network_device_groups=unclaimed_ndgs, meraki_serial="UNKNOWN", meraki_model="Unclaimed_Gateway",
                    meraki_mac="UNKNOWN", meraki_network_id=net_id, meraki_network_name=net_name
                ))

        self._update_progress(85, f"Successfully extracted {len(desired_devices)} target IP profiles. Scanning ISE...")
        
        unique_devices = {d.ip_address: d for d in desired_devices if d.ip_address}
        final_list = list(unique_devices.values())
        
        if hasattr(self.meraki_provider, 'cache'):
            self.meraki_provider.cache.set(cache_key, final_list)
        
        return final_list

    def _index_existing_devices(self, devices: List[Dict[str, Any]]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
        by_ip: Dict[str, Dict[str, Any]] = {}
        by_name: Dict[str, Dict[str, Any]] = {}

        for device in devices:
            name = safe_string(device.get("name"))
            if name:
                by_name[name.lower()] = device

            for ip_entry in device.get("NetworkDeviceIPList", []) or []:
                ip_address = safe_string(ip_entry.get("ipaddress"))
                if ip_address:
                    by_ip[ip_address] = device

        return by_ip, by_name

    def _handle_create_candidate(self, desired: DesiredNetworkDevice, dry_run: bool) -> AuditItem:
        payload = self._build_create_payload(desired)

        if dry_run:
            return AuditItem(
                action="WOULD_CREATE", name=desired.name, ip_address=desired.ip_address,
                reason="Device exists in Meraki but not in ISE.", meraki_serial=desired.meraki_serial,
                meraki_model=desired.meraki_model, dry_run=True, changed=False, diff={"desired": payload},
            )

        try:
            self.ise_client.create_network_device(payload)
            return AuditItem(
                action="CREATED", name=desired.name, ip_address=desired.ip_address,
                reason="Created missing ISE NetworkDevice from Meraki inventory.", meraki_serial=desired.meraki_serial,
                meraki_model=desired.meraki_model, dry_run=False, changed=True,
            )
        except Exception as exc:
            logger.exception("Failed creating ISE networkdevice name=%s ip=%s", desired.name, desired.ip_address)
            return AuditItem(
                action="CREATE_FAILED", name=desired.name, ip_address=desired.ip_address,
                reason="Create operation failed.", meraki_serial=desired.meraki_serial,
                meraki_model=desired.meraki_model, dry_run=False, changed=False, error=str(exc), diff={"desired": payload},
            )

    def _handle_existing_candidate(self, desired: DesiredNetworkDevice, existing: Dict[str, Any], dry_run: bool, apply_updates: bool) -> AuditItem:
        existing_groups = existing.get("NetworkDeviceGroupList", []) or []
        existing_locations = [g for g in existing_groups if g.startswith("Location#")]
        
        filtered_ndgs = [g for g in desired.network_device_groups if not g.startswith("Location#")]
        if existing_locations:
            filtered_ndgs.extend(existing_locations)
        else:
            filtered_ndgs.append(self.config.default_location_ndg)
            
        desired.network_device_groups = filtered_ndgs
        
        diff = self._calculate_diff(desired, existing)
        ise_id = safe_string(existing.get("id"))

        if not diff:
            return AuditItem(
                action="UNCHANGED", name=desired.name, ip_address=desired.ip_address,
                reason="ISE NetworkDevice already matches desired Meraki-derived state.", ise_id=ise_id,
                meraki_serial=desired.meraki_serial, meraki_model=desired.meraki_model, dry_run=dry_run, changed=False,
            )

        if dry_run or not apply_updates:
            return AuditItem(
                action="WOULD_UPDATE", name=desired.name, ip_address=desired.ip_address,
                reason="ISE NetworkDevice differs from desired Meraki-derived state.", ise_id=ise_id,
                meraki_serial=desired.meraki_serial, meraki_model=desired.meraki_model, dry_run=dry_run, changed=False, diff=diff,
            )

        try:
            payload = self._build_update_payload(desired, existing)
            self.ise_client.update_network_device(ise_id, payload)
            return AuditItem(
                action="UPDATED", name=desired.name, ip_address=desired.ip_address,
                reason="Updated ISE NetworkDevice to match Meraki-derived state.", ise_id=ise_id,
                meraki_serial=desired.meraki_serial, meraki_model=desired.meraki_model, dry_run=False, changed=True, diff=diff,
            )
        except Exception as exc:
            logger.exception("Failed updating ISE networkdevice id=%s name=%s", ise_id, desired.name)
            return AuditItem(
                action="UPDATE_FAILED", name=desired.name, ip_address=desired.ip_address,
                reason="Update operation failed.", ise_id=ise_id, meraki_serial=desired.meraki_serial,
                meraki_model=desired.meraki_model, dry_run=False, changed=False, error=str(exc), diff=diff,
            )

    def _calculate_diff(self, desired: DesiredNetworkDevice, existing: Dict[str, Any]) -> Dict[str, Any]:
        diff: Dict[str, Any] = {}

        existing_name = safe_string(existing.get("name"))
        if existing_name != desired.name:
            diff["name"] = {"current": existing_name, "desired": desired.name}

        existing_description = safe_string(existing.get("description"))
        if existing_description != desired.description:
            diff["description"] = {"current": existing_description, "desired": desired.description}

        existing_ips = {
            safe_string(entry.get("ipaddress")): normalize_ip_mask(entry.get("mask"))
            for entry in existing.get("NetworkDeviceIPList", []) or []
            if safe_string(entry.get("ipaddress"))
        }
        desired_ips = {desired.ip_address: desired.mask}
        
        if existing_ips != desired_ips:
            diff["ip_address"] = {"current": existing_ips, "desired": desired_ips}

        existing_groups = set(existing.get("NetworkDeviceGroupList", []) or [])
        desired_groups = set(desired.network_device_groups)
        if existing_groups != desired_groups:
            diff["network_device_groups"] = {"current": sorted(existing_groups), "desired": sorted(desired_groups)}

        if self.config.update_radius_secret and self.config.radius_shared_secret:
            diff["radius_shared_secret"] = {"current": "<redacted>", "desired": "<redacted>", "reason": "ISE_NDG_UPDATE_RADIUS_SECRET is enabled."}

        return diff

    def _build_create_payload(self, desired: DesiredNetworkDevice) -> Dict[str, Any]:
        network_device: Dict[str, Any] = {
            "name": desired.name,
            "description": desired.description,
            "NetworkDeviceIPList": [{"ipaddress": desired.ip_address, "mask": desired.mask}],
            "NetworkDeviceGroupList": desired.network_device_groups,
            "authenticationSettings": {"networkProtocol": "RADIUS"},
        }
        if self.config.radius_shared_secret:
            network_device["authenticationSettings"]["radiusSharedSecret"] = self.config.radius_shared_secret
        return {"NetworkDevice": network_device}

    def _build_update_payload(self, desired: DesiredNetworkDevice, existing: Dict[str, Any]) -> Dict[str, Any]:
        network_device = dict(existing)
        scrub_previous_secret_fields(network_device)

        network_device["name"] = desired.name
        network_device["description"] = desired.description
        network_device["NetworkDeviceGroupList"] = desired.network_device_groups
        
        existing_ips = network_device.get("NetworkDeviceIPList", [])
        if existing_ips and existing_ips[0].get("ipaddress") == desired.ip_address:
            existing_ips[0]["mask"] = desired.mask
            network_device["NetworkDeviceIPList"] = existing_ips
        else:
            network_device["NetworkDeviceIPList"] = [{"ipaddress": desired.ip_address, "mask": desired.mask}]

        # CRITICAL API FIX: 
        # Cisco ISE ERS requires the 'radiusSharedSecret' to be included in every PUT request 
        # if RADIUS is configured, but the GET request intentionally hides it.
        if self.config.radius_shared_secret:
            auth_settings = network_device.get("authenticationSettings", {})
            auth_settings["networkProtocol"] = "RADIUS"
            auth_settings["radiusSharedSecret"] = self.config.radius_shared_secret
            network_device["authenticationSettings"] = auth_settings

        return {"NetworkDevice": network_device}

    @staticmethod
    def _build_summary(items: List[AuditItem], desired_count: int, existing_count: int) -> Dict[str, int]:
        summary = {
            "meraki_desired_devices": desired_count, "ise_existing_devices": existing_count, "would_create": 0,
            "created": 0, "would_update": 0, "updated": 0, "unchanged": 0, "create_failed": 0, "update_failed": 0, "errors": 0,
        }
        action_map = {
            "WOULD_CREATE": "would_create", "CREATED": "created", "WOULD_UPDATE": "would_update",
            "UPDATED": "updated", "UNCHANGED": "unchanged", "CREATE_FAILED": "create_failed", "UPDATE_FAILED": "update_failed",
        }
        for item in items:
            key = action_map.get(item.action)
            if key: summary[key] += 1
            if item.error: summary["errors"] += 1
        return summary

    def _write_reports(self, result: AuditResult) -> None:
        report_dir = Path(self.config.report_dir)
        report_dir.mkdir(parents=True, exist_ok=True)
        stamp = timestamp_for_filename()
        
        json_path = report_dir / f"ise_ndg_audit_{stamp}.json"
        csv_path = report_dir / f"ise_ndg_audit_{stamp}.csv"
        anomaly_json_path = report_dir / f"ise_ndg_anomalies_{stamp}.json"
        anomaly_csv_path = report_dir / f"ise_ndg_anomalies_{stamp}.csv"

        with json_path.open("w", encoding="utf-8") as handle:
            json.dump(result.to_dict(), handle, indent=2)

        csv_fields = ["action", "name", "ip_address", "reason", "ise_id", "meraki_serial", "meraki_model", "dry_run", "changed", "error"]
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=csv_fields)
            writer.writeheader()
            for item in result.items:
                writer.writerow({f: getattr(item, f, "") if hasattr(item, f) else item.to_dict().get(f, "") for f in csv_fields})

        anomaly_items = [item for item in result.items if item.action in ["WOULD_CREATE", "WOULD_UPDATE", "CREATE_FAILED", "UPDATE_FAILED"]]

        anomaly_dict = result.to_dict()
        anomaly_dict["items"] = [item.to_dict() for item in anomaly_items]
        with anomaly_json_path.open("w", encoding="utf-8") as handle:
            json.dump(anomaly_dict, handle, indent=2)

        with anomaly_csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=csv_fields)
            writer.writeheader()
            for item in anomaly_items:
                writer.writerow({f: getattr(item, f, "") if hasattr(item, f) else item.to_dict().get(f, "") for f in csv_fields})

        result.json_report_path = str(json_path)
        result.csv_report_path = str(csv_path)
        result.anomaly_json_path = str(anomaly_json_path)
        result.anomaly_csv_path = str(anomaly_csv_path)

def run_ndg_audit_from_env(meraki_provider: Any, dry_run: bool = True, apply_updates: bool = False) -> Dict[str, Any]:
    service = NDGAuditService(meraki_provider=meraki_provider, config=NDGAuditConfig.from_env())
    return service.run_audit(dry_run=dry_run, apply_updates=apply_updates).to_dict()