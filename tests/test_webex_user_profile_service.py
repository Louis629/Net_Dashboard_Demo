"""
tests/test_webex_user_profile_service.py

Unit test suite for Phase 2 User Entitlement & Calling Profile Provisioning:
- WebexCallingProvider Caller ID, Cloud Voicemail, and Profile methods
- WebexUserProfileService (identity resolution, dry-run validation, live provisioning)
- Telecom router endpoints (/users/provision-calling-profile, /users/{email}/calling-profile)
"""

from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from main import app
from providers.webex_calling_provider import WebexCallingProvider, WebexAPIError
from services.webex_user_profile_service import WebexUserProfileService
from models.telecom import UserCallingProfileItem
from security.auth import get_current_user, CurrentUser
from security.roles import Role


def override_telecom_admin():
    return CurrentUser(
        username="admin_test",
        role=Role.NETWORK_ADMIN,
        permissions=["telecom:audit", "telecom:provision", "telecom:write"]
    )


@pytest.fixture(autouse=True)
def setup_auth_override():
    app.dependency_overrides[get_current_user] = override_telecom_admin
    yield
    app.dependency_overrides.pop(get_current_user, None)


client = TestClient(app)


# =============================================================================
# 1. Provider-Level Tests (WebexCallingProvider Calling Profile Methods)
# =============================================================================

def test_webex_provider_configure_caller_id():
    provider = WebexCallingProvider(access_token="fake_token")
    with patch.object(provider, "_request") as mock_req:
        mock_req.return_value = {"status": "SUCCESS"}

        # Direct line
        provider.configure_caller_id(
            person_id="p-123",
            caller_id_type="DIRECT_LINE",
            external_caller_id_name="Sonic Pathology",
        )
        mock_req.assert_called_with(
            "PUT",
            "/people/p-123/features/callerId",
            json={
                "selected": "DIRECT_LINE",
                "customExternalCallerIdName": "Sonic Pathology",
                "externalCallerIdNamePolicy": "OTHER",
            }
        )

        # Location main number
        provider.configure_caller_id(
            person_id="p-123",
            caller_id_type="LOCATION_NUMBER",
        )
        mock_req.assert_called_with(
            "PUT",
            "/people/p-123/features/callerId",
            json={"selected": "LOCATION_NUMBER"}
        )


def test_webex_provider_configure_voicemail():
    provider = WebexCallingProvider(access_token="fake_token")
    with patch.object(provider, "_request") as mock_req:
        mock_req.return_value = {"status": "SUCCESS"}

        provider.configure_voicemail(
            person_id="p-123",
            enabled=True,
            send_email_notification=True,
            email_copy_of_message=True,
            notification_email="doctor@example.com",
            default_pin="849201",
        )

        assert mock_req.call_count == 2
        # First call: Voicemail configuration
        first_call = mock_req.call_args_list[0]
        assert first_call[0] == ("PUT", "/people/p-123/features/voicemail")
        vm_payload = first_call[1]["json"]
        assert vm_payload["enabled"] is True
        assert vm_payload["sendEmailNotification"] is True
        assert vm_payload["emailCopyOfMessage"] is True
        assert vm_payload["notifications"]["destination"] == "doctor@example.com"
        assert vm_payload["messageStorage"]["mwi"]["enabled"] is True

        # Second call: Voicemail passcode PIN
        second_call = mock_req.call_args_list[1]
        assert second_call[0] == ("PUT", "/people/p-123/features/voicemail/passcode")
        assert second_call[1]["json"]["passcode"] == "849201"


def test_webex_provider_provision_user_calling_profile():
    provider = WebexCallingProvider(access_token="fake_token")
    with patch.object(provider, "enable_webex_calling_for_person") as mock_enable, \
         patch.object(provider, "configure_caller_id") as mock_cid, \
         patch.object(provider, "configure_voicemail") as mock_vm:

        mock_enable.return_value = {"id": "p-123", "emails": ["nurse@example.com"]}
        mock_cid.return_value = {"selected": "DIRECT_LINE"}
        mock_vm.return_value = {"enabled": True}

        summary = provider.provision_user_calling_profile(
            person_id="p-123",
            location_id="loc-555",
            calling_license_id="lic-pro",
            extension="4199",
            phone_number="+15125550199",
            caller_id_type="DIRECT_LINE",
            caller_id_name="Sonic Lab",
            voicemail_enabled=True,
            voicemail_email_notification=True,
            voicemail_attach_audio=True,
            voicemail_pin="123456",
        )

        assert "calling" in summary
        assert "caller_id" in summary
        assert "voicemail" in summary
        mock_enable.assert_called_once_with(
            person_id="p-123",
            location_id="loc-555",
            calling_license_id="lic-pro",
            extension="4199",
            phone_number="+15125550199",
        )
        mock_cid.assert_called_once_with(
            person_id="p-123",
            caller_id_type="DIRECT_LINE",
            external_caller_id_name="Sonic Lab",
        )
        mock_vm.assert_called_once_with(
            person_id="p-123",
            enabled=True,
            send_email_notification=True,
            email_copy_of_message=True,
            notification_email="nurse@example.com",
            default_pin="123456",
        )


# =============================================================================
# 2. Service-Level Tests (WebexUserProfileService)
# =============================================================================

def test_service_identity_resolution():
    mock_webex = MagicMock(spec=WebexCallingProvider)

    # 1. Direct email match
    mock_webex.get_person_by_email.side_effect = lambda email: (
        {"id": "p-1", "displayName": "Alex User"} if email == "alex@example.com"
        else ({"id": "p-2", "displayName": "Bob UPN"} if email == "bjones@example.com" else None)
    )

    service = WebexUserProfileService(mock_webex)

    p1 = service.resolve_person("alex@example.com", "auser")
    assert p1 is not None
    assert p1["id"] == "p-1"

    # 2. Fallback to UPN
    p2 = service.resolve_person("wrong.email@example.com", "bjones")
    assert p2 is not None
    assert p2["id"] == "p-2"

    # 3. Not found
    p3 = service.resolve_person("unknown@example.com", "unknown_user")
    assert p3 is None


def test_service_provision_user_profiles_dry_run_and_live():
    mock_webex = MagicMock(spec=WebexCallingProvider)
    mock_webex.get_locations.return_value = [{"id": "loc-1", "name": "Dallas Branch"}]
    mock_webex.get_person_by_email.return_value = {
        "id": "p-100",
        "displayName": "Dr. Sarah Connor",
        "emails": ["sconnor@example.com"]
    }
    mock_webex.provision_user_calling_profile.return_value = {"status": "SUCCESS"}

    service = WebexUserProfileService(mock_webex)

    items = [
        UserCallingProfileItem(
            email="sconnor@example.com",
            userid="sconnor",
            location_id="loc-1",
            extension="4199",
            phone_number="+15125550199",
            calling_license_id="lic-pro",
            caller_id_type="DIRECT_LINE",
            caller_id_name="Sonic Pathology",
            voicemail_enabled=True,
            voicemail_email_notification=True,
            voicemail_attach_audio=True,
            voicemail_pin="554433",
        )
    ]

    # 1. Dry Run Execution
    dry_res = service.provision_user_profiles(items, dry_run=True)
    assert dry_res.dry_run is True
    assert dry_res.successful_count == 1
    assert dry_res.results[0].status == "DRY_RUN"
    assert dry_res.results[0].person_id == "p-100"
    mock_webex.provision_user_calling_profile.assert_not_called()

    # 2. Live Execution
    live_res = service.provision_user_profiles(items, dry_run=False)
    assert live_res.dry_run is False
    assert live_res.successful_count == 1
    assert live_res.results[0].status == "SUCCESS"
    mock_webex.provision_user_calling_profile.assert_called_once_with(
        person_id="p-100",
        location_id="loc-1",
        calling_license_id="lic-pro",
        extension="4199",
        phone_number="+15125550199",
        caller_id_type="DIRECT_LINE",
        caller_id_name="Sonic Pathology",
        voicemail_enabled=True,
        voicemail_email_notification=True,
        voicemail_attach_audio=True,
        voicemail_pin="554433",
    )


def test_service_provision_user_profiles_missing_location():
    mock_webex = MagicMock(spec=WebexCallingProvider)
    mock_webex.get_locations.return_value = [{"id": "loc-valid", "name": "Valid Site"}]

    service = WebexUserProfileService(mock_webex)
    items = [
        UserCallingProfileItem(
            email="tech@example.com",
            location_id="loc-nonexistent",
            extension="4100",
            calling_license_id="lic-1",
        )
    ]

    res = service.provision_user_profiles(items, dry_run=True)
    assert res.failed_count == 1
    assert "not found in Webex Control Hub" in res.results[0].details


# =============================================================================
# 3. Router-Level API Endpoint Tests
# =============================================================================

def test_api_users_provision_calling_profile_dry_run():
    with patch("routers.telecom.WebexCallingProvider") as mock_webex_cls:
        mock_webex = MagicMock(spec=WebexCallingProvider)
        mock_webex.get_locations.return_value = [{"id": "loc-austin", "name": "Austin Clinic"}]
        mock_webex.get_person_by_email.return_value = {
            "id": "person-austin-1",
            "displayName": "Austin Tech",
            "emails": ["tech@austin.com"]
        }
        mock_webex_cls.return_value = mock_webex
        mock_webex_cls.extract_existing_did.side_effect = WebexCallingProvider.extract_existing_did

        payload = {
            "dry_run": True,
            "calling_license_id": "lic-pro-default",
            "users": [
                {
                    "email": "tech@austin.com",
                    "location_id": "loc-austin",
                    "extension": "4201",
                    "phone_number": "+15125550201",
                    "caller_id_type": "DIRECT_LINE",
                    "voicemail_enabled": True,
                }
            ]
        }

        response = client.post(
            "/api/v1/telecom/users/provision-calling-profile",
            json=payload,
            headers={"X-Webex-Access-Token": "test_bearer_token"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "SUCCESS"
        assert data["dry_run"] is True
        assert data["successful_count"] == 1
        assert data["results"][0]["status"] == "DRY_RUN"


def test_api_get_user_calling_profile():
    with patch("routers.telecom.WebexCallingProvider") as mock_webex_cls:
        mock_webex = MagicMock(spec=WebexCallingProvider)
        mock_webex.get_person_by_email.return_value = {
            "id": "person-uuid-999",
            "displayName": "Dr. House",
            "emails": ["house@example.com"],
            "locationId": "loc-princeton",
            "extension": "4999",
            "licenses": ["lic-pro"],
        }
        mock_webex.get_caller_id.return_value = {"selected": "DIRECT_LINE"}
        mock_webex.get_voicemail_settings.return_value = {"enabled": True, "sendEmailNotification": True}
        mock_webex_cls.return_value = mock_webex
        mock_webex_cls.extract_existing_did.side_effect = WebexCallingProvider.extract_existing_did

        response = client.get(
            "/api/v1/telecom/users/house@example.com/calling-profile",
            headers={"X-Webex-Access-Token": "test_bearer_token"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["display_name"] == "Dr. House"
        assert data["caller_id"]["selected"] == "DIRECT_LINE"
        assert data["voicemail"]["enabled"] is True


def test_service_provision_user_profile_optional_extension_dirsync():
    mock_webex = MagicMock(spec=WebexCallingProvider)
    mock_webex.get_locations.return_value = [{"id": "loc-1", "name": "Dallas Branch"}]
    mock_webex.get_person_by_email.return_value = {
        "id": "p-100",
        "displayName": "Louis Gonzales",
        "emails": ["lgonzales@example.com"],
    }
    mock_webex.provision_user_calling_profile.return_value = {"status": "SUCCESS"}

    service = WebexUserProfileService(mock_webex)

    # Extension is omitted (None) because it's managed via Active Directory / Dir Sync
    items = [
        UserCallingProfileItem(
            email="lgonzales@example.com",
            location_id="loc-1",
            phone_number="+15124391662",
            calling_license_id="lic-pro",
        )
    ]

    res = service.provision_user_profiles(items, dry_run=False)
    assert res.successful_count == 1
    assert res.results[0].status == "SUCCESS"
    assert res.results[0].extension is None
    assert "Extension (Dir Sync)" in res.results[0].details
    mock_webex.provision_user_calling_profile.assert_called_once_with(
        person_id="p-100",
        location_id="loc-1",
        calling_license_id="lic-pro",
        extension=None,
        phone_number="+15124391662",
        caller_id_type="DIRECT_LINE",
        caller_id_name=None,
        voicemail_enabled=True,
        voicemail_email_notification=True,
        voicemail_attach_audio=True,
        voicemail_pin=None,
    )


def test_service_provision_user_profile_automated_redsky_sync():
    mock_webex = MagicMock(spec=WebexCallingProvider)
    mock_webex.get_locations.return_value = [{"id": "loc-1", "name": "Cedar Park Site"}]
    mock_webex.get_person_by_email.return_value = {
        "id": "p-200",
        "displayName": "Louis Gonzales",
        "firstName": "Louis",
        "lastName": "Gonzales",
        "emails": ["lgonzales@corp.internal"],
    }
    mock_webex.provision_user_calling_profile.return_value = {"status": "SUCCESS"}

    mock_redsky = MagicMock()
    mock_redsky.is_configured.return_value = True
    mock_redsky.sync_device_user_mye911.return_value = {
        "status": "SUCCESS",
        "action": "UPDATED",
        "phone_number": "+15124391662",
    }

    service = WebexUserProfileService(webex_provider=mock_webex, redsky_provider=mock_redsky)

    items = [
        UserCallingProfileItem(
            email="lgonzales@corp.internal",
            location_id="loc-1",
            phone_number="+15124391662",
            calling_license_id="lic-pro",
        )
    ]

    # Dry run
    dry_res = service.provision_user_profiles(items, dry_run=True, sync_redsky=True)
    assert dry_res.results[0].redsky_mye911_status == "READY"
    assert "RedSky MyE911 sync ready" in dry_res.results[0].details
    mock_redsky.sync_device_user_mye911.assert_not_called()

    # Live run
    live_res = service.provision_user_profiles(items, dry_run=False, sync_redsky=True)
    assert live_res.results[0].status == "SUCCESS"
    assert live_res.results[0].redsky_mye911_status == "CONFIGURED"
    assert "RedSky MyE911 active for '+15124391662'" in live_res.results[0].details
    mock_redsky.sync_device_user_mye911.assert_called_once_with(
        email="lgonzales@corp.internal",
        phone_number="+15124391662",
        first_name="Louis",
        last_name="Gonzales",
        callback_number="+15124391662",
        enable_mye911=True,
    )


def test_service_provision_user_profile_redsky_warning_resilience():
    mock_webex = MagicMock(spec=WebexCallingProvider)
    mock_webex.get_locations.return_value = [{"id": "loc-1", "name": "Cedar Park Site"}]
    mock_webex.get_person_by_email.return_value = {
        "id": "p-200",
        "displayName": "Louis Gonzales",
        "emails": ["lgonzales@corp.internal"],
    }
    mock_webex.provision_user_calling_profile.return_value = {"status": "SUCCESS"}

    mock_redsky = MagicMock()
    mock_redsky.is_configured.return_value = True
    mock_redsky.sync_device_user_mye911.side_effect = RuntimeError("RedSky API Gateway Timeout")

    service = WebexUserProfileService(webex_provider=mock_webex, redsky_provider=mock_redsky)

    items = [
        UserCallingProfileItem(
            email="lgonzales@corp.internal",
            location_id="loc-1",
            phone_number="+15124391662",
            calling_license_id="lic-pro",
        )
    ]

    res = service.provision_user_profiles(items, dry_run=False, sync_redsky=True)
    # The overall profile still succeeds, but RedSky status is marked WARNING
    assert res.results[0].status == "SUCCESS"
    assert res.results[0].redsky_mye911_status == "WARNING"
    assert "RedSky MyE911 warning: RedSky API Gateway Timeout" in res.results[0].details


def test_api_sync_user_redsky_mye911_success():
    with patch("routers.telecom.RedskyProvider") as mock_redsky_cls:
        mock_redsky = MagicMock()
        mock_redsky.is_configured.return_value = True
        mock_redsky.sync_device_user_mye911.return_value = {
            "status": "SUCCESS",
            "action": "UPDATED",
            "phone_number": "5124391662",
            "callback_number": "5124391662",
        }
        mock_redsky_cls.return_value = mock_redsky

        resp = client.post(
            "/api/v1/telecom/users/lgonzales@corp.internal/redsky-mye911-sync",
            headers={"X-Webex-Access-Token": "mock_token_123"},
            json={
                "phone_number": "5124391662",
                "enable_mye911": True,
            },
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "SUCCESS"
        assert data["action"] == "UPDATED"
        assert data["phone_number"] == "5124391662"
        assert data["enable_mye911"] is True
        mock_redsky.sync_device_user_mye911.assert_called_once_with(
            email="lgonzales@corp.internal",
            phone_number="5124391662",
            first_name=None,
            last_name=None,
            callback_number="5124391662",
            enable_mye911=True,
        )


def test_api_sync_user_redsky_mye911_auto_detect_from_webex():
    with patch("routers.telecom.RedskyProvider") as mock_redsky_cls, \
         patch("routers.telecom.WebexCallingProvider") as mock_webex_cls:
        mock_redsky = MagicMock()
        mock_redsky.is_configured.return_value = True
        mock_redsky.sync_device_user_mye911.return_value = {
            "status": "SUCCESS",
            "action": "CREATED",
            "phone_number": "+15124391662",
        }
        mock_redsky_cls.return_value = mock_redsky

        mock_webex = MagicMock()
        mock_webex.get_person_by_email.return_value = {
            "id": "p-123",
            "firstName": "Louis",
            "lastName": "Gonzales",
            "phoneNumbers": [{"type": "work", "value": "+15124391662"}],
        }
        mock_webex_cls.return_value = mock_webex
        mock_webex_cls.extract_existing_did.side_effect = WebexCallingProvider.extract_existing_did

        # Omitting phone_number and first/last name in payload; router auto-detects from Webex
        resp = client.post(
            "/api/v1/telecom/users/lgonzales@corp.internal/redsky-mye911-sync",
            headers={"X-Webex-Access-Token": "mock_token_123"},
            json={},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "SUCCESS"
        assert data["action"] == "CREATED"
        mock_redsky.sync_device_user_mye911.assert_called_once_with(
            email="lgonzales@corp.internal",
            phone_number="+15124391662",
            first_name="Louis",
            last_name="Gonzales",
            callback_number="+15124391662",
            enable_mye911=True,
        )


def test_api_sync_user_redsky_mye911_unconfigured():
    with patch("routers.telecom.RedskyProvider") as mock_redsky_cls:
        mock_redsky = MagicMock()
        mock_redsky.is_configured.return_value = False
        mock_redsky_cls.return_value = mock_redsky

        resp = client.post(
            "/api/v1/telecom/users/lgonzales@corp.internal/redsky-mye911-sync",
            headers={"X-Webex-Access-Token": "mock_token_123"},
            json={"phone_number": "5124391662"},
        )

        assert resp.status_code == 503
        assert "RedSky E911 is not configured" in resp.json()["detail"]



