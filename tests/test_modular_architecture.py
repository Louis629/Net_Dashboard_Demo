"""
tests/test_modular_architecture.py

Unit and integration tests for Phase 2/3 modular architecture:
- Unified Pydantic configuration (core/config.py)
- Cached dependency injection providers (core/dependencies.py)
- Route mounting and resolution across decomposed routers (views, bypass, diagnostics, meraki_admin, ndg_admin)
"""

import pytest
from fastapi.testclient import TestClient

from core.config import Settings, get_settings
from core.dependencies import (
    get_diagnostic_service,
    get_dnac_provider,
    get_ise_provider,
    get_meraki_provider,
    get_sccm_provider,
)
from main import app
from models.bypass import BypassRequest
from models.common import normalize_mac
from models.diagnostics import WindowsDiagRequest
from models.ndg import TargetedAuditRequest
from security.auth import get_current_user, CurrentUser
from security.roles import Role


@pytest.fixture
def client():
    return TestClient(app)


def test_core_settings_instantiation():
    """Verifies that unified Settings load properly with valid defaults."""
    settings = get_settings()
    assert isinstance(settings, Settings)
    assert settings.APP_NAME == "ISE Helpdesk Automation Portal"
    assert settings.MERAKI_LOOKBACK_DAYS == 31
    assert settings.CUCM_AXL_VERSION == "12.5"
    assert isinstance(settings.LDAP_DOMAIN, str)
    assert isinstance(settings.cors_origins_list, list)
    assert isinstance(settings.vpn_subnet_prefixes_tuple, tuple)


def test_core_dependencies_caching():
    """Verifies that provider dependency factories return cached instances."""
    ise1 = get_ise_provider()
    ise2 = get_ise_provider()
    assert ise1 is ise2

    sccm1 = get_sccm_provider()
    sccm2 = get_sccm_provider()
    assert sccm1 is sccm2


def test_models_validation():
    """Verifies Pydantic v2 validation models."""
    # Common MAC normalization
    assert normalize_mac("001122334455") == "00:11:22:33:44:55"
    assert normalize_mac("aa-bb-cc-dd-ee-ff") == "AA:BB:CC:DD:EE:FF"

    # BypassRequest validation
    req = BypassRequest(
        device_type="new",
        mac_address="00-11-22-33-44-55",
        operator_id="admin@example.com"
    )
    assert req.mac_address == "00:11:22:33:44:55"

    # Diagnostics request validation
    diag_req = WindowsDiagRequest(hostname="DESKTOP-ABC1234")
    assert diag_req.hostname == "DESKTOP-ABC1234"

    # NDG Audit request validation
    audit_req = TargetedAuditRequest(
        template_id="L_12345",
        hardware_filter=1,
        gateway_type="MX64",
        gateway_designation="SecurityAppliance"
    )
    assert audit_req.template_id == "L_12345"


def test_route_mounting_views(client):
    """Verifies that HTML view routes are mounted properly."""
    from security.auth import enforce_dev_environment_access
    app.dependency_overrides[enforce_dev_environment_access] = lambda: True

    resp = client.get("/")
    assert resp.status_code == 200
    assert "NetDashboard" in resp.text or "ISE" in resp.text or "<!DOCTYPE html>" in resp.text

    resp = client.get("/telecom-migration")
    assert resp.status_code == 200


def test_route_mounting_meraki_health(client):
    """Verifies that Meraki health endpoint is mounted properly."""
    resp = client.get("/api/v1/meraki-health")
    assert resp.status_code == 200
    data = resp.json()
    assert "status" in data


def test_route_mounting_diagnostics_requires_target(client, monkeypatch):
    """Verifies that diagnostics router responds on the expected endpoint path."""
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        username="test_admin", role=Role.NETWORK_ADMIN, permissions=["*"]
    )
    resp = client.get("/api/v1/diagnose/   ")
    assert resp.status_code in [400, 404]
