import csv
import io
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from core.dependencies import get_meraki_provider
from main import app
from routers.meraki_admin import _parse_webex_location_address
from security.auth import AuthFactory, CurrentUser, get_current_user
from security.roles import Role


@pytest.fixture
def api_client(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "LOCAL")
    previous_overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        username="operator@example.com",
        role=Role.NETWORK_ADMIN,
        permissions=[],
    )
    app.dependency_overrides[get_meraki_provider] = lambda: object()
    yield TestClient(app)
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous_overrides)


@pytest.fixture
def step_up_provider(monkeypatch):
    provider = Mock()
    provider.resolve_user_role.return_value = Role.NETWORK_ADMIN
    monkeypatch.setattr(
        AuthFactory,
        "get_step_up_provider",
        classmethod(lambda cls: provider),
    )

    from routers import meraki_admin

    task = Mock()
    task.delay.return_value = SimpleNamespace(id="swap-task-123")
    monkeypatch.setattr(
        meraki_admin,
        "execute_meraki_appliance_swap_task",
        task,
    )
    return provider, task


def appliance_swap_payload(**overrides):
    payload = {
        "network_id": "N_123",
        "old_serial": "Q2XX-1111-1111",
        "new_serial": "Q2YY-2222-2222",
        "step_up_user": "operator@example.com",
        "step_up_pass": "ValidPassword123!",
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    ("session_role", "step_up_role"),
    [
        (Role.LOCAL_TECH_SERVICES, Role.LOCAL_TECH_SERVICES),
        (Role.LOCAL_TECH_SERVICES, Role.NETWORK_ADMIN),
        (Role.NETWORK_ADMIN, Role.LOCAL_TECH_SERVICES),
        (Role.NETWORK_ADMIN, Role.NETWORK_ADMIN),
    ],
)
def test_appliance_swap_allows_only_approved_roles(
    api_client, step_up_provider, session_role, step_up_role
):
    provider, task = step_up_provider
    provider.resolve_user_role.return_value = step_up_role
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        username="operator@example.com",
        role=session_role,
        permissions=[],
    )

    response = api_client.post(
        "/api/v1/admin/meraki-swap-appliance",
        json=appliance_swap_payload(),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "QUEUED"
    provider.resolve_user_role.assert_called_once_with(
        "operator@example.com", "ValidPassword123!"
    )
    task.delay.assert_called_once_with(
        network_id="N_123",
        old_serial="Q2XX-1111-1111",
        new_serial="Q2YY-2222-2222",
    )


def test_appliance_swap_denies_disallowed_session_role(api_client, step_up_provider):
    provider, task = step_up_provider
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        username="helpdesk@example.com",
        role=Role.HELPDESK,
        permissions=[],
    )

    response = api_client.post(
        "/api/v1/admin/meraki-swap-appliance",
        json=appliance_swap_payload(),
    )

    assert response.status_code == 403
    provider.resolve_user_role.assert_not_called()
    task.delay.assert_not_called()


def test_meraki_network_list_denies_disallowed_session_role(api_client):
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        username="helpdesk@example.com",
        role=Role.HELPDESK,
        permissions=[],
    )

    response = api_client.get("/api/v1/admin/meraki-networks")

    assert response.status_code == 403


def test_meraki_location_export_returns_appliance_address_csv(api_client):
    provider = Mock()
    provider.get_appliance_location_inventory.return_value = [{
        "network_id": "N_123",
        "network_name": "Clinic, North",
        "address": "2230 Liliha St, Honolulu, HI, 96817, United States",
        "time_zone": "America/Chicago",
        "appliance_models": "MX68, Z4C",
        "serial_numbers": "MX-1, Z4-1",
        "lookup_error": "",
    }]
    app.dependency_overrides[get_meraki_provider] = lambda: provider

    response = api_client.get("/api/v1/admin/meraki-webex-location-export")

    assert response.status_code == 200
    assert "attachment; filename=meraki_webex_locations_import.csv" in response.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(response.text.lstrip("\ufeff"))))
    assert rows == [{
        "Location Name": "Clinic, North",
        "Address Line 1": "2230 Liliha St",
        "City": "Honolulu",
        "State": "HI",
        "ZIP": "96817",
        "Country": "United States",
        "Time Zone": "America/Chicago",
    }]
    provider.get_appliance_location_inventory.assert_called_once_with()


def test_meraki_location_export_fails_with_network_ids_when_detail_lookup_fails(api_client):
    provider = Mock()
    provider.get_appliance_location_inventory.return_value = [{
        "network_id": "N_503",
        "network_name": "Remote Clinic",
        "address": "",
        "time_zone": "",
        "appliance_models": "MX68",
        "serial_numbers": "MX-503",
        "lookup_error": "Meraki API returned 503",
    }]
    app.dependency_overrides[get_meraki_provider] = lambda: provider

    response = api_client.get("/api/v1/admin/meraki-webex-location-export")

    assert response.status_code == 503
    assert "Remote Clinic (N_503)" in response.json()["detail"]


def test_meraki_location_preview_includes_source_and_missing_address_status(api_client):
    provider = Mock()
    provider.get_appliance_location_inventory.return_value = [{
        "network_id": "N_missing_address",
        "network_name": "Remote Clinic",
        "address": "",
        "time_zone": "America/Chicago",
        "appliance_models": "MX68",
        "serial_numbers": "MX-1",
        "lookup_error": "",
    }]
    app.dependency_overrides[get_meraki_provider] = lambda: provider

    response = api_client.get("/api/v1/admin/meraki-webex-location-preview")

    assert response.status_code == 200
    data = response.json()
    assert data["columns"] == [
        "Location Name", "Address Line 1", "City", "State", "ZIP", "Country", "Time Zone"
    ]
    assert data["appliance_count"] == 1
    assert data["rows"] == [{
        "network_id": "N_missing_address",
        "source_address": "",
        "address_source": "",
        "address_conflict": False,
        "device_count": 1,
        "appliance_models": "MX68",
        "serial_numbers": "MX-1",
        "lookup_error": "",
        "missing_fields": ["Address Line 1", "City", "State", "ZIP", "Country"],
        "values": {
            "Location Name": "Remote Clinic",
            "Address Line 1": "",
            "City": "",
            "State": "",
            "ZIP": "",
            "Country": "",
            "Time Zone": "America/Chicago",
        },
    }]


def test_unrecognized_meraki_address_is_preserved_for_review():
    assert _parse_webex_location_address("Unparsed address from Meraki") == {
        "Address Line 1": "Unparsed address from Meraki",
        "City": "",
        "State": "",
        "ZIP": "",
        "Country": "",
    }


def test_appliance_swap_requires_step_up_credentials(api_client, step_up_provider):
    provider, task = step_up_provider

    response = api_client.post(
        "/api/v1/admin/meraki-swap-appliance",
        json=appliance_swap_payload(step_up_user=None, step_up_pass=None),
    )

    assert response.status_code == 401
    assert "Step-up authentication required" in response.json()["detail"]
    provider.resolve_user_role.assert_not_called()
    task.delay.assert_not_called()


def test_appliance_swap_rejects_invalid_step_up_credentials(api_client, step_up_provider):
    provider, task = step_up_provider
    provider.resolve_user_role.side_effect = ValueError("Invalid LDAP credentials")

    response = api_client.post(
        "/api/v1/admin/meraki-swap-appliance",
        json=appliance_swap_payload(),
    )

    assert response.status_code == 401
    provider.resolve_user_role.assert_called_once()
    task.delay.assert_not_called()


def test_appliance_swap_rejects_step_up_user_outside_allowed_groups(
    api_client, step_up_provider
):
    provider, task = step_up_provider
    provider.resolve_user_role.return_value = Role.HELPDESK

    response = api_client.post(
        "/api/v1/admin/meraki-swap-appliance",
        json=appliance_swap_payload(),
    )

    assert response.status_code == 403
    assert "credentials must belong" in response.json()["detail"]
    task.delay.assert_not_called()
