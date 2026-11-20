"""
tests/test_migration_templates.py

Tests for downloading clean sample CSV templates and preset loaders for:
- Workspaces
- Calling Users
- Locations
- Vendor sample
"""

import pytest
from fastapi.testclient import TestClient
from main import app
from security.auth import get_current_user, CurrentUser
from security.roles import Role


@pytest.fixture(autouse=True)
def setup_telecom_auth():
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        username="admin_test",
        role=Role.NETWORK_ADMIN,
        permissions=["telecom:audit", "telecom:provision", "telecom:write"]
    )
    yield
    app.dependency_overrides.pop(get_current_user, None)

client = TestClient(app)


def test_template_downloads_are_independent_of_server_working_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for template_type, expected_filename in (
        ("workspaces", "sample_webex_workspaces_template.csv"),
        ("users", "sample_webex_calling_users_template.csv"),
        ("locations", "sample_webex_locations_template.csv"),
    ):
        resp = client.get(f"/api/v1/telecom/migration/templates/{template_type}")
        assert resp.status_code == 200
        assert "text/csv" in resp.headers.get("content-type", "")
        assert expected_filename in resp.headers.get("content-disposition", "")


def test_download_workspaces_template():
    resp = client.get("/api/v1/telecom/migration/templates/workspaces")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers.get("content-type", "")
    assert "sample_webex_workspaces_template.csv" in resp.headers.get("content-disposition", "")
    content = resp.text
    assert "displayName" in content
    assert "Lanihuli Lounge Phone" in content


def test_download_users_template():
    resp = client.get("/api/v1/telecom/migration/templates/users")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers.get("content-type", "")
    assert "sample_webex_calling_users_template.csv" in resp.headers.get("content-disposition", "")
    content = resp.text
    assert "Display Name" in content
    assert "Email" in content


def test_download_locations_template():
    resp = client.get("/api/v1/telecom/migration/templates/locations")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers.get("content-type", "")
    assert "sample_webex_locations_template.csv" in resp.headers.get("content-disposition", "")
    content = resp.text
    assert "Location Name" in content
    assert "Time Zone" in content


def test_download_unknown_template():
    resp = client.get("/api/v1/telecom/migration/templates/invalid_tmpl")
    assert resp.status_code == 400


def test_sample_csv_presets():
    # CLH 222 rows
    resp_sample = client.get("/api/v1/telecom/workspaces/sample-csv?preset=sample")
    assert resp_sample.status_code == 200
    assert resp_sample.json()["filename"] == "sample_vendor_workspaces.csv"

    # Vendor 177 rows
    resp_vendor = client.get("/api/v1/telecom/workspaces/sample-csv?preset=vendor")
    assert resp_vendor.status_code == 200
    assert resp_vendor.json()["filename"] == "webex_workspaces_import.csv"

    # Clean 5 rows template
    resp_template = client.get("/api/v1/telecom/workspaces/sample-csv?preset=template")
    assert resp_template.status_code == 200
    assert resp_template.json()["filename"] == "sample_webex_workspaces_template.csv"
