# tests/test_windows_diag.py
import os
import zipfile
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from pydantic import ValidationError

from main import app
from models.diagnostics import WindowsDiagRequest
from security.auth import get_current_user, CurrentUser
from security.roles import Role


@pytest.fixture
def client():
    return TestClient(app)


# -----------------------------------------------------------------------------
# 1. WindowsDiagRequest Validation Tests
# -----------------------------------------------------------------------------

def test_windows_diag_request_valid():
    req = WindowsDiagRequest(hostname="US40217")
    assert req.hostname == "US40217"


def test_windows_diag_request_valid_with_dashes_and_dots():
    req = WindowsDiagRequest(hostname="US-PC123.us.int")
    assert req.hostname == "US-PC123.us.int"


def test_windows_diag_request_invalid_characters():
    with pytest.raises(ValidationError) as exc:
        WindowsDiagRequest(hostname="US40217;rm -rf")
    assert "Invalid hostname format" in str(exc.value)


def test_windows_diag_request_empty():
    with pytest.raises(ValidationError) as exc:
        WindowsDiagRequest(hostname="")
    assert "Invalid hostname format" in str(exc.value)


# -----------------------------------------------------------------------------
# 2. PowerShell Script Integrity Tests
# -----------------------------------------------------------------------------

def test_get_teap_logs_script_exists_and_has_cert_logic():
    script_path = os.path.abspath("scripts/get_teap_logs.ps1")
    assert os.path.exists(script_path), f"Script not found at {script_path}"

    with open(script_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Ensure machine certificate store collection is present
    assert "Cert:\\LocalMachine\\My" in content
    assert "machine_certificates.csv" in content

    # Ensure user certificate store collection for logged-in user is present
    assert "Win32_UserProfile" in content
    assert "Win32_ComputerSystem" in content
    assert "user_certificates.csv" in content
    assert "certificate_verification_summary.txt" in content
    assert "Client Authentication" in content


# -----------------------------------------------------------------------------
# 3. /api/v1/windows-diag Endpoint Integration Tests
# -----------------------------------------------------------------------------

def test_windows_diag_unauthenticated(client, monkeypatch):
    from core.config import get_settings
    monkeypatch.setenv("ENVIRONMENT", "PROD")
    monkeypatch.setattr(get_settings(), "ENVIRONMENT", "PROD")
    resp = client.post("/api/v1/windows-diag", json={"hostname": "US40217"})
    assert resp.status_code in {401, 403}


@patch("routers.diagnostics.run_powershell_script")
def test_windows_diag_success(mock_ps, client, tmp_path):
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        username="net_admin",
        role=Role.NETWORK_ADMIN,
        permissions=["networkManagement"]
    )

    hostname = "US40217"
    # Create the expected ZIP file in temp_downloads/US40217/Network_Diagnostics.zip
    target_dir = os.path.join("temp_downloads", hostname)
    os.makedirs(target_dir, exist_ok=True)
    zip_path = os.path.join(target_dir, "Network_Diagnostics.zip")

    # Build a simulated ZIP archive containing certificate files
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("machine_certificates.csv", "Subject,Thumbprint,Status\nCN=US40217,ABCD1234,VALID\n")
        zf.writestr("user_certificates.csv", "UserAccount,Subject,Status\nUS\\sitlrg,CN=Louis R. Gonzales,VALID\n")
        zf.writestr("certificate_verification_summary.txt", "TEAP / 802.1X CERTIFICATE REPORT\nREADY FOR TEAP\n")
        zf.writestr("active_wlan_profiles.txt", "Profiles on interface Wi-Fi: Sonic-Wired-SOE\n")

    mock_process = MagicMock()
    mock_process.returncode = 0
    mock_process.stdout = "Collection successful."
    mock_process.stderr = ""
    mock_ps.return_value = mock_process

    try:
        resp = client.post(
            "/api/v1/windows-diag",
            headers={"X-API-Authorization": "Bearer test_token"},
            json={"hostname": hostname}
        )

        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"
        assert f"{hostname}_TEAP_Logs.zip" in resp.headers.get("content-disposition", "")

        # Verify downloaded zip content
        import io
        downloaded_zip = zipfile.ZipFile(io.BytesIO(resp.content))
        names = downloaded_zip.namelist()
        assert "machine_certificates.csv" in names
        assert "user_certificates.csv" in names
        assert "certificate_verification_summary.txt" in names

    finally:
        app.dependency_overrides.pop(get_current_user, None)
        if os.path.exists(zip_path):
            try:
                os.remove(zip_path)
            except OSError:
                pass


@patch("routers.diagnostics.run_powershell_script")
def test_windows_diag_winrm_failure(mock_ps, client):
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        username="net_admin",
        role=Role.NETWORK_ADMIN,
        permissions=["networkManagement"]
    )

    mock_process = MagicMock()
    mock_process.returncode = 1
    mock_process.stdout = ""
    mock_process.stderr = "Failed to connect to US40217. Ensure WinRM is active."
    mock_ps.return_value = mock_process

    try:
        resp = client.post(
            "/api/v1/windows-diag",
            headers={"X-API-Authorization": "Bearer test_token"},
            json={"hostname": "US40217"}
        )

        assert resp.status_code == 500
        assert "WinRM Error" in resp.json()["detail"]

    finally:
        app.dependency_overrides.pop(get_current_user, None)
