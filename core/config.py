"""
core/config.py

Centralized Pydantic application configuration and environment settings.
"""

from functools import lru_cache
import os
from typing import List, Optional, Tuple
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # App & Environment
    APP_NAME: str = "ISE Helpdesk Automation Portal"
    ENVIRONMENT: str = "DEV"
    DEMO_MODE: bool = False
    DEV_DEFAULT_USER: str = "dev-engineer@corp.internal"
    AUTH_METHOD: str = "OKTA"
    LOG_DIR: str = "logs"
    SECRET_KEY: str = "fallback_dev_secret_key_change_me"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480

    # Portal & Network Boundaries
    CORS_ORIGINS: str = "http://localhost:8000"
    VPN_SUBNET_PREFIXES: str = ""
    DEFAULT_EMAIL_DOMAIN: str = ""

    # Okta Configuration
    OKTA_ISSUER: str = "https://your-preview-domain.oktapreview.com"
    OKTA_CLIENT_ID: str = ""
    OKTA_AUDIENCE: str = ""
    OKTA_GROUP_NETWORK_ADMIN: str = "Network_Admin"
    OKTA_GROUP_TECH_SERVICES: str = "Tech_Services"
    OKTA_GROUP_HELPDESK: str = "Helpdesk"

    # Active Directory Groups
    AD_GROUP_NETWORK_ADMIN: str = "AD_NetAdmin_Group"
    AD_GROUP_TECH_SERVICES: str = "AD_TechServices_Group"
    AD_GROUP_HELPDESK: str = "AD_Helpdesk_Group"

    # LDAP Configuration
    LDAP_SERVER_URI: str = ""
    LDAP_DOMAIN: str = ""
    LDAP_SEARCH_BASE: str = ""
    AD_NETBIOS_DOMAIN: str = ""
    LDAP_BIND_USER: str = ""
    LDAP_BIND_PASSWORD: str = ""
    LDAP_VERIFY_SSL: bool = True

    # Cisco ISE Configuration
    ISE_PAN_IP: str = ""
    ISE_ERS_USER: str = ""
    ISE_ERS_PASS: str = ""
    ISE_API_VERSION: str = "3.3_patch_1"
    ISE_VERIFY_SSL: bool = True
    RADIUS_SHARED_SECRET: str = ""
    ISE_DEFAULT_LOCATION_NDG: str = "Location#All Locations#Corporate"
    ISE_NDG_TARGET_VLAN_ID: int = 21
    ISE_NDG_REPORT_DIR: str = "reports"
    ISE_NDG_UPDATE_RADIUS_SECRET: bool = False
    ISE_NDG_REQUEST_TIMEOUT_SECONDS: int = 30
    ISE_NDG_ERS_PAGE_SIZE: int = 100

    # Cisco Meraki Configuration
    MERAKI_API_KEY: str = ""
    MERAKI_ORG_ID: str = ""
    MERAKI_LOOKBACK_DAYS: int = 31
    MERAKI_CACHE_TTL_SECONDS: int = 1800
    MERAKI_MAX_RETRIES: int = 1
    MERAKI_REQUEST_TIMEOUT_SECONDS: int = 10

    # Cisco Catalyst Center (DNA Center)
    DNAC_IP: str = ""
    DNAC_USER: str = ""
    DNAC_PASS: str = ""
    DNAC_VERIFY_SSL: bool = True

    # Microsoft SCCM / ConfigMgr
    SCCM_SITE_SERVER: str = ""
    SCCM_SITE_CODE: str = ""
    SCCM_LOG_PATH: str = ""
    SCCM_TIMEOUT_SECONDS: int = 45
    SCCM_REIMAGING_GROUP: str = "AD_REIMAGING_GROUP"

    # Diagnostics & ZTP DHCP
    IPERF_SERVERS: str = ""
    TEAP_XML_PROFILES: str = ""
    TEAP_CA_FILTER: str = ""
    ZTP_DHCP_PRIMARY_SERVER: str = ""
    ZTP_DHCP_PARTNER_SERVER: str = ""
    ZTP_DHCP_FAILOVER_RELATIONSHIP: str = ""
    ZTP_DHCP_DNS_SERVERS: str = ""
    ZTP_DHCP_DNS_DOMAIN: str = ""
    ZTP_DHCP_LEASE_DAYS: int = 1

    # Redis / Valkey & Celery
    REDIS_URL: str = "redis://127.0.0.1:6379/0"
    VALKEY_URL: Optional[str] = None
    CELERY_BROKER_URL: Optional[str] = None
    CELERY_RESULT_BACKEND: Optional[str] = None

    # Telecom / CUCM / UCCX / Webex
    PRIMARY_CUCM_AXL_HOST: str = ""
    HAWAII_CUCM_AXL_HOST: str = ""
    CUCM_PAN_IP: str = ""
    CUCM_AXL_HOST: str = ""
    CUCM_AXL_USER: str = ""
    CUCM_AXL_PASS: str = ""
    CUCM_AXL_VERSION: str = "12.5"
    CUCM_AXL_VERIFY_SSL: bool = True
    CUCM_AXL_TIMEOUT_SECONDS: int = 30
    CUCM_AXL_CONNECT_TIMEOUT_SECONDS: float = 3.0

    UCCX_HOST: str = ""
    UCCX_USER: str = ""
    UCCX_PASS: str = ""
    UCCX_VERIFY_SSL: bool = True
    UCCX_TIMEOUT_SECONDS: int = 15
    UCCX_CONNECT_TIMEOUT_SECONDS: float = 3.0

    CUC_HOST: str = ""
    CUC_USER: str = ""
    CUC_PASS: str = ""
    CUC_VERIFY_SSL: bool = True
    CUC_TIMEOUT_SECONDS: int = 30
    CUC_PAGE_SIZE: int = 100

    WEBEX_ACCESS_TOKEN: str = ""
    WEBEX_VERIFY_SSL: bool = True
    WEBEX_ORG_ID: str = ""
    WEBEX_CALLING_LICENSE_ID: str = ""
    WEBEX_DEFAULT_LOCATION_ID: str = ""
    VOICE_SUPERNET_CIDR: str = ""

    # RedSky E911 Anywhere
    REDSKY_URL: str = ""
    REDSKY_API: str = ""
    REDSKY_API_KEY: str = ""
    REDSKY_BASE_URL: str = "https://api.wxc.e911cloud.com"

    model_config = SettingsConfigDict(
        env_file=str(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")),
        extra="ignore",
    )

    @property
    def cors_origins_list(self) -> List[str]:
        """Returns configured CORS origins as a stripped list."""
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    @property
    def vpn_subnet_prefixes_list(self) -> List[str]:
        """Returns configured VPN IPv4 prefix strings as a stripped list."""
        return [prefix.strip() for prefix in self.VPN_SUBNET_PREFIXES.split(",") if prefix.strip()]

    @property
    def vpn_subnet_prefixes_tuple(self) -> Tuple[str, ...]:
        """Returns configured VPN IPv4 prefix strings as a stripped tuple."""
        return tuple(self.vpn_subnet_prefixes_list)

    @property
    def ztp_dhcp_dns_servers_list(self) -> List[str]:
        """Returns configured ZTP DHCP DNS server IPs as a stripped list."""
        return [server.strip() for server in self.ZTP_DHCP_DNS_SERVERS.split(",") if server.strip()]

    @property
    def effective_ztp_dhcp_dns_domain(self) -> str:
        """Returns the DNS domain for ZTP DHCP scopes, falling back to LDAP_DOMAIN."""
        return (self.ZTP_DHCP_DNS_DOMAIN or self.LDAP_DOMAIN).strip()


@lru_cache()
def get_settings() -> Settings:
    """Returns cached application settings instance."""
    return Settings()