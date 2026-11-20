"""
tests/test_webex_verification_service.py

Comprehensive unit tests for Phase 5:
Post-Cutover Verification & License Reconciliation Service and API Endpoints.
"""

import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from main import app
from models.telecom import (
    ClusterTarget,
    PostCutoverVerificationRequest,
    PostCutoverVerificationResponse,
    LicenseReconciliationResponse,
)
from providers.webex_calling_provider import WebexCallingProvider
from providers.cucm_axl_provider import CUCMAXLProvider
from services.webex_verification_service import WebexVerificationService
from security.roles import Role
from security.auth import get_current_user, CurrentUser


@pytest.fixture
def mock_admin_user():
    return CurrentUser(
        username="netadmin",
        role=Role.NETWORK_ADMIN,
        permissions=["telecom:audit", "telecom:provision", "telecom:write"],
    )


@pytest.fixture
def mock_webex_provider():
    provider = MagicMock(spec=WebexCallingProvider)
    
    # Mock user query
    provider.get_person_by_email.return_value = {
        "id": "person_wxc_123",
        "displayName": "Jane Doe",
        "emails": ["jane.doe@corp.internal"],
    }
    
    provider.get_person_by_id.return_value = {
        "id": "person_wxc_123",
        "displayName": "Jane Doe",
        "extension": "4199",
        "phoneNumbers": [
            {"type": "work", "value": "+15125554199", "primary": True}
        ],
        "callingData": {
            "callerId": {
                "selected": "DIRECT_LINE",
                "directNumber": "+15125554199",
            },
            "location": {
                "id": "loc_austin_123",
                "name": "Austin Branch",
                "address": {
                    "address1": "100 Main St",
                    "city": "Austin",
                    "state": "TX",
                    "postalCode": "78701",
                    "country": "US",
                },
            },
        },
    }

    # Mock device query
    provider.get_device_by_mac.return_value = {
        "id": "dev_webex_999",
        "mac": "001122334455",
        "displayName": "Jane Doe - Cisco 8851",
        "product": "Cisco 8851 MPP",
        "connectionStatus": "connected",
        "softwareVersion": "12.0.1MPP",
    }

    # Mock caller ID
    provider.get_caller_id.return_value = {
        "selected": "DIRECT_LINE",
        "customExternalCallerIdName": "Jane Doe",
    }

    # Mock location details
    provider.get_location_details.return_value = {
        "id": "loc_austin_123",
        "name": "Austin Branch",
        "address": {
            "address1": "100 Main St",
            "city": "Austin",
            "state": "TX",
            "postalCode": "78701",
        },
    }

    # Mock voicemail settings
    provider.get_voicemail_settings.return_value = {
        "enabled": True,
        "messageStorage": {"mwi": {"enabled": True}},
        "emailCopyOfMessage": True,
    }

    # Mock licenses
    provider.get_licenses.return_value = [
        {
            "id": "lic_wxc_pro",
            "name": "Webex Calling - Professional",
            "totalUnits": 500,
            "consumedUnits": 25,
        },
        {
            "id": "lic_wxc_ws",
            "name": "Webex Calling - Workspace",
            "totalUnits": 100,
            "consumedUnits": 10,
        },
    ]

    return provider


@pytest.fixture
def mock_axl_provider():
    axl = MagicMock(spec=CUCMAXLProvider)
    axl.version = "12.5"

    # Translation pattern lookup returns LGW pattern for 4199
    def mock_get_tp(pattern, partition=None):
        if pattern == "4199":
            return {
                "pkid": "uuid_tp_4199",
                "dnorpattern": "4199",
                "partition_name": partition or "Internal_PT",
                "css_name": "To_LGW_CSS",
            }
        return None

    axl.get_translation_pattern.side_effect = mock_get_tp
    axl.get_route_pattern.return_value = None

    # Device lookup: device is already decommissioned from CUCM (returns None)
    axl.get_device_by_name_or_mac.return_value = None

    return axl


@pytest.fixture
def mock_redsky_provider():
    provider = MagicMock()
    provider.is_configured.return_value = True
    provider.get_device_user_by_email.return_value = {
        "id": "r_usr_123",
        "phoneNumber": "+15125554199",
        "userOptions": "MYE911_ENABLED",
    }
    return provider


def test_verify_cutover_all_pass(mock_webex_provider, mock_axl_provider, mock_redsky_provider):
    service = WebexVerificationService(
        webex_provider=mock_webex_provider,
        axl_provider=mock_axl_provider,
        redsky_provider=mock_redsky_provider,
        cluster_name="PRIMARY",
    )

    resp = service.verify_cutover(
        user_email="jane.doe@corp.internal",
        mac_address="00:11:22:33:44:55",
        extension="4199",
    )

    assert isinstance(resp, PostCutoverVerificationResponse)
    assert resp.readiness_status == "CUTOVER_VERIFIED"
    assert resp.checks_failed == 0
    assert resp.checks_passed >= 7

    check_map = {c.check_id: c for c in resp.checks}
    assert "DEV-REG-01" in check_map
    assert check_map["DEV-REG-01"].status == "PASS"

    assert "DEV-FW-02" in check_map
    assert check_map["DEV-FW-02"].status == "PASS"

    assert "CALL-DID-03" in check_map
    assert check_map["CALL-DID-03"].status == "PASS"

    assert "CALL-CID-04" in check_map
    assert check_map["CALL-CID-04"].status == "PASS"

    assert "HYBRID-05" in check_map
    assert check_map["HYBRID-05"].status == "PASS"

    assert "E911-06" in check_map
    assert check_map["E911-06"].status == "PASS"

    assert "VM-MWI-07" in check_map
    assert check_map["VM-MWI-07"].status == "PASS"

    assert "CUCM-DEC-08" in check_map
    assert check_map["CUCM-DEC-08"].status == "PASS"


def test_verify_cutover_enterprise_firmware_warning(mock_webex_provider, mock_axl_provider):
    # Endpoint is registered but still running Enterprise firmware
    mock_webex_provider.get_device_by_mac.return_value = {
        "id": "dev_webex_888",
        "mac": "001122334455",
        "displayName": "Cisco 7841 Handset",
        "product": "Cisco 7841",
        "connectionStatus": "online",
        "softwareVersion": "sip78xx.12-5-1SR1-1",
    }

    service = WebexVerificationService(
        webex_provider=mock_webex_provider,
        axl_provider=mock_axl_provider,
    )

    resp = service.verify_cutover(
        mac_address="001122334455",
        extension="4199",
    )

    check_map = {c.check_id: c for c in resp.checks}
    assert "DEV-FW-02" in check_map
    assert check_map["DEV-FW-02"].status == "WARNING"
    assert "Enterprise firmware" in check_map["DEV-FW-02"].details


def test_verify_cutover_cucm_device_still_registered(mock_webex_provider, mock_axl_provider):
    # Device was NOT decommissioned from CUCM
    mock_axl_provider.get_device_by_name_or_mac.return_value = {
        "device_name": "SEP001122334455",
        "model": "Cisco 8851",
        "status": "Registered",
    }

    service = WebexVerificationService(
        webex_provider=mock_webex_provider,
        axl_provider=mock_axl_provider,
    )

    resp = service.verify_cutover(
        mac_address="001122334455",
        extension="4199",
    )

    check_map = {c.check_id: c for c in resp.checks}
    assert "CUCM-DEC-08" in check_map
    assert check_map["CUCM-DEC-08"].status == "WARNING"
    assert "still registered in CUCM" in check_map["CUCM-DEC-08"].details
    assert resp.readiness_status == "ACTION_REQUIRED"


def test_verify_cutover_hybrid_pattern_missing(mock_webex_provider, mock_axl_provider):
    # Both Route Pattern and Translation Pattern are missing in CUCM
    mock_axl_provider.get_route_pattern.return_value = None
    mock_axl_provider.get_translation_pattern.return_value = None

    service = WebexVerificationService(
        webex_provider=mock_webex_provider,
        axl_provider=mock_axl_provider,
    )

    resp = service.verify_cutover(
        extension="9999",  # Pattern not in CUCM
    )

    check_map = {c.check_id: c for c in resp.checks}
    assert "HYBRID-05" in check_map
    assert check_map["HYBRID-05"].status == "WARNING"
    assert "No CUCM Route Pattern or Translation Pattern found" in check_map["HYBRID-05"].details


def test_verify_cutover_hybrid_route_pattern_pass(mock_webex_provider, mock_axl_provider):
    # Route pattern points to Route List WebexCUBE_RL
    mock_axl_provider.get_route_pattern.return_value = {
        "pkid": "uuid_rp_801662",
        "pattern": "801662",
        "partition_name": "Global-PT-Internal",
        "destination_name": "WebexCUBE_RL",
        "destination_type": "Route List",
    }
    mock_axl_provider.get_translation_pattern.return_value = None

    service = WebexVerificationService(
        webex_provider=mock_webex_provider,
        axl_provider=mock_axl_provider,
    )

    resp = service.verify_cutover(extension="801662")
    check_map = {c.check_id: c for c in resp.checks}
    assert "HYBRID-05" in check_map
    assert check_map["HYBRID-05"].status == "PASS"
    assert "WebexCUBE_RL" in check_map["HYBRID-05"].details
    assert "Route List" in check_map["HYBRID-05"].details
    assert "Global-PT-Internal" in check_map["HYBRID-05"].details


def test_verify_cutover_extension_only_warning(mock_webex_provider, mock_axl_provider):
    # User has extension but NO direct PSTN DID
    mock_webex_provider.get_person_by_id.return_value = {
        "id": "person_wxc_ext_only",
        "displayName": "Ext Only User",
        "extension": "5500",
        "phoneNumbers": [],
        "callingData": {},
    }

    service = WebexVerificationService(
        webex_provider=mock_webex_provider,
        axl_provider=mock_axl_provider,
    )

    resp = service.verify_cutover(
        user_email="extonly@corp.internal",
    )

    check_map = {c.check_id: c for c in resp.checks}
    assert "CALL-DID-03" in check_map
    assert check_map["CALL-DID-03"].status == "WARNING"
    assert "no external direct DID assigned" in check_map["CALL-DID-03"].details


def test_reconcile_licenses(mock_webex_provider, mock_axl_provider):
    service = WebexVerificationService(
        webex_provider=mock_webex_provider,
        axl_provider=mock_axl_provider,
        cluster_name="PRIMARY",
    )

    resp = service.reconcile_licenses()
    assert isinstance(resp, LicenseReconciliationResponse)
    assert resp.status == "SUCCESS"
    assert resp.cluster == "PRIMARY"
    assert resp.total_reclaimed_cucm_licenses > 0
    assert resp.total_active_webex_licenses > 0
    assert len(resp.items) == 3

    # Professional tier matches
    pro_item = next(i for i in resp.items if i.category == "Professional Knowledge Worker")
    assert pro_item.cucm_smart_license == "Enhanced UCL / CUWL Standard"
    assert pro_item.reclaimed_count == 25
    assert pro_item.webex_consumed_count == 25
    assert pro_item.status == "BALANCED"


def test_api_post_cutover_verification(mock_admin_user, mock_webex_provider, mock_axl_provider, mock_redsky_provider):
    app.dependency_overrides[get_current_user] = lambda: mock_admin_user
    client = TestClient(app)

    try:
        with patch.object(WebexCallingProvider, "get_person_by_email", return_value=mock_webex_provider.get_person_by_email.return_value), \
             patch.object(WebexCallingProvider, "get_person_by_id", return_value=mock_webex_provider.get_person_by_id.return_value), \
             patch.object(WebexCallingProvider, "get_device_by_mac", return_value=mock_webex_provider.get_device_by_mac.return_value), \
             patch.object(WebexCallingProvider, "get_caller_id", return_value=mock_webex_provider.get_caller_id.return_value), \
             patch.object(WebexCallingProvider, "get_location_details", return_value=mock_webex_provider.get_location_details.return_value), \
             patch.object(WebexCallingProvider, "get_voicemail_settings", return_value=mock_webex_provider.get_voicemail_settings.return_value), \
             patch("providers.telecom_factory.TelecomClusterRegistry.get_cucm_provider", return_value=mock_axl_provider), \
             patch("routers.telecom.RedskyProvider", return_value=mock_redsky_provider):
            resp = client.post(
                "/api/v1/telecom/verification/post-cutover",
                headers={"X-Webex-Access-Token": "mock_token_123"},
                json={
                    "user_email": "jane.doe@corp.internal",
                    "mac_address": "001122334455",
                    "extension": "4199",
                    "cluster": "PRIMARY",
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "SUCCESS"
            assert data["readiness_status"] == "CUTOVER_VERIFIED"
            assert len(data["checks"]) == 8
    finally:
        app.dependency_overrides.clear()


def test_api_license_reconciliation(mock_admin_user, mock_webex_provider, mock_axl_provider):
    app.dependency_overrides[get_current_user] = lambda: mock_admin_user
    client = TestClient(app)

    try:
        with patch("providers.webex_calling_provider.WebexCallingProvider.get_licenses", return_value=mock_webex_provider.get_licenses.return_value), \
             patch("providers.telecom_factory.TelecomClusterRegistry.get_cucm_provider", return_value=mock_axl_provider):
            resp = client.get(
                "/api/v1/telecom/verification/license-reconciliation?cluster=PRIMARY",
                headers={"X-Webex-Access-Token": "mock_token_123"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "SUCCESS"
            assert data["cluster"] == "PRIMARY"
            assert len(data["items"]) == 3
            assert data["total_active_webex_licenses"] > 0
    finally:
        app.dependency_overrides.clear()


def test_verify_cutover_e911_redsky_active(mock_webex_provider, mock_axl_provider):
    mock_redsky = MagicMock()
    mock_redsky.is_configured.return_value = True
    mock_redsky.get_device_user_by_email.return_value = {
        "id": "r-user-1",
        "email": "lgonzales@corp.internal",
        "phoneNumber": {"phoneNumber": "5124391662"},
        "userOptions": "MYE911_ENABLED",
    }

    service = WebexVerificationService(
        webex_provider=mock_webex_provider,
        axl_provider=mock_axl_provider,
        redsky_provider=mock_redsky,
    )

    resp = service.verify_cutover(
        user_email="lgonzales@corp.internal",
        mac_address="001122334455",
        extension="801662",
    )

    check_map = {c.check_id: c for c in resp.checks}
    assert "E911-06" in check_map
    assert check_map["E911-06"].status == "PASS"
    assert "RedSky MyE911 active for '5124391662'" in check_map["E911-06"].details


def test_verify_cutover_e911_redsky_missing_phone_warning(mock_webex_provider, mock_axl_provider):
    mock_redsky = MagicMock()
    mock_redsky.is_configured.return_value = True
    mock_redsky.get_device_user_by_email.return_value = {
        "id": "r-user-2",
        "email": "lgonzales@corp.internal",
        "phoneNumber": None,
        "userOptions": "NONE",
    }

    service = WebexVerificationService(
        webex_provider=mock_webex_provider,
        axl_provider=mock_axl_provider,
        redsky_provider=mock_redsky,
    )

    resp = service.verify_cutover(
        user_email="lgonzales@corp.internal",
        mac_address="001122334455",
        extension="801662",
    )

    check_map = {c.check_id: c for c in resp.checks}
    assert "E911-06" in check_map
    assert check_map["E911-06"].status == "WARNING"
    assert "phone number is unassigned" in check_map["E911-06"].details

