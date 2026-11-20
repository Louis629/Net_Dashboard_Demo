# tests/conftest.py
import sys
import os
from pathlib import Path
import pytest

# Append project root to sys.path
root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

# Ensure import-time provider initialization succeeds during pytest collection without .env
os.environ.setdefault("ENVIRONMENT", "LOCAL")
os.environ.setdefault("MERAKI_API_KEY", "dummy_api_key_123456789")
os.environ.setdefault("MERAKI_ORG_ID", "dummy_org_id_987654321")
os.environ.setdefault("ISE_PAN_IP", "127.0.0.1")
os.environ.setdefault("ISE_ERS_USER", "dummy_ise_user")
os.environ.setdefault("ISE_ERS_PASS", "dummy_ise_pass")
os.environ.setdefault("VALKEY_URL", "redis://127.0.0.1:6379/0")


@pytest.fixture(autouse=True)
def mock_env_vars(monkeypatch):
    """
    Globally injects dummy environment variables for unit testing 
    to prevent provider instantiation exceptions.
    """
    monkeypatch.setenv("ENVIRONMENT", "LOCAL")
    monkeypatch.setenv("MERAKI_API_KEY", "dummy_api_key_123456789")
    monkeypatch.setenv("MERAKI_ORG_ID", "dummy_org_id_987654321")
    monkeypatch.setenv("ISE_PAN_IP", "127.0.0.1")
    monkeypatch.setenv("ISE_ERS_USER", "dummy_ise_user")
    monkeypatch.setenv("ISE_ERS_PASS", "dummy_ise_pass")
    monkeypatch.setenv("VALKEY_URL", "redis://127.0.0.1:6379/0")


@pytest.fixture(autouse=True)
def clean_dependency_overrides():
    """Ensures dependency_overrides are properly scoped per test and cleaned up."""
    yield
    try:
        from main import app
        app.dependency_overrides.clear()
    except Exception:
        pass