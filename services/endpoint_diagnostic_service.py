# services/endpoint_diagnostic_service.py

import logging
import os
import re
import socket
import xml.etree.ElementTree as ET
from typing import Any, Dict, Optional

from core.config import get_settings
from providers.meraki_provider import MerakiProvider
from providers.ise_provider import ISEProvider

logger = logging.getLogger("Endpoint_Diagnostic_Service")

MAC_PATTERN = re.compile(
    r"^(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$|^[0-9A-Fa-f]{12}$"
)
IPV4_PATTERN = re.compile(r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$")


class EndpointDiagnosticService:
    """
    Core business logic for network endpoint diagnostics.
    Orchestrates data gathering across Cisco ISE, Catalyst Center, Meraki, and Active Directory.
    """

    def __init__(
        self, 
        ise_provider: ISEProvider, 
        meraki_provider: Optional[MerakiProvider] = None,
        dnac_provider: Optional[Any] = None,
        ldap_provider: Optional[Any] = None,
        troubleshooting_engine: Optional[Any] = None
    ):
        self.ise = ise_provider
        self.meraki = meraki_provider
        self.dnac = dnac_provider
        self.ldap = ldap_provider
        self.engine = troubleshooting_engine

    def diagnose_target(self, target: str) -> Dict[str, Any]:
        """
        Orchestrate target resolution and complete multi-provider diagnostics.
        Defensively handles VPN endpoints and isolates provider exceptions.
        """
        mac_address = None
        try:
            mac_address = self._resolve_to_mac(target)
        except Exception as winrm_err:
            logger.warning("WinRM pre-flight connection failed for target %s (Expected on VPN): %s", target, winrm_err)
        
        # 1. Short-Circuit if endpoint is definitively offline
        if mac_address == "OFFLINE":
            logger.info("Target %s is confirmed offline. Short-circuiting.", target)
            return self._build_offline_payload(target)

        # 2. Extract baseline network data from ISE Auth footprints
        lookup_target = mac_address if mac_address else target
        
        try:
            ise_report = self.ise.diagnose_endpoint_session(lookup_target)
            ise_report["ui_type"] = "DIAGNOSTIC_REPORT"
        except Exception as exc:
            logger.exception("ISE provider failed to diagnose target=%s", lookup_target)
            return self._build_error_payload(lookup_target, str(exc))

        # 3. Defensive Catalyst Center Real-Time Topology Enrichment
        if self.dnac and ise_report.get("mac_address") and ise_report.get("mac_address") != "N/A":
            dnac_mac = ise_report["mac_address"]
            try:
                dnac_telemetry = self.dnac.get_client_topology_by_mac(dnac_mac)
                if dnac_telemetry and dnac_telemetry.get("dnac_found"):
                    
                    for field in ["nas_name", "nas_ip", "switch_port"]:
                        if dnac_telemetry.get(field) in ["N/A", None, ""] and ise_report.get(field) not in ["N/A", None, ""]:
                            dnac_telemetry[field] = ise_report[field]
                            
                    ise_report["catalyst_telemetry"] = dnac_telemetry
                    
                    for field in ["switch_port", "vlan", "nas_name", "nas_ip"]:
                        if ise_report.get(field) in ["N/A", None, ""] and dnac_telemetry.get(field) not in ["N/A", None, ""]:
                            ise_report[field] = dnac_telemetry[field]

                    score = dnac_telemetry.get("health_score")
                    score_str = f" | Health Score: {score}/10" if score not in ["N/A", None] else ""
                    ise_report["triage_hint"] = f"{ise_report.get('triage_hint', '')} (Catalyst Center Enriched{score_str})".strip()
            except Exception as dnac_exc:
                logger.warning("Catalyst Center enrichment bypassed for MAC %s (Likely VPN): %s", dnac_mac, dnac_exc)
                
        # 4. Meraki Telemetry Enrichment
        if self.meraki and ise_report.get("mac_address") and ise_report.get("mac_address") != "N/A":
            try:
                meraki_context = self.meraki.get_client_by_mac(
                    mac_address=ise_report["mac_address"],
                    preferred_ip=ise_report.get("nas_ip", "")
                )
                
                if meraki_context and meraki_context.meraki_found:
                    ise_report["meraki_telemetry"] = meraki_context.to_dict()
                    
                    if ise_report.get("vlan", "N/A") == "N/A" and meraki_context.vlan and meraki_context.vlan != "N/A":
                        ise_report["vlan"] = meraki_context.vlan
                        
                    if ise_report.get("switch_port", "N/A") == "N/A" and hasattr(meraki_context, 'port_ssid') and meraki_context.port_ssid != "N/A":
                        ise_report["switch_port"] = meraki_context.port_ssid
                        
            except Exception as exc:
                logger.error("Failed to supplement diagnostic report with Meraki telemetry: %s", exc)
                ise_report["meraki_telemetry"] = {"error": "Meraki enrichment failed."}

        # 5. VPN Footprint Normalization
        nas_name = ise_report.get("nas_name", "").upper()
        if "VPN" in nas_name or "ASA" in nas_name or "ASA-VPN" in nas_name or ise_report.get("auth_method") == "VPN":
            ise_report["switch_port"] = "Virtual Adapter"
            ise_report["triage_hint"] = f"Endpoint is securely connected remotely via corporate VPN headend ({ise_report.get('nas_name', 'ASA/FTD')})."

            if "catalyst_telemetry" in ise_report and ise_report["catalyst_telemetry"]:
                ise_report["catalyst_telemetry"]["health_score"] = "N/A"
                ise_report["catalyst_telemetry"]["client_type"] = "VPN"
                ise_report["catalyst_telemetry"]["status"] = "Remote Access VPN"

        # 6. NEW: Active Directory Duplicate Identity Collision Inspection
        if self.ldap:
            lookup_user = ise_report.get("username") or target
            if lookup_user and lookup_user != "N/A":
                try:
                    collision_res = self.ldap.check_identity_collisions(lookup_user)
                    ise_report["identity_collision"] = collision_res.model_dump()
                    
                    if collision_res.has_collision:
                        ise_report["triage_hint"] = f"🚨 AD COLLISION: {collision_res.summary_message} | {ise_report.get('triage_hint', '')}"
                except Exception as ldap_err:
                    logger.warning("Active Directory collision check failed for %s: %s", lookup_user, ldap_err)

        # 7. NEW: Evaluate Matched JSON Playbooks (Rules Engine)
        if self.engine:
            try:
                telemetry_payload = {
                    "ise_failure_code": ise_report.get("failure_code", 0),
                    "ad_account_match_count": ise_report.get("identity_collision", {}).get("match_count", 1),
                    "vlan": ise_report.get("vlan"),
                    "status": ise_report.get("status")
                }
                ise_report["matched_playbooks"] = self.engine.evaluate(telemetry_payload)
            except Exception as engine_err:
                logger.warning("Playbook engine evaluation failed: %s", engine_err)

        return ise_report

    @staticmethod
    def _build_offline_payload(target: str) -> Dict[str, Any]:
        return {
            "ui_type": "DIAGNOSTIC_REPORT",
            "mac_address": "Offline / None",
            "status": "DEVICE_OFFLINE",
            "endpoint_profile": "Unknown / Offline",
            "username": "N/A",
            "nas_name": "N/A",
            "nas_ip": "N/A",
            "switch_port": "N/A",
            "vlan": "N/A",
            "auth_method": "N/A",
            "triage_hint": f"The target '{target}' is currently offline or communicating outside an authenticated network segment. No active session exists inside Cisco ISE.",
            "recent_history": []
        }

    def _resolve_to_mac(self, target: str) -> Optional[str]:
        cleaned = target.strip()

        if MAC_PATTERN.match(cleaned):
            return self._normalize_mac(cleaned)

        try:
            user_url = f"https://{self.ise.host}/admin/API/mnt/Session/UserName/{cleaned}"
            response = self.ise.api.session.get(user_url, headers={"Accept": "application/xml"})
            if response.status_code == 200 and "Session data is not available" not in response.text:
                root = ET.fromstring(response.text)
                for child in root.iter():
                    if child.tag == "calling_station_id" and child.text:
                        logger.info("Resolved MAC via ISE Username lookup for %s", cleaned)
                        return self._normalize_mac(child.text)
        except Exception:
            pass

        try:
            host_url = f"https://{self.ise.host}/admin/API/mnt/Session/UserName/host/{cleaned}"
            response = self.ise.api.session.get(host_url, headers={"Accept": "application/xml"})
            if response.status_code == 200 and "Session data is not available" not in response.text:
                root = ET.fromstring(response.text)
                for child in root.iter():
                    if child.tag == "calling_station_id" and child.text:
                        logger.info("Resolved MAC via ISE Machine Auth lookup for %s", cleaned)
                        return self._normalize_mac(child.text)
        except Exception:
            pass

        ip_address: Optional[str] = None
        if IPV4_PATTERN.match(cleaned):
            ip_address = cleaned
        else:
            try:
                ip_address = socket.gethostbyname(cleaned)
                logger.info("Resolved diagnostic hostname target=%s ip=%s", cleaned, ip_address)
            except socket.gaierror:
                logger.warning("DNS lookup failed for diagnostic target=%s", cleaned)
                return None

        if not ip_address:
            return None

        try:
            session_url = f"https://{self.ise.host}/admin/API/mnt/Session/IPAddress/{ip_address}"
            response = self.ise.api.session.get(session_url, headers={"Accept": "application/xml"})

            if response.status_code == 200 and response.text and "Session data is not available" not in response.text:
                root = ET.fromstring(response.text)
                for child in root.iter():
                    if child.tag == "calling_station_id" and child.text:
                        return self._normalize_mac(child.text)
        except Exception as exc:
            logger.warning("ISE IP session lookup failed ip=%s error=%s", ip_address, exc)

        settings = get_settings()
        raw_vpn_prefixes = os.getenv("VPN_SUBNET_PREFIXES")
        vpn_prefixes = (
            [p.strip() for p in raw_vpn_prefixes.split(",") if p.strip()]
            if raw_vpn_prefixes is not None
            else settings.vpn_subnet_prefixes_list
        )
        if any(ip_address.startswith(prefix) for prefix in vpn_prefixes):
            logger.info("Target IP %s matches VPN footprint. Skipping Meraki sweep to prevent API stalls.", ip_address)
            return "OFFLINE"

        if self.meraki:
            try:
                logger.info("Executing Meraki cloud IP sweep for %s...", ip_address)
                meraki_context = self.meraki.get_client_by_ip(ip_address)
                if meraki_context.meraki_found and meraki_context.mac_address != "N/A":
                    return self._normalize_mac(meraki_context.mac_address)
            except Exception as exc:
                logger.warning("Meraki IP-to-MAC lookup failed ip=%s error=%s", ip_address, exc)

        return "OFFLINE"

    @staticmethod
    def _normalize_mac(mac_address: str) -> str:
        if not mac_address or mac_address == "OFFLINE":
            return "OFFLINE"
            
        candidate = mac_address.strip().replace("-", ":").upper()
        if re.fullmatch(r"[0-9A-F]{12}", candidate):
            candidate = ":".join(candidate[i : i + 2] for i in range(0, 12, 2))
        return candidate

    @staticmethod
    def _build_unknown_target_payload(target: str) -> Dict[str, Any]:
        return {
            "ui_type": "DIAGNOSTIC_REPORT",
            "mac_address": "N/A",
            "status": "UNKNOWN_TARGET",
            "endpoint_profile": "N/A",
            "username": "N/A",
            "nas_name": "N/A",
            "nas_ip": "N/A",
            "switch_port": "N/A",
            "vlan": "N/A",
            "auth_method": "N/A",
            "triage_hint": (
                f"Could not translate target '{target}' to a MAC address. "
                "Use a MAC address directly or verify DNS/ISE/Meraki visibility."
            ),
            "recent_history": [],
        }

    @staticmethod
    def _build_error_payload(mac: str, error_msg: str) -> Dict[str, Any]:
        return {
            "ui_type": "DIAGNOSTIC_REPORT",
            "mac_address": mac,
            "status": "PROVIDER_ERROR",
            "triage_hint": f"Diagnostic orchestration failed: {error_msg}",
            "recent_history": [],
        }