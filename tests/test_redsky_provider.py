"""
tests/test_redsky_provider.py

Unit test suite for RedskyProvider (RedSky E911 Anywhere REST API client).
Covers authentication, building registration, MSAG address validation,
and IP subnet wiremapping for Kari's Law / Ray Baum's Act compliance.
"""

from unittest.mock import MagicMock, patch
import pytest
import requests

from providers.redsky_provider import RedskyAPIError, RedskyAuthError, RedskyProvider


# =============================================================================
# 1. Initialization & Configuration Tests
# =============================================================================

def test_redsky_provider_init_with_key():
    provider = RedskyProvider(api_key="my_secret_key")
    assert provider.api_key == "my_secret_key"
    assert provider.is_configured() is True


def test_redsky_provider_init_strips_quotes():
    provider = RedskyProvider(api_key='"my_quoted_key"')
    assert provider.api_key == "my_quoted_key"
    assert provider.is_configured() is True


def test_redsky_provider_init_empty():
    with patch.dict("os.environ", {}, clear=True):
        provider = RedskyProvider(api_key="")
        assert provider.api_key == ""
        assert provider.is_configured() is False


# =============================================================================
# 2. Authentication Tests (_authenticate & _get_valid_token)
# =============================================================================

def test_redsky_authenticate_success():
    provider = RedskyProvider(api_key="valid_api_key")

    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = {"accessToken": "fake_jwt_token_12345"}

    with patch("requests.post", return_value=mock_resp) as mock_post:
        token = provider._authenticate()

        assert token == "fake_jwt_token_12345"
        assert provider._access_token == "fake_jwt_token_12345"
        assert provider._token_expiry > 0
        mock_post.assert_called_once_with(
            f"{provider.base_url}/auth-service/login/apiKey",
            json="valid_api_key",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=30,
        )


def test_redsky_authenticate_unconfigured_error():
    provider = RedskyProvider(api_key="")
    with pytest.raises(RedskyAuthError, match="not configured"):
        provider._authenticate()


def test_redsky_authenticate_401_error():
    provider = RedskyProvider(api_key="invalid_api_key")

    mock_resp = MagicMock()
    mock_resp.ok = False
    mock_resp.status_code = 401
    mock_resp.text = "API Key could not be authenticated"
    mock_resp.json.return_value = {"detail": "API Key could not be authenticated"}

    with patch("requests.post", return_value=mock_resp):
        with pytest.raises(RedskyAuthError, match="HTTP 401"):
            provider._authenticate()


def test_redsky_authenticate_network_error():
    provider = RedskyProvider(api_key="valid_api_key")

    with patch("requests.post", side_effect=requests.exceptions.ConnectionError("Connection timed out")):
        with pytest.raises(RedskyAuthError, match="connection error"):
            provider._authenticate()


def test_redsky_get_valid_token_uses_cache():
    provider = RedskyProvider(api_key="valid_api_key")
    provider._access_token = "cached_token_xyz"
    provider._token_expiry = 9999999999.0  # Far future

    with patch.object(provider, "_authenticate") as mock_auth:
        token = provider._get_valid_token()
        assert token == "cached_token_xyz"
        mock_auth.assert_not_called()


# =============================================================================
# 3. Microservice Request & Re-auth Tests (_request)
# =============================================================================

def test_redsky_request_retries_on_401():
    provider = RedskyProvider(api_key="valid_key")
    provider._access_token = "expired_token"
    provider._token_expiry = 9999999999.0

    # First call returns 401; second call returns 200 OK
    resp_401 = MagicMock(status_code=401, ok=False, text="Unauthorized")
    resp_401.json.return_value = {"message": "Token expired"}

    resp_200 = MagicMock(status_code=200, ok=True)
    resp_200.json.return_value = [{"id": "b1", "name": "Headquarters"}]

    with patch("requests.request", side_effect=[resp_401, resp_200]), \
         patch.object(provider, "_authenticate", return_value="new_fresh_token"):
        result = provider._request("GET", "/buildings", service="geography-service")
        assert result == [{"id": "b1", "name": "Headquarters"}]


def test_redsky_request_http_failure():
    provider = RedskyProvider(api_key="valid_key")
    provider._access_token = "token"
    provider._token_expiry = 9999999999.0

    resp_500 = MagicMock(status_code=500, ok=False, text="Internal Server Error")
    resp_500.json.return_value = {"message": "Service unavailable"}

    with patch("requests.request", return_value=resp_500):
        with pytest.raises(RedskyAPIError, match="HTTP 500"):
            provider._request("GET", "/buildings", service="geography-service")


# =============================================================================
# 4. Geography Service (Buildings & Addresses) Tests
# =============================================================================

def test_redsky_get_buildings():
    provider = RedskyProvider(api_key="valid_key")
    sample_buildings = [
        {"id": "bldg-1", "name": "Austin HQ", "msagValid": "VALID"},
        {"id": "bldg-2", "name": "Dallas Branch", "msagValid": "VALID"},
    ]

    with patch.object(provider, "_request", return_value=sample_buildings):
        buildings = provider.get_buildings()
        assert len(buildings) == 2
        assert buildings[0]["name"] == "Austin HQ"


def test_redsky_get_building_by_name():
    provider = RedskyProvider(api_key="valid_key")
    sample_buildings = [
        {"id": "bldg-1", "name": "SIG_LouisGonzales", "msagValid": "VALID"},
    ]

    with patch.object(provider, "get_buildings", return_value=sample_buildings):
        found = provider.get_building_by_name("sig_louisgonzales")
        assert found is not None
        assert found["id"] == "bldg-1"

        not_found = provider.get_building_by_name("NonExistent")
        assert not_found is None


def test_redsky_create_building():
    provider = RedskyProvider(api_key="valid_key")

    with patch.object(provider, "_request") as mock_req:
        mock_req.return_value = {
            "id": "bldg-new-123",
            "name": "SIG_LouisGonzales",
            "msagValid": "VALID",
            "status": "APPROVED",
        }

        result = provider.create_building(
            name="SIG_LouisGonzales",
            address1="1234 Main St",
            city="Austin",
            state="TX",
            postal_code="78701",
            country="United States",
        )

        assert result["id"] == "bldg-new-123"
        assert result["msagValid"] == "VALID"
        mock_req.assert_called_once_with(
            "POST",
            "/buildings",
            service="geography-service",
            json={
                "name": "SIG_LouisGonzales",
                "fullAddress": "1234 Main St, Austin, TX 78701",
            },
        )


def test_redsky_get_or_create_building_existing():
    provider = RedskyProvider(api_key="valid_key")
    existing_building = {"id": "bldg-exist", "name": "Austin Branch"}

    with patch.object(provider, "get_building_by_name", return_value=existing_building), \
         patch.object(provider, "create_building") as mock_create:
        bldg, was_created = provider.get_or_create_building(
            name="Austin Branch",
            address1="100 Congress Ave",
            city="Austin",
            state="TX",
            postal_code="78701",
        )
        assert was_created is False
        assert bldg["id"] == "bldg-exist"
        mock_create.assert_not_called()


def test_redsky_get_or_create_building_new():
    provider = RedskyProvider(api_key="valid_key")
    new_building = {"id": "bldg-created", "name": "Houston Lab"}

    with patch.object(provider, "get_building_by_name", return_value=None), \
         patch.object(provider, "create_building", return_value=new_building) as mock_create:
        bldg, was_created = provider.get_or_create_building(
            name="Houston Lab",
            address1="500 Main St",
            city="Houston",
            state="TX",
            postal_code="77002",
        )
        assert was_created is True
        assert bldg["id"] == "bldg-created"
        mock_create.assert_called_once()


# =============================================================================
# 5. Networking Service (Wiremapping Subnets) Tests
# =============================================================================

def test_redsky_map_subnet_to_building_slash24():
    provider = RedskyProvider(api_key="valid_key")

    with patch.object(provider, "_request") as mock_req:
        mock_req.return_value = {"id": "ip-range-1", "locationId": "bldg-1"}

        result = provider.map_subnet_to_building(
            building_id="bldg-1",
            subnet_cidr="10.10.1.0/24",
            description="Branch Voice VLAN",
        )

        assert result["id"] == "ip-range-1"
        mock_req.assert_called_once_with(
            "POST",
            "/ipRange",
            service="networking-service",
            params={},
            json={
                "locationId": "bldg-1",
                "ipAddressLow": "10.10.1.1",
                "ipAddressHigh": "10.10.1.254",
                "description": "Branch Voice VLAN",
            },
        )


def test_redsky_map_subnet_to_building_slash32():
    provider = RedskyProvider(api_key="valid_key")

    with patch.object(provider, "_request") as mock_req:
        mock_req.return_value = {"id": "ip-range-2"}

        provider.map_subnet_to_building(
            building_id="bldg-2",
            subnet_cidr="192.168.1.50/32",
        )

        mock_req.assert_called_once_with(
            "POST",
            "/ipRange",
            service="networking-service",
            params={},
            json={
                "locationId": "bldg-2",
                "ipAddressLow": "192.168.1.50",
                "ipAddressHigh": "192.168.1.50",
                "description": "Branch subnet 192.168.1.50/32",
            },
        )


def test_redsky_map_subnet_invalid_cidr():
    provider = RedskyProvider(api_key="valid_key")
    with pytest.raises(ValueError, match="Invalid CIDR subnet"):
        provider.map_subnet_to_building(building_id="bldg-1", subnet_cidr="invalid_ip_range")


# =============================================================================
# 5. Device User & MyE911 Provisioning Tests
# =============================================================================

def test_redsky_get_companies():
    provider = RedskyProvider(api_key="valid_key")
    with patch.object(provider, "_request") as mock_req:
        mock_req.return_value = [{"id": "comp-123", "name": "Sonic Healthcare USA"}]
        companies = provider.get_companies()
        assert len(companies) == 1
        assert companies[0]["id"] == "comp-123"
        mock_req.assert_called_once_with("GET", "/company", service="admin-service")


def test_redsky_get_company_id():
    provider = RedskyProvider(api_key="valid_key")
    with patch.object(provider, "get_companies") as mock_comp:
        mock_comp.return_value = [{"id": "comp-456", "name": "Test Org"}]
        assert provider.get_company_id() == "comp-456"


def test_redsky_get_device_users():
    provider = RedskyProvider(api_key="valid_key")
    with patch.object(provider, "_request") as mock_req:
        mock_req.return_value = [
            {"id": "user-1", "email": "lgonzales@corp.internal", "phoneNumber": "5124391662"}
        ]
        users = provider.get_device_users(company_id="comp-123", search_term="lgonzales")
        assert len(users) == 1
        assert users[0]["id"] == "user-1"
        mock_req.assert_called_once_with(
            "GET",
            "/deviceUser/company/comp-123",
            service="admin-service",
            params={"searchTerm": "lgonzales"},
        )


def test_redsky_get_device_user_by_email():
    provider = RedskyProvider(api_key="valid_key")
    with patch.object(provider, "get_device_users") as mock_users:
        mock_users.return_value = [
            {"id": "user-1", "email": "lgonzales@corp.internal", "heldUserId": "lgonzales@corp.internal"}
        ]
        found = provider.get_device_user_by_email("LGONZALES@corp.internal")
        assert found is not None
        assert found["id"] == "user-1"


def test_redsky_sync_device_user_mye911_update_existing():
    provider = RedskyProvider(api_key="valid_key")
    existing_user = {
        "id": "user-uuid-1",
        "email": "lgonzales@corp.internal",
        "firstName": "Louis",
        "lastName": "Gonzales",
        "heldUserId": "lgonzales@corp.internal",
    }
    with patch.object(provider, "get_device_user_by_email", return_value=existing_user), \
         patch.object(provider, "_request") as mock_req:
        mock_req.return_value = {"id": "user-uuid-1", "userOptions": "MYE911_ENABLED"}

        res = provider.sync_device_user_mye911(
            email="lgonzales@corp.internal",
            phone_number="+1 (512) 439-1662",
            first_name="Louis",
            last_name="Gonzales",
            enable_mye911=True,
        )

        assert res["status"] == "SUCCESS"
        assert res["action"] == "UPDATED"
        assert res["phone_number"] == "+15124391662"
        mock_req.assert_called_once_with(
            "PUT",
            "/deviceUser",
            service="admin-service",
            json={
                "id": "user-uuid-1",
                "email": "lgonzales@corp.internal",
                "firstName": "Louis",
                "lastName": "Gonzales",
                "phoneNumber": "+15124391662",
                "callbackNumber": "+15124391662",
                "heldUserId": "lgonzales@corp.internal",
                "userOptions": "MYE911_ENABLED",
            },
        )


def test_redsky_sync_device_user_mye911_create_new():
    provider = RedskyProvider(api_key="valid_key")
    with patch.object(provider, "get_device_user_by_email", return_value=None), \
         patch.object(provider, "get_company_id", return_value="comp-uuid-99"), \
         patch.object(provider, "_request") as mock_req:
        mock_req.return_value = {"id": "new-user-uuid", "userOptions": "MYE911_ENABLED"}

        res = provider.sync_device_user_mye911(
            email="newuser@corp.internal",
            phone_number="5124391662",
            first_name="Jane",
            last_name="Doe",
            enable_mye911=True,
        )

        assert res["status"] == "SUCCESS"
        assert res["action"] == "CREATED"
        assert res["user_id"] == "new-user-uuid"
        mock_req.assert_called_once_with(
            "POST",
            "/deviceUser",
            service="admin-service",
            json={
                "companyId": "comp-uuid-99",
                "email": "newuser@corp.internal",
                "firstName": "Jane",
                "lastName": "Doe",
                "phoneNumber": "5124391662",
                "callbackNumber": "5124391662",
                "heldUserId": "newuser@corp.internal",
                "userOptions": "MYE911_ENABLED",
            },
        )


def test_redsky_sync_device_user_mye911_falls_back_to_10_digit_nanp():
    provider = RedskyProvider(api_key="valid_key")
    sent = []

    def fake_request(method, endpoint, **kwargs):
        body = dict(kwargs["json"])
        sent.append(body)
        if body["phoneNumber"].startswith("+"):
            raise RedskyAPIError("RedSky API HTTP 400: '+15124391682' is not a valid phone number.")
        return {"id": "user-uuid-2", "userOptions": "MYE911_ENABLED"}

    with patch.object(provider, "get_device_user_by_email", return_value=None), \
         patch.object(provider, "get_company_id", return_value="comp-uuid-99"), \
         patch.object(provider, "_request", side_effect=fake_request):
        res = provider.sync_device_user_mye911(
            email="tmustwillo@corp.internal",
            phone_number="+15124391682",
            first_name="Thomas",
            last_name="Mustwillo",
            enable_mye911=True,
        )

    assert len(sent) == 2
    assert sent[0]["phoneNumber"] == "+15124391682"
    assert sent[1]["phoneNumber"] == "5124391682"
    assert sent[1]["callbackNumber"] == "5124391682"
    assert res["status"] == "SUCCESS"
    assert res["phone_number"] == "5124391682"

