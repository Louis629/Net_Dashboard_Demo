"""
tests/test_workspace_router_endpoints.py

Tests for telecom router endpoints supporting the workspace & device CSV migration:
- GET /api/v1/telecom/webex/licenses
- GET /api/v1/telecom/workspaces/sample-csv
- POST /api/v1/telecom/workspaces/validate-csv
- POST /api/v1/telecom/workspaces/import
- POST /api/v1/telecom/workspaces/export-postman
"""

from unittest.mock import patch, MagicMock
from fastapi import HTTPException
from fastapi.testclient import TestClient
from main import app
from providers.webex_calling_provider import WebexAPIError
from security.auth import get_current_user, CurrentUser
from security.roles import Role


def override_get_current_user():
    return CurrentUser(
        username="admin_test",
        role=Role.NETWORK_ADMIN,
        permissions=["telecom:audit", "telecom:provision", "telecom:write"]
    )


import pytest

@pytest.fixture(autouse=True)
def setup_telecom_auth():
    app.dependency_overrides[get_current_user] = override_get_current_user
    yield
    app.dependency_overrides.pop(get_current_user, None)

client = TestClient(app)


def test_get_sample_csv_endpoint():
    response = client.get("/api/v1/telecom/workspaces/sample-csv")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "SUCCESS"
    assert "CLH Lanihuli Lounge Phone" in data["csv_content"]


def test_get_webex_licenses_endpoint():
    with patch("routers.telecom.WebexCallingProvider") as mock_prov_cls:
        mock_prov = MagicMock()
        mock_prov.get_licenses.return_value = [
            {"id": "lic_wxc", "name": "Webex Calling - Workspaces", "totalUnits": 100, "consumedUnits": 20}
        ]
        mock_prov.get_workspace_calling_licenses.return_value = [
            {"id": "lic_wxc", "name": "Webex Calling - Workspaces"}
        ]
        mock_prov.get_user_calling_licenses.return_value = []
        mock_prov_cls.return_value = mock_prov

        response = client.get(
            "/api/v1/telecom/webex/licenses",
            headers={"X-Webex-Access-Token": "engineer-token"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "SUCCESS"
        assert len(data["licenses"]) >= 1
        assert data["licenses"][0]["is_workspace_eligible"] is True
        assert data["licenses"][0]["is_user_eligible"] is False
        assert mock_prov_cls.call_args.kwargs == {"access_token": "engineer-token"}


def test_get_webex_licenses_requires_token():
    with patch("routers.telecom.WebexCallingProvider") as provider_cls:
        response = client.get("/api/v1/telecom/webex/licenses")

    assert response.status_code == 401
    provider_cls.assert_not_called()


def test_get_webex_locations_uses_engineer_token_without_fake_fallback():
    with patch("routers.telecom.WebexCallingProvider") as provider_cls:
        provider = MagicMock()
        provider.get_calling_locations.return_value = [{"id": "loc_1", "name": "Office"}]
        provider_cls.return_value = provider

        response = client.get(
            "/api/v1/telecom/webex-locations",
            headers={"X-Webex-Access-Token": "engineer-token"},
        )

    assert response.status_code == 200
    assert response.json()[0]["id"] == "loc_1"
    assert provider_cls.call_args.kwargs == {"access_token": "engineer-token"}


def test_get_webex_locations_requires_token_instead_of_returning_defaults():
    with patch("routers.telecom.WebexCallingProvider") as provider_cls:
        response = client.get("/api/v1/telecom/webex-locations")

    assert response.status_code == 401
    provider_cls.assert_not_called()


def test_cucm_webex_provisioning_queues_engineer_token():
    with patch("routers.telecom.execute_webex_batch_provisioning_task.apply_async") as apply_async:
        apply_async.return_value.id = "task-123"
        response = client.post(
            "/api/v1/telecom/webex-provision",
            json={"cucm_audit_data": {"devices": []}},
            headers={"X-Webex-Access-Token": "engineer-token"},
        )

    assert response.status_code == 202
    assert response.json()["task_id"] == "task-123"
    kwargs = apply_async.call_args.kwargs
    assert kwargs["kwargs"] == {
        "cucm_audit_data": {"devices": []},
        "location_id": None,
        "calling_license_id": None,
        "webex_access_token": "engineer-token",
    }
    assert "engineer-token" not in kwargs["kwargsrepr"]


def test_validate_csv_endpoint_with_sample():
    with patch("routers.telecom.WebexCallingProvider") as mock_prov_cls:
        mock_prov = MagicMock()
        mock_prov.get_locations.return_value = [
            {"id": "loc_1", "name": "CLH - Billing Lanihuli"},
            {"id": "loc_2", "name": "CLH Aiea Heights"}
        ]
        mock_prov.get_workspace_calling_licenses.return_value = [
            {"id": "lic_1", "name": "Webex Calling - Workspaces"}
        ]
        mock_prov_cls.return_value = mock_prov

        response = client.post(
            "/api/v1/telecom/workspaces/validate-csv",
            json={"use_sample": True},
            headers={"X-Webex-Access-Token": "engineer-token"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "SUCCESS"
        assert data["validation"]["total_rows"] == 222
        assert data["validation"]["ready_count"] > 0
        assert mock_prov_cls.call_args.kwargs == {"access_token": "engineer-token"}


def test_import_workspace_batch_endpoint_dry_run():
    payload = {
        "rows": [
            {
                "row_number": 1,
                "display_name": "Test Lounge",
                "location_name": "CLH - Billing Lanihuli",
                "location_id": "loc_1",
                "mac": "70BD96EC9906",
                "model": "Cisco 9841",
                "extension": "851845",
                "selected": True
            }
        ],
        "dry_run": True
    }
    with patch("routers.telecom.WebexWorkspaceImportService") as service_cls:
        service = MagicMock()
        service.execute_batch.return_value.model_dump.return_value = {
            "dry_run": True,
            "successful_count": 1,
        }
        service_cls.return_value = service
        response = client.post(
            "/api/v1/telecom/workspaces/import",
            json=payload,
            headers={"X-Webex-Access-Token": "engineer-token"},
        )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "SUCCESS"
    assert data["execution"]["dry_run"] is True
    assert data["execution"]["successful_count"] == 1
    assert service_cls.call_args.args[0].access_token == "engineer-token"


def test_import_workspace_batch_live_run_requires_step_up_credentials():
    payload = {
        "rows": [
            {
                "row_number": 1,
                "display_name": "Test Lounge",
                "location_name": "CLH - Billing Lanihuli",
                "location_id": "loc_1",
                "mac": "70BD96EC9906",
                "model": "Cisco 9841",
                "selected": True,
            }
        ],
        "dry_run": False,
        "step_up_user": "engineer@corp.internal",
        "step_up_pass": "directory-password",
    }
    with (
        patch("routers.telecom.authenticate_step_up_credentials") as step_up_auth,
        patch("routers.telecom.WebexWorkspaceImportService") as service_cls,
    ):
        service = MagicMock()
        service.execute_batch.return_value.model_dump.return_value = {
            "dry_run": False,
            "successful_count": 1,
        }
        service_cls.return_value = service
        response = client.post(
            "/api/v1/telecom/workspaces/import",
            json=payload,
            headers={"X-Webex-Access-Token": "engineer-token"},
        )

    assert response.status_code == 200
    step_up_auth.assert_called_once_with(
        "engineer@corp.internal",
        "directory-password",
        "Webex Workspace Live Import",
    )
    assert service.execute_batch.call_args.args[2] is False


def test_import_workspace_batch_live_run_rejects_invalid_step_up_credentials():
    payload = {
        "rows": [{"row_number": 1, "display_name": "Test Lounge", "mac": "70BD96EC9906"}],
        "dry_run": False,
        "step_up_user": "engineer",
        "step_up_pass": "wrong-password",
    }
    with (
        patch(
            "routers.telecom.authenticate_step_up_credentials",
            side_effect=HTTPException(status_code=401, detail="LDAP step-up authentication failed."),
        ),
        patch("routers.telecom.WebexWorkspaceImportService") as service_cls,
    ):
        response = client.post(
            "/api/v1/telecom/workspaces/import",
            json=payload,
            headers={"X-Webex-Access-Token": "engineer-token"},
        )

    assert response.status_code == 401
    service_cls.assert_not_called()


def test_import_workspace_batch_live_run_rejects_missing_step_up_credentials():
    payload = {
        "rows": [{"row_number": 1, "display_name": "Test Lounge", "mac": "70BD96EC9906"}],
        "dry_run": False,
    }
    with (
        patch("security.auth.LDAPSAuthProvider") as ldap_provider,
        patch("routers.telecom.WebexWorkspaceImportService") as service_cls,
    ):
        response = client.post(
            "/api/v1/telecom/workspaces/import",
            json=payload,
            headers={"X-Webex-Access-Token": "engineer-token"},
        )

    assert response.status_code == 401
    ldap_provider.assert_not_called()
    service_cls.assert_not_called()


def test_workspace_location_migration_endpoint_previews_csv():
    csv_content = "displayName,locationName,mac\nDesk,Office,001122334455"
    with patch("routers.telecom.WebexWorkspaceLocationService") as service_cls:
        service = MagicMock()
        service.correct_locations.return_value = {
            "dry_run": True,
            "total_processed": 1,
            "successful_count": 1,
            "unchanged_count": 0,
            "failed_count": 0,
            "items": [{"row_number": 2, "status": "DRY_RUN"}],
        }
        service_cls.return_value = service

        response = client.post(
            "/api/v1/telecom/workspaces/correct-locations",
            json={"csv_content": csv_content},
            headers={"X-Webex-Access-Token": "engineer-token"},
        )

    assert response.status_code == 200
    assert response.json()["execution"]["items"][0]["status"] == "DRY_RUN"
    service.correct_locations.assert_called_once_with(csv_content, True)
    service_cls.assert_called_once()
    assert service_cls.call_args.args[0].access_token == "engineer-token"


def test_workspace_location_migration_live_run_requires_step_up_credentials():
    csv_content = "displayName,locationName,mac\nDesk,Office,001122334455"
    with (
        patch("routers.telecom.authenticate_step_up_credentials") as step_up_auth,
        patch("routers.telecom.WebexWorkspaceLocationService") as service_cls,
    ):
        service = MagicMock()
        service.correct_locations.return_value = {
            "dry_run": False,
            "total_processed": 1,
            "successful_count": 1,
            "unchanged_count": 0,
            "failed_count": 0,
            "items": [{"row_number": 2, "status": "SUCCESS"}],
        }
        service_cls.return_value = service
        response = client.post(
            "/api/v1/telecom/workspaces/correct-locations",
            json={
                "csv_content": csv_content,
                "dry_run": False,
                "step_up_user": "engineer",
                "step_up_pass": "directory-password",
            },
            headers={"X-Webex-Access-Token": "engineer-token"},
        )

    assert response.status_code == 200
    step_up_auth.assert_called_once_with(
        "engineer",
        "directory-password",
        "Webex Workspace Location Migration",
    )
    service.correct_locations.assert_called_once_with(csv_content, False)


def test_workspace_location_migration_live_run_rejects_missing_step_up_credentials():
    with (
        patch("security.auth.LDAPSAuthProvider") as ldap_provider,
        patch("routers.telecom.WebexWorkspaceLocationService") as service_cls,
    ):
        response = client.post(
            "/api/v1/telecom/workspaces/correct-locations",
            json={
                "csv_content": "displayName,locationName,mac\nDesk,Office,001122334455",
                "dry_run": False,
            },
            headers={"X-Webex-Access-Token": "engineer-token"},
        )

    assert response.status_code == 401
    ldap_provider.assert_not_called()
    service_cls.assert_not_called()


def test_workspace_location_migration_requires_engineer_token():
    with patch("routers.telecom.WebexCallingProvider") as provider_cls:
        response = client.post(
            "/api/v1/telecom/workspaces/correct-locations",
            json={"csv_content": "displayName,locationName,mac\nDesk,Office,001122334455"},
        )

    assert response.status_code == 401
    assert "bearer token" in response.json()["detail"]
    provider_cls.assert_not_called()


def test_workspace_device_recovery_endpoint_previews_csv():
    with patch("routers.telecom.WebexWorkspaceLocationService") as service_cls:
        service = MagicMock()
        service.recover_device_assignments.return_value = {
            "dry_run": True,
            "total_processed": 1,
            "successful_count": 1,
            "unchanged_count": 0,
            "failed_count": 0,
            "items": [{"row_number": 2, "status": "DRY_RUN"}],
        }
        service_cls.return_value = service

        response = client.post(
            "/api/v1/telecom/workspaces/recover-device-assignments",
            json={"csv_content": "displayName,mac,model\nDesk,001122334455,Cisco 9841"},
            headers={"X-Webex-Access-Token": "engineer-token"},
        )

    assert response.status_code == 200
    assert response.json()["execution"]["items"][0]["status"] == "DRY_RUN"
    service.recover_device_assignments.assert_called_once_with(
        "displayName,mac,model\nDesk,001122334455,Cisco 9841",
        True,
    )
    service_cls.assert_called_once()
    assert service_cls.call_args.args[0].access_token == "engineer-token"


def test_workspace_device_recovery_live_run_requires_step_up_credentials():
    csv_content = "displayName,mac,model\nDesk,001122334455,Cisco 9841"
    with (
        patch("routers.telecom.authenticate_step_up_credentials") as step_up_auth,
        patch("routers.telecom.WebexWorkspaceLocationService") as service_cls,
    ):
        service = MagicMock()
        service.recover_device_assignments.return_value = {
            "dry_run": False,
            "total_processed": 1,
            "successful_count": 1,
            "unchanged_count": 0,
            "failed_count": 0,
            "items": [{"row_number": 2, "status": "SUCCESS"}],
        }
        service_cls.return_value = service
        response = client.post(
            "/api/v1/telecom/workspaces/recover-device-assignments",
            json={
                "csv_content": csv_content,
                "dry_run": False,
                "step_up_user": "engineer",
                "step_up_pass": "directory-password",
            },
            headers={"X-Webex-Access-Token": "engineer-token"},
        )

    assert response.status_code == 200
    step_up_auth.assert_called_once_with(
        "engineer",
        "directory-password",
        "Webex Workspace Device Recovery",
    )
    service.recover_device_assignments.assert_called_once_with(csv_content, False)


def test_workspace_device_recovery_live_run_rejects_missing_step_up_credentials():
    with (
        patch("security.auth.LDAPSAuthProvider") as ldap_provider,
        patch("routers.telecom.WebexWorkspaceLocationService") as service_cls,
    ):
        response = client.post(
            "/api/v1/telecom/workspaces/recover-device-assignments",
            json={
                "csv_content": "displayName,mac,model\nDesk,001122334455,Cisco 9841",
                "dry_run": False,
            },
            headers={"X-Webex-Access-Token": "engineer-token"},
        )

    assert response.status_code == 401
    ldap_provider.assert_not_called()
    service_cls.assert_not_called()


def test_workspace_device_recovery_requires_engineer_token():
    with patch("routers.telecom.WebexCallingProvider") as provider_cls:
        response = client.post(
            "/api/v1/telecom/workspaces/recover-device-assignments",
            json={"csv_content": "displayName,mac,model\nDesk,001122334455,Cisco 9841"},
        )

    assert response.status_code == 401
    assert "bearer token" in response.json()["detail"]
    provider_cls.assert_not_called()


def test_workspace_device_recovery_reports_expired_webex_token():
    with patch("routers.telecom.WebexWorkspaceLocationService") as service_cls:
        service_cls.return_value.recover_device_assignments.side_effect = WebexAPIError("HTTP 401: Unauthorized")
        response = client.post(
            "/api/v1/telecom/workspaces/recover-device-assignments",
            json={"csv_content": "displayName,mac,model\nDesk,001122334455,Cisco 9841"},
            headers={"X-Webex-Access-Token": "expired-engineer-token"},
        )

    assert response.status_code == 401
    assert "valid, unexpired" in response.json()["detail"]


def test_export_postman_collection_endpoint():
    payload = {
        "rows": [
            {
                "row_number": 1,
                "display_name": "Test Lounge",
                "location_name": "CLH - Billing Lanihuli",
                "mac": "70BD96EC9906",
                "model": "Cisco 9841",
                "extension": "851845",
                "selected": True
            }
        ]
    }
    response = client.post("/api/v1/telecom/workspaces/export-postman", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "SUCCESS"
    assert data["collection"]["info"]["name"] == "WxC Workspaces Import (Generated)"
    assert len(data["collection"]["item"]) == 4


def test_non_admin_forbidden_on_telecom_endpoints():
    """Verify that non-admin roles (e.g. LOCAL_TECH_SERVICES, HELPDESK) receive HTTP 403 on all telecom endpoints."""
    def override_tech_services_user():
        return CurrentUser(
            username="tech_user",
            role=Role.LOCAL_TECH_SERVICES,
            permissions=[]
        )

    app.dependency_overrides[get_current_user] = override_tech_services_user
    try:
        # Template download
        res = client.get("/api/v1/telecom/migration/templates/workspaces")
        assert res.status_code == 403
        assert "Administrator privileges" in res.json()["detail"]

        # Sample CSV
        res = client.get("/api/v1/telecom/workspaces/sample-csv")
        assert res.status_code == 403

        # Webex licenses
        res = client.get("/api/v1/telecom/webex/licenses")
        assert res.status_code == 403

        # CSV validation
        res = client.post(
            "/api/v1/telecom/workspaces/validate-csv",
            json={"use_sample": True},
            headers={"X-Webex-Access-Token": "engineer-token"},
        )
        assert res.status_code == 403

        # Workspace batch import
        res = client.post(
            "/api/v1/telecom/workspaces/import",
            json={"rows": [], "dry_run": True},
            headers={"X-Webex-Access-Token": "engineer-token"},
        )
        assert res.status_code == 403

        # Workspace location correction
        res = client.post(
            "/api/v1/telecom/workspaces/correct-locations",
            json={"csv_content": "displayName,locationName,mac\n"},
            headers={"X-Webex-Access-Token": "engineer-token"},
        )
        assert res.status_code == 403

        # Postman export
        res = client.post("/api/v1/telecom/workspaces/export-postman", json={"rows": []})
        assert res.status_code == 403

        # UCCX applications
        res = client.get("/api/v1/telecom/uccx/applications")
        assert res.status_code == 403

        # CUCM audit
        res = client.post("/api/v1/telecom/cucm-audit", json={"cluster": "PRIMARY"})
        assert res.status_code == 403
    finally:
        # Restore admin override
        app.dependency_overrides[get_current_user] = override_get_current_user
