"""
tests/test_webex_device_onboarding_service.py

Comprehensive unit tests for Phase 3:
Webex Calling Device Onboarding & MPP Transition Service and API Endpoints.
"""

import pytest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient

from main import app
from models.telecom import DeviceOnboardingItem, DeviceOnboardingRequest
from providers.webex_calling_provider import WebexCallingProvider, WebexAPIError
from services.webex_device_onboarding_service import (
    WebexDeviceOnboardingService,
    CiscoDeviceClassifier,
)
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
    provider.get_locations.return_value = [
        {"id": "loc_austin_123", "name": "Austin Branch"},
        {"id": "loc_dallas_456", "name": "Dallas Branch"},
    ]
    provider.get_person_by_email.side_effect = lambda email: {
        "id": f"person_{email.split('@')[0]}",
        "displayName": email.split("@")[0].title(),
        "emails": [email],
    } if "unknown" not in email else None

    provider.get_workspace_by_name.side_effect = lambda name: {
        "id": f"ws_{name.lower().replace(' ', '_')}",
        "displayName": name,
    } if "Unknown" not in name else None

    provider.generate_device_activation_code.return_value = {
        "code": "1234567890123456",
        "expiryTime": "2026-10-29T12:00:00.000Z",
    }
    provider.provision_mpp_device.return_value = {
        "id": "dev_mpp_user_999",
        "mac": "001122334455",
        "model": "Cisco 8851 MPP",
    }
    provider.provision_workspace_device.return_value = {
        "id": "dev_mpp_ws_888",
        "mac": "AABBCCDDEEFF",
        "model": "Cisco 7841 MPP",
    }
    return provider


def test_mac_normalization():
    norm = WebexDeviceOnboardingService.normalize_mac
    assert norm("00:11:22:33:44:55") == "001122334455"
    assert norm("00-11-22-33-44-55") == "001122334455"
    assert norm("0011.2233.4455") == "001122334455"
    assert norm("aabbccddeeff") == "AABBCCDDEEFF"

    with pytest.raises(ValueError, match="Invalid MAC address"):
        norm("0011223344")  # Too short

    with pytest.raises(ValueError, match="Invalid MAC address"):
        norm("00112233445566")  # Too long

    with pytest.raises(ValueError, match="Invalid MAC address"):
        norm("0011223344ZZ")  # Non-hex


def test_cisco_device_classifier():
    # Native MPP
    c1 = CiscoDeviceClassifier.classify("Cisco 8851 3PCC")
    assert c1["category"] == "NATIVE_MPP"
    assert c1["is_mpp_supported"] is True
    assert c1["recommended_mode"] == "NATIVE_MPP"

    c2 = CiscoDeviceClassifier.classify("CP-6821-3PCC-K9=")
    assert c2["category"] == "NATIVE_MPP"

    c3 = CiscoDeviceClassifier.classify("Webex Desk Pro")
    assert c3["category"] == "NATIVE_MPP"

    # Enterprise Convertible
    c4 = CiscoDeviceClassifier.classify("Cisco 8851")
    assert c4["category"] == "ENTERPRISE_CONVERTIBLE"
    assert c4["is_conversion_supported"] is True
    assert c4["recommended_mode"] == "ENTERPRISE_TO_MPP"

    c5 = CiscoDeviceClassifier.classify("CP-7841")
    assert c5["category"] == "ENTERPRISE_CONVERTIBLE"

    c6 = CiscoDeviceClassifier.classify("CP-8831")
    assert c6["category"] == "ENTERPRISE_CONVERTIBLE"

    # Legacy Unsupported
    c7 = CiscoDeviceClassifier.classify("Cisco 7960G")
    assert c7["category"] == "LEGACY_UNSUPPORTED"
    assert c7["is_mpp_supported"] is False
    assert c7["recommended_mode"] == "HARDWARE_REFRESH"

    c8 = CiscoDeviceClassifier.classify("CP-6921")
    assert c8["category"] == "LEGACY_UNSUPPORTED"


def test_technician_instructions_generation():
    service = WebexDeviceOnboardingService(MagicMock())

    # Option A (Native MPP)
    inst_a = service.generate_technician_instructions("NATIVE_MPP", "1234567890123456", "2026-10-29")
    assert "Native MPP" in inst_a.factory_reset_sequence
    assert inst_a.activation_code == "1234567890123456"
    assert any("Activation Code" in s for s in inst_a.steps)

    # Option B (Enterprise to MPP)
    inst_b = service.generate_technician_instructions("ENTERPRISE_TO_MPP", "9876543210987654", "2026-10-29")
    assert inst_b.factory_reset_sequence == "123456789*0#"
    assert "clouducm.cisco.com" in inst_b.cloud_onboarding_url
    assert any("123456789*0#" in s for s in inst_b.steps)


def test_onboard_devices_dry_run(mock_webex_provider):
    service = WebexDeviceOnboardingService(mock_webex_provider)

    devices = [
        DeviceOnboardingItem(
            mac_address="00:11:22:33:44:55",
            model="Cisco 8851 MPP",
            migration_mode="NATIVE_MPP",
            location_name="Austin Branch",
            user_email="jdoe@example.com",
        ),
        DeviceOnboardingItem(
            mac_address="AA-BB-CC-DD-EE-FF",
            model="Cisco 7841",
            migration_mode="ENTERPRISE_TO_MPP",
            location_name="Dallas Branch",
            workspace_name="Conf Room A",
        ),
        DeviceOnboardingItem(
            mac_address="1122.3344.5566",
            model="Cisco 7960",  # Legacy unsupported
            migration_mode="ENTERPRISE_TO_MPP",
            location_name="Austin Branch",
            user_email="asmith@example.com",
        ),
        DeviceOnboardingItem(
            mac_address="INVALID_MAC",
            model="Cisco 8851",
            migration_mode="NATIVE_MPP",
            location_name="Austin Branch",
            user_email="jdoe@example.com",
        ),
    ]

    response = service.onboard_devices(devices=devices, dry_run=True)

    assert response.status == "SUCCESS"
    assert response.dry_run is True
    assert response.total_evaluated == 4
    assert response.successful_count == 2
    assert response.skipped_count == 1  # 7960 skipped
    assert response.failed_count == 1   # invalid MAC failed

    # Ensure no mutating Webex API calls were executed in dry run
    mock_webex_provider.provision_mpp_device.assert_not_called()
    mock_webex_provider.provision_workspace_device.assert_not_called()


def test_onboard_devices_live_execution(mock_webex_provider):
    service = WebexDeviceOnboardingService(mock_webex_provider)

    devices = [
        DeviceOnboardingItem(
            mac_address="00:11:22:33:44:55",
            model="Cisco 8851 MPP",
            migration_mode="NATIVE_MPP",
            location_name="Austin Branch",
            user_email="jdoe@example.com",
        ),
        DeviceOnboardingItem(
            mac_address="AA-BB-CC-DD-EE-FF",
            model="CP-7841",
            migration_mode="ENTERPRISE_TO_MPP",
            location_id="loc_dallas_456",
            workspace_name="Conf Room A",
        ),
    ]

    response = service.onboard_devices(devices=devices, dry_run=False)

    assert response.status == "SUCCESS"
    assert response.dry_run is False
    assert response.total_evaluated == 2
    assert response.successful_count == 2
    assert response.failed_count == 0

    # User MPP registration verification
    mock_webex_provider.provision_mpp_device.assert_called_once_with(
        mac_address="001122334455",
        model="Cisco 8851 MPP",
        location_id="loc_austin_123",
        person_id="person_jdoe",
    )

    # Workspace registration verification
    mock_webex_provider.provision_workspace_device.assert_called_once_with(
        mac_address="AABBCCDDEEFF",
        model="CP-7841",
        location_id="loc_dallas_456",
        workspace_id="ws_conf_room_a",
    )

    # Both items should have activation codes and technician instructions
    assert response.results[0].activation_code == "1234567890123456"
    assert response.results[1].activation_code == "1234567890123456"
    assert response.results[1].technician_instructions.factory_reset_sequence == "123456789*0#"


def test_onboard_devices_updates_user_location_and_phone(mock_webex_provider):
    service = WebexDeviceOnboardingService(mock_webex_provider)
    mock_webex_provider.get_person_by_id.return_value = {
        "id": "person_louis",
        "firstName": "Louis",
        "lastName": "Gonzales",
        "emails": ["lgonzales@example.com"],
        "licenses": ["lic_calling_pro"],
        "locationId": "old_location_quickhill",
        "extension": "801662",
    }

    devices = [
        DeviceOnboardingItem(
            mac_address="BC:C4:93:51:FB:60",
            model="Cisco 8851 MPP",
            migration_mode="NATIVE_MPP",
            location_id="loc_sig_louis",
            user_email="lgonzales@example.com",
            phone_number="+15124391662",
            extension="801662",
        )
    ]

    response = service.onboard_devices(devices=devices, dry_run=False)
    assert response.status == "SUCCESS"
    assert response.successful_count == 1

    # Verify user calling profile updated via enable_webex_calling_for_person
    mock_webex_provider.enable_webex_calling_for_person.assert_called_once_with(
        person_id="person_lgonzales",
        location_id="loc_sig_louis",
        calling_license_id="",
        extension="801662",
        phone_number="+15124391662",
    )


def test_api_onboard_devices_endpoint(mock_admin_user, mock_webex_provider):
    app.dependency_overrides[get_current_user] = lambda: mock_admin_user
    client = TestClient(app)

    try:
        from unittest.mock import patch
        with patch("routers.telecom.WebexCallingProvider", return_value=mock_webex_provider):
            # Test missing token -> 401
            resp_no_token = client.post(
                "/api/v1/telecom/devices/onboard",
                json={"devices": []},
            )
            assert resp_no_token.status_code == 401

            # Test dry run
            resp = client.post(
                "/api/v1/telecom/devices/onboard",
                headers={"X-Webex-Access-Token": "valid_token"},
                json={
                    "devices": [
                        {
                            "mac_address": "00:11:22:33:44:55",
                            "model": "Cisco 8851 MPP",
                            "migration_mode": "NATIVE_MPP",
                            "location_name": "Austin Branch",
                            "user_email": "jdoe@example.com",
                        }
                    ],
                    "dry_run": True,
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "SUCCESS"
            assert data["dry_run"] is True
            assert data["successful_count"] == 1
    finally:
        app.dependency_overrides.clear()


def test_api_device_onboarding_live_requires_step_up(mock_admin_user):
    app.dependency_overrides[get_current_user] = lambda: mock_admin_user
    client = TestClient(app)

    try:
        resp = client.post(
            "/api/v1/telecom/devices/onboard",
            headers={"X-Webex-Access-Token": "valid_token"},
            json={
                "devices": [
                    {
                        "mac_address": "00:11:22:33:44:55",
                        "model": "Cisco 8851 MPP",
                        "migration_mode": "NATIVE_MPP",
                        "location_name": "Austin Branch",
                        "user_email": "jdoe@example.com",
                    }
                ],
                "dry_run": False,
            },
        )
        assert resp.status_code == 401
        assert "LDAP step-up authentication failed" in resp.json()["detail"]
    finally:
        app.dependency_overrides.clear()


def test_api_device_onboarding_live_with_valid_step_up(mock_admin_user, mock_webex_provider):
    app.dependency_overrides[get_current_user] = lambda: mock_admin_user
    client = TestClient(app)

    try:
        from unittest.mock import patch
        with patch("routers.telecom.WebexCallingProvider", return_value=mock_webex_provider), \
             patch("routers.telecom.require_workspace_step_up") as mock_step_up:
            resp = client.post(
                "/api/v1/telecom/devices/onboard",
                headers={"X-Webex-Access-Token": "valid_token"},
                json={
                    "devices": [
                        {
                            "mac_address": "00:11:22:33:44:55",
                            "model": "Cisco 8851 MPP",
                            "migration_mode": "NATIVE_MPP",
                            "location_name": "Austin Branch",
                            "user_email": "jdoe@example.com",
                        }
                    ],
                    "dry_run": False,
                    "step_up_user": "test_admin",
                    "step_up_pass": "secret_pass",
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "SUCCESS"
            assert data["dry_run"] is False
            mock_step_up.assert_called_once()
    finally:
        app.dependency_overrides.clear()


def test_api_activation_code_endpoint(mock_admin_user, mock_webex_provider):
    app.dependency_overrides[get_current_user] = lambda: mock_admin_user
    client = TestClient(app)

    try:
        from unittest.mock import patch
        with patch("routers.telecom.WebexCallingProvider", return_value=mock_webex_provider):
            resp = client.post(
                "/api/v1/telecom/devices/activation-code",
                headers={"X-Webex-Access-Token": "valid_token"},
                json={"user_email": "jdoe@example.com"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["code"] == "1234567890123456"
            assert data["owner_type"] == "USER"
    finally:
        app.dependency_overrides.clear()


def test_api_device_compatibility_endpoint(mock_admin_user):
    app.dependency_overrides[get_current_user] = lambda: mock_admin_user
    client = TestClient(app)

    try:
        resp = client.get("/api/v1/telecom/devices/check-compatibility/Cisco%208851")
        assert resp.status_code == 200
        data = resp.json()
        assert data["category"] == "ENTERPRISE_CONVERTIBLE"
        assert data["is_conversion_supported"] is True
        assert data["recommended_mode"] == "ENTERPRISE_TO_MPP"

        resp_eol = client.get("/api/v1/telecom/devices/check-compatibility/CP-7960G")
        assert resp_eol.status_code == 200
        assert resp_eol.json()["category"] == "LEGACY_UNSUPPORTED"
    finally:
        app.dependency_overrides.clear()


def test_normalize_webex_device_model():
    from providers.webex_calling_provider import normalize_webex_device_model

    assert normalize_webex_device_model("Cisco 8851 MPP") == "Cisco 8851"
    assert normalize_webex_device_model("Cisco 8851 (Enterprise)") == "Cisco 8851"
    assert normalize_webex_device_model("CP-8851-3PCC") == "Cisco 8851"
    assert normalize_webex_device_model("CP-8851") == "Cisco 8851"
    assert normalize_webex_device_model("Cisco 7841 MPP") == "Cisco 7841"
    assert normalize_webex_device_model("Cisco 6851 MPP") == "Cisco 6851"
    assert normalize_webex_device_model("Cisco ATA 191") == "Cisco ATA 191"
    assert normalize_webex_device_model("Webex Desk Pro") == "Webex Desk Pro"


def test_resolve_location_auto_resolve_from_user():
    mock_prov = MagicMock(spec=WebexCallingProvider)
    mock_prov.get_person_by_email.return_value = {
        "id": "person_123",
        "displayName": "Test User",
        "locationId": "loc_auto_resolved_from_user",
    }
    service = WebexDeviceOnboardingService(mock_prov)

    item = DeviceOnboardingItem(
        mac_address="001122334455",
        model="Cisco 8851 MPP",
        user_email="testuser@example.com",
    )

    resolved_loc = service.resolve_location_id(item)
    assert resolved_loc == "loc_auto_resolved_from_user"
    mock_prov.get_person_by_email.assert_called_once_with("testuser@example.com")


def test_resolve_location_meraki_unsynced_error():
    mock_prov = MagicMock(spec=WebexCallingProvider)
    service = WebexDeviceOnboardingService(mock_prov)

    item = DeviceOnboardingItem(
        mac_address="001122334455",
        model="Cisco 8851 MPP",
        location_id="MERAKI:L_998877",
        location_name="Cedar Park Clinic",
        user_email="testuser@example.com",
    )

    with pytest.raises(ValueError, match="is a Meraki branch site that has not yet been synced to Webex Calling"):
        service.resolve_location_id(item)


def test_resolve_location_unresolved_error():
    mock_prov = MagicMock(spec=WebexCallingProvider)
    mock_prov.get_person_by_email.return_value = {"id": "person_123", "displayName": "Test User"}  # No locationId
    service = WebexDeviceOnboardingService(mock_prov)

    item = DeviceOnboardingItem(
        mac_address="001122334455",
        model="Cisco 8851 MPP",
        user_email="testuser@example.com",
    )

    with pytest.raises(ValueError, match="could not auto-resolve location"):
        service.resolve_location_id(item)


def test_onboard_device_with_dir_sync_user(mock_webex_provider):
    mock_webex_provider.get_locations.return_value = [{"id": "loc_target", "name": "SIG_LouisGonzales"}]
    mock_webex_provider.get_person_by_email.return_value = {
        "id": "person_lg",
        "displayName": "Louis Gonzales",
        "emails": ["lgonzales@corp.internal"],
        "locationId": "loc_old",
    }
    mock_webex_provider.enable_webex_calling_for_person.side_effect = Exception(
        'HTTP 400: {"message":"Failed to update people. Invalid Request. Phone Number of type Work is part of Dir Sync attributes and cannot be updated manually."}'
    )
    mock_webex_provider.provision_mpp_device.return_value = {
        "id": "dev_123",
        "activationCode": "1964824275448286",
    }

    service = WebexDeviceOnboardingService(mock_webex_provider)
    item = DeviceOnboardingItem(
        mac_address="BCC49351FB60",
        model="Cisco 8851 MPP",
        location_id="loc_target",
        user_email="lgonzales@corp.internal",
        phone_number="+15124391662",
        # extension omitted
    )

    resp = service.onboard_devices([item], dry_run=False)
    assert resp.status == "SUCCESS"
    assert resp.successful_count == 1
    assert "Active Directory / Dir Sync" in resp.results[0].details
    assert resp.results[0].activation_code == "1234567890123456"

