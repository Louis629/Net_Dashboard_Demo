"""
providers/demo_providers.py

Self-contained Demo Mode provider implementations for Net_Dashboard.
Allows running locally without live Cisco ISE, Catalyst Center, Meraki,
Active Directory (LDAPS), CUCM, UCCX, or Unity Connection (CUC) appliances.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import struct
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlsplit

from providers.ldap_provider import IdentityCollisionResult, LDAPAccountDetails
from providers.meraki_provider import MerakiClientContext, MerakiProvider, TTLCache

logger = logging.getLogger("Net_Dashboard.DemoProviders")


def _normalize_mac_permissive(mac_address: str) -> str:
    candidate = (mac_address or "").strip().replace("-", ":").upper()
    if re.fullmatch(r"[0-9A-F]{12}", candidate):
        return ":".join(candidate[i : i + 2] for i in range(0, 12, 2))
    if re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", candidate):
        return candidate
    return (mac_address or "").strip().upper()


def _build_demo_wav_bytes(duration_ms: int = 50) -> bytes:
    """
    Generates a valid WxCC-compliant RIFF/WAVE PCM audio stream:
    - Format Tag: 1 (PCM)
    - Channels: 1 (Mono)
    - Sample Rate: 8000 Hz
    - Bits Per Sample: 16
    """
    sample_rate = 8000
    channels = 1
    bits_per_sample = 16
    block_align = channels * (bits_per_sample // 8)
    avg_bytes_sec = sample_rate * block_align
    num_samples = max(1, int(sample_rate * duration_ms / 1000))
    data_size = num_samples * block_align
    riff_size = 36 + data_size

    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        riff_size,
        b"WAVE",
        b"fmt ",
        16,
        1,
        channels,
        sample_rate,
        avg_bytes_sec,
        block_align,
        bits_per_sample,
        b"data",
        data_size,
    )
    return header + (b"\x00" * data_size)


# =============================================================================
# Shared Cohesive Demo Dataset (ISE / DNAC / Meraki / LDAP)
# =============================================================================

_DEMO_ENDPOINT_SCENARIOS: List[Dict[str, Any]] = [
    {
        "mac": "AA:BB:CC:11:22:33",
        "ip": "10.10.20.45",
        "username": "jdoe@corp.internal",
        "sam": "jdoe",
        "hostname": "WKSTN-DAL-0042",
        "nas_name": "SW-DAL-ACC-01",
        "nas_ip": "10.10.1.11",
        "switch_port": "GigabitEthernet1/0/14",
        "vlan": "20",
        "endpoint_profile": "Microsoft-Workstation",
        "auth_method": "DOT1X",
        "status": "ACTIVE_SESSION_FOUND",
        "authz_profile": "Permit_Corp_Workstation",
        "failure_reason": "N/A",
        "acs_session_id": "demo-ise/501100/100001",
        "client_type": "WIRED",
        "health_score": 9,
        "speed": "1000",
        "duplex": "Full",
        "manufacturer": "Dell Inc.",
        "os": "Windows 11 Enterprise",
        "network_id": "N_DEMO_DAL_001",
        "network_name": "Dallas HQ - Corporate",
        "device_name": "SW-DAL-ACC-01",
        "device_serial": "Q2XX-DEMO-SW01",
        "device_mac": "00:18:0A:AA:10:01",
        "meraki_found": False,
    },
    {
        "mac": "AA:BB:CC:44:55:66",
        "ip": "10.10.20.88",
        "username": "msmith@corp.internal",
        "sam": "msmith",
        "hostname": "WKSTN-HOU-0109",
        "nas_name": "SW-HOU-ACC-02",
        "nas_ip": "10.10.2.12",
        "switch_port": "GigabitEthernet2/0/7",
        "vlan": "999",
        "endpoint_profile": "Microsoft-Workstation",
        "auth_method": "DOT1X",
        "status": "NO_ACTIVE_SESSION",
        "authz_profile": "Default_Deny_Access",
        "failure_reason": (
            "24408 User authentication against Active Directory failed since user "
            "has entered the wrong password or multiple matching accounts"
        ),
        "acs_session_id": "demo-ise/501234/987654",
        "client_type": "WIRED",
        "health_score": 4,
        "speed": "1000",
        "duplex": "Full",
        "manufacturer": "Lenovo",
        "os": "Windows 11 Enterprise",
        "network_id": "N_DEMO_AUS_002",
        "network_name": "Austin Clinic - North",
        "device_name": "SW-HOU-ACC-02",
        "device_serial": "Q2XX-DEMO-SW02",
        "device_mac": "00:18:0A:BB:20:02",
        "meraki_found": True,
    },
    {
        "mac": "DE:AD:BE:EF:00:01",
        "ip": "10.20.30.105",
        "username": "asmith@corp.internal",
        "sam": "asmith",
        "hostname": "LT-AUS-0215",
        "nas_name": "AP-AUS-FL2-04",
        "nas_ip": "10.20.1.24",
        "switch_port": "Corp-Secure-WiFi",
        "vlan": "30",
        "endpoint_profile": "Apple-MacBook",
        "auth_method": "DOT1X",
        "status": "ACTIVE_SESSION_FOUND",
        "authz_profile": "Permit_Corp_Wireless",
        "failure_reason": "N/A",
        "acs_session_id": "demo-ise/501300/200002",
        "client_type": "WIRELESS",
        "health_score": 8,
        "ssid": "Corp-Secure-WiFi",
        "rssi": -56,
        "snr": 32,
        "frequency": "5 GHz",
        "channel": "149",
        "manufacturer": "Apple, Inc.",
        "os": "macOS 15.1",
        "network_id": "N_DEMO_AUS_002",
        "network_name": "Austin Clinic - North",
        "device_name": "AP-AUS-FL2-04",
        "device_serial": "Q2MD-DEMO-AP04",
        "device_mac": "00:18:0A:CC:30:04",
        "meraki_found": True,
    },
    {
        "mac": "00:50:56:AA:BB:CC",
        "ip": "10.200.10.55",
        "username": "remote.user@corp.internal",
        "sam": "remote.user",
        "hostname": "LT-REM-0088",
        "nas_name": "ASA-VPN-GW01",
        "nas_ip": "10.200.1.5",
        "switch_port": "Virtual Adapter",
        "vlan": "N/A",
        "endpoint_profile": "Cisco-AnyConnect",
        "auth_method": "VPN",
        "status": "ACTIVE_SESSION_FOUND",
        "authz_profile": "Permit_VPN_Employee",
        "failure_reason": "N/A",
        "acs_session_id": "demo-ise/501400/300003",
        "client_type": "VPN",
        "health_score": "N/A",
        "manufacturer": "VMware, Inc.",
        "os": "Windows 11 Pro",
        "network_id": "N/A",
        "network_name": "N/A",
        "device_name": "ASA-VPN-GW01",
        "device_serial": "N/A",
        "device_mac": "N/A",
        "meraki_found": False,
    },
]


class _DemoHTTPResponse:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


class _DemoISESession:
    """In-memory MnT session stub supporting EndpointDiagnosticService._resolve_to_mac."""

    def __init__(self, scenarios: List[Dict[str, Any]]) -> None:
        self._scenarios = scenarios

    def get(self, url: str, headers: Optional[Dict[str, str]] = None, **kwargs: Any) -> _DemoHTTPResponse:
        path = unquote(urlsplit(url).path)
        host_prefix = "/admin/API/mnt/Session/UserName/host/"
        user_prefix = "/admin/API/mnt/Session/UserName/"
        ip_prefix = "/admin/API/mnt/Session/IPAddress/"
        mac_prefix = "/admin/API/mnt/Session/MACAddress/"

        matched: Optional[Dict[str, Any]] = None
        if host_prefix in path:
            raw_host = path.split(host_prefix, 1)[1].strip("/").split(".", 1)[0].lower()
            matched = next(
                (s for s in self._scenarios if s["hostname"].lower() == raw_host),
                None,
            )
        elif user_prefix in path:
            raw_user = path.split(user_prefix, 1)[1].strip("/").lower()
            matched = next(
                (
                    s
                    for s in self._scenarios
                    if s["username"].lower() == raw_user
                    or s["sam"].lower() == raw_user
                    or s["hostname"].lower() == raw_user
                ),
                None,
            )
        elif ip_prefix in path:
            raw_ip = path.split(ip_prefix, 1)[1].strip("/")
            matched = next((s for s in self._scenarios if s["ip"] == raw_ip), None)
        elif mac_prefix in path:
            raw_mac = path.split(mac_prefix, 1)[1].strip("/").upper()
            matched = next(
                (
                    s
                    for s in self._scenarios
                    if s["mac"].replace(":", "") == raw_mac and s["status"] == "ACTIVE_SESSION_FOUND"
                ),
                None,
            )

        if matched:
            xml_body = (
                "<sessionParameters>"
                f"<calling_station_id>{matched['mac']}</calling_station_id>"
                f"<user_name>{matched['username']}</user_name>"
                f"<nas_ip_address>{matched['nas_ip']}</nas_ip_address>"
                f"<nas_port_id>{matched['switch_port']}</nas_port_id>"
                f"<vlan>{matched['vlan']}</vlan>"
                f"<authentication_method>{matched['auth_method']}</authentication_method>"
                "</sessionParameters>"
            )
            return _DemoHTTPResponse(200, xml_body)

        return _DemoHTTPResponse(200, "<error>Session data is not available</error>")


class _DemoISEAPIStub:
    def __init__(self, session: _DemoISESession) -> None:
        self.session = session


class DemoISEProvider:
    """Self-contained demo replacement for ISEProvider with zero network calls."""

    def __init__(
        self,
        host: str = "demo-ise.local",
        username: str = "demo_ise",
        token: str = "demo_token",
        version: str = "3.3_patch_1",
        verify_ssl: Optional[bool] = False,
    ) -> None:
        self.host = host or "demo-ise.local"
        self.username = username
        self.version = version
        self.verify_ssl = bool(verify_ssl) if verify_ssl is not None else False
        self.demo_mode = True
        self._scenarios = copy.deepcopy(_DEMO_ENDPOINT_SCENARIOS)
        self._scenario_by_mac: Dict[str, Dict[str, Any]] = {
            s["mac"]: s for s in self._scenarios
        }
        self._endpoints: Dict[str, Dict[str, Any]] = {
            s["mac"]: {
                "id": f"demo-ep-{idx + 1:03d}",
                "mac": s["mac"],
                "group": "Corp_Workstations",
                "description": f"Demo endpoint {s['hostname']}",
            }
            for idx, s in enumerate(self._scenarios)
        }
        self._ep_counter = len(self._endpoints) + 1
        self.api = _DemoISEAPIStub(_DemoISESession(self._scenarios))

    def trigger_session_coa_terminate(self, mac_address: str) -> bool:
        logger.debug("DemoISEProvider CoA terminate simulated for MAC %s", mac_address)
        return True

    def provision_imaging_bypass(self, mac_address: str, operator_teams_id: str) -> Dict[str, Any]:
        staging_group = "Staging_Imaging_Bypass"
        clean_mac = _normalize_mac_permissive(mac_address)
        existing = self._endpoints.get(clean_mac)
        if existing:
            existing["group"] = staging_group
            existing["description"] = f"Authorized via Demo Automation by {operator_teams_id}"
        else:
            self._endpoints[clean_mac] = {
                "id": f"demo-ep-{self._ep_counter:03d}",
                "mac": clean_mac,
                "group": staging_group,
                "description": f"Staging initialized via Demo Automation by {operator_teams_id}",
            }
            self._ep_counter += 1

        coa_triggered = self.trigger_session_coa_terminate(clean_mac)
        return {
            "status": "SUCCESS",
            "mac": clean_mac,
            "group": staging_group,
            "coa_applied": coa_triggered,
        }

    def remove_endpoint(self, mac_address: str) -> Dict[str, Any]:
        clean_mac = _normalize_mac_permissive(mac_address)
        removed = self._endpoints.pop(clean_mac, None)
        if not removed:
            return {
                "status": "NOT_FOUND",
                "mac": clean_mac,
                "deleted": False,
                "coa_applied": False,
                "message": f"Endpoint with MAC '{clean_mac}' not found in demo ISE inventory.",
            }

        coa_triggered = self.trigger_session_coa_terminate(clean_mac)
        return {
            "status": "SUCCESS",
            "mac": clean_mac,
            "endpoint_id": removed["id"],
            "deleted": True,
            "coa_applied": coa_triggered,
            "message": f"Successfully deleted endpoint '{clean_mac}' from demo ISE.",
        }

    def get_auth_history(
        self,
        mac_address: str,
        hours_back: int = 24,
        record_limit: int = 3,
    ) -> List[Dict[str, Any]]:
        clean_mac = _normalize_mac_permissive(mac_address)
        scenario = self._scenario_by_mac.get(clean_mac)
        if scenario:
            passed = scenario["status"] == "ACTIVE_SESSION_FOUND"
            entry = {
                "timestamp": "2026-11-20 10:42:15.120",
                "status": "Passed" if passed else "Failed/Rejected",
                "username": scenario["username"],
                "nas_name": scenario["nas_name"],
                "nas_ip": scenario["nas_ip"],
                "switch_port": scenario["switch_port"],
                "authz_profile": scenario["authz_profile"],
                "failure_reason": scenario["failure_reason"],
                "endpoint_profile": scenario["endpoint_profile"],
                "auth_method": scenario["auth_method"],
                "vlan": scenario["vlan"],
                "acs_session_id": scenario["acs_session_id"],
            }
            history = [entry]
            if not passed:
                history.append(
                    {
                        "timestamp": "2026-11-20 10:41:02.045",
                        "status": "Failed/Rejected",
                        "username": f"host/{scenario['hostname']}.corp.internal",
                        "nas_name": scenario["nas_name"],
                        "nas_ip": scenario["nas_ip"],
                        "switch_port": scenario["switch_port"],
                        "authz_profile": "Default_Deny_Access",
                        "failure_reason": "5440 Endpoint abandoned EAP session and started new",
                        "endpoint_profile": scenario["endpoint_profile"],
                        "auth_method": scenario["auth_method"],
                        "vlan": scenario["vlan"],
                        "acs_session_id": "demo-ise/501234/987650",
                    }
                )
            return history[: max(1, record_limit)]

        fallback = [
            {
                "timestamp": "2026-11-20 10:35:00.000",
                "status": "Passed",
                "username": "demo.user@corp.internal",
                "nas_name": "SW-DAL-ACC-01",
                "nas_ip": "10.10.1.11",
                "switch_port": "GigabitEthernet1/0/22",
                "authz_profile": "Permit_Corp_Workstation",
                "failure_reason": "N/A",
                "endpoint_profile": "Microsoft-Workstation",
                "auth_method": "DOT1X",
                "vlan": "20",
                "acs_session_id": "demo-ise/501999/000001",
            }
        ]
        return fallback[: max(1, record_limit)]

    def get_deep_auth_diagnostics(self, acs_session_id: str) -> Optional[str]:
        if acs_session_id == "demo-ise/501234/987654":
            return (
                "Identity resolution failed due to multiple matching AD accounts. "
                "Conflicting accounts detected: corp\\msmith, corp\\msmith_legacy."
            )
        return None

    def diagnose_endpoint_session(self, mac_address: str) -> Dict[str, Any]:
        clean_mac = _normalize_mac_permissive(mac_address)
        history = self.get_auth_history(clean_mac)
        last_auth = history[0] if history else {}
        scenario = self._scenario_by_mac.get(clean_mac)

        payload: Dict[str, Any] = {
            "mac_address": clean_mac,
            "status": scenario["status"] if scenario else "ACTIVE_SESSION_FOUND",
            "endpoint_profile": last_auth.get("endpoint_profile", "Microsoft-Workstation"),
            "username": last_auth.get("username", "demo.user@corp.internal"),
            "nas_name": last_auth.get("nas_name", "SW-DAL-ACC-01"),
            "nas_ip": last_auth.get("nas_ip", "10.10.1.11"),
            "switch_port": last_auth.get("switch_port", "GigabitEthernet1/0/22"),
            "vlan": last_auth.get("vlan", "20"),
            "auth_method": last_auth.get("auth_method", "DOT1X"),
            "triage_hint": "",
            "recent_history": history,
        }

        if payload["status"] == "NO_ACTIVE_SESSION":
            deep_cause = self.get_deep_auth_diagnostics(last_auth.get("acs_session_id", ""))
            if deep_cause:
                payload["triage_hint"] = (
                    f"Device actively hitting Default Deny on {payload['nas_name']}. {deep_cause}"
                )
            else:
                payload["triage_hint"] = (
                    f"Device actively hitting Default Deny on {payload['nas_name']}. "
                    f"Failure Reason: {last_auth.get('failure_reason', 'Unknown')}."
                )
        else:
            payload["triage_hint"] = (
                f"Session is currently live and authenticated on {payload['nas_name']}."
            )

        return payload

    def get_meraki_ndg_options(self) -> Dict[str, List[str]]:
        return {
            "device_types": ["APs", "Security Appliance", "Switch", "Z3", "Z4"],
            "roles": ["Branch Clinic", "IOP", "PSC", "Regional Office", "Teleworker"],
        }


# =============================================================================
# Demo Cisco Catalyst Center (DNAC) Provider
# =============================================================================


class DemoDNACProvider:
    """Self-contained demo replacement for CatalystCenterProvider."""

    def __init__(
        self,
        host: str = "demo-dnac.local",
        username: str = "demo_dnac",
        password: str = "demo_pass",
        verify_ssl: bool = False,
    ) -> None:
        self.host = host or "demo-dnac.local"
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.base_url = f"https://{self.host}"
        self.demo_mode = True
        self._scenario_by_mac = {s["mac"]: s for s in _DEMO_ENDPOINT_SCENARIOS}

    def get_client_topology_by_mac(self, mac_address: str) -> Dict[str, Any]:
        clean_mac = _normalize_mac_permissive(mac_address)
        scenario = self._scenario_by_mac.get(clean_mac)

        if scenario:
            if scenario["client_type"] == "VPN":
                return {"dnac_found": False}

            if scenario["client_type"] == "WIRELESS":
                return {
                    "dnac_found": True,
                    "client_type": "WIRELESS",
                    "nas_name": scenario["nas_name"],
                    "nas_ip": scenario["nas_ip"],
                    "switch_port": "N/A",
                    "vlan": scenario["vlan"],
                    "status": "Connected",
                    "health_score": scenario["health_score"],
                    "ip_address": scenario["ip"],
                    "ssid": scenario.get("ssid", "Corp-Secure-WiFi"),
                    "rssi": scenario.get("rssi", -56),
                    "snr": scenario.get("snr", 32),
                    "frequency": scenario.get("frequency", "5 GHz"),
                    "channel": scenario.get("channel", "149"),
                    "speed": "866",
                    "duplex": "N/A",
                }

            return {
                "dnac_found": True,
                "client_type": "WIRED",
                "nas_name": scenario["nas_name"],
                "nas_ip": scenario["nas_ip"],
                "switch_port": scenario["switch_port"],
                "vlan": scenario["vlan"],
                "status": "Connected" if scenario["status"] == "ACTIVE_SESSION_FOUND" else "Auth Failed",
                "health_score": scenario["health_score"],
                "ip_address": scenario["ip"],
                "ssid": "N/A",
                "rssi": "N/A",
                "snr": "N/A",
                "frequency": "N/A",
                "channel": "N/A",
                "speed": scenario.get("speed", "1000"),
                "duplex": scenario.get("duplex", "Full"),
            }

        return {
            "dnac_found": True,
            "client_type": "WIRED",
            "nas_name": "SW-DAL-ACC-01",
            "nas_ip": "10.10.1.11",
            "switch_port": "GigabitEthernet1/0/22",
            "vlan": "20",
            "status": "Connected",
            "health_score": 8,
            "ip_address": "10.10.20.120",
            "ssid": "N/A",
            "rssi": "N/A",
            "snr": "N/A",
            "frequency": "N/A",
            "channel": "N/A",
            "speed": "1000",
            "duplex": "Full",
        }


# =============================================================================
# Demo Cisco Meraki Provider (with in-memory Dashboard API stub)
# =============================================================================


class _DemoMerakiOrganizationsAPI:
    def __init__(self, provider: "DemoMerakiProvider") -> None:
        self._p = provider

    def getOrganizationNetworks(self, organizationId: str, **kwargs: Any) -> List[Dict[str, Any]]:
        return copy.deepcopy(self._p._networks)

    def getOrganizationDevices(self, organizationId: str, **kwargs: Any) -> List[Dict[str, Any]]:
        return copy.deepcopy(self._p._devices)

    def getOrganizationConfigTemplates(self, organizationId: str, **kwargs: Any) -> List[Dict[str, Any]]:
        return copy.deepcopy(self._p._templates)

    def createOrganizationNetwork(
        self,
        organizationId: str,
        name: str,
        productTypes: Optional[List[str]] = None,
        timeZone: str = "America/Chicago",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        new_id = f"N_DEMO_NEW_{len(self._p._networks) + 1:03d}"
        net = {
            "id": new_id,
            "organizationId": organizationId,
            "name": name,
            "productTypes": productTypes or ["appliance"],
            "timeZone": timeZone,
            "address": kwargs.get("address", ""),
            "configTemplateId": "",
        }
        self._p._networks.append(net)
        self._p._vlans_by_network.setdefault(
            new_id,
            [
                {"id": 1, "name": "Data", "subnet": "10.99.1.0/24", "applianceIp": "10.99.1.1"},
                {"id": 21, "name": "Voice", "subnet": "10.99.21.0/24", "applianceIp": "10.99.21.1"},
            ],
        )
        return copy.deepcopy(net)


class _DemoMerakiNetworksAPI:
    def __init__(self, provider: "DemoMerakiProvider") -> None:
        self._p = provider

    def getNetwork(self, networkId: str) -> Dict[str, Any]:
        for net in self._p._networks:
            if net["id"] == networkId:
                return copy.deepcopy(net)
        return {"id": networkId, "name": networkId, "timeZone": "America/Chicago", "address": ""}

    def getNetworkDevices(self, networkId: str) -> List[Dict[str, Any]]:
        return [copy.deepcopy(d) for d in self._p._devices if d.get("networkId") == networkId]

    def claimNetworkDevices(self, networkId: str, serials: List[str]) -> Dict[str, Any]:
        net = self.getNetwork(networkId)
        for serial in serials:
            existing = next((d for d in self._p._devices if d.get("serial") == serial), None)
            if existing:
                existing["networkId"] = networkId
                existing["networkName"] = net.get("name", networkId)
            else:
                self._p._devices.append(
                    {
                        "serial": serial,
                        "mac": "00:18:0A:FE:ED:01",
                        "name": f"{net.get('name', 'Demo')}_MX68_{serial[-4:]}",
                        "model": "MX68",
                        "networkId": networkId,
                        "networkName": net.get("name", networkId),
                        "address": net.get("address", "100 Demo Plaza, Dallas, TX 75201"),
                    }
                )
        return {"serials": serials}

    def removeNetworkDevices(self, networkId: str, serial: str) -> None:
        self._p._devices = [
            d for d in self._p._devices if not (d.get("networkId") == networkId and d.get("serial") == serial)
        ]

    def deleteNetwork(self, networkId: str) -> None:
        self._p._networks = [n for n in self._p._networks if n.get("id") != networkId]

    def bindNetwork(self, networkId: str, configTemplateId: str, **kwargs: Any) -> Dict[str, Any]:
        for net in self._p._networks:
            if net["id"] == networkId:
                net["configTemplateId"] = configTemplateId
                return copy.deepcopy(net)
        return {"id": networkId, "configTemplateId": configTemplateId}

    def getNetworkClients(self, networkId: str, **kwargs: Any) -> List[Dict[str, Any]]:
        return [copy.deepcopy(c) for c in self._p._clients if c.get("networkId") == networkId]


class _DemoMerakiApplianceAPI:
    def __init__(self, provider: "DemoMerakiProvider") -> None:
        self._p = provider

    def getNetworkApplianceVlans(self, networkId: str) -> List[Dict[str, Any]]:
        return copy.deepcopy(self._p._vlans_by_network.get(networkId, []))

    def getNetworkApplianceSingleLan(self, networkId: str) -> Dict[str, Any]:
        return copy.deepcopy(self._p._single_lan_by_network.get(networkId, {}))

    def swapNetworkAppliance(self, networkId: str, oldSerial: str, newSerial: str) -> Dict[str, Any]:
        return {
            "status": "swapped",
            "networkId": networkId,
            "oldSerial": oldSerial,
            "newSerial": newSerial,
        }


class _DemoMerakiDevicesAPI:
    def __init__(self, provider: "DemoMerakiProvider") -> None:
        self._p = provider

    def getDevice(self, serial: str) -> Dict[str, Any]:
        for d in self._p._devices:
            if d.get("serial") == serial:
                return copy.deepcopy(d)
        return {"serial": serial, "model": "MX68", "name": f"Demo_MX68_{serial[-4:]}"}

    def updateDevice(self, serial: str, **kwargs: Any) -> Dict[str, Any]:
        for d in self._p._devices:
            if d.get("serial") == serial:
                d.update(kwargs)
                return copy.deepcopy(d)
        return {"serial": serial, **kwargs}


class _DemoMerakiDashboard:
    def __init__(self, provider: "DemoMerakiProvider") -> None:
        self.organizations = _DemoMerakiOrganizationsAPI(provider)
        self.networks = _DemoMerakiNetworksAPI(provider)
        self.appliance = _DemoMerakiApplianceAPI(provider)
        self.devices = _DemoMerakiDevicesAPI(provider)


class DemoMerakiProvider:
    """Self-contained demo replacement for MerakiProvider."""

    def __init__(
        self,
        organization_id: str = "demo-org-001",
        lookback_days: int = 31,
        cache_ttl_seconds: int = 1800,
    ) -> None:
        self.organization_id = (organization_id or "demo-org-001").strip()
        self.lookback_days = lookback_days
        self.timespan_seconds = lookback_days * 24 * 60 * 60
        self.cache = TTLCache(ttl_seconds=cache_ttl_seconds)
        self.enrich_device_names = True
        self.demo_mode = True

        self._templates: List[Dict[str, Any]] = [
            {"id": "T_DEMO_MX_CORP", "name": "Corp-MX-Standard-Template", "productTypes": ["appliance", "switch", "wireless"]},
            {"id": "T_DEMO_MX_BRANCH", "name": "Branch-Clinic-MX68-Template", "productTypes": ["appliance", "switch", "wireless"]},
            {"id": "T_DEMO_Z4_TELEWORKER", "name": "Teleworker-Z4-Template", "productTypes": ["appliance", "wireless"]},
        ]

        self._networks: List[Dict[str, Any]] = [
            {
                "id": "N_DEMO_DAL_001",
                "organizationId": self.organization_id,
                "name": "Dallas HQ - Corporate",
                "timeZone": "America/Chicago",
                "address": "1700 Pacific Ave, Dallas, TX 75201, United States",
                "configTemplateId": "T_DEMO_MX_CORP",
                "productTypes": ["appliance", "switch", "wireless"],
            },
            {
                "id": "N_DEMO_AUS_002",
                "organizationId": self.organization_id,
                "name": "Austin Clinic - North",
                "timeZone": "America/Chicago",
                "address": "3801 N Lamar Blvd, Austin, TX 78756, United States",
                "configTemplateId": "T_DEMO_MX_BRANCH",
                "productTypes": ["appliance", "switch", "wireless"],
            },
            {
                "id": "N_DEMO_HNL_003",
                "organizationId": self.organization_id,
                "name": "Honolulu Branch - Pacific",
                "timeZone": "Pacific/Honolulu",
                "address": "2230 Liliha St, Honolulu, HI 96817, United States",
                "configTemplateId": "T_DEMO_Z4_TELEWORKER",
                "productTypes": ["appliance", "wireless"],
            },
        ]

        self._devices: List[Dict[str, Any]] = [
            {
                "serial": "Q2XX-DEMO-MX01",
                "mac": "00:18:0A:11:00:01",
                "name": "DAL-HQ-MX250-01",
                "model": "MX250",
                "networkId": "N_DEMO_DAL_001",
                "networkName": "Dallas HQ - Corporate",
                "address": "1700 Pacific Ave, Dallas, TX 75201, United States",
                "lanIp": "10.10.21.1",
            },
            {
                "serial": "Q2XX-DEMO-SW01",
                "mac": "00:18:0A:AA:10:01",
                "name": "SW-DAL-ACC-01",
                "model": "MS250-48FP",
                "networkId": "N_DEMO_DAL_001",
                "networkName": "Dallas HQ - Corporate",
                "address": "1700 Pacific Ave, Dallas, TX 75201, United States",
                "lanIp": "10.10.1.11",
            },
            {
                "serial": "Q2XX-DEMO-MX02",
                "mac": "00:18:0A:22:00:02",
                "name": "AUS-CLN-MX68-01",
                "model": "MX68",
                "networkId": "N_DEMO_AUS_002",
                "networkName": "Austin Clinic - North",
                "address": "3801 N Lamar Blvd, Austin, TX 78756, United States",
                "lanIp": "10.20.21.1",
            },
            {
                "serial": "Q2XX-DEMO-SW02",
                "mac": "00:18:0A:BB:20:02",
                "name": "SW-HOU-ACC-02",
                "model": "MS225-48LP",
                "networkId": "N_DEMO_AUS_002",
                "networkName": "Austin Clinic - North",
                "address": "3801 N Lamar Blvd, Austin, TX 78756, United States",
                "lanIp": "10.10.2.12",
            },
            {
                "serial": "Q2MD-DEMO-AP04",
                "mac": "00:18:0A:CC:30:04",
                "name": "AP-AUS-FL2-04",
                "model": "MR46",
                "networkId": "N_DEMO_AUS_002",
                "networkName": "Austin Clinic - North",
                "address": "3801 N Lamar Blvd, Austin, TX 78756, United States",
                "lanIp": "10.20.1.24",
            },
            {
                "serial": "Q2XX-DEMO-Z403",
                "mac": "00:18:0A:33:00:03",
                "name": "HNL-BR-Z4-01",
                "model": "Z4",
                "networkId": "N_DEMO_HNL_003",
                "networkName": "Honolulu Branch - Pacific",
                "address": "2230 Liliha St, Honolulu, HI 96817, United States",
                "lanIp": "10.30.10.1",
            },
        ]

        self._vlans_by_network: Dict[str, List[Dict[str, Any]]] = {
            "N_DEMO_DAL_001": [
                {"id": 10, "name": "Corporate Data", "subnet": "10.10.20.0/24", "applianceIp": "10.10.20.1"},
                {"id": 21, "name": "Voice Phones", "subnet": "10.10.21.0/24", "applianceIp": "10.10.21.1"},
                {"id": 99, "name": "Guest WiFi", "subnet": "172.16.99.0/24", "applianceIp": "172.16.99.1"},
            ],
            "N_DEMO_AUS_002": [
                {"id": 21, "name": "VoIP Phones", "subnet": "10.20.21.0/24", "applianceIp": "10.20.21.1"},
                {"id": 30, "name": "Corp Wireless", "subnet": "10.20.30.0/24", "applianceIp": "10.20.30.1"},
            ],
            "N_DEMO_HNL_003": [],
        }

        self._single_lan_by_network: Dict[str, Dict[str, Any]] = {
            "N_DEMO_HNL_003": {"subnet": "10.30.10.0/24", "applianceIp": "10.30.10.1"},
        }

        self._clients: List[Dict[str, Any]] = [
            {
                "mac": s["mac"],
                "ip": s["ip"],
                "networkId": s["network_id"],
                "networkName": s["network_name"],
                "network": {"id": s["network_id"], "name": s["network_name"]},
                "recentDeviceMac": s["device_mac"],
                "recentDeviceSerial": s["device_serial"],
                "recentDeviceName": s["device_name"],
                "switchport": s["switch_port"] if s["client_type"] == "WIRED" else None,
                "ssid": s.get("ssid"),
                "vlan": s["vlan"],
                "status": "Online" if s["status"] == "ACTIVE_SESSION_FOUND" else "Offline",
                "manufacturer": s["manufacturer"],
                "os": s["os"],
                "user": s["username"],
                "description": s["hostname"],
                "firstSeen": 1763590000,
                "lastSeen": 1763650000,
            }
            for s in _DEMO_ENDPOINT_SCENARIOS
            if s.get("meraki_found")
        ]

        self.dashboard = _DemoMerakiDashboard(self)

    @staticmethod
    def normalize_mac(mac_address: str) -> str:
        return MerakiProvider.normalize_mac(mac_address)

    @staticmethod
    def normalize_mac_compact(mac_address: str) -> str:
        return MerakiProvider.normalize_mac_compact(mac_address)

    @staticmethod
    def validate_ipv4(ip_address: str) -> str:
        return MerakiProvider.validate_ipv4(ip_address)

    def _empty_context(
        self,
        mac_address: str = "",
        ip_address: str = "",
        lookup_source: str = "demo",
        error: str = "",
    ) -> MerakiClientContext:
        return MerakiClientContext(
            meraki_found=False,
            mac_address=mac_address or "N/A",
            ip_address=ip_address or "N/A",
            network_id="N/A",
            network_name="N/A",
            switch_ap_name="N/A",
            switch_ap_serial="N/A",
            switch_ap_mac="N/A",
            port_ssid="N/A",
            vlan="N/A",
            status="Not Found",
            manufacturer="N/A",
            os="N/A",
            user="N/A",
            description="N/A",
            first_seen="N/A",
            last_seen="N/A",
            lookup_source=lookup_source,
            error=error,
        )

    def _normalize_record(
        self,
        record: Dict[str, Any],
        fallback_mac: str,
        lookup_source: str,
    ) -> MerakiClientContext:
        network = record.get("network") or {}
        network_id = str(network.get("id") or record.get("networkId") or "N/A")
        network_name = str(network.get("name") or record.get("networkName") or "N/A")
        port_ssid = record.get("switchport") or record.get("ssid") or "N/A"
        return MerakiClientContext(
            meraki_found=True,
            mac_address=self.normalize_mac(record.get("mac", fallback_mac)),
            ip_address=str(record.get("ip") or "N/A"),
            network_id=network_id,
            network_name=network_name,
            switch_ap_name=str(record.get("recentDeviceName") or "N/A"),
            switch_ap_serial=str(record.get("recentDeviceSerial") or "N/A"),
            switch_ap_mac=str(record.get("recentDeviceMac") or "N/A"),
            port_ssid=str(port_ssid),
            vlan=str(record.get("vlan") or "N/A"),
            status=str(record.get("status") or "Online"),
            manufacturer=str(record.get("manufacturer") or "N/A"),
            os=str(record.get("os") or "N/A"),
            user=str(record.get("user") or "N/A"),
            description=str(record.get("description") or "N/A"),
            first_seen=MerakiProvider._epoch_to_utc_string(record.get("firstSeen")),
            last_seen=MerakiProvider._epoch_to_utc_string(record.get("lastSeen")),
            lookup_source=lookup_source,
            error="",
        )

    def get_organization_networks(self) -> List[Dict[str, Any]]:
        networks = copy.deepcopy(self._networks)
        self.cache.set("organization_networks", networks)
        return networks

    def get_network_details(self, network_id: str) -> Dict[str, Any]:
        return self.dashboard.networks.getNetwork(network_id)

    def get_organization_devices(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        devices = copy.deepcopy(self._devices)
        self.cache.set("organization_devices", devices)
        return devices

    def get_appliance_location_inventory(self) -> List[Dict[str, Any]]:
        networks_by_id = {n["id"]: n for n in self._networks}
        grouped: Dict[str, Dict[str, Any]] = {}

        for dev in self._devices:
            model = str(dev.get("model", ""))
            if not model.upper().startswith(("MX", "Z4")):
                continue
            net_id = str(dev.get("networkId", ""))
            if not net_id:
                continue
            net_meta = networks_by_id.get(net_id, {})
            rec = grouped.setdefault(
                net_id,
                {
                    "network_id": net_id,
                    "network_name": net_meta.get("name") or dev.get("networkName") or net_id,
                    "address": dev.get("address") or net_meta.get("address") or "",
                    "address_source": "device" if dev.get("address") else "network",
                    "address_conflict": False,
                    "time_zone": net_meta.get("timeZone") or "America/Chicago",
                    "device_count": 0,
                    "appliance_models": set(),
                    "serial_numbers": set(),
                    "lookup_error": "",
                },
            )
            rec["device_count"] += 1
            rec["appliance_models"].add(model)
            if dev.get("serial"):
                rec["serial_numbers"].add(dev["serial"])

        inventory: List[Dict[str, Any]] = []
        for rec in grouped.values():
            rec["appliance_models"] = ", ".join(sorted(rec["appliance_models"]))
            rec["serial_numbers"] = ", ".join(sorted(rec["serial_numbers"]))
            inventory.append(rec)

        return sorted(inventory, key=lambda row: row["network_name"].lower())

    def get_organization_templates(self) -> List[Dict[str, Any]]:
        templates = copy.deepcopy(self._templates)
        self.cache.set("organization_config_templates", templates)
        return templates

    def get_network_appliance_vlans(self, network_id: str) -> List[Dict[str, Any]]:
        return copy.deepcopy(self._vlans_by_network.get(network_id, []))

    def get_network_clients_paginated(
        self,
        network_id: str,
        ip_address: str = "",
        mac_address: str = "",
        statuses: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        target_ip = self.validate_ipv4(ip_address) if ip_address else ""
        target_mac = self.normalize_mac(mac_address) if mac_address else ""
        status_set = {s.lower() for s in statuses} if statuses else None

        results: List[Dict[str, Any]] = []
        for c in self._clients:
            if c.get("networkId") != network_id.strip():
                continue
            if target_ip and c.get("ip") != target_ip:
                continue
            if target_mac and self.normalize_mac(c.get("mac", "")) != target_mac:
                continue
            if status_set and str(c.get("status", "")).lower() not in status_set:
                continue
            results.append(copy.deepcopy(c))
        return results

    def get_client_by_mac(self, mac_address: str, preferred_ip: str = "") -> MerakiClientContext:
        normalized_mac = self.normalize_mac(mac_address)
        normalized_ip = (
            self.validate_ipv4(preferred_ip)
            if preferred_ip and preferred_ip.strip() and preferred_ip != "No IP Assigned"
            else ""
        )

        match = next(
            (c for c in self._clients if self.normalize_mac(c["mac"]) == normalized_mac),
            None,
        )
        if not match:
            return self._empty_context(
                mac_address=normalized_mac,
                ip_address=normalized_ip,
                lookup_source="demo_org_search",
                error="Not found.",
            )
        return self._normalize_record(match, fallback_mac=normalized_mac, lookup_source="demo_org_search")

    def get_client_by_ip(self, ip_address: str) -> MerakiClientContext:
        normalized_ip = self.validate_ipv4(ip_address)
        match = next((c for c in self._clients if c.get("ip") == normalized_ip), None)
        if not match:
            return self._empty_context(
                ip_address=normalized_ip,
                lookup_source="demo_net_search",
                error="Not found.",
            )
        return self._normalize_record(
            match,
            fallback_mac=match.get("mac", "N/A"),
            lookup_source="demo_net_search",
        )

    def get_client_by_mac_or_ip(self, mac_address: str = "", ip_address: str = "") -> MerakiClientContext:
        if mac_address and mac_address.strip():
            return self.get_client_by_mac(mac_address=mac_address, preferred_ip=ip_address)
        if ip_address and ip_address.strip() and ip_address != "No IP Assigned":
            return self.get_client_by_ip(ip_address=ip_address)
        return self._empty_context(
            mac_address=mac_address or "N/A",
            ip_address=ip_address or "N/A",
            lookup_source="mac_or_ip",
            error="Neither MAC nor IP provided.",
        )

    def enrich_adapter_record(self, adapter_record: Dict[str, Any]) -> Dict[str, Any]:
        adapter_name = str(adapter_record.get("Name") or "Unknown")
        adapter_type = str(adapter_record.get("Type") or "Unknown")
        mac_address = str(adapter_record.get("MacAddress") or "")
        ip_address = str(adapter_record.get("IPAddress") or "No IP Assigned")
        os_status = str(adapter_record.get("Status") or "Unknown")

        context = self.get_client_by_mac_or_ip(
            mac_address=mac_address,
            ip_address="" if ip_address == "No IP Assigned" else ip_address,
        )

        return {
            "adapter_name": adapter_name,
            "adapter_type": adapter_type,
            "mac_address": mac_address or "N/A",
            "ip_address": ip_address or "No IP Assigned",
            "os_status": os_status,
            "meraki_found": context.meraki_found,
            "network_name": context.network_name,
            "switch_ap_name": context.switch_ap_name,
            "switch_ap_serial": context.switch_ap_serial,
            "switch_ap_mac": context.switch_ap_mac,
            "port_ssid": context.port_ssid,
            "vlan": context.vlan,
            "meraki_status": context.status,
            "manufacturer": context.manufacturer,
            "os": context.os,
            "user": context.user,
            "description": context.description,
            "first_seen": context.first_seen,
            "last_seen": context.last_seen,
            "lookup_source": context.lookup_source,
            "meraki_error": context.error,
        }

    def health_check(self) -> Dict[str, Any]:
        return {
            "status": "OK",
            "mode": "DEMO",
            "organization_id": self.organization_id,
            "network_count": len(self._networks),
            "lookback_days": self.lookback_days,
        }


# =============================================================================
# Demo Active Directory / LDAP Provider
# =============================================================================


class DemoLDAPProvider:
    """Self-contained demo replacement for LDAPProvider."""

    def __init__(self) -> None:
        self.server_uri = "ldaps://demo-dc01.corp.internal:636"
        self.domain = "corp.internal"
        self.search_base = "DC=corp,DC=internal"
        self.demo_mode = True
        self._accounts: List[LDAPAccountDetails] = [
            LDAPAccountDetails(
                sam_account_name="jdoe",
                user_principal_name="jdoe@corp.internal",
                mail="jdoe@corp.internal",
                display_name="John Doe",
                distinguished_name="CN=John Doe,OU=Dallas,OU=Users,DC=corp,DC=internal",
                is_enabled=True,
            ),
            LDAPAccountDetails(
                sam_account_name="asmith",
                user_principal_name="asmith@corp.internal",
                mail="asmith@corp.internal",
                display_name="Alice Smith",
                distinguished_name="CN=Alice Smith,OU=Austin,OU=Users,DC=corp,DC=internal",
                is_enabled=True,
            ),
            LDAPAccountDetails(
                sam_account_name="dev-engineer",
                user_principal_name="dev-engineer@corp.internal",
                mail="dev-engineer@corp.internal",
                display_name="Dev Engineer",
                distinguished_name="CN=Dev Engineer,OU=IT,OU=Users,DC=corp,DC=internal",
                is_enabled=True,
            ),
            # Intentional collision for msmith@corp.internal
            LDAPAccountDetails(
                sam_account_name="msmith",
                user_principal_name="msmith@corp.internal",
                mail="msmith@corp.internal",
                display_name="Morgan Smith",
                distinguished_name="CN=Morgan Smith,OU=Houston,OU=Users,DC=corp,DC=internal",
                is_enabled=True,
            ),
            LDAPAccountDetails(
                sam_account_name="msmith_legacy",
                user_principal_name="msmith_legacy@corp.internal",
                mail="msmith@corp.internal",
                display_name="Morgan Smith (Legacy)",
                distinguished_name="CN=Morgan Smith Legacy,OU=Disabled,DC=corp,DC=internal",
                is_enabled=False,
            ),
        ]

    def authenticate(self, username: str, password: str) -> bool:
        if not username or not username.strip() or not password or not password.strip():
            return False
        return True

    def check_identity_collisions(self, identifier: str) -> IdentityCollisionResult:
        clean_id = (identifier or "").strip()
        needle = clean_id.lower()
        matches: List[LDAPAccountDetails] = []
        if needle:
            for acc in self._accounts:
                candidates = [
                    acc.sam_account_name.lower(),
                    (acc.user_principal_name or "").lower(),
                    (acc.mail or "").lower(),
                ]
                if needle in candidates:
                    matches.append(acc.model_copy())

        match_count = len(matches)
        has_collision = match_count > 1

        if match_count == 0:
            summary = f"No Active Directory account found matching '{clean_id}'."
        elif match_count == 1:
            summary = f"Unique identity resolved: {matches[0].sam_account_name}."
        else:
            sams = ", ".join(a.sam_account_name for a in matches)
            summary = (
                f"CRITICAL: {match_count} AD accounts share identity '{clean_id}' [{sams}]. "
                "This causes Cisco ISE 802.1X failures."
            )

        return IdentityCollisionResult(
            search_term=clean_id,
            has_collision=has_collision,
            match_count=match_count,
            accounts=matches,
            summary_message=summary,
        )


# =============================================================================
# Demo Cisco Telecom Providers (CUCM AXL / UCCX REST / CUC CUPI)
# =============================================================================


class DemoCUCMProvider:
    """Self-contained demo replacement for CUCMAXLProvider supporting PRIMARY (12.5) and HAWAII (11.5)."""

    def __init__(
        self,
        cluster: str = "PRIMARY",
        host: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        version: Optional[str] = None,
        verify_ssl: Optional[bool] = False,
        timeout: Optional[int] = None,
        connect_timeout: float = 3.0,
    ) -> None:
        self.cluster = (cluster or "PRIMARY").strip().upper()
        is_hawaii = self.cluster == "HAWAII"

        self.host = host or ("demo-cucm-hawaii.local" if is_hawaii else "demo-cucm-primary.local")
        self.username = username or "demo_axl"
        self.password = password or "demo_pass"
        self.version = str(version or ("11.5" if is_hawaii else "12.5")).strip()
        self.verify_ssl = bool(verify_ssl) if verify_ssl is not None else False
        self.connect_timeout = float(connect_timeout)
        self.timeout = int(timeout) if timeout is not None else (45 if is_hawaii else 30)
        self.axl_url = f"https://{self.host}:8443/axl/"
        self.demo_mode = True

        self._partitions: List[Dict[str, str]] = (
            [
                {"pkid": "pt-hnl-01", "name": "Hawaii_Internal_PT", "description": "Hawaii Internal Directory Numbers"},
                {"pkid": "pt-hnl-02", "name": "Internal_PT", "description": "Shared Enterprise Internal Partition"},
            ]
            if is_hawaii
            else [
                {"pkid": "pt-pri-01", "name": "Internal_PT", "description": "Primary Corporate Internal Partition"},
                {"pkid": "pt-pri-02", "name": "Dallas_HQ_PT", "description": "Dallas HQ Partition"},
            ]
        )

        self._gateways_route_lists: List[Dict[str, Any]] = (
            [
                {
                    "pkid": "rl-hnl-01",
                    "name": "HNL_WebexCUBE_RL",
                    "description": "Hawaii Local Gateway Route List to Webex Calling",
                    "class_name": "Route List",
                    "is_route_list": True,
                },
                {
                    "pkid": "tr-hnl-01",
                    "name": "HNL-CUBE-SIP-Trunk",
                    "description": "Honolulu CUBE SIP Trunk",
                    "class_name": "Trunk",
                    "is_route_list": False,
                },
            ]
            if is_hawaii
            else [
                {
                    "pkid": "rl-pri-01",
                    "name": "WebexCUBE_RL",
                    "description": "Primary Local Gateway Route List to Webex Calling",
                    "class_name": "Route List",
                    "is_route_list": True,
                },
                {
                    "pkid": "tr-pri-01",
                    "name": "DAL-CUBE-SIP-Trunk",
                    "description": "Dallas HQ CUBE SIP Trunk",
                    "class_name": "Trunk",
                    "is_route_list": False,
                },
            ]
        )

        self._devices: List[Dict[str, Any]] = self._build_demo_devices(is_hawaii)
        self._route_patterns: List[Dict[str, Any]] = []
        self._translation_patterns: List[Dict[str, Any]] = []
        self._pattern_seq = 1

    @staticmethod
    def _build_demo_devices(is_hawaii: bool) -> List[Dict[str, Any]]:
        if is_hawaii:
            return [
                {
                    "pkid": "dev-hnl-01",
                    "device_name": "SEPAABBCCDDEE01",
                    "model": "Cisco 8841",
                    "description": "Honolulu Clinic Front Desk",
                    "device_pool": "DP_Hawaii_Main",
                    "owner_userid": "kmana",
                    "owner_email": "kmana@corp.internal",
                    "lines": [
                        {
                            "line_dn": "5101",
                            "partition_name": "Hawaii_Internal_PT",
                            "line_display": "Koa Mana",
                            "line_label": "Front Desk 5101",
                            "external_mask": "808555XXXX",
                        }
                    ],
                },
                {
                    "pkid": "dev-hnl-02",
                    "device_name": "CSFKMANA",
                    "model": "Cisco Unified Client Services Framework",
                    "description": "Koa Mana Jabber Softphone",
                    "device_pool": "DP_Hawaii_Main",
                    "owner_userid": "kmana",
                    "owner_email": "kmana@corp.internal",
                    "lines": [
                        {
                            "line_dn": "5101",
                            "partition_name": "Hawaii_Internal_PT",
                            "line_display": "Koa Mana",
                            "line_label": "Jabber 5101",
                            "external_mask": "808555XXXX",
                        }
                    ],
                },
                {
                    "pkid": "dev-hnl-03",
                    "device_name": "SEPAABBCCDDEE03",
                    "model": "Cisco 7965",
                    "description": "Hawaii Breakroom Legacy Phone",
                    "device_pool": "DP_Hawaii_Branch",
                    "owner_userid": "Unassigned",
                    "owner_email": "",
                    "lines": [
                        {
                            "line_dn": "5199",
                            "partition_name": "Hawaii_Internal_PT",
                            "line_display": "Breakroom",
                            "line_label": "5199",
                            "external_mask": "",
                        }
                    ],
                },
                {
                    "pkid": "dev-hnl-04",
                    "device_name": "ATA001122334401",
                    "model": "Cisco ATA 191",
                    "description": "Hawaii Lab Fax Line",
                    "device_pool": "DP_Hawaii_Main",
                    "owner_userid": "hnlfax",
                    "owner_email": "hnlfax@corp.internal",
                    "lines": [
                        {
                            "line_dn": "+18085555150",
                            "partition_name": "Hawaii_Internal_PT",
                            "line_display": "HNL Lab Fax",
                            "line_label": "Fax 5150",
                            "external_mask": "+18085555150",
                        }
                    ],
                },
            ]

        return [
            {
                "pkid": "dev-pri-01",
                "device_name": "SEP001122334455",
                "model": "Cisco 8841",
                "description": "John Doe - Dallas HQ",
                "device_pool": "DP_Dallas_HQ",
                "owner_userid": "jdoe",
                "owner_email": "jdoe@corp.internal",
                "lines": [
                    {
                        "line_dn": "4101",
                        "partition_name": "Internal_PT",
                        "line_display": "John Doe",
                        "line_label": "4101",
                        "external_mask": "214555XXXX",
                    }
                ],
            },
            {
                "pkid": "dev-pri-02",
                "device_name": "CSFJDOE",
                "model": "Cisco Unified Client Services Framework",
                "description": "John Doe Jabber",
                "device_pool": "DP_Dallas_HQ",
                "owner_userid": "jdoe",
                "owner_email": "jdoe@corp.internal",
                "lines": [
                    {
                        "line_dn": "4101",
                        "partition_name": "Internal_PT",
                        "line_display": "John Doe",
                        "line_label": "Jabber 4101",
                        "external_mask": "214555XXXX",
                    }
                ],
            },
            {
                "pkid": "dev-pri-03",
                "device_name": "SEP001122334466",
                "model": "Cisco Desk Phone 9851",
                "description": "Alice Smith - Austin Clinic",
                "device_pool": "DP_Austin_Clinic",
                "owner_userid": "asmith",
                "owner_email": "asmith@corp.internal",
                "lines": [
                    {
                        "line_dn": "4102",
                        "partition_name": "Internal_PT",
                        "line_display": "Alice Smith",
                        "line_label": "4102",
                        "external_mask": "512555XXXX",
                    }
                ],
            },
            {
                "pkid": "dev-pri-04",
                "device_name": "BOTASMITH",
                "model": "Cisco Dual Mode for Android",
                "description": "Alice Smith Mobile Jabber",
                "device_pool": "DP_Austin_Clinic",
                "owner_userid": "asmith",
                "owner_email": "asmith@corp.internal",
                "lines": [
                    {
                        "line_dn": "4102",
                        "partition_name": "Internal_PT",
                        "line_display": "Alice Smith",
                        "line_label": "Mobile 4102",
                        "external_mask": "512555XXXX",
                    }
                ],
            },
            {
                "pkid": "dev-pri-05",
                "device_name": "SEP001122334499",
                "model": "Cisco 7965",
                "description": "Legacy Warehouse Desk",
                "device_pool": "DP_Dallas_HQ",
                "owner_userid": "Unassigned",
                "owner_email": "",
                "lines": [
                    {
                        "line_dn": "4199",
                        "partition_name": "Internal_PT",
                        "line_display": "Warehouse",
                        "line_label": "4199",
                        "external_mask": "",
                    }
                ],
            },
            {
                "pkid": "dev-pri-06",
                "device_name": "VG400-DAL-01",
                "model": "Cisco VG400",
                "description": "Dallas Analog Paging Gateway",
                "device_pool": "DP_Dallas_HQ",
                "owner_userid": "Unassigned",
                "owner_email": "",
                "lines": [
                    {
                        "line_dn": "+12145554150",
                        "partition_name": "Internal_PT",
                        "line_display": "DAL Paging",
                        "line_label": "Paging",
                        "external_mask": "+12145554150",
                    }
                ],
            },
        ]

    @property
    def request_timeout(self) -> Tuple[float, float]:
        return (self.connect_timeout, float(self.timeout))

    def execute_axl_soap(self, action: str, body_xml: str) -> str:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">'
            f"<soapenv:Body><ns:{action}Response xmlns:ns=\"http://www.cisco.com/AXL/API/{self.version}\">"
            "<return>{DEMO-UUID-0001}</return>"
            f"</ns:{action}Response></soapenv:Body></soapenv:Envelope>"
        )

    def execute_sql_update(self, sql_update: str) -> Dict[str, Any]:
        return {"status": "SUCCESS", "rows_updated": 1}

    def execute_sql_query(self, sql_query: str) -> List[Dict[str, str]]:
        sql_upper = (sql_query or "").upper()
        if "FROM PROCESSNODE" in sql_upper:
            return [{"name": self.host, "description": f"{self.cluster} Demo CUCM Publisher", "nodeid": "2"}]
        if "FROM ROUTEPARTITION" in sql_upper:
            return self.get_route_partitions()

        pool_match = re.search(r"dp\.name\s*=\s*'([^']+)'", sql_query or "", re.IGNORECASE)
        pool_filter = pool_match.group(1).strip() if pool_match else None

        is_physical_scope = "D.NAME LIKE 'SEP%'" in sql_upper
        is_softphone_scope = "D.NAME LIKE 'CSF%'" in sql_upper
        is_gateway_scope = "D.NAME LIKE 'ATA%'" in sql_upper

        rows: List[Dict[str, str]] = []
        for dev in self._devices:
            if pool_filter and dev.get("device_pool") != pool_filter:
                continue

            name_upper = dev["device_name"].upper()
            model_upper = dev["model"].upper()

            if is_physical_scope and not name_upper.startswith("SEP"):
                continue
            if is_softphone_scope and not (
                name_upper.startswith(("CSF", "TCT", "BOT", "TAB"))
                or "CLIENT SERVICES FRAMEWORK" in model_upper
                or "DUAL MODE" in model_upper
                or "JABBER" in model_upper
            ):
                continue
            if is_gateway_scope and not (
                name_upper.startswith(("ATA", "VG"))
                or "ATA" in model_upper
                or "VG" in model_upper
            ):
                continue

            lines = dev.get("lines") or [{}]
            for line in lines:
                rows.append(
                    {
                        "device_name": dev["device_name"],
                        "device_description": dev.get("description", ""),
                        "device_model": dev.get("model", ""),
                        "device_pool": dev.get("device_pool", "Default"),
                        "owner_userid": dev.get("owner_userid", "Unassigned"),
                        "owner_email": dev.get("owner_email", ""),
                        "line_dn": line.get("line_dn", ""),
                        "partition_name": line.get("partition_name", "Internal_PT"),
                        "line_display": line.get("line_display", ""),
                        "line_label": line.get("line_label", ""),
                        "external_mask": line.get("external_mask", ""),
                    }
                )
        return rows

    def fetch_paginated_sql(self, base_sql: str, chunk_size: int = 1000) -> List[Dict[str, str]]:
        return self.execute_sql_query(base_sql)

    def get_route_partitions(self) -> List[Dict[str, str]]:
        return copy.deepcopy(self._partitions)

    def get_gateways_and_route_lists(self) -> List[Dict[str, Any]]:
        return copy.deepcopy(self._gateways_route_lists)

    def add_translation_pattern(
        self,
        pattern: str,
        partition: str,
        calling_search_space: str,
        description: Optional[str] = None,
        urgent_priority: bool = True,
    ) -> Dict[str, Any]:
        uuid_val = f"demo-tp-{self._pattern_seq:04d}"
        self._pattern_seq += 1
        desc = description or f"Migrated to Webex Calling - Ext {pattern}"
        self._translation_patterns.append(
            {
                "pkid": uuid_val,
                "dnorpattern": pattern.strip(),
                "partition_name": partition.strip(),
                "css_name": calling_search_space.strip(),
                "description": desc,
            }
        )
        return {
            "status": "SUCCESS",
            "uuid": uuid_val,
            "pattern": pattern,
            "partition": partition,
            "calling_search_space": calling_search_space,
        }

    def get_translation_pattern(self, pattern: str, partition: Optional[str] = None) -> Optional[Dict[str, Any]]:
        clean_pat = pattern.strip()
        clean_part = partition.strip() if partition else None
        for tp in self._translation_patterns:
            if tp["dnorpattern"] == clean_pat and (not clean_part or tp["partition_name"] == clean_part):
                return copy.deepcopy(tp)
        return None

    def remove_translation_pattern(self, pattern: str, partition: Optional[str] = None) -> bool:
        clean_pat = pattern.strip()
        clean_part = partition.strip() if partition else None
        self._translation_patterns = [
            tp
            for tp in self._translation_patterns
            if not (tp["dnorpattern"] == clean_pat and (not clean_part or tp["partition_name"] == clean_part))
        ]
        return True

    def add_route_pattern(
        self,
        pattern: str,
        partition: str,
        gateway_route_list: str,
        is_route_list: Optional[bool] = None,
        description: Optional[str] = None,
        urgent_priority: bool = True,
    ) -> Dict[str, Any]:
        if is_route_list is None:
            match = next(
                (g for g in self._gateways_route_lists if g["name"] == gateway_route_list),
                None,
            )
            is_route_list = bool(match.get("is_route_list")) if match else ("_RL" in gateway_route_list.upper())

        uuid_val = f"demo-rp-{self._pattern_seq:04d}"
        self._pattern_seq += 1
        dest_type = "Route List" if is_route_list else "Gateway/Trunk"
        desc = description or f"Migrated to Webex Calling - Ext {pattern}"
        self._route_patterns.append(
            {
                "pkid": uuid_val,
                "pattern": pattern.strip(),
                "partition_name": partition.strip(),
                "destination_name": gateway_route_list.strip(),
                "destination_type": dest_type,
                "description": desc,
            }
        )
        return {
            "status": "SUCCESS",
            "uuid": uuid_val,
            "pattern": pattern,
            "partition": partition,
            "gateway_route_list": gateway_route_list,
            "destination_type": dest_type,
        }

    def get_route_pattern(self, pattern: str, partition: Optional[str] = None) -> Optional[Dict[str, Any]]:
        clean_pat = pattern.strip()
        clean_part = partition.strip() if partition else None
        for rp in self._route_patterns:
            if rp["pattern"] == clean_pat and (not clean_part or rp["partition_name"] == clean_part):
                return copy.deepcopy(rp)
        return None

    def remove_route_pattern(self, pattern: str, partition: Optional[str] = None) -> bool:
        clean_pat = pattern.strip()
        clean_part = partition.strip() if partition else None
        self._route_patterns = [
            rp
            for rp in self._route_patterns
            if not (rp["pattern"] == clean_pat and (not clean_part or rp["partition_name"] == clean_part))
        ]
        return True

    @staticmethod
    def _normalize_cucm_lookup_name(device_name_or_mac: str) -> str:
        raw = (device_name_or_mac or "").strip()
        upper = raw.upper()
        if not upper.startswith(("CSF", "TCT", "BOT", "TAB")):
            raw_mac = upper[3:] if upper.startswith("SEP") else upper
            hex_mac = raw_mac.replace(":", "").replace("-", "").replace(".", "")
            if len(hex_mac) == 12 and all(ch in "0123456789ABCDEF" for ch in hex_mac):
                return f"SEP{hex_mac}"
        return upper

    def get_device_by_name_or_mac(self, device_name_or_mac: str) -> Optional[Dict[str, Any]]:
        target = self._normalize_cucm_lookup_name(device_name_or_mac)
        for dev in self._devices:
            if dev["device_name"].upper() == target:
                return {
                    "pkid": dev["pkid"],
                    "device_name": dev["device_name"],
                    "model": dev["model"],
                    "description": dev.get("description", ""),
                    "device_pool": dev.get("device_pool", "Default"),
                    "owner_userid": dev.get("owner_userid", ""),
                    "lines": [
                        {
                            "line_dn": line.get("line_dn", ""),
                            "partition_name": line.get("partition_name", ""),
                            "numplanindex": str(idx + 1),
                        }
                        for idx, line in enumerate(dev.get("lines", []))
                    ],
                }
        return None

    def get_devices_by_extension(self, pattern: str, partition: Optional[str] = None) -> List[Dict[str, Any]]:
        clean_pat = pattern.strip()
        clean_part = partition.strip() if partition and partition.strip() else None
        results: List[Dict[str, Any]] = []
        for dev in self._devices:
            for line in dev.get("lines", []):
                if line.get("line_dn") == clean_pat and (
                    not clean_part or line.get("partition_name") == clean_part
                ):
                    results.append(
                        {
                            "pkid": dev["pkid"],
                            "device_name": dev["device_name"],
                            "model": dev["model"],
                            "description": dev.get("description", ""),
                            "device_pool": dev.get("device_pool", "Default"),
                            "owner_userid": dev.get("owner_userid", ""),
                            "line_dn": line.get("line_dn", ""),
                            "partition_name": line.get("partition_name", ""),
                        }
                    )
        return sorted(results, key=lambda r: r["device_name"])

    def remove_phone(self, device_name_or_mac: str) -> bool:
        target = self._normalize_cucm_lookup_name(device_name_or_mac)
        self._devices = [d for d in self._devices if d["device_name"].upper() != target]
        return True

    def remove_line(self, pattern: str, partition: Optional[str] = None) -> bool:
        clean_pat = pattern.strip()
        clean_part = partition.strip() if partition else None
        for dev in self._devices:
            dev["lines"] = [
                line
                for line in dev.get("lines", [])
                if not (line.get("line_dn") == clean_pat and (not clean_part or line.get("partition_name") == clean_part))
            ]
        return True


class DemoUCCXProvider:
    """Self-contained demo replacement for UCCXProvider supporting PRIMARY (12.5) and HAWAII (12.0)."""

    def __init__(
        self,
        cluster: str = "PRIMARY",
        host: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        verify_ssl: Optional[bool] = False,
        timeout: Optional[int] = 30,
    ) -> None:
        self.cluster = (cluster or "PRIMARY").strip().upper()
        is_hawaii = self.cluster == "HAWAII"
        self.host = host or ("demo-uccx-hawaii.local" if is_hawaii else "demo-uccx-primary.local")
        self.username = username or "demo_uccx"
        self.password = password or "demo_pass"
        self.verify_ssl = bool(verify_ssl) if verify_ssl is not None else False
        self.timeout = (3.0, float(timeout or (45 if is_hawaii else 30)))
        self.base_url = f"https://{self.host}:8443/adminapi"
        self.demo_mode = True

        if is_hawaii:
            self._csqs = [
                {"id": "101", "name": "CSQ_Hawaii_Patient_Sched", "type": "VOICE", "algorithm": "FIFO", "ref_url": f"{self.base_url}/csq/101"},
                {"id": "102", "name": "CSQ_Hawaii_Billing", "type": "VOICE", "algorithm": "FIFO", "ref_url": f"{self.base_url}/csq/102"},
            ]
            self._csq_details = {
                "101": {
                    "id": "101",
                    "name": "CSQ_Hawaii_Patient_Sched",
                    "type": "VOICE",
                    "algorithm": "FIFO",
                    "csq_selection_criteria": "MOST_SKILLED",
                    "skills": [{"skill_name": "HNL_Scheduling", "weight": "5"}],
                },
                "102": {
                    "id": "102",
                    "name": "CSQ_Hawaii_Billing",
                    "type": "VOICE",
                    "algorithm": "FIFO",
                    "csq_selection_criteria": "LONGEST_AVAILABLE",
                    "skills": [{"skill_name": "HNL_Billing", "weight": "5"}],
                },
            }
            self._skills = [
                {"skill_name": "HNL_Scheduling", "skill_id": "1"},
                {"skill_name": "HNL_Billing", "skill_id": "2"},
            ]
            self._resources = [
                {"user_id": "kmana", "first_name": "Koa", "last_name": "Mana", "extension": "5101", "type": "AGENT"},
                {"user_id": "lkealoha", "first_name": "Leilani", "last_name": "Kealoha", "extension": "5102", "type": "SUPERVISOR"},
            ]
            self._applications = [
                {
                    "name": "Hawaii_Main_IVR",
                    "type": "SCRIPT",
                    "script_name": "Hawaii_Main_IVR.aef",
                    "max_sessions": "25",
                    "triggers": [{"directory_number": "8085550100", "locale": "en_US", "enabled": "true"}],
                }
            ]
            self._scripts = [{"name": "Hawaii_Main_IVR.json", "path": "Hawaii_Main_IVR.json"}]
            self._prompt_tree = {
                "en_US": {"files": ["HNL_Welcome.wav", "HNL_Closed.wav"], "subfolders": ["IVR"]},
                "en_US/IVR": {"files": ["HNL_Menu_Options.wav"], "subfolders": []},
                "es_US": {"files": ["HNL_Welcome_ES.wav"], "subfolders": []},
            }
        else:
            self._csqs = [
                {"id": "1", "name": "CSQ_Corp_Helpdesk", "type": "VOICE", "algorithm": "FIFO", "ref_url": f"{self.base_url}/csq/1"},
                {"id": "2", "name": "CSQ_Patient_Intake", "type": "VOICE", "algorithm": "FIFO", "ref_url": f"{self.base_url}/csq/2"},
            ]
            self._csq_details = {
                "1": {
                    "id": "1",
                    "name": "CSQ_Corp_Helpdesk",
                    "type": "VOICE",
                    "algorithm": "FIFO",
                    "csq_selection_criteria": "LONGEST_AVAILABLE",
                    "skills": [{"skill_name": "IT_Helpdesk", "weight": "7"}],
                },
                "2": {
                    "id": "2",
                    "name": "CSQ_Patient_Intake",
                    "type": "VOICE",
                    "algorithm": "FIFO",
                    "csq_selection_criteria": "MOST_SKILLED",
                    "skills": [{"skill_name": "Patient_Intake", "weight": "5"}],
                },
            }
            self._skills = [
                {"skill_name": "IT_Helpdesk", "skill_id": "10"},
                {"skill_name": "Patient_Intake", "skill_id": "11"},
            ]
            self._resources = [
                {"user_id": "jdoe", "first_name": "John", "last_name": "Doe", "extension": "4101", "type": "AGENT"},
                {"user_id": "asmith", "first_name": "Alice", "last_name": "Smith", "extension": "4102", "type": "AGENT"},
            ]
            self._applications = [
                {
                    "name": "Corporate_Main_AA",
                    "type": "SCRIPT",
                    "script_name": "Corporate_Main_AA.aef",
                    "max_sessions": "50",
                    "triggers": [{"directory_number": "2145550100", "locale": "en_US", "enabled": "true"}],
                }
            ]
            self._scripts = [{"name": "Corporate_Main_AA.json", "path": "Corporate_Main_AA.json"}]
            self._prompt_tree = {
                "en_US": {"files": ["Corp_Welcome.wav", "Corp_AfterHours.wav"], "subfolders": ["Queues"]},
                "en_US/Queues": {"files": ["Helpdesk_Hold.wav"], "subfolders": []},
                "es_US": {"files": ["Corp_Welcome_ES.wav"], "subfolders": []},
            }

    def is_reachable(self, port: int = 8443, timeout_sec: float = 2.0) -> bool:
        return True

    def get_csqs(self) -> List[Dict[str, Any]]:
        return copy.deepcopy(self._csqs)

    def get_csq_detail(self, csq_id_or_name: str) -> Dict[str, Any]:
        key = str(csq_id_or_name)
        if key in self._csq_details:
            return copy.deepcopy(self._csq_details[key])
        for detail in self._csq_details.values():
            if detail["name"] == key:
                return copy.deepcopy(detail)
        return {
            "id": key,
            "name": key,
            "type": "VOICE",
            "algorithm": "FIFO",
            "csq_selection_criteria": "LONGEST_AVAILABLE",
            "skills": [],
        }

    def get_skills(self) -> List[Dict[str, str]]:
        return copy.deepcopy(self._skills)

    def get_resources(self) -> List[Dict[str, Any]]:
        return copy.deepcopy(self._resources)

    def get_applications(self) -> List[Dict[str, Any]]:
        return copy.deepcopy(self._applications)

    def get_scripts(self) -> List[Dict[str, Any]]:
        return copy.deepcopy(self._scripts)

    def download_script(self, script_path: str) -> str:
        queue_name = self._csqs[0]["name"] if self._csqs else "CSQ_Default"
        payload = {
            "script_name": os.path.basename(script_path) or "Demo_IVR.json",
            "nodes": [
                {"id": "n_start", "type": "start", "name": "Start Call"},
                {"id": "n_prompt", "type": "playprompt", "name": "Play Welcome", "properties": {"prompt": "Welcome.wav"}},
                {"id": "n_menu", "type": "menu", "name": "Main Menu"},
                {"id": "n_queue", "type": "selectresource", "name": f"Queue {queue_name}", "properties": {"csq": queue_name}},
                {"id": "n_end", "type": "disconnect", "name": "Disconnect"},
            ],
            "edges": [
                {"source": "n_start", "output": "success", "target": "n_prompt"},
                {"source": "n_prompt", "output": "success", "target": "n_menu"},
                {"source": "n_menu", "output": "1", "target": "n_queue"},
                {"source": "n_menu", "output": "timeout", "target": "n_end"},
            ],
        }
        return json.dumps(payload)

    def list_folder_contents(self, folder_path: str) -> Dict[str, List[str]]:
        clean_path = folder_path.strip("/")
        if clean_path.startswith("user/"):
            clean_path = clean_path[5:]
        entry = self._prompt_tree.get(clean_path, {"files": [], "subfolders": []})
        return {
            "files": list(entry["files"]),
            "subfolders": list(entry["subfolders"]),
        }

    def discover_prompts_recursively(self, base_folder: str) -> List[Dict[str, str]]:
        discovered: List[Dict[str, str]] = []
        clean_base = base_folder.strip("/")
        if clean_base.startswith("user/"):
            clean_base = clean_base[5:]

        queue = [clean_base]
        while queue:
            current = queue.pop(0)
            contents = self.list_folder_contents(current)
            for fname in contents["files"]:
                discovered.append(
                    {
                        "file_name": fname,
                        "folder_path": current,
                        "relative_file_path": f"{current}/{fname}",
                    }
                )
            for sub in contents["subfolders"]:
                queue.append(f"{current}/{sub}")
        return discovered

    def download_prompt_bytes(self, relative_path: str) -> bytes:
        return _build_demo_wav_bytes()


class DemoCUPIProvider:
    """Self-contained demo replacement for CUPIProvider supporting PRIMARY (14) and HAWAII (11.5)."""

    def __init__(
        self,
        cluster: str = "PRIMARY",
        host: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        verify_ssl: Optional[bool] = False,
        timeout: Optional[int] = 30,
    ) -> None:
        self.cluster = (cluster or "PRIMARY").strip().upper()
        is_hawaii = self.cluster == "HAWAII"
        self.host = host or ("demo-cuc-hawaii.local" if is_hawaii else "demo-cuc-primary.local")
        self.username = username or "demo_cuc"
        self.password = password or "demo_pass"
        self.verify_ssl = bool(verify_ssl) if verify_ssl is not None else False
        self.timeout = int(timeout or (45 if is_hawaii else 30))
        self.base_url = f"https://{self.host}:8443/vmrest"
        self.demo_mode = True

        if is_hawaii:
            self._users = [
                {
                    "ObjectId": "cuc-hnl-u1",
                    "Alias": "kmana",
                    "DisplayName": "Koa Mana",
                    "DtmfAccessId": "5101",
                    "SmtpAddress": "kmana@corp.internal",
                    "PartitionString": "Hawaii_Partition",
                }
            ]
            self._handlers = [
                {
                    "ObjectId": "cuc-hnl-ch1",
                    "DisplayName": "Hawaii_Main_AutoAttendant",
                    "Extension": "5000",
                }
            ]
            self._menu_entries = {
                "cuc-hnl-ch1": [
                    {"Touchtone": "1", "Action": "Transfer to Extension", "TargetHandlerDisplayName": "Scheduling", "TransferExtension": "5101"},
                    {"Touchtone": "2", "Action": "Queue", "TargetHandlerDisplayName": "CSQ_Hawaii_Billing", "TransferExtension": "5102"},
                ]
            }
            self._greetings = {
                "cuc-hnl-ch1": [
                    {"GreetingType": "Standard", "Enabled": True, "PlayWhat": "Recorded Greeting"},
                    {"GreetingType": "Closed", "Enabled": True, "PlayWhat": "Recorded Greeting"},
                ]
            }
        else:
            self._users = [
                {
                    "ObjectId": "cuc-pri-u1",
                    "Alias": "jdoe",
                    "DisplayName": "John Doe",
                    "DtmfAccessId": "4101",
                    "SmtpAddress": "jdoe@corp.internal",
                    "PartitionString": "Global",
                },
                {
                    "ObjectId": "cuc-pri-u2",
                    "Alias": "asmith",
                    "DisplayName": "Alice Smith",
                    "DtmfAccessId": "4102",
                    "SmtpAddress": "asmith@corp.internal",
                    "PartitionString": "Global",
                },
            ]
            self._handlers = [
                {
                    "ObjectId": "cuc-pri-ch1",
                    "DisplayName": "Corporate_Main_AutoAttendant",
                    "Extension": "4000",
                }
            ]
            self._menu_entries = {
                "cuc-pri-ch1": [
                    {"Touchtone": "1", "Action": "Transfer to Extension", "TargetHandlerDisplayName": "Operator", "TransferExtension": "4101"},
                    {"Touchtone": "2", "Action": "Queue", "TargetHandlerDisplayName": "CSQ_Corp_Helpdesk", "TransferExtension": "4102"},
                ]
            }
            self._greetings = {
                "cuc-pri-ch1": [
                    {"GreetingType": "Standard", "Enabled": True, "PlayWhat": "Recorded Greeting"},
                    {"GreetingType": "Closed", "Enabled": True, "PlayWhat": "Recorded Greeting"},
                ]
            }

    def get_users_paginated(self, page_size: int = 100) -> List[Dict[str, Any]]:
        return copy.deepcopy(self._users)

    def get_call_handlers_paginated(self, page_size: int = 100) -> List[Dict[str, Any]]:
        return copy.deepcopy(self._handlers)

    def get_call_handler_menu_entries(self, handler_object_id: str) -> List[Dict[str, Any]]:
        return copy.deepcopy(self._menu_entries.get(handler_object_id, []))

    def get_call_handler_greetings(self, handler_object_id: str) -> List[Dict[str, Any]]:
        return copy.deepcopy(self._greetings.get(handler_object_id, []))

    def download_greeting_audio_file(
        self,
        handler_object_id: str,
        greeting_type: str,
        destination_path: str,
    ) -> bool:
        greetings = self._greetings.get(handler_object_id, [])
        has_greeting = any(
            str(g.get("GreetingType", "")).lower() == str(greeting_type).lower() and g.get("Enabled")
            for g in greetings
        )
        if not has_greeting:
            return False

        parent_dir = os.path.dirname(destination_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        with open(destination_path, "wb") as f:
            f.write(_build_demo_wav_bytes())
        return True
