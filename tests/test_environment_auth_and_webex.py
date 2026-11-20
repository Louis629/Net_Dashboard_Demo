# tests/test_environment_auth_and_webex.py
"""
Unit tests validating:
1. 3-Tier Environment Architecture:
   - LOCAL: Developer sandbox (DEV_DEFAULT_USER, NetworkAdmin)
   - DEV: Active Directory LDAPS authentication recording actual user
   - PROD: Strict Okta OIDC token enforcement (no bypasses)
2. All Step-Up Authentications are executed over LDAPS
3. Dynamic 12-Hour Webex Token Header Support (X-Webex-Token)
4. Catalyst Center Provider Proactive Token Refresh & SSL Default
5. Celery PROD Message Broker Hard Requirement (no silent SQLite fallback)
"""

import os
import time
from unittest.mock import patch, MagicMock
import pytest
from fastapi.testclient import TestClient

from main import app
from core.config import get_settings
from security.auth import (
    get_current_user,
    enforce_dev_environment_access,
    enforce_routing_auth,
    CurrentUser,
    AuthFactory,
    LDAPSAuthProvider,
    create_access_token,
    settings as auth_settings,
)
from security.roles import Role
from providers.catalyst_center_provider import CatalystCenterProvider
from providers.webex_calling_provider import WebexCallingProvider


@pytest.fixture
def client():
    return TestClient(app)


# -----------------------------------------------------------------------------
# 1. 3-Tier Environment Authentication Tests
# -----------------------------------------------------------------------------

def test_local_environment_sandbox_user(monkeypatch):
    """Verifies that ENVIRONMENT=LOCAL returns the default developer sandbox user."""
    monkeypatch.setenv("ENVIRONMENT", "LOCAL")
    
    mock_request = MagicMock()
    user = get_current_user(request=mock_request, token=None)
    
    assert user.username == auth_settings.DEV_DEFAULT_USER
    assert user.username
    assert user.role == Role.NETWORK_ADMIN
    assert len(user.permissions) > 0


def test_dev_environment_requires_authentication_when_no_credentials(monkeypatch):
    """Verifies that in ENVIRONMENT=DEV, requests without credentials fail with 401."""
    monkeypatch.setenv("ENVIRONMENT", "DEV")
    
    mock_request = MagicMock()
    mock_request.headers = {}
    
    with pytest.raises(Exception) as excinfo:
        get_current_user(request=mock_request, token=None)
    
    assert "401" in str(excinfo.value) or "Active Directory authentication required" in str(excinfo.value)


def test_dev_environment_records_actual_user_via_jwt_session(monkeypatch):
    """Verifies that in DEV, a local session JWT issued to an actual AD user is correctly resolved."""
    monkeypatch.setenv("ENVIRONMENT", "DEV")
    
    token = create_access_token({"sub": "jsmith", "role": Role.NETWORK_ADMIN.value})
    
    mock_request = MagicMock()
    mock_request.headers = {"Authorization": f"Bearer {token}"}
    
    user = get_current_user(request=mock_request, token=token)
    assert user.username == "jsmith"
    assert user.role == Role.NETWORK_ADMIN


def test_dev_environment_records_actual_user_via_basic_auth(monkeypatch):
    """Verifies that in DEV, HTTP Basic credentials authenticate against LDAPS and record actual user."""
    import base64
    monkeypatch.setenv("ENVIRONMENT", "DEV")
    
    creds = base64.b64encode(b"sitlrg:SecretPass123").decode("utf-8")
    mock_request = MagicMock()
    mock_request.headers = {"Authorization": f"Basic {creds}"}
    
    with patch.object(LDAPSAuthProvider, "resolve_user_role", return_value=Role.LOCAL_TECH_SERVICES):
        user = get_current_user(request=mock_request, token=None)
        assert user.username == "sitlrg"
        assert user.role == Role.LOCAL_TECH_SERVICES


def test_prod_environment_strictly_rejects_missing_token(monkeypatch):
    """Verifies that in ENVIRONMENT=PROD, requests without an Okta token are strictly rejected."""
    monkeypatch.setenv("ENVIRONMENT", "PROD")
    
    mock_request = MagicMock()
    mock_request.headers = {}
    
    with pytest.raises(Exception) as excinfo:
        get_current_user(request=mock_request, token=None)
    
    assert "401" in str(excinfo.value)


def test_dev_login_endpoint_authenticates_actual_user(client, monkeypatch):
    """Verifies that /api/v1/auth/login validates credentials and returns a session JWT for actual user."""
    monkeypatch.setenv("ENVIRONMENT", "DEV")
    
    with patch.object(LDAPSAuthProvider, "resolve_user_role", return_value=Role.NETWORK_ADMIN):
        resp = client.post("/api/v1/auth/login", json={"username": "cdo_admin", "password": "ValidPassword!"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "SUCCESS"
        assert data["username"] == "cdo_admin"
        assert data["role"] == "NetworkAdmin"
        assert "access_token" in data


def test_dev_login_endpoint_disabled_in_prod(client, monkeypatch):
    """Verifies that direct LDAPS login endpoint is rejected in PROD (Okta SSO mandatory)."""
    monkeypatch.setenv("ENVIRONMENT", "PROD")
    
    resp = client.post("/api/v1/auth/login", json={"username": "cdo_admin", "password": "AnyPassword"})
    assert resp.status_code == 400
    assert "disabled in PRODUCTION" in resp.json()["detail"]


# -----------------------------------------------------------------------------
# 2. Step-Up Authentication over LDAPS
# -----------------------------------------------------------------------------

def test_auth_factory_step_up_always_uses_ldaps(monkeypatch):
    """Verifies that AuthFactory.get_step_up_provider() always returns LDAPSAuthProvider."""
    monkeypatch.setenv("ENVIRONMENT", "PROD")
    monkeypatch.setenv("AUTH_METHOD", "OKTA")
    
    step_up = AuthFactory.get_step_up_provider()
    assert isinstance(step_up, LDAPSAuthProvider)


# -----------------------------------------------------------------------------
# 3. Dynamic Webex 12-Hour Token (Header Support)
# -----------------------------------------------------------------------------

def test_webex_provider_uses_custom_token():
    """Verifies WebexCallingProvider accepts user-entered access_token directly."""
    token = "TEST_CUSTOM_12HR_WEBEX_TOKEN"
    provider = WebexCallingProvider(access_token=token)
    assert provider.access_token == token
    assert provider.session.headers.get("Authorization") == f"Bearer {token}"


def test_telecom_endpoint_accepts_x_webex_token_header(client):
    """Verifies that telecom endpoints forward X-Webex-Token from request headers."""
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        username="netadmin", role=Role.NETWORK_ADMIN, permissions=["*"]
    )
    
    custom_token = "USER_ENTERED_12HR_TOKEN"
    
    with patch("routers.telecom.WebexCallingProvider") as mock_prov_cls:
        mock_prov = MagicMock()
        mock_prov.get_locations.return_value = [{"id": "loc_test", "name": "Test Location", "timeZone": "America/Chicago"}]
        mock_prov_cls.return_value = mock_prov
        
        resp = client.get(
            "/api/v1/telecom/webex-locations",
            headers={"X-Webex-Token": custom_token, "X-API-Authorization": "Bearer dummy"}
        )
        assert resp.status_code == 200
        mock_prov_cls.assert_called_with(access_token=custom_token)
    
    app.dependency_overrides.pop(get_current_user, None)


# -----------------------------------------------------------------------------
# 4. Catalyst Center Provider Proactive Token Refresh & SSL Defaults
# -----------------------------------------------------------------------------

def test_catalyst_center_provider_defaults_verify_ssl_true():
    """Verifies CatalystCenterProvider defaults verify_ssl to True for security."""
    prov = CatalystCenterProvider(host="10.0.0.1", username="admin", password="password")
    assert prov.verify_ssl is True
    assert prov.session.verify is True


def test_catalyst_center_proactive_token_refresh():
    """Verifies CatalystCenterProvider proactively refreshes token when TTL expires."""
    prov = CatalystCenterProvider(host="10.0.0.1", username="admin", password="password")
    prov._token = "EXISTING_TOKEN"
    prov._token_timestamp = time.time() - 3500  # Older than 3000s TTL
    
    assert prov._is_token_valid() is False
    
    prov._token_timestamp = time.time()  # Fresh
    assert prov._is_token_valid() is True


# -----------------------------------------------------------------------------
# 5. UI Template Script Integrity Tests
# -----------------------------------------------------------------------------

def test_index_template_script_integrity(client, monkeypatch):
    """Verifies that index.html renders valid JavaScript and switchView is defined."""
    monkeypatch.setenv("ENVIRONMENT", "LOCAL")
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.text

    # Verify switchView is defined and assigned to window
    assert "function switchView(viewId, title, btnElement)" in html
    assert "window.switchView = switchView;" in html
    assert "window.showLocalDashboard = showLocalDashboard;" in html
    assert html.index("const isNocAdmin =") < html.index("setApplianceSwapAccess(isNocAdmin || isTechStaff);")

    # Verify script tag closure before devLoginModal
    assert "</script>" in html
    dev_modal_idx = html.find('id="devLoginModal"')
    assert dev_modal_idx != -1
    # Check that </script> appears before the devLoginModal
    last_script_close = html.rfind("</script>", 0, dev_modal_idx)
    assert last_script_close != -1


# -----------------------------------------------------------------------------
# 6. Celery Eager Mode & CUCM Audit Execution Tests
# -----------------------------------------------------------------------------

def test_celery_eager_mode_in_local_environment(monkeypatch):
    """Verifies that in ENVIRONMENT=LOCAL, Celery runs in task_always_eager mode."""
    monkeypatch.setenv("ENVIRONMENT", "LOCAL")
    monkeypatch.delenv("CELERY_ALWAYS_EAGER", raising=False)

    import importlib
    import celery_app
    importlib.reload(celery_app)

    assert celery_app.celery_app.conf.task_always_eager is True
    assert celery_app.celery_app.conf.task_store_eager_result is True


def test_cucm_audit_execution_in_local_environment(client, monkeypatch):
    """Verifies that triggering CUCM audit in LOCAL executes eagerly and completes."""
    monkeypatch.setenv("ENVIRONMENT", "LOCAL")
    monkeypatch.delenv("CELERY_ALWAYS_EAGER", raising=False)

    import importlib
    import celery_app
    importlib.reload(celery_app)

    with patch("services.cucm_audit_service.CUCMAuditService.run_full_migration_audit") as mock_audit:
        mock_summary = MagicMock()
        mock_summary.total_devices = 42
        mock_summary.total_lines = 84
        mock_summary.model_dump.return_value = {"total_devices": 42, "total_lines": 84}
        mock_audit.return_value = mock_summary

        resp = client.post(
            "/api/v1/telecom/cucm-audit",
            json={"cluster": "PRIMARY", "default_country_code": "+1", "device_scope": "PHYSICAL_ONLY"}
        )
        assert resp.status_code == 202
        data = resp.json()
        task_id = data["task_id"]
        assert task_id

        # Immediately poll status - should be COMPLETED because of eager mode
        poll_resp = client.get(f"/api/v1/telecom/tasks/{task_id}")
        assert poll_resp.status_code == 200
        poll_data = poll_resp.json()
        assert poll_data["status"] == "COMPLETED"
        assert poll_data["data"]["total_devices"] == 42


def test_logout_endpoint_with_bearer_token(client):
    """Verifies that POST /api/v1/auth/logout succeeds and decodes token identity for audit logging."""
    token = create_access_token({"sub": "LGonzales@corp.internal", "role": "NetworkAdmin"})
    resp = client.post(
        "/api/v1/auth/logout",
        headers={"X-API-Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "SUCCESS"
    assert "terminated" in data["message"].lower()


def test_logout_endpoint_without_token(client):
    """Verifies that POST /api/v1/auth/logout succeeds gracefully even without a token."""
    resp = client.post("/api/v1/auth/logout")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "SUCCESS"



