# Save as: providers/ise_provider.py
import logging
import xml.etree.ElementTree as ET
import re
import os
from typing import Dict, Any, List, Optional
from ciscoisesdk import IdentityServicesEngineAPI
from ciscoisesdk.exceptions import ciscoisesdkException, ApiError

logger = logging.getLogger("ISE_Provider")

class ISEProvider:
    """
    Cisco ISE Infrastructure Provider.
    Handles raw API interactions, CoA triggers, and endpoint provisioning.
    """
    def __init__(self, host: str, username: str, token: str, version: str = "3.3_patch_1", verify_ssl: Optional[bool] = None):
        self.host = host
        if verify_ssl is not None:
            self.verify_ssl = verify_ssl
        else:
            self.verify_ssl = os.getenv("ISE_VERIFY_SSL", "false").strip().lower() in {"1", "true", "yes"}

        self.api = IdentityServicesEngineAPI(
            username=username,
            password=token,
            base_url=f"https://{host}:9060",
            version=version,
            verify=self.verify_ssl
        )

    def trigger_session_coa_terminate(self, mac_address: str) -> bool:
        mnt_mac = mac_address.replace(":", "").replace("-", "").upper()
        try:
            session_url = f"https://{self.host}/admin/API/mnt/Session/MACAddress/{mnt_mac}"
            response = self.api.session.get(session_url)
            
            if response.status_code != 200 or not response.text:
                return False
                
            root = ET.fromstring(response.text)
            nas_ip = None
            for child in root.iter():
                if child.tag == "nas_ip_address":
                    nas_ip = child.text
                    break
                    
            if not nas_ip:
                return False
                
            coa_url = f"https://{self.host}/admin/API/mnt/CoA/Disconnect"
            coa_payload = f"<coaCommand><nasIp>{nas_ip}</nasIp><mac>{mac_address}</mac><disconnectType>REAUTHENTICATE</disconnectType></coaCommand>"
            headers = {"Content-Type": "application/xml", "Accept": "application/xml"}
            coa_resp = self.api.session.post(coa_url, data=coa_payload, headers=headers)
            
            return coa_resp.status_code in [200, 204]
        except Exception as exc:
            logger.warning(f"CoA termination failed for MAC {mac_address}: {exc}")
            return False

    def provision_imaging_bypass(self, mac_address: str, operator_teams_id: str) -> Dict[str, Any]:
        STAGING_GROUP = "Staging_Imaging_Bypass"
        try:
            group_query = self.api.endpoint_identity_group.get_endpoint_groups(filter=f"name.EQ.{STAGING_GROUP}")
            resources = group_query.response.SearchResult.resources
            if not resources:
                return {"status": "ERROR", "message": f"ISE Group '{STAGING_GROUP}' missing."}
            group_id = resources[0].id

            try:
                ep_search = self.api.endpoint.get_endpoints(filter=f"mac.EQ.{mac_address}")
                ep_id = ep_search.response.SearchResult.resources[0].id
                self.api.endpoint.update_endpoint_by_id(
                    id=ep_id,
                    description=f"Authorized via Automation by {operator_teams_id}",
                    groupId=group_id,
                    staticGroupAssignment=True
                )
            except (ApiError, IndexError, ciscoisesdkException):
                self.api.endpoint.create_endpoint(
                    mac=mac_address,
                    description=f"Staging initialized via Automation by {operator_teams_id}",
                    groupId=group_id,
                    staticGroupAssignment=True,
                    profileId="",
                    staticProfileAssignment=False
                )

            coa_triggered = self.trigger_session_coa_terminate(mac_address)
            return {"status": "SUCCESS", "mac": mac_address, "group": STAGING_GROUP, "coa_applied": coa_triggered}
        except Exception as e:
            return {"status": "CRITICAL_FAILURE", "message": str(e)}

    def remove_endpoint(self, mac_address: str) -> Dict[str, Any]:
        """
        Purges an endpoint from Cisco ISE by MAC address and terminates any active switch session.
        Used to clear stale 802.1X identities, certificates, and group assignments before reimaging.
        """
        clean_mac = mac_address.replace("-", ":").upper()
        try:
            ep_search = self.api.endpoint.get_endpoints(filter=f"mac.EQ.{clean_mac}")
            resources = (
                ep_search.response.SearchResult.resources 
                if hasattr(ep_search, "response") and hasattr(ep_search.response, "SearchResult") 
                else []
            )
            if not resources:
                return {
                    "status": "NOT_FOUND",
                    "mac": clean_mac,
                    "deleted": False,
                    "coa_applied": False,
                    "message": f"Endpoint with MAC '{clean_mac}' not found in ISE inventory."
                }

            ep_id = resources[0].id
            self.api.endpoint.delete_endpoint_by_id(id=ep_id)
            coa_triggered = self.trigger_session_coa_terminate(clean_mac)
            logger.info("Successfully deleted ISE endpoint ID %s (MAC: %s), CoA: %s", ep_id, clean_mac, coa_triggered)
            return {
                "status": "SUCCESS",
                "mac": clean_mac,
                "endpoint_id": ep_id,
                "deleted": True,
                "coa_applied": coa_triggered,
                "message": f"Successfully deleted endpoint '{clean_mac}' from ISE."
            }
        except Exception as exc:
            logger.error("Failed to delete endpoint %s from ISE: %s", clean_mac, exc)
            return {
                "status": "ERROR",
                "mac": clean_mac,
                "deleted": False,
                "coa_applied": False,
                "message": str(exc)
            }

    def get_auth_history(self, mac_address: str, hours_back: int = 24, record_limit: int = 3) -> List[Dict[str, Any]]:
        clean_mac = mac_address.replace("-", ":").upper()
        if len(clean_mac) == 12:  
            clean_mac = ':'.join(clean_mac[i:i+2] for i in range(0, 12, 2))
        
        seconds_back = hours_back * 3600
        history_url = f"https://{self.host}/admin/API/mnt/AuthStatus/MACAddress/{clean_mac}/{seconds_back}/{record_limit}/All"
        
        try:
            headers = {"Accept": "application/xml"}
            response = self.api.session.get(history_url, headers=headers)
            
            if response.status_code == 200 and response.text:
                xml_data = re.sub(r'\sxmlns="[^"]+"', '', response.text, count=1)
                root = ET.fromstring(xml_data)
                
                for elem in root.iter():
                    if '}' in elem.tag:
                        elem.tag = elem.tag.split('}', 1)[1]
                        
                history = []
                for record in root.findall('.//authStatusElements'):
                    def get_xml_val(tag_name):
                        elem = record.find(tag_name)
                        return elem.text if elem is not None else "N/A"
                        
                    other_attr = get_xml_val("other_attr_string")
                    ep_match = re.search(r'dc-profile-name=([^,:]+)', other_attr) if other_attr != "N/A" else None
                    ep_profile = ep_match.group(1) if ep_match else "Unknown"

                    entry = {
                        "timestamp": get_xml_val("acs_timestamp"),
                        "status": "Passed" if get_xml_val("passed") == "true" else "Failed/Rejected",
                        "username": get_xml_val("user_name"),
                        "nas_name": get_xml_val("network_device_name"),
                        "nas_ip": get_xml_val("nas_ip_address"),
                        "switch_port": get_xml_val("nas_port_id"),
                        "authz_profile": get_xml_val("selected_azn_profiles"), 
                        "failure_reason": get_xml_val("failure_reason"),
                        "endpoint_profile": ep_profile,
                        "auth_method": get_xml_val("authentication_method"), 
                        "vlan": get_xml_val("vlan"),
                        "acs_session_id": get_xml_val("acs_session_id")  # NEW: Capture true alphanumeric string ID
                    }
                    history.append(entry)
                return history
            return []
        except Exception as e:
            logger.error(f"Failed to fetch historical auth logs for {mac_address}: {e}")
            return []

    def get_deep_auth_diagnostics(self, acs_session_id: str) -> Optional[str]:
        """
        Queries the detailed AuthenticationDetail MnT API using the explicit alphanumeric
        acs_session_id to pull down step-trace files safely without 404 errors.
        """
        if not acs_session_id or acs_session_id == "N/A":
            return None
            
        # Target the explicit logging detail directory path
        detail_url = f"https://{self.host}/admin/API/mnt/Log/AuthenticationDetail/Id/{acs_session_id}"
        try:
            headers = {"Accept": "application/xml"}
            response = self.api.session.get(detail_url, headers=headers)
            
            if response.status_code == 200 and response.text:
                xml_text = response.text
                
                if "multiple matching accounts" in xml_text or "Multiple matching accounts" in xml_text:
                    # Snips matching principal accounts cleanly regardless of brackets or spacing
                    account_matches = re.findall(
                        r"Client certificate does not match AD account certificate\s*[-:]*\s*([^<\s,]+)", 
                        xml_text, 
                        re.IGNORECASE
                    )
                    
                    if account_matches:
                        accounts = sorted(list(set(account_matches)))
                        return f"Identity resolution failed due to multiple matching AD accounts. Conflicting accounts detected: {', '.join(accounts)}."
                    else:
                        return "Identity resolution failed due to multiple matching AD accounts. (Specific account names could not be parsed from trace details)."
                        
            return None
        except Exception as e:
            logger.warning(f"Failed to fetch deep auth diagnostics for session {acs_session_id}: {e}")
            return None

    def diagnose_endpoint_session(self, mac_address: str) -> Dict[str, Any]:
        """
        Fetches live session data and layers it over the most recent authentication footprint.
        """
        mnt_mac = mac_address.replace(":", "").replace("-", "").upper()
        
        payload = {
            "mac_address": mac_address,
            "status": "UNKNOWN",
            "endpoint_profile": "N/A",
            "username": "N/A",
            "nas_name": "N/A",
            "nas_ip": "N/A",
            "switch_port": "N/A",
            "vlan": "N/A",
            "auth_method": "N/A",
            "triage_hint": "",
            "recent_history": self.get_auth_history(mac_address)
        }
        
        if payload["recent_history"]:
            last_auth = payload["recent_history"][0]
            payload.update({
                "nas_name": last_auth.get("nas_name", "N/A"),
                "nas_ip": last_auth.get("nas_ip", "N/A"),
                "switch_port": last_auth.get("switch_port", "N/A"),
                "endpoint_profile": last_auth.get("endpoint_profile", "Unknown"),
                "username": last_auth.get("username", "N/A"),
                "auth_method": last_auth.get("auth_method", "N/A"), 
                "vlan": last_auth.get("vlan", "N/A") 
            })
        
        try:
            headers = {"Accept": "application/xml"}
            session_url = f"https://{self.host}/admin/API/mnt/Session/MACAddress/{mnt_mac}"
            response = self.api.session.get(session_url, headers=headers)

            if response.status_code == 200 and response.text and "Session data is not available" not in response.text:
                root = ET.fromstring(response.text)
                def get_xml_val(tag_name):
                    elem = root.find(tag_name)
                    return elem.text if elem is not None else "N/A"

                payload["status"] = "ACTIVE_SESSION_FOUND"
                payload["triage_hint"] = f"Session is currently live and authenticated on {payload['nas_name']}."
                
                for key, xml_tag in [("username", "user_name"), ("nas_ip", "nas_ip_address"), 
                                     ("switch_port", "nas_port_id"), ("vlan", "vlan"), 
                                     ("auth_method", "authentication_method")]:
                    val = get_xml_val(xml_tag)
                    if val != "N/A": 
                        payload[key] = val
            else:
                raise ValueError("Session data is not available")
                
        except Exception as e:
            error_msg = str(e)
            
            if "Session data is not available" in error_msg or "500" in error_msg:
                payload["status"] = "NO_ACTIVE_SESSION"
                
                if payload["recent_history"]:
                    # Target the actual failed inner-tunnel transaction row containing a corporate principal ID
                    target_auth = payload["recent_history"][0]
                    for auth in payload["recent_history"]:
                        if "DENY" in auth.get("authz_profile", "").upper() or auth.get("status") == "Failed/Rejected":
                            username = auth.get("username", "")
                            if "@" in username or "\\" in username:
                                target_auth = auth
                                break
                    
                    # Execute our newly corrected standalone log deep-dive parameter fetch
                    deep_cause = self.get_deep_auth_diagnostics(target_auth.get("acs_session_id"))
                    
                    if deep_cause:
                        payload["triage_hint"] = f"Device actively hitting Default Deny on {target_auth.get('nas_name', 'N/A')}. {deep_cause}"
                    else:
                        payload["triage_hint"] = f"Device actively hitting Default Deny on {target_auth.get('nas_name', 'N/A')}. Failure Reason: {target_auth.get('failure_reason')}."
                else:
                    payload["triage_hint"] = "No connection footprint found. Port is dead or not configured for AAA."
            else:
                payload["status"] = "API_ERROR"
                payload["triage_hint"] = f"ISE Session lookup failed: {error_msg}"
            
        return payload

    def get_meraki_ndg_options(self) -> Dict[str, List[str]]:
        device_types = set()
        roles = set()
        try:
            response = self.api.network_device_group.get_network_device_group()
            if hasattr(response, 'response') and hasattr(response.response, 'SearchResult'):
                for res in response.response.SearchResult.resources:
                    name = res.name
                    if name.startswith("Device Type#All Device Types#Meraki#"):
                        device_types.add(name.split("#")[-1])
                    elif name.startswith("Designation#Designation#"):
                        roles.add(name.split("#")[-1])
            return {
                "device_types": sorted(list(device_types)) if device_types else ["APs", "Security Appliance", "Switch", "Z3"],
                "roles": sorted(list(roles)) if roles else ["IOP", "PSC", "Regional Office", "Teleworker"]
            }
        except Exception as exc:
            logger.error(f"Critical failure fetching NDGs from ISE: {exc}")
            return {
                "device_types": ["APs", "Security Appliance", "Switch", "Z3"],
                "roles": ["IOP", "PSC", "Regional Office", "Teleworker"]
            }