# tests/test_sccm_bypass.py
import json
import pytest
from unittest.mock import MagicMock, patch
from pydantic import ValidationError

from main import BypassRequest, app
from providers.sccm_provider import SCCMProvider
from providers.ise_provider import ISEProvider
from fastapi.testclient import TestClient


# -----------------------------------------------------------------------------
# 1. Pydantic Model Validation Tests
# -----------------------------------------------------------------------------

def test_bypass_request_new_device_valid():
    req = BypassRequest(
        device_type="new",
        mac_address="00-11-22-33-44-55",
        operator_id="tech@corp.internal"
    )
    assert req.device_type == "new"
    assert req.mac_address == "00:11:22:33:44:55"
    assert req.operator_id == "tech@corp.internal"
    assert req.stage_in_ise is False


def test_bypass_request_new_device_missing_mac():
    with pytest.raises(ValidationError) as exc_info:
        BypassRequest(
            device_type="new",
            operator_id="tech@corp.internal"
        )
    assert "MAC address is required" in str(exc_info.value)


def test_bypass_request_reimage_with_device_name_only():
    req = BypassRequest(
        device_type="reimage",
        device_name="US38307LT01",
        operator_id="tech@corp.internal",
        remove_sccm=True,
        remove_ad=True,
        remove_ise=True,
        stage_in_ise=False
    )
    assert req.device_type == "reimage"
    assert req.device_name == "US38307LT01"
    assert req.mac_address is None
    assert req.stage_in_ise is False


def test_bypass_request_reimage_with_mac_only():
    req = BypassRequest(
        device_type="reimage",
        mac_address="AA-BB-CC-DD-EE-FF",
        operator_id="tech@corp.internal"
    )
    assert req.device_type == "reimage"
    assert req.mac_address == "AA:BB:CC:DD:EE:FF"
    assert req.device_name is None


def test_bypass_request_reimage_missing_both_identifiers():
    with pytest.raises(ValidationError) as exc_info:
        BypassRequest(
            device_type="reimage",
            operator_id="tech@corp.internal"
        )
    assert "At least one identifier (Computer Name or MAC Address) is required" in str(exc_info.value)


def test_bypass_request_invalid_device_type():
    with pytest.raises(ValidationError) as exc_info:
        BypassRequest(
            device_type="invalid_mode",
            mac_address="00:11:22:33:44:55",
            operator_id="tech@corp.internal"
        )
    assert "device_type must be either 'new' or 'reimage'" in str(exc_info.value)


# -----------------------------------------------------------------------------
# 2. SCCMProvider Unit Tests
# -----------------------------------------------------------------------------

def test_sccm_provider_missing_both_identifiers():
    provider = SCCMProvider()
    res = provider.remove_device(device_name="", mac_address="")
    assert res["success"] is False
    assert "Either DeviceName or MacAddress must be supplied" in res["error"]


@patch("subprocess.run")
def test_sccm_provider_successful_removal(mock_run):
    mock_payload = {
        "success": True,
        "sccm": {
            "attempted": True,
            "found": True,
            "removed": True,
            "resource_id": 16777216,
            "device_name": "US38307LT01",
            "mac_addresses": ["AA:BB:CC:DD:EE:FF"],
            "log_written": True,
            "message": "Successfully removed device 'US38307LT01' from SCCM."
        },
        "ad": {
            "attempted": True,
            "found": True,
            "removed": True,
            "device_name": "US38307LT01",
            "message": "Successfully deleted computer account 'US38307LT01' from Active Directory."
        },
        "resolved_mac": "AA:BB:CC:DD:EE:FF",
        "operator_id": "tech@corp.internal",
        "timestamp": "2026-09-18 12:00:00"
    }

    mock_run.return_value = MagicMock(
        returncode=0,
        stdout=json.dumps(mock_payload),
        stderr=""
    )

    provider = SCCMProvider()
    result = provider.remove_device(
        device_name="US38307LT01",
        mac_address="AA:BB:CC:DD:EE:FF",
        operator_id="tech@corp.internal"
    )

    assert result["success"] is True
    assert result["sccm"]["removed"] is True
    assert result["sccm"]["resource_id"] == 16777216
    assert result["ad"]["removed"] is True
    assert result["resolved_mac"] == "AA:BB:CC:DD:EE:FF"


@patch("subprocess.run")
def test_sccm_provider_device_name_only_resolves_mac(mock_run):
    mock_payload = {
        "success": True,
        "sccm": {
            "attempted": True,
            "found": True,
            "removed": True,
            "resource_id": 16777216,
            "device_name": "us66641",
            "mac_addresses": ["00:50:56:B2:12:34"],
            "log_written": True,
            "message": "Successfully removed device 'us66641' from SCCM."
        },
        "ad": {
            "attempted": True,
            "found": True,
            "removed": True,
            "device_name": "us66641",
            "message": "Successfully deleted computer account 'us66641' from Active Directory (ADSI)."
        },
        "resolved_mac": "00:50:56:B2:12:34",
        "operator_id": "sitladadm",
        "timestamp": "2026-10-05 12:19:18"
    }

    mock_run.return_value = MagicMock(
        returncode=0,
        stdout=json.dumps(mock_payload),
        stderr=""
    )

    provider = SCCMProvider()
    result = provider.remove_device(
        device_name="us66641",
        operator_id="sitladadm"
    )

    assert result["success"] is True
    assert result["sccm"]["removed"] is True
    assert result["sccm"]["device_name"] == "us66641"
    assert result["resolved_mac"] == "00:50:56:B2:12:34"
    assert result["ad"]["removed"] is True


@patch("subprocess.run")
def test_sccm_provider_script_failure(mock_run):
    mock_run.return_value = MagicMock(
        returncode=1,
        stdout="",
        stderr="DCOM connection to site server failed."
    )

    provider = SCCMProvider()
    result = provider.remove_device(
        device_name="US38307LT01",
        operator_id="tech@corp.internal"
    )

    assert result["success"] is False
    assert "DCOM connection" in result["error"]


# -----------------------------------------------------------------------------
# 3. ISEProvider Endpoint Removal Unit Tests
# -----------------------------------------------------------------------------

def test_ise_provider_remove_endpoint_found():
    provider = ISEProvider.__new__(ISEProvider)
    provider.host = "10.100.10.15"
    provider.api = MagicMock()
    provider.trigger_session_coa_terminate = MagicMock(return_value=True)

    # Mock endpoint query response
    mock_resource = MagicMock()
    mock_resource.id = "ep-12345"
    provider.api.endpoint.get_endpoints.return_value = MagicMock(
        response=MagicMock(SearchResult=MagicMock(resources=[mock_resource]))
    )

    result = provider.remove_endpoint("AA:BB:CC:DD:EE:FF")
    assert result["status"] == "SUCCESS"
    assert result["deleted"] is True
    assert result["coa_applied"] is True
    assert result["endpoint_id"] == "ep-12345"
    provider.api.endpoint.delete_endpoint_by_id.assert_called_once_with(id="ep-12345")
    provider.trigger_session_coa_terminate.assert_called_once_with("AA:BB:CC:DD:EE:FF")


def test_ise_provider_remove_endpoint_not_found():
    provider = ISEProvider.__new__(ISEProvider)
    provider.host = "10.100.10.15"
    provider.api = MagicMock()
    provider.trigger_session_coa_terminate = MagicMock(return_value=False)

    provider.api.endpoint.get_endpoints.return_value = MagicMock(
        response=MagicMock(SearchResult=MagicMock(resources=[]))
    )

    result = provider.remove_endpoint("11:22:33:44:55:66")
    assert result["status"] == "NOT_FOUND"
    assert result["deleted"] is False
    assert result["coa_applied"] is False


# -----------------------------------------------------------------------------
# 4. API Route /api/v1/bypass Integration Tests
# -----------------------------------------------------------------------------

@pytest.fixture
def api_client():
    return TestClient(app)


def test_api_bypass_new_device_route(api_client, monkeypatch):
    from main import ise_provider
    monkeypatch.setattr(
        ise_provider,
        "provision_imaging_bypass",
        lambda mac_address, operator_teams_id: {
            "status": "SUCCESS",
            "mac": mac_address,
            "group": "Staging_Imaging_Bypass",
            "coa_applied": True
        }
    )

    resp = api_client.post(
        "/api/v1/bypass",
        headers={"X-API-Authorization": "Bearer dummy_token"},
        json={
            "device_type": "new",
            "mac_address": "00:11:22:33:44:55",
            "operator_id": "tech@corp.internal"
        }
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["device_type"] == "new"
    assert data["mac"] == "00:11:22:33:44:55"
    assert data["ise_bypass"]["status"] == "SUCCESS"


def test_api_bypass_reimage_route_complete_workflow(api_client, monkeypatch):
    from main import sccm_provider, ise_provider, AuthFactory

    mock_auth = MagicMock()
    mock_auth.verify_reimaging_authorization.return_value = (True, "Authorized")
    monkeypatch.setattr(AuthFactory, "get_provider", lambda: mock_auth)

    monkeypatch.setattr(
        sccm_provider,
        "remove_device",
        lambda device_name=None, mac_address=None, operator_id="", remove_sccm=True, remove_ad=True, **kwargs: {
            "success": True,
            "sccm": {
                "attempted": True,
                "found": True,
                "removed": True,
                "resource_id": 9999,
                "device_name": device_name,
                "mac_addresses": ["AA:BB:CC:DD:EE:FF"],
                "log_written": True,
                "message": "Device removed from SCCM."
            },
            "ad": {
                "attempted": True,
                "found": True,
                "removed": True,
                "device_name": device_name,
                "message": "Device removed from Active Directory."
            },
            "resolved_mac": "AA:BB:CC:DD:EE:FF",
            "operator_id": operator_id
        }
    )

    monkeypatch.setattr(
        ise_provider,
        "remove_endpoint",
        lambda mac_address: {
            "status": "SUCCESS",
            "mac": mac_address,
            "deleted": True,
            "coa_applied": True,
            "message": "Deleted from ISE."
        }
    )

    monkeypatch.setattr(
        ise_provider,
        "provision_imaging_bypass",
        lambda mac_address, operator_teams_id: {
            "status": "SUCCESS",
            "mac": mac_address,
            "group": "Staging_Imaging_Bypass",
            "coa_applied": True
        }
    )

    resp = api_client.post(
        "/api/v1/bypass",
        headers={"X-API-Authorization": "Bearer dummy_token"},
        json={
            "device_type": "reimage",
            "device_name": "US38307LT01",
            "operator_id": "tech@corp.internal",
            "remove_sccm": True,
            "remove_ad": True,
            "remove_ise": True,
            "stage_in_ise": True,
            "step_up_user": "tech@corp.internal",
            "step_up_pass": "SecurePass123!"
        }
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["device_type"] == "reimage"
    assert data["mac"] == "AA:BB:CC:DD:EE:FF"  # Resolved from SCCM!
    assert data["sccm"]["removed"] is True
    assert data["ad"]["removed"] is True
    assert data["ise_removal"]["deleted"] is True
    assert data["ise_bypass"]["status"] == "SUCCESS"
    assert any("Auto-resolved MAC" in m for m in data["messages"])


def test_api_bypass_reimage_missing_step_up_credentials(api_client):
    """Verifies that re-imaging without secondary credentials returns 401 Unauthorized."""
    resp = api_client.post(
        "/api/v1/bypass",
        headers={"X-API-Authorization": "Bearer dummy_token"},
        json={
            "device_type": "reimage",
            "device_name": "US38307LT01",
            "operator_id": "tech@corp.internal"
        }
    )
    assert resp.status_code == 401
    assert "Secondary authentication required" in resp.json()["detail"]


def test_api_bypass_reimage_invalid_credentials(api_client, monkeypatch):
    """Verifies that wrong secondary password returns 401 Unauthorized."""
    from main import AuthFactory
    mock_auth = MagicMock()
    mock_auth.verify_reimaging_authorization.return_value = (False, "Invalid corporate directory credentials.")
    monkeypatch.setattr(AuthFactory, "get_provider", lambda: mock_auth)

    resp = api_client.post(
        "/api/v1/bypass",
        headers={"X-API-Authorization": "Bearer dummy_token"},
        json={
            "device_type": "reimage",
            "device_name": "US38307LT01",
            "operator_id": "tech@corp.internal",
            "step_up_user": "tech@corp.internal",
            "step_up_pass": "WrongPassword"
        }
    )
    assert resp.status_code == 401
    assert "Secondary authentication failed: Invalid corporate directory credentials." in resp.json()["detail"]


def test_api_bypass_reimage_admin_denied_without_reimaging_group(api_client, monkeypatch):
    """
    Verifies that even an administrator (NetworkAdmin) who is NOT a member
    of SCCM_REIMAGING_GROUP is strictly rejected with 403 Forbidden.
    """
    from main import AuthFactory
    mock_auth = MagicMock()
    mock_auth.verify_reimaging_authorization.return_value = (
        False,
        "User 'admin_user' is not a member of the required re-imaging authorization group."
    )
    monkeypatch.setattr(AuthFactory, "get_provider", lambda: mock_auth)

    resp = api_client.post(
        "/api/v1/bypass",
        headers={"X-API-Authorization": "Bearer dummy_token"},
        json={
            "device_type": "reimage",
            "device_name": "US38307LT01",
            "operator_id": "admin_user@corp.internal",
            "step_up_user": "admin_user@corp.internal",
            "step_up_pass": "ValidPassword123!"
        }
    )
    assert resp.status_code == 403
    assert "Secondary authorization failed" in resp.json()["detail"]
    assert "not a member of the required re-imaging authorization group" in resp.json()["detail"]


def test_api_bypass_reimage_authorized_in_reimaging_group(api_client, monkeypatch):
    """Verifies that a technician in SCCM_REIMAGING_GROUP is authorized (200 OK)."""
    from main import AuthFactory, sccm_provider, ise_provider
    mock_auth = MagicMock()
    mock_auth.verify_reimaging_authorization.return_value = (True, "Authorized")
    monkeypatch.setattr(AuthFactory, "get_provider", lambda: mock_auth)

    monkeypatch.setattr(
        sccm_provider,
        "remove_device",
        lambda device_name=None, mac_address=None, operator_id="", remove_sccm=True, remove_ad=True, **kwargs: {
            "success": True,
            "sccm": {"attempted": True, "found": True, "removed": True},
            "ad": {"attempted": True, "found": True, "removed": True},
            "resolved_mac": "11:22:33:44:55:66"
        }
    )
    monkeypatch.setattr(
        ise_provider,
        "remove_endpoint",
        lambda mac_address: {"status": "SUCCESS", "deleted": True}
    )

    resp = api_client.post(
        "/api/v1/bypass",
        headers={"X-API-Authorization": "Bearer dummy_token"},
        json={
            "device_type": "reimage",
            "device_name": "US38307LT01",
            "operator_id": "tech_user@corp.internal",
            "step_up_user": "tech_user@corp.internal",
            "step_up_pass": "ValidPassword123!"
        }
    )
    assert resp.status_code == 200
    assert resp.json()["device_type"] == "reimage"


def test_api_bypass_rbac_allows_tech_services(api_client, monkeypatch):
    from security.roles import Role
    from security.auth import CurrentUser, get_current_user

    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        username="tech_user",
        role=Role.LOCAL_TECH_SERVICES,
        permissions=["imagingBypassExecute", "sccmDeleteDevice"]
    )

    from main import ise_provider
    monkeypatch.setattr(
        ise_provider,
        "provision_imaging_bypass",
        lambda mac_address, operator_teams_id: {"status": "SUCCESS", "mac": mac_address, "group": "Staging_Imaging_Bypass", "coa_applied": True}
    )

    try:
        resp = api_client.post(
            "/api/v1/bypass",
            headers={"X-API-Authorization": "Bearer dummy_token"},
            json={
                "device_type": "new",
                "mac_address": "00:11:22:33:44:55",
                "operator_id": "tech@corp.internal"
            }
        )
        assert resp.status_code == 200
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_api_bypass_rbac_denies_helpdesk(api_client):
    from security.roles import Role
    from security.auth import CurrentUser, get_current_user

    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        username="helpdesk_user",
        role=Role.HELPDESK,
        permissions=["identityTracker"]
    )

    try:
        resp = api_client.post(
            "/api/v1/bypass",
            headers={"X-API-Authorization": "Bearer dummy_token"},
            json={
                "device_type": "new",
                "mac_address": "00:11:22:33:44:55",
                "operator_id": "helpdesk@corp.internal"
            }
        )
        assert resp.status_code == 403
        data = resp.json()
        assert "restricted to Network Admin and Tech Services" in data["detail"]
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# -----------------------------------------------------------------------------
# 5. Security Layer LDAPSAuthProvider Unit Tests
# -----------------------------------------------------------------------------

def test_ldaps_auth_provider_reimage_invalid_credentials(monkeypatch):
    """Verifies that LDAPSAuthProvider returns False when credentials fail bind."""
    from security.auth import LDAPSAuthProvider
    provider = LDAPSAuthProvider()

    monkeypatch.setattr(provider, "_get_user_connection", lambda username, password: None)
    success, reason = provider.verify_reimaging_authorization("testuser", "badpass")

    assert success is False
    assert "Invalid corporate directory credentials" in reason


def test_ldaps_auth_provider_reimage_not_in_group(monkeypatch):
    """Verifies that LDAPSAuthProvider returns False when user is not in SCCM_REIMAGING_GROUP."""
    from security.auth import LDAPSAuthProvider, settings
    provider = LDAPSAuthProvider()

    mock_conn = MagicMock()
    monkeypatch.setattr(provider, "_get_user_connection", lambda username, password: mock_conn)
    monkeypatch.setattr(provider, "is_user_in_group", lambda username, group: False)
    monkeypatch.setattr(settings, "SCCM_REIMAGING_GROUP", "CN=SIT_ConfigMgr_Technical Service,DC=us,DC=int")

    success, reason = provider.verify_reimaging_authorization("testadmin", "goodpass")

    assert success is False
    assert "not a member of the required re-imaging authorization group" in reason


def test_ldaps_auth_provider_reimage_in_group(monkeypatch):
    """Verifies that LDAPSAuthProvider returns True when user is in SCCM_REIMAGING_GROUP."""
    from security.auth import LDAPSAuthProvider, settings
    provider = LDAPSAuthProvider()

    mock_conn = MagicMock()
    monkeypatch.setattr(provider, "_get_user_connection", lambda username, password: mock_conn)
    monkeypatch.setattr(provider, "is_user_in_group", lambda username, group: True)
    monkeypatch.setattr(settings, "SCCM_REIMAGING_GROUP", "CN=SIT_ConfigMgr_Technical Service,DC=us,DC=int")

    success, reason = provider.verify_reimaging_authorization("reimage_tech", "goodpass")

    assert success is True
    assert reason == "Authorized"


# -----------------------------------------------------------------------------
# 6. Step-Up Credential WMI Integration Tests
# -----------------------------------------------------------------------------

@patch("subprocess.run")
def test_sccm_provider_step_up_credentials_piped_via_stdin(mock_run):
    """Verifies that credentials are piped via stdin and never exposed on command line."""
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout='{"success":true,"sccm":{"removed":true,"resource_id":123,"device_name":"US123","message":"Removed"},"ad":{"removed":false,"message":""},"resolved_mac":""}',
        stderr=""
    )

    provider = SCCMProvider()
    res = provider.remove_device(
        device_name="US123",
        operator_id="tech@corp.internal",
        credential_user="sitlrgadm",
        credential_pass="SuperSecretPassword123!"
    )

    assert res["success"] is True
    mock_run.assert_called_once()
    called_cmd = mock_run.call_args[0][0]
    called_input = mock_run.call_args[1]["input"]

    # Ensure -UseStdinCredentials switch is passed
    assert "-UseStdinCredentials" in called_cmd
    # Ensure raw password is NOT on the CLI
    assert "SuperSecretPassword123!" not in " ".join(called_cmd)
    assert "sitlrgadm" not in " ".join(called_cmd)

    # Ensure credentials are in stdin JSON payload
    assert called_input is not None
    parsed_input = json.loads(called_input)
    assert parsed_input["username"] == "sitlrgadm"
    assert parsed_input["password"] == "SuperSecretPassword123!"


@patch("main.sccm_provider.remove_device")
def test_api_reimage_passes_step_up_creds_to_sccm_provider(mock_remove, api_client, monkeypatch):
    """Verifies that step-up user and pass from request payload are forwarded to sccm_provider."""
    from security.roles import Role
    from security.auth import CurrentUser, get_current_user, AuthFactory

    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        username="tech_user",
        role=Role.LOCAL_TECH_SERVICES,
        permissions=["networkManagement"]
    )

    mock_auth = MagicMock()
    mock_auth.verify_reimaging_authorization.return_value = (True, "Authorized")
    monkeypatch.setattr(AuthFactory, "get_provider", lambda: mock_auth)

    mock_remove.return_value = {
        "success": True,
        "sccm": {"removed": True, "message": "Removed from SCCM."},
        "ad": {"removed": True, "message": "Removed from AD."},
        "resolved_mac": "00:11:22:33:44:55"
    }

    try:
        resp = api_client.post(
            "/api/v1/bypass",
            headers={"X-API-Authorization": "Bearer dummy_token"},
            json={
                "device_type": "reimage",
                "device_name": "US27914",
                "operator_id": "tech@corp.internal",
                "remove_sccm": True,
                "remove_ad": True,
                "remove_ise": False,
                "stage_in_ise": False,
                "step_up_user": "US\\sitcdoadm",
                "step_up_pass": "ValidAdminPassword!"
            }
        )
        assert resp.status_code == 200
        mock_remove.assert_called_once_with(
            device_name="US27914",
            mac_address=None,
            operator_id="US\\sitcdoadm",
            remove_sccm=True,
            remove_ad=True,
            credential_user="US\\sitcdoadm",
            credential_pass="ValidAdminPassword!"
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# -----------------------------------------------------------------------------
# 7. Bulk Workstation Removal & Reimaging Tests
# -----------------------------------------------------------------------------

def test_bypass_request_bulk_newline_separated():
    """Verifies that newline-separated hostnames pasted from tickets are parsed correctly."""
    raw_input = """US27249
US27211
US24675
US23863
US27115"""
    req = BypassRequest(
        device_type="reimage",
        device_name=raw_input,
        operator_id="tech@corp.internal"
    )
    assert req.device_names == ["US27249", "US27211", "US24675", "US23863", "US27115"]


def test_bypass_request_bulk_comma_separated():
    """Verifies that comma-separated hostnames are parsed into device_names."""
    raw_input = "US27249, US27211, US24675"
    req = BypassRequest(
        device_type="reimage",
        device_name=raw_input,
        operator_id="tech@corp.internal"
    )
    assert req.device_names == ["US27249", "US27211", "US24675"]


def test_bypass_request_bulk_with_invalid_hostname():
    """Verifies that invalid characters anywhere in a bulk hostname list raise ValidationError."""
    with pytest.raises(ValidationError) as exc:
        BypassRequest(
            device_type="reimage",
            device_name="US27249\nUS;BAD$NAME\nUS24675",
            operator_id="tech@corp.internal"
        )
    assert "Invalid computer name format" in str(exc.value)


@patch("subprocess.run")
def test_sccm_provider_bulk_removal_stdin_payload(mock_run):
    """Verifies that SCCMProvider passes devices list via stdin and parses bulk response."""
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout=json.dumps({
            "success": True,
            "is_bulk": True,
            "total": 3,
            "summary": {
                "total": 3,
                "sccm_removed": 3,
                "sccm_not_found": 0,
                "ad_removed": 3,
                "ad_not_found": 0,
                "failed": 0
            },
            "devices": [
                {
                    "device_name": "US27249",
                    "resolved_mac": "00:11:22:33:44:01",
                    "success": True,
                    "sccm": {"removed": True, "resource_id": 101, "message": "Removed"},
                    "ad": {"removed": True, "message": "Removed"}
                },
                {
                    "device_name": "US27211",
                    "resolved_mac": "00:11:22:33:44:02",
                    "success": True,
                    "sccm": {"removed": True, "resource_id": 102, "message": "Removed"},
                    "ad": {"removed": True, "message": "Removed"}
                },
                {
                    "device_name": "US24675",
                    "resolved_mac": "",
                    "success": True,
                    "sccm": {"removed": True, "resource_id": 103, "message": "Removed"},
                    "ad": {"removed": True, "message": "Removed"}
                }
            ],
            "operator_id": "sitlrgadm"
        }),
        stderr=""
    )

    provider = SCCMProvider()
    res = provider.remove_device(
        device_names=["US27249", "US27211", "US24675"],
        operator_id="sitlrgadm",
        credential_user="sitlrgadm",
        credential_pass="AdminPass123!"
    )

    assert res["is_bulk"] is True
    assert res["total"] == 3
    assert len(res["devices"]) == 3
    mock_run.assert_called_once()
    called_cmd = mock_run.call_args[0][0]
    called_input = json.loads(mock_run.call_args[1]["input"])
    assert "-UseStdinCredentials" in called_cmd
    assert called_input["devices"] == ["US27249", "US27211", "US24675"]
    assert called_input["username"] == "sitlrgadm"
    assert called_input["password"] == "AdminPass123!"


@patch("main.sccm_provider.remove_device")
@patch("main.ise_provider.remove_endpoint")
def test_api_reimage_bulk_workflow(mock_ise_remove, mock_sccm_remove, api_client, monkeypatch):
    """Verifies that /api/v1/bypass coordinates bulk SCCM/AD removal and per-device ISE purging."""
    from security.roles import Role
    from security.auth import CurrentUser, get_current_user, AuthFactory

    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        username="tech_user",
        role=Role.LOCAL_TECH_SERVICES,
        permissions=["networkManagement"]
    )

    mock_auth = MagicMock()
    mock_auth.verify_reimaging_authorization.return_value = (True, "Authorized")
    monkeypatch.setattr(AuthFactory, "get_provider", lambda: mock_auth)

    mock_sccm_remove.return_value = {
        "success": True,
        "is_bulk": True,
        "total": 2,
        "summary": {
            "total": 2,
            "sccm_removed": 2,
            "sccm_not_found": 0,
            "ad_removed": 2,
            "ad_not_found": 0,
            "failed": 0
        },
        "devices": [
            {
                "device_name": "US27249",
                "resolved_mac": "00:11:22:33:44:01",
                "success": True,
                "sccm": {"removed": True, "message": "Removed"},
                "ad": {"removed": True, "message": "Removed"}
            },
            {
                "device_name": "US27211",
                "resolved_mac": "",
                "success": True,
                "sccm": {"removed": True, "message": "Removed"},
                "ad": {"removed": True, "message": "Removed"}
            }
        ]
    }
    mock_ise_remove.return_value = {"status": "SUCCESS", "message": "Deleted"}

    try:
        resp = api_client.post(
            "/api/v1/bypass",
            headers={"X-API-Authorization": "Bearer dummy_token"},
            json={
                "device_type": "reimage",
                "device_names": ["US27249", "US27211"],
                "operator_id": "tech@corp.internal",
                "remove_sccm": True,
                "remove_ad": True,
                "remove_ise": True,
                "step_up_user": "US\\sitcdoadm",
                "step_up_pass": "ValidAdminPassword!"
            }
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_bulk"] is True
        assert data["total"] == 2
        assert len(data["devices"]) == 2
        # ISE endpoint removal should only be called for US27249 because US27211 has no resolved MAC
        mock_ise_remove.assert_called_once_with("00:11:22:33:44:01")
        assert data["summary"]["ise_purged"] == 1
    finally:
        app.dependency_overrides.pop(get_current_user, None)




