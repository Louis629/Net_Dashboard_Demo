import pytest
from fastapi import HTTPException
from fastapi.security import HTTPBasicCredentials
from starlette.requests import Request

import security.auth as auth


def make_request() -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "client": ("127.0.0.1", 12345),
    })


def test_local_environment_does_not_require_primary_authentication(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "LOCAL")

    assert auth.enforce_dev_environment_access(make_request(), None) is True


def test_dev_environment_requires_ldap_primary_authentication(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "DEV")

    with pytest.raises(HTTPException) as exc_info:
        auth.enforce_dev_environment_access(make_request(), None)

    assert exc_info.value.status_code == 401


def test_dev_environment_validates_ldap_primary_credentials(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "DEV")

    class SuccessfulLDAPProvider:
        def authenticate(self, username, password, target_action):
            assert username == "engineer"
            assert password == "directory-password"
            assert target_action == "Dev Sandbox Login"
            return True

    monkeypatch.setattr(auth, "LDAPSAuthProvider", SuccessfulLDAPProvider)
    credentials = HTTPBasicCredentials(username="engineer", password="directory-password")

    assert auth.enforce_dev_environment_access(make_request(), credentials) is True


def test_prod_environment_delegates_primary_authentication(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "PROD")

    assert auth.enforce_dev_environment_access(make_request(), None) is True


def test_step_up_authentication_rejects_invalid_credentials(monkeypatch):
    class FailedLDAPProvider:
        def authenticate(self, username, password, target_action):
            return False

    monkeypatch.setattr(auth, "LDAPSAuthProvider", FailedLDAPProvider)

    with pytest.raises(HTTPException) as exc_info:
        auth.authenticate_step_up_credentials("engineer", "wrong-password", "Privileged task")

    assert exc_info.value.status_code == 401


def test_step_up_authentication_accepts_valid_credentials(monkeypatch):
    class SuccessfulLDAPProvider:
        def authenticate(self, username, password, target_action):
            assert username == "engineer"
            assert password == "directory-password"
            assert target_action == "Privileged task"
            return True

    monkeypatch.setattr(auth, "LDAPSAuthProvider", SuccessfulLDAPProvider)

    assert auth.authenticate_step_up_credentials(
        "engineer",
        "directory-password",
        "Privileged task"
    ) is None


def test_step_up_authentication_requires_credentials(monkeypatch):
    class UnexpectedLDAPProvider:
        def authenticate(self, username, password, target_action):
            pytest.fail("LDAP should not be called without credentials")

    monkeypatch.setattr(auth, "LDAPSAuthProvider", UnexpectedLDAPProvider)

    with pytest.raises(HTTPException) as exc_info:
        auth.authenticate_step_up_credentials("", "", "Privileged task")

    assert exc_info.value.status_code == 401
