import os
import time
import logging
import requests
from requests.auth import HTTPBasicAuth
from typing import Dict, Any, Optional

logger = logging.getLogger("Catalyst_Center_Provider")

class CatalystCenterProvider:
    """
    Cisco Catalyst Center (DNAC) API Provider.
    Manages token lifecycle, proactive expiration renewal, and real-time client topology/telemetry lookups.
    """
    def __init__(self, host: str, username: str, password: str, verify_ssl: bool = True):
        self.host = host
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.base_url = f"https://{self.host}"
        
        self._token: Optional[str] = None
        self._token_timestamp: float = 0.0
        # DNAC tokens expire after 3600s (1 hour); proactively refresh at 50 min (3000s)
        self._token_ttl_seconds: int = 3000
        self.session = requests.Session()
        self.session.verify = self.verify_ssl

    def _is_token_valid(self) -> bool:
        """Checks if current token exists and has not exceeded TTL threshold."""
        if not self._token:
            return False
        return (time.time() - self._token_timestamp) < self._token_ttl_seconds

    def _get_token(self) -> str:
        """Exchanges Basic Auth credentials for a short-lived X-Auth-Token."""
        auth_url = f"{self.base_url}/dna/system/api/v1/auth/token"
        
        try:
            response = self.session.post(
                auth_url, 
                auth=HTTPBasicAuth(self.username, self.password),
                timeout=10
            )
            response.raise_for_status()
            self._token = response.json().get("Token")
            self._token_timestamp = time.time()
            return self._token
        except Exception as exc:
            logger.error("Catalyst Center authentication failed: %s", exc)
            raise

    def _request(self, endpoint: str) -> Dict[str, Any]:
        """Wrapper to handle token injection, proactive refresh, and automatic retries on 401s."""
        if not self._is_token_valid():
            self._get_token()

        url = f"{self.base_url}{endpoint}"
        headers = {"X-Auth-Token": self._token, "Accept": "application/json"}
        
        response = self.session.get(url, headers=headers, timeout=15)
        
        if response.status_code == 401:
            headers["X-Auth-Token"] = self._get_token()
            response = self.session.get(url, headers=headers, timeout=15)
            
        response.raise_for_status()
        return response.json()

    def get_client_topology_by_mac(self, mac_address: str) -> Dict[str, Any]:
        """
        Queries DNAC for real-time switch port, wireless health, and topology metrics.
        """
        clean_mac = mac_address.replace("-", ":").lower()
        endpoint = f"/dna/intent/api/v1/client-detail?macAddress={clean_mac}"
        
        try:
            data = self._request(endpoint)
            topology = data.get("topology", {}) or {}
            detail = data.get("detail", {}) or {}
            connection = data.get("connectionInfo", {}) or {} # <-- Added to catch alternate schemas
            
            if not topology and not detail and not connection:
                return {"dnac_found": False}

            # Safely parse Assurance Health Score
            health_score = "N/A"
            raw_health = detail.get("healthScore")
            if isinstance(raw_health, dict):
                health_score = raw_health.get("score", "N/A")
            elif isinstance(raw_health, list) and raw_health:
                health_score = raw_health[0].get("score", "N/A")
            elif isinstance(raw_health, (int, float)):
                health_score = int(raw_health)

            client_type = str(detail.get("clientType") or connection.get("hostType") or "UNKNOWN").upper()

            return {
                "dnac_found": True,
                "client_type": client_type,
                "nas_name": topology.get("connectedDeviceName") or detail.get("deviceName") or connection.get("nwDeviceName") or "N/A",
                "nas_ip": topology.get("connectedDeviceIpAddress") or detail.get("deviceIpAddress") or connection.get("nwDeviceIp") or "N/A",
                "switch_port": topology.get("port") or detail.get("port") or connection.get("port") or "N/A",
                "vlan": detail.get("vlanId") or "N/A",
                "status": detail.get("connectionStatus") or "Unknown",
                "health_score": health_score,
                # Catch multiple IP key variations:
                "ip_address": detail.get("ipAddress") or detail.get("hostIpV4") or "N/A", 
                "ssid": detail.get("ssid") or (connection.get("nwDeviceName") if client_type == "WIRELESS" else "N/A"),
                "rssi": detail.get("rssi") if detail.get("rssi") is not None else "N/A",
                "snr": detail.get("snr") if detail.get("snr") is not None else "N/A",
                "frequency": detail.get("frequency") or "N/A",
                "channel": detail.get("channel") or "N/A",
                "speed": topology.get("interfaceSpeed") or "N/A",
                "duplex": topology.get("duplexMode") or "N/A"
            }
        except Exception as exc:
            logger.warning("Catalyst Center client lookup failed for %s: %s", mac_address, exc)
            return {"dnac_found": False, "error": str(exc)}