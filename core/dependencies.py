"""
core/dependencies.py

Cached dependency injection factories for infrastructure providers and services.
"""

from functools import lru_cache
import logging
import os
from typing import Any, Optional, Union

from fastapi import Depends

from core.config import get_settings, Settings
from providers.ise_provider import ISEProvider
from providers.meraki_provider import MerakiProvider, MerakiConfigurationError
from providers.catalyst_center_provider import CatalystCenterProvider
from providers.ldap_provider import LDAPProvider
from providers.sccm_provider import SCCMProvider
from providers.demo_providers import (
    DemoDNACProvider,
    DemoISEProvider,
    DemoLDAPProvider,
    DemoMerakiProvider,
)
from services.endpoint_diagnostic_service import EndpointDiagnosticService
from security.auth import enforce_routing_auth

logger = logging.getLogger("Net_Dashboard.Dependencies")


@lru_cache()
def get_ise_provider() -> Union[ISEProvider, DemoISEProvider]:
    settings = get_settings()
    if settings.DEMO_MODE:
        logger.info("DEMO_MODE enabled: initializing DemoISEProvider.")
        return DemoISEProvider()
    return ISEProvider(
        host=settings.ISE_PAN_IP,
        username=settings.ISE_ERS_USER,
        token=settings.ISE_ERS_PASS,
        version=settings.ISE_API_VERSION,
        verify_ssl=settings.ISE_VERIFY_SSL,
    )


@lru_cache()
def get_sccm_provider() -> SCCMProvider:
    return SCCMProvider()


@lru_cache()
def get_meraki_provider() -> Optional[Union[MerakiProvider, DemoMerakiProvider]]:
    settings = get_settings()
    if settings.DEMO_MODE:
        logger.info("DEMO_MODE enabled: initializing DemoMerakiProvider.")
        return DemoMerakiProvider()
    try:
        provider = MerakiProvider(
            api_key=settings.MERAKI_API_KEY,
            organization_id=settings.MERAKI_ORG_ID,
            lookback_days=settings.MERAKI_LOOKBACK_DAYS,
            cache_ttl_seconds=settings.MERAKI_CACHE_TTL_SECONDS,
            max_retries=settings.MERAKI_MAX_RETRIES,
            request_timeout_seconds=settings.MERAKI_REQUEST_TIMEOUT_SECONDS,
        )
        logger.info("Meraki provider initialized successfully.")
        return provider
    except MerakiConfigurationError as exc:
        logger.warning("Meraki provider disabled due to configuration issue: %s", exc)
        return None
    except Exception as exc:
        logger.exception("Unexpected failure initializing Meraki provider: %s", exc)
        return None


@lru_cache()
def get_dnac_provider() -> Optional[Union[CatalystCenterProvider, DemoDNACProvider]]:
    settings = get_settings()
    if settings.DEMO_MODE:
        logger.info("DEMO_MODE enabled: initializing DemoDNACProvider.")
        return DemoDNACProvider()
    if settings.DNAC_IP:
        try:
            provider = CatalystCenterProvider(
                host=settings.DNAC_IP,
                username=settings.DNAC_USER,
                password=settings.DNAC_PASS,
                verify_ssl=settings.DNAC_VERIFY_SSL
            )
            logger.info("Catalyst Center provider initialized successfully.")
            return provider
        except Exception as exc:
            logger.exception("Unexpected failure initializing Catalyst Center provider: %s", exc)
            return None
    logger.info("Catalyst Center provider skipped (DNAC_IP not set).")
    return None


@lru_cache()
def get_ldap_provider() -> Optional[Union[LDAPProvider, DemoLDAPProvider]]:
    settings = get_settings()
    if settings.DEMO_MODE:
        logger.info("DEMO_MODE enabled: initializing DemoLDAPProvider.")
        return DemoLDAPProvider()
    if settings.LDAP_SERVER_URI or os.getenv("LDAP_SERVER_URI"):
        try:
            provider = LDAPProvider()
            logger.info("LDAP provider initialized successfully.")
            return provider
        except Exception as exc:
            logger.exception("Unexpected failure initializing LDAP provider: %s", exc)
            return None
    logger.info("LDAP provider skipped (LDAP_SERVER_URI not set).")
    return None


def get_diagnostic_service(
    ise: Any = Depends(get_ise_provider),
    meraki: Optional[Any] = Depends(get_meraki_provider),
    dnac: Optional[Any] = Depends(get_dnac_provider),
    ldap: Optional[Any] = Depends(get_ldap_provider),
) -> EndpointDiagnosticService:
    return EndpointDiagnosticService(
        ise_provider=ise,
        meraki_provider=meraki,
        dnac_provider=dnac,
        ldap_provider=ldap,
    )
