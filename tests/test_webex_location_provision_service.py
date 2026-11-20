"""
tests/test_webex_location_provision_service.py

Unit test suite for Phase 1 Meraki-to-Webex Calling Location and DID Provisioning:
- WebexCallingProvider location and number methods
- WebexLocationProvisionService (preview, sync, DID provisioning)
- Telecom router endpoints (/locations/meraki-preview, /locations/sync-meraki, /locations/{loc_id}/dids)
"""

from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from main import app
from core.dependencies import get_meraki_provider
from providers.webex_calling_provider import WebexAPIError, WebexCallingProvider
from providers.meraki_provider import MerakiProvider
from providers.redsky_provider import RedskyAPIError, RedskyAuthError, RedskyProvider
from services.webex_location_provision_service import WebexLocationProvisionService
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
# 1. Provider-Level Tests (WebexCallingProvider)
# =============================================================================

def test_webex_provider_create_location():
    provider = WebexCallingProvider(access_token="fake_token")
    with patch.object(provider, "_request") as mock_req:
        mock_req.return_value = {"id": "loc-123", "name": "US-DAL-Branch01"}

        result = provider.create_location(
            name="US-DAL-Branch01",
            address1="123 Main St",
            city="Dallas",
            state="TX",
            postal_code="75201",
            country="United States",
            time_zone="America/Chicago",
        )

        assert result["id"] == "loc-123"
        mock_req.assert_called_once()
        call_args = mock_req.call_args
        assert call_args[0] == ("POST", "/locations")
        payload = call_args[1]["json"]
        assert payload["name"] == "US-DAL-Branch01"
        assert payload["address"]["country"] == "US"
        assert payload["address"]["postalCode"] == "75201"


def test_webex_provider_get_or_create_location():
    provider = WebexCallingProvider(access_token="fake_token")
    with patch.object(provider, "get_locations") as mock_get_locs, \
         patch.object(provider, "create_location") as mock_create:

        # Case 1: Location already exists
        mock_get_locs.return_value = [{"id": "loc-exist", "name": "Existing Site"}]
        loc, created = provider.get_or_create_location(
            name="existing site",
            address1="123 Main St",
            city="Dallas",
            state="TX",
            postal_code="75201",
        )
        assert loc["id"] == "loc-exist"
        assert created is False
        mock_create.assert_not_called()

        # Case 2: Location does not exist
        mock_get_locs.return_value = []
        mock_create.return_value = {"id": "loc-new", "name": "New Site"}
        loc, created = provider.get_or_create_location(
            name="New Site",
            address1="456 Elm St",
            city="Austin",
            state="TX",
            postal_code="78701",
        )
        assert loc["id"] == "loc-new"
        assert created is True
        mock_create.assert_called_once()


def test_webex_provider_enable_calling_for_location():
    provider = WebexCallingProvider(access_token="fake_token")
    with patch.object(provider, "_request") as mock_req:
        mock_req.return_value = {"status": "SUCCESS"}

        res = provider.enable_calling_for_location(
            location_id="loc-123",
            connection_type="LOCAL_GATEWAY",
            routing_group_id="rg-999",
            internal_extension_length=4,
            name="Test Site",
        )

        assert res["status"] == "SUCCESS"
        mock_req.assert_called_with(
            "POST",
            "/telephony/config/locations",
            json={
                "id": "loc-123",
                "name": "Test Site",
                "announcementLanguage": "en_us",
                "preferredLanguage": "en_US",
            }
        )


def test_webex_provider_add_location_numbers_and_set_main():
    provider = WebexCallingProvider(access_token="fake_token")
    with patch.object(provider, "_request") as mock_req:
        mock_req.return_value = {"status": "SUCCESS"}

        # Test adding numbers
        provider.add_location_numbers(
            location_id="loc-123",
            phone_numbers=["+15125550199", "15125550200"],
            state="ACTIVE"
        )
        mock_req.assert_called_with(
            "POST",
            "/telephony/config/locations/loc-123/numbers",
            json={"phoneNumbers": ["+15125550199", "+15125550200"], "state": "ACTIVE"}
        )

        # Test setting main number
        provider.set_location_main_number("loc-123", "+15125550199")
        mock_req.assert_called_with(
            "PUT",
            "/telephony/config/locations/loc-123",
            json={"callingLineId": {"phoneNumber": "+15125550199"}}
        )


# =============================================================================
# 2. Service-Level Tests (WebexLocationProvisionService)
# =============================================================================

@pytest.fixture
def mock_meraki_inventory():
    return [
        {
            "network_id": "N_111",
            "network_name": "CLH - Honolulu Lab",
            "address": "2230 Liliha St, Honolulu, HI 96817",
            "time_zone": "Pacific/Honolulu",
            "appliance_models": ["MX68"],
            "serial_numbers": ["Q2MN-AAAA-BBBB"],
            "device_count": 1,
            "lookup_error": False,
        },
        {
            "network_id": "N_222",
            "network_name": "CLH - Incomplete Address Branch",
            "address": "Incomplete Address Without City",
            "time_zone": "America/Chicago",
            "appliance_models": ["Z4"],
            "serial_numbers": ["Q2MN-CCCC-DDDD"],
            "device_count": 1,
            "lookup_error": False,
        },
    ]


def test_service_preview_meraki_locations(mock_meraki_inventory):
    mock_webex = MagicMock(spec=WebexCallingProvider)
    mock_meraki = MagicMock(spec=MerakiProvider)

    mock_meraki.get_appliance_location_inventory.return_value = mock_meraki_inventory
    mock_webex.get_locations.return_value = [
        {"id": "wxc_loc_111", "name": "CLH - Honolulu Lab", "timeZone": "Pacific/Honolulu"}
    ]

    service = WebexLocationProvisionService(webex_provider=mock_webex, meraki_provider=mock_meraki)
    preview = service.preview_meraki_locations()

    assert preview.total_meraki_networks == 2
    assert preview.existing_in_webex_count == 1
    assert preview.new_to_create_count == 0
    assert preview.invalid_address_count == 1

    honolulu_loc = next(loc for loc in preview.locations if loc.network_id == "N_111")
    assert honolulu_loc.exists_in_webex is True
    assert honolulu_loc.webex_location_id == "wxc_loc_111"
    assert honolulu_loc.is_valid is True
    assert honolulu_loc.city == "Honolulu"
    assert honolulu_loc.state == "HI"
    assert honolulu_loc.zip_code == "96817"

    incomplete_loc = next(loc for loc in preview.locations if loc.network_id == "N_222")
    assert incomplete_loc.is_valid is False
    assert len(incomplete_loc.missing_fields) > 0


def test_service_sync_locations_dry_run_and_live(mock_meraki_inventory):
    mock_webex = MagicMock(spec=WebexCallingProvider)
    mock_meraki = MagicMock(spec=MerakiProvider)

    mock_meraki.get_appliance_location_inventory.return_value = [
        mock_meraki_inventory[0]  # Valid address
    ]
    # Simulate site does not yet exist in Webex
    mock_webex.get_locations.return_value = []
    mock_webex.create_location.return_value = {"id": "new-wxc-loc-777", "name": "CLH - Honolulu Lab"}

    service = WebexLocationProvisionService(webex_provider=mock_webex, meraki_provider=mock_meraki)

    # 1. Dry Run Execution
    dry_run_result = service.sync_locations(dry_run=True, connection_type="LOCAL_GATEWAY", internal_extension_length=4)
    assert dry_run_result.dry_run is True
    assert dry_run_result.created_count == 1
    assert dry_run_result.results[0].status == "DRY_RUN"
    mock_webex.create_location.assert_not_called()
    mock_webex.enable_calling_for_location.assert_not_called()

    # 2. Live Mutation Execution
    live_result = service.sync_locations(dry_run=False, connection_type="LOCAL_GATEWAY", internal_extension_length=4)
    assert live_result.dry_run is False
    assert live_result.created_count == 1
    assert live_result.results[0].status == "CREATED"
    assert live_result.results[0].webex_location_id == "new-wxc-loc-777"
    mock_webex.create_location.assert_called_once()
    mock_webex.enable_calling_for_location.assert_called_once_with(
        location_id="new-wxc-loc-777",
        connection_type="LOCAL_GATEWAY",
        routing_group_id=None,
        internal_extension_length=4,
        routing_prefix=None,
        name="CLH - Honolulu Lab",
    )


def test_service_provision_dids_validation_and_execution():
    mock_webex = MagicMock(spec=WebexCallingProvider)
    mock_webex.get_location_details.return_value = {"id": "loc-123", "name": "Test Site"}

    service = WebexLocationProvisionService(webex_provider=mock_webex)

    # 1. Format rejection for invalid numbers
    with pytest.raises(ValueError, match="Invalid E.164 format"):
        service.provision_dids(
            location_id="loc-123",
            phone_numbers=["12345", "not-a-number"],
            dry_run=True,
        )

    # 2. Dry run with valid numbers
    dry_result = service.provision_dids(
        location_id="loc-123",
        phone_numbers=["+15125550199", "15125550200"],
        main_number="+15125550199",
        dry_run=True,
    )
    assert dry_result.dry_run is True
    assert dry_result.status == "SUCCESS"
    assert "+15125550199" in dry_result.phone_numbers_added
    assert "+15125550200" in dry_result.phone_numbers_added
    mock_webex.add_location_numbers.assert_not_called()

    # 3. Live execution
    live_result = service.provision_dids(
        location_id="loc-123",
        phone_numbers=["+15125550199"],
        main_number="+15125550199",
        dry_run=False,
    )
    assert live_result.dry_run is False
    assert live_result.status == "SUCCESS"
    mock_webex.add_location_numbers.assert_called_once_with("loc-123", ["+15125550199"], state="ACTIVE")
    mock_webex.set_location_main_number.assert_called_once_with("loc-123", "+15125550199")


# =============================================================================
# 3. Router-Level API Endpoint Tests
# =============================================================================

def test_api_locations_meraki_preview():
    mock_meraki = MagicMock(spec=MerakiProvider)
    mock_meraki.get_appliance_location_inventory.return_value = [
        {
            "network_id": "N_100",
            "network_name": "Austin Clinic",
            "address": "100 Congress Ave, Austin, TX 78701",
            "time_zone": "America/Chicago",
            "appliance_models": ["MX68"],
            "serial_numbers": ["Q2MN-1111"],
            "device_count": 1,
            "lookup_error": False,
        }
    ]
    app.dependency_overrides[get_meraki_provider] = lambda: mock_meraki

    try:
        with patch("routers.telecom.WebexCallingProvider") as mock_webex_cls:
            mock_webex = MagicMock(spec=WebexCallingProvider)
            mock_webex.get_locations.return_value = []
            mock_webex_cls.return_value = mock_webex

            response = client.get(
                "/api/v1/telecom/locations/meraki-preview",
                headers={"X-Webex-Access-Token": "test_bearer_token"}
            )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "SUCCESS"
            assert data["total_meraki_networks"] == 1
            assert data["locations"][0]["location_name"] == "Austin Clinic"
            assert data["locations"][0]["city"] == "Austin"
    finally:
        app.dependency_overrides.pop(get_meraki_provider, None)


def test_api_locations_sync_meraki_dry_run():
    mock_meraki = MagicMock(spec=MerakiProvider)
    mock_meraki.get_appliance_location_inventory.return_value = [
        {
            "network_id": "N_100",
            "network_name": "Austin Clinic",
            "address": "100 Congress Ave, Austin, TX 78701",
            "time_zone": "America/Chicago",
            "appliance_models": ["MX68"],
            "serial_numbers": ["Q2MN-1111"],
            "device_count": 1,
            "lookup_error": False,
        }
    ]
    app.dependency_overrides[get_meraki_provider] = lambda: mock_meraki

    try:
        with patch("routers.telecom.WebexCallingProvider") as mock_webex_cls:
            mock_webex = MagicMock(spec=WebexCallingProvider)
            mock_webex.get_locations.return_value = []
            mock_webex_cls.return_value = mock_webex

            payload = {
                "network_ids": ["N_100"],
                "dry_run": True,
                "connection_type": "LOCAL_GATEWAY",
                "internal_extension_length": 4,
            }

            response = client.post(
                "/api/v1/telecom/locations/sync-meraki",
                json=payload,
                headers={"X-Webex-Access-Token": "test_bearer_token"}
            )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "SUCCESS"
            assert data["dry_run"] is True
            assert data["results"][0]["status"] == "DRY_RUN"
    finally:
        app.dependency_overrides.pop(get_meraki_provider, None)


def test_api_locations_provision_dids_dry_run():
    with patch("routers.telecom.WebexCallingProvider") as mock_webex_cls:
        mock_webex = MagicMock(spec=WebexCallingProvider)
        mock_webex.get_location_details.return_value = {"id": "loc-555", "name": "Dallas Hub"}
        mock_webex_cls.return_value = mock_webex

        payload = {
            "phone_numbers": ["+15125550199"],
            "main_number": "+15125550199",
            "dry_run": True,
        }

        response = client.post(
            "/api/v1/telecom/locations/loc-555/dids",
            json=payload,
            headers={"X-Webex-Access-Token": "test_bearer_token"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "SUCCESS"
        assert data["dry_run"] is True
        assert "+15125550199" in data["phone_numbers_added"]


def test_api_locations_sync_meraki_live_requires_step_up():
    payload = {
        "network_ids": ["N_100"],
        "dry_run": False,
        "connection_type": "LOCAL_GATEWAY",
        "internal_extension_length": 4,
    }

    response = client.post(
        "/api/v1/telecom/locations/sync-meraki",
        json=payload,
        headers={"X-Webex-Access-Token": "test_bearer_token"}
    )
    assert response.status_code == 401
    assert "LDAP step-up authentication failed" in response.json()["detail"]


def test_api_locations_sync_meraki_live_with_valid_step_up():
    mock_meraki = MagicMock(spec=MerakiProvider)
    mock_meraki.get_appliance_location_inventory.return_value = [
        {
            "network_id": "N_100",
            "network_name": "Austin Clinic",
            "address": "100 Congress Ave, Austin, TX 78701",
            "time_zone": "America/Chicago",
            "appliance_models": ["MX68"],
            "serial_numbers": ["Q2MN-1111"],
            "device_count": 1,
            "lookup_error": False,
        }
    ]
    app.dependency_overrides[get_meraki_provider] = lambda: mock_meraki

    try:
        with patch("routers.telecom.WebexCallingProvider") as mock_webex_cls, \
             patch("routers.telecom.require_workspace_step_up") as mock_step_up:
            mock_webex = MagicMock(spec=WebexCallingProvider)
            mock_webex.get_locations.return_value = []
            mock_webex.create_location.return_value = {"id": "loc-new", "name": "Austin Clinic"}
            mock_webex_cls.return_value = mock_webex

            payload = {
                "network_ids": ["N_100"],
                "dry_run": False,
                "connection_type": "LOCAL_GATEWAY",
                "internal_extension_length": 4,
                "step_up_user": "test_admin",
                "step_up_pass": "secret_pass",
            }

            response = client.post(
                "/api/v1/telecom/locations/sync-meraki",
                json=payload,
                headers={"X-Webex-Access-Token": "test_bearer_token"}
            )
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "SUCCESS"
            assert data["dry_run"] is False
            mock_step_up.assert_called_once()
    finally:
        app.dependency_overrides.pop(get_meraki_provider, None)


def test_api_locations_provision_dids_live_requires_step_up():
    payload = {
        "phone_numbers": ["+15125550199"],
        "main_number": "+15125550199",
        "dry_run": False,
    }

    response = client.post(
        "/api/v1/telecom/locations/loc-555/dids",
        json=payload,
        headers={"X-Webex-Access-Token": "test_bearer_token"}
    )
    assert response.status_code == 401
    assert "LDAP step-up authentication failed" in response.json()["detail"]


def test_api_locations_provision_dids_live_with_valid_step_up():
    with patch("routers.telecom.WebexCallingProvider") as mock_webex_cls, \
         patch("routers.telecom.require_workspace_step_up") as mock_step_up:
        mock_webex = MagicMock(spec=WebexCallingProvider)
        mock_webex.get_location_details.return_value = {"id": "loc-555", "name": "Dallas Hub"}
        mock_webex.add_location_numbers.return_value = {"status": "SUCCESS"}
        mock_webex_cls.return_value = mock_webex

        payload = {
            "phone_numbers": ["+15125550199"],
            "main_number": "+15125550199",
            "dry_run": False,
            "step_up_user": "test_admin",
            "step_up_pass": "secret_pass",
        }

        response = client.post(
            "/api/v1/telecom/locations/loc-555/dids",
            json=payload,
            headers={"X-Webex-Access-Token": "test_bearer_token"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "SUCCESS"
        assert data["dry_run"] is False
        mock_step_up.assert_called_once()


def test_preview_with_string_appliance_models_and_serials():
    mock_webex = MagicMock(spec=WebexCallingProvider)
    mock_meraki = MagicMock(spec=MerakiProvider)

    mock_meraki.get_appliance_location_inventory.return_value = [
        {
            "network_id": "N_PROD_1",
            "network_name": "Sonic Austin Lab",
            "address": "123 Main St, Austin, TX 78701",
            "time_zone": "America/Chicago",
            "appliance_models": "MX68, MX68W",
            "serial_numbers": "Q2KY-BZ22-VAVA, Q2KY-LC5N-3PUQ",
            "device_count": 2,
        }
    ]
    mock_webex.get_locations.return_value = []

    service = WebexLocationProvisionService(webex_provider=mock_webex, meraki_provider=mock_meraki)
    preview = service.preview_meraki_locations()

    assert preview.total_meraki_networks == 1
    item = preview.locations[0]
    assert item.appliance_models == ["MX68", "MX68W"]
    assert item.serial_numbers == ["Q2KY-BZ22-VAVA", "Q2KY-LC5N-3PUQ"]
    assert item.is_valid is True


def test_complex_and_unformatted_meraki_address_parsing():
    from routers.meraki_admin import _parse_webex_location_address

    # Case 1: Suite in separate comma section
    res1 = _parse_webex_location_address("12720 Hillcrest Road, Suite 725, Dallas, TX 75230")
    assert res1["Address Line 1"] == "12720 Hillcrest Road, Suite 725"
    assert res1["City"] == "Dallas"
    assert res1["State"] == "TX"
    assert res1["ZIP"] == "75230"
    assert res1["Country"] == "United States"

    # Case 2: Unit in separate comma section
    res2 = _parse_webex_location_address("1212 N Josey Ln, Unit 110, Carrolton, TX 75006")
    assert res2["Address Line 1"] == "1212 N Josey Ln, Unit 110"
    assert res2["City"] == "Carrolton"
    assert res2["State"] == "TX"
    assert res2["ZIP"] == "75006"

    # Case 3: Ste in separate comma section
    res3 = _parse_webex_location_address("1060 S Preston Rd, Ste 104, Celina, TX 75009")
    assert res3["Address Line 1"] == "1060 S Preston Rd, Ste 104"
    assert res3["City"] == "Celina"
    assert res3["State"] == "TX"
    assert res3["ZIP"] == "75009"

    # Case 4: No comma between street and city (known city)
    res4 = _parse_webex_location_address("4621 S STAPLES CORPUS CHRISTI, TX 78412")
    assert res4["Address Line 1"] == "4621 S STAPLES"
    assert res4["City"] == "CORPUS CHRISTI"
    assert res4["State"] == "TX"
    assert res4["ZIP"] == "78412"

    # Case 5: Period after street suffix and trailing period on state
    res5 = _parse_webex_location_address("115 Northshore blvd. Portland, TX. 78374")
    assert res5["Address Line 1"] == "115 Northshore blvd."
    assert res5["City"] == "Portland"
    assert res5["State"] == "TX"
    assert res5["ZIP"] == "78374"

    # Case 6: Street suffix without comma before city
    res6 = _parse_webex_location_address("4835 STAPLES ST. CORPUS CHRISTI, TX 78411")
    assert res6["Address Line 1"] == "4835 STAPLES ST."
    assert res6["City"] == "CORPUS CHRISTI"
    assert res6["State"] == "TX"
    assert res6["ZIP"] == "78411"

    # Case 7: Highway designator without comma before city
    res7 = _parse_webex_location_address("2011 HWY 35N ROCKPORT, TX 78382")
    assert res7["Address Line 1"] == "2011 HWY 35N"
    assert res7["City"] == "ROCKPORT"
    assert res7["State"] == "TX"
    assert res7["ZIP"] == "78382"

    # Case 8: Secondary unit with pound sign without comma before city
    res8 = _parse_webex_location_address("4325 N JOSEY LN #105 CARROLLTON, TX 75010")
    assert res8["Address Line 1"] in ("4325 N JOSEY LN #105", "4325 N JOSEY LN # 105")
    assert res8["City"] == "CARROLLTON"
    assert res8["State"] == "TX"
    assert res8["ZIP"] == "75010"

    # Case 9: Zero commas anywhere in address
    res9 = _parse_webex_location_address("3600 Gus Thomasson Rd Unit 117 Mesquite TX 75150")
    assert res9["Address Line 1"] in ("3600 Gus Thomasson Rd Unit 117", "3600 Gus Thomasson Rd, Unit 117")
    assert res9["City"] == "Mesquite"
    assert res9["State"] == "TX"
    assert res9["ZIP"] == "75150"

    # Case 10: Preserves unrecognizable address for manual admin review
    res10 = _parse_webex_location_address("Unrecognized clinic location")
    assert res10["Address Line 1"] == "Unrecognized clinic location"
    assert res10["City"] == ""
    assert res10["State"] == ""
    assert res10["ZIP"] == ""


def test_preview_meraki_locations_with_suite_and_unformatted_addresses():
    mock_webex = MagicMock(spec=WebexCallingProvider)
    mock_meraki = MagicMock(spec=MerakiProvider)

    mock_meraki.get_appliance_location_inventory.return_value = [
        {
            "network_id": "N_1",
            "network_name": "CPLDAL_255622_IOP",
            "address": "12720 Hillcrest Road, Suite 725, Dallas, TX 75230",
            "time_zone": "America/Chicago",
            "appliance_models": ["Z4"],
            "serial_numbers": ["Q2MN-1111"],
            "device_count": 1,
            "lookup_error": False,
        },
        {
            "network_id": "N_2",
            "network_name": "CPLCOC_49282_IOP",
            "address": "4621 S STAPLES CORPUS CHRISTI, TX 78412",
            "time_zone": "America/Chicago",
            "appliance_models": ["Z4"],
            "serial_numbers": ["Q2MN-2222"],
            "device_count": 1,
            "lookup_error": False,
        },
        {
            "network_id": "N_3",
            "network_name": "CPLDAL_260737_IOP",
            "address": "3600 Gus Thomasson Rd Unit 117 Mesquite TX 75150",
            "time_zone": "America/Chicago",
            "appliance_models": ["Z4"],
            "serial_numbers": ["Q2MN-3333"],
            "device_count": 1,
            "lookup_error": False,
        },
    ]
    mock_webex.get_locations.return_value = []

    service = WebexLocationProvisionService(webex_provider=mock_webex, meraki_provider=mock_meraki)
    preview = service.preview_meraki_locations()

    assert preview.total_meraki_networks == 3
    assert preview.new_to_create_count == 3
    assert preview.invalid_address_count == 0

    loc1 = next(l for l in preview.locations if l.network_id == "N_1")
    assert loc1.is_valid is True
    assert loc1.address_line_1 == "12720 Hillcrest Road, Suite 725"
    assert loc1.city == "Dallas"
    assert loc1.state == "TX"
    assert loc1.zip_code == "75230"

    loc2 = next(l for l in preview.locations if l.network_id == "N_2")
    assert loc2.is_valid is True
    assert loc2.address_line_1 == "4621 S STAPLES"
    assert loc2.city == "CORPUS CHRISTI"
    assert loc2.state == "TX"
    assert loc2.zip_code == "78412"

    loc3 = next(l for l in preview.locations if l.network_id == "N_3")
    assert loc3.is_valid is True
    assert loc3.address_line_1 in ("3600 Gus Thomasson Rd Unit 117", "3600 Gus Thomasson Rd, Unit 117")
    assert loc3.city == "Mesquite"
    assert loc3.state == "TX"
    assert loc3.zip_code == "75150"


def test_webex_provider_get_route_groups_and_trunks():
    provider = WebexCallingProvider(access_token="fake_token")
    with patch.object(provider, "_request_page") as mock_page:
        mock_page.side_effect = [
            ({"routeGroups": [{"id": "rg-1", "name": "CUCM"}]}, None),
            ({"trunks": [{"id": "trk-1", "name": "ussigatxcvg003p_01"}]}, None),
        ]

        rgs = provider.get_route_groups()
        trks = provider.get_trunks()
        assert len(rgs) == 1
        assert rgs[0]["name"] == "CUCM"
        assert len(trks) == 1
        assert trks[0]["name"] == "ussigatxcvg003p_01"


def test_webex_provider_get_pstn_routing_options():
    provider = WebexCallingProvider(access_token="fake_token")
    with patch.object(provider, "get_route_groups") as mock_rg, \
         patch.object(provider, "get_trunks") as mock_trk:
        mock_rg.return_value = [{"id": "rg-1", "name": "CUCM"}]
        mock_trk.return_value = [{"id": "trk-1", "name": "CLH Aiea Paging"}]

        options = provider.get_pstn_routing_options()
        assert len(options) == 2
        assert options[0]["name"] == "CUCM"
        assert options[0]["type"] == "ROUTE_GROUP"
        assert options[1]["name"] == "CLH Aiea Paging"
        assert options[1]["type"] == "TRUNK"


def test_webex_provider_set_location_pstn_connection_name_resolution():
    provider = WebexCallingProvider(access_token="fake_token")
    with patch.object(provider, "get_route_groups") as mock_rg, \
         patch.object(provider, "get_trunks") as mock_trk, \
         patch.object(provider, "_request") as mock_req:
        mock_rg.return_value = [{"id": "rg-cucm-12345", "name": "CUCM"}]
        mock_trk.return_value = []
        mock_req.return_value = {"status": "SUCCESS"}

        # Name resolution: passing "CUCM" resolves to rg-cucm-12345
        res = provider.set_location_pstn_connection(
            location_id="loc-888",
            route_type="ROUTE_GROUP",
            route_id="CUCM",
        )
        assert res["status"] == "SUCCESS"
        mock_req.assert_called_once_with(
            "PUT",
            "/telephony/pstn/locations/loc-888/connection",
            json={
                "premiseRouteType": "ROUTE_GROUP",
                "premiseRouteId": "rg-cucm-12345",
            }
        )


def test_sync_locations_configures_pstn_connection():
    mock_webex = MagicMock(spec=WebexCallingProvider)
    mock_meraki = MagicMock(spec=MerakiProvider)

    mock_meraki.get_appliance_location_inventory.return_value = [
        {
            "network_id": "net-louis",
            "network_name": "SIG_LouisGonzales",
            "address": "1620 Hawk Dr, Cedar Park, TX 78613",
            "time_zone": "America/Chicago",
            "appliance_models": ["MX68"],
            "serial_numbers": ["Q2MN-0000"],
            "device_count": 1,
            "lookup_error": False,
        }
    ]
    # Location does not exist in Webex
    mock_webex.get_locations.return_value = []
    mock_webex.create_location.return_value = {"id": "loc-louis-new", "name": "SIG_LouisGonzales"}
    mock_webex.enable_calling_for_location.return_value = {"id": "loc-louis-new"}
    mock_webex.set_location_pstn_connection.return_value = {"status": "SUCCESS"}

    service = WebexLocationProvisionService(webex_provider=mock_webex, meraki_provider=mock_meraki)
    response = service.sync_locations(
        network_ids=["net-louis"],
        dry_run=False,
        connection_type="LOCAL_GATEWAY",
        routing_group_id="CUCM",
    )

    assert response.status == "SUCCESS"
    assert response.created_count == 1
    assert "Premises-based PSTN routed to CUCM" in response.results[0].details
    mock_webex.create_location.assert_called_once()
    mock_webex.enable_calling_for_location.assert_called_once()
    mock_webex.set_location_pstn_connection.assert_called_once_with(
        location_id="loc-louis-new",
        route_type="ROUTE_GROUP",
        route_id="CUCM",
    )


def test_api_get_pstn_routing_options():
    with patch("routers.telecom.WebexCallingProvider") as mock_prov_cls:
        mock_prov = MagicMock()
        mock_prov_cls.return_value = mock_prov
        mock_prov.get_pstn_routing_options.return_value = [
            {"id": "rg-1", "name": "CUCM", "type": "ROUTE_GROUP", "display": "CUCM (Route Group)"}
        ]

        resp = client.get(
            "/api/v1/telecom/pstn/routing-options",
            headers={"X-Webex-Token": "test-token-abc"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "CUCM"


# =============================================================================
# 4. RedSky E911 Integration Tests
# =============================================================================

def test_service_preview_with_redsky(mock_meraki_inventory):
    mock_webex = MagicMock(spec=WebexCallingProvider)
    mock_meraki = MagicMock(spec=MerakiProvider)
    mock_redsky = MagicMock(spec=RedskyProvider)

    mock_meraki.get_appliance_location_inventory.return_value = mock_meraki_inventory
    mock_webex.get_locations.return_value = []
    mock_redsky.is_configured.return_value = True
    mock_redsky.get_buildings.return_value = [
        {"id": "rs-bldg-111", "name": "CLH - Honolulu Lab", "msagValid": "VALID"}
    ]

    service = WebexLocationProvisionService(
        webex_provider=mock_webex,
        meraki_provider=mock_meraki,
        redsky_provider=mock_redsky,
    )
    preview = service.preview_meraki_locations()

    honolulu = next(l for l in preview.locations if l.network_id == "N_111")
    assert honolulu.redsky_building_id == "rs-bldg-111"
    assert honolulu.redsky_status == "VALID"

    incomplete = next(l for l in preview.locations if l.network_id == "N_222")
    assert incomplete.redsky_status == "INCOMPLETE"


def test_service_sync_locations_with_redsky_live():
    mock_webex = MagicMock(spec=WebexCallingProvider)
    mock_meraki = MagicMock()
    mock_redsky = MagicMock(spec=RedskyProvider)

    mock_meraki.get_appliance_location_inventory.return_value = [
        {
            "network_id": "net-rs-test",
            "network_name": "Austin Branch",
            "address": "100 Congress Ave, Austin, TX 78701",
            "time_zone": "America/Chicago",
            "appliance_models": ["MX68"],
            "serial_numbers": ["Q2MN-1111-2222"],
            "device_count": 1,
            "lookup_error": False,
        }
    ]
    mock_meraki.dashboard.appliance.getNetworkApplianceVlans.return_value = [
        {"id": 10, "name": "Voice", "subnet": "10.10.10.0/24"}
    ]

    mock_webex.get_locations.return_value = []
    mock_webex.create_location.return_value = {"id": "loc-austin-1", "name": "Austin Branch"}
    mock_webex.enable_calling_for_location.return_value = {"status": "SUCCESS"}
    mock_webex.set_location_pstn_connection.return_value = {"status": "SUCCESS"}

    mock_redsky.is_configured.return_value = True
    mock_redsky.get_buildings.return_value = []
    mock_redsky.get_or_create_building.return_value = (
        {"id": "bldg-rs-austin", "name": "Austin Branch", "msagValid": "VALID"},
        True,
    )
    mock_redsky.map_subnet_to_building.return_value = {"id": "ip-range-1"}

    service = WebexLocationProvisionService(
        webex_provider=mock_webex,
        meraki_provider=mock_meraki,
        redsky_provider=mock_redsky,
    )

    response = service.sync_locations(
        network_ids=["net-rs-test"],
        dry_run=False,
        connection_type="LOCAL_GATEWAY",
        routing_group_id="CUCM",
        sync_redsky=True,
    )

    assert response.status == "SUCCESS"
    assert response.created_count == 1
    item = response.results[0]
    assert item.status == "CREATED"
    assert "RedSky E911 registered [MSAG: VALID, 1 subnet(s) wiremapped]" in item.details

    mock_redsky.get_or_create_building.assert_called_once_with(
        name="Austin Branch",
        address1="100 Congress Ave",
        city="Austin",
        state="TX",
        postal_code="78701",
        country="United States",
    )
    mock_redsky.map_subnet_to_building.assert_called_once_with(
        "bldg-rs-austin",
        "10.10.10.0/24",
        "Austin Branch - Voice",
    )


def test_service_sync_locations_with_redsky_disabled():
    mock_webex = MagicMock(spec=WebexCallingProvider)
    mock_meraki = MagicMock(spec=MerakiProvider)
    mock_redsky = MagicMock(spec=RedskyProvider)

    mock_meraki.get_appliance_location_inventory.return_value = [
        {
            "network_id": "net-rs-test2",
            "network_name": "Dallas Branch",
            "address": "123 Main St, Dallas, TX 75201",
            "time_zone": "America/Chicago",
            "appliance_models": ["MX68"],
            "serial_numbers": ["Q2MN-3333-4444"],
            "device_count": 1,
            "lookup_error": False,
        }
    ]
    mock_webex.get_locations.return_value = []
    mock_webex.create_location.return_value = {"id": "loc-dal-1", "name": "Dallas Branch"}
    mock_webex.enable_calling_for_location.return_value = {"status": "SUCCESS"}
    mock_webex.set_location_pstn_connection.return_value = {"status": "SUCCESS"}

    mock_redsky.is_configured.return_value = True

    service = WebexLocationProvisionService(
        webex_provider=mock_webex,
        meraki_provider=mock_meraki,
        redsky_provider=mock_redsky,
    )

    response = service.sync_locations(
        network_ids=["net-rs-test2"],
        dry_run=False,
        connection_type="LOCAL_GATEWAY",
        routing_group_id="CUCM",
        sync_redsky=False,
    )

    assert response.status == "SUCCESS"
    assert response.created_count == 1
    mock_redsky.get_or_create_building.assert_not_called()
    assert "RedSky" not in response.results[0].details


def test_service_sync_locations_redsky_error_gracefully_degrades():
    mock_webex = MagicMock(spec=WebexCallingProvider)
    mock_meraki = MagicMock(spec=MerakiProvider)
    mock_redsky = MagicMock(spec=RedskyProvider)

    mock_meraki.get_appliance_location_inventory.return_value = [
        {
            "network_id": "net-rs-fail",
            "network_name": "Houston Lab",
            "address": "500 Main St, Houston, TX 77002",
            "time_zone": "America/Chicago",
            "appliance_models": ["MX68"],
            "serial_numbers": ["Q2MN-5555-6666"],
            "device_count": 1,
            "lookup_error": False,
        }
    ]
    mock_webex.get_locations.return_value = []
    mock_webex.create_location.return_value = {"id": "loc-hou-1", "name": "Houston Lab"}
    mock_webex.enable_calling_for_location.return_value = {"status": "SUCCESS"}
    mock_webex.set_location_pstn_connection.return_value = {"status": "SUCCESS"}

    mock_redsky.is_configured.return_value = True
    mock_redsky.get_buildings.return_value = []
    mock_redsky.get_or_create_building.side_effect = RedskyAuthError("API Key could not be authenticated")

    service = WebexLocationProvisionService(
        webex_provider=mock_webex,
        meraki_provider=mock_meraki,
        redsky_provider=mock_redsky,
    )

    response = service.sync_locations(
        network_ids=["net-rs-fail"],
        dry_run=False,
        connection_type="LOCAL_GATEWAY",
        routing_group_id="CUCM",
        sync_redsky=True,
    )

    assert response.status == "SUCCESS"
    assert response.created_count == 1
    item = response.results[0]
    assert item.status == "CREATED"
    assert "RedSky note: API Key could not be authenticated" in item.details


def test_normalize_webex_timezone():
    from providers.webex_calling_provider import normalize_webex_timezone

    # Common Meraki POSIX values
    assert normalize_webex_timezone("US/Central") == "America/Chicago"
    assert normalize_webex_timezone("US/Eastern") == "America/New_York"
    assert normalize_webex_timezone("US/Mountain") == "America/Denver"
    assert normalize_webex_timezone("US/Pacific") == "America/Los_Angeles"
    assert normalize_webex_timezone("US/Arizona") == "America/Phoenix"
    assert normalize_webex_timezone("US/Hawaii") == "Pacific/Honolulu"
    assert normalize_webex_timezone("US/Alaska") == "America/Anchorage"

    # Standard abbreviations
    assert normalize_webex_timezone("CST") == "America/Chicago"
    assert normalize_webex_timezone("EST") == "America/New_York"
    assert normalize_webex_timezone("EST5EDT") == "America/New_York"
    assert normalize_webex_timezone("CST6CDT") == "America/Chicago"
    assert normalize_webex_timezone("MST") == "America/Denver"
    assert normalize_webex_timezone("PST") == "America/Los_Angeles"

    # Already valid IANA
    assert normalize_webex_timezone("America/Chicago") == "America/Chicago"
    assert normalize_webex_timezone("America/New_York") == "America/New_York"

    # State fallbacks
    assert normalize_webex_timezone(None, state="TX") == "America/Chicago"
    assert normalize_webex_timezone("", state="FL") == "America/New_York"
    assert normalize_webex_timezone(None, state="CA") == "America/Los_Angeles"
    assert normalize_webex_timezone(None, state="CO") == "America/Denver"
    assert normalize_webex_timezone(None, state="UNKNOWN") == "America/Chicago"


def test_parse_webex_location_address_multiline_newlines():
    from routers.meraki_admin import _parse_webex_location_address

    # Case: Meraki address contains newlines (e.g. SIG_LouisGonzales)
    raw = "1620 Hawk Dr\nCedar Park, TX 78613"
    parsed = _parse_webex_location_address(raw)

    assert "\n" not in parsed["Address Line 1"]
    assert "\r" not in parsed["Address Line 1"]
    assert "\n" not in parsed["City"]
    assert parsed["Address Line 1"] == "1620 Hawk Dr"
    assert parsed["City"] == "Cedar Park"
    assert parsed["State"] == "TX"
    assert parsed["ZIP"] == "78613"


def test_webex_provider_create_location_recovers_from_400_duplicate():
    provider = WebexCallingProvider(access_token="fake_token")
    with patch.object(provider, "_request") as mock_req, \
         patch.object(provider, "get_location_by_name") as mock_get_by_name:

        # Simulate 400 Validation failure on POST /locations
        mock_req.side_effect = WebexAPIError(
            'HTTP 400: {"message":"Validation failure","errors":[{"description":"Validation failure"}]}'
        )
        mock_get_by_name.return_value = {
            "id": "loc-recovered-123",
            "name": "SIG_LouisGonzales"
        }

        loc = provider.create_location(
            name="SIG_LouisGonzales",
            address1="1620 Hawk Dr",
            city="Cedar Park",
            state="TX",
            postal_code="78613",
            country="US",
            time_zone="US/Central",
        )

        assert loc["id"] == "loc-recovered-123"
        assert loc["name"] == "SIG_LouisGonzales"
        mock_get_by_name.assert_called_once_with("SIG_LouisGonzales")


# =============================================================================
# Automated Phone Subnet Wiremapping & Classification Tests (Ray Baum's Act)
# =============================================================================

def test_classify_phone_subnets_voice_supernet():
    """Validates that subnets within Sonic's 10.236.0.0/15 supernet are classified as DEDICATED_VOICE."""
    mock_webex = MagicMock(spec=WebexCallingProvider)
    mock_meraki = MagicMock()
    mock_meraki.dashboard.appliance.getNetworkApplianceVlans.return_value = [
        {"id": 100, "name": "Corporate Data", "subnet": "10.100.1.0/24"},
        {"id": 200, "name": "Phone VLAN", "subnet": "10.236.14.0/24"},
        {"id": 300, "name": "Guest Wi-Fi", "subnet": "192.168.200.0/24"},
    ]

    service = WebexLocationProvisionService(webex_provider=mock_webex, meraki_provider=mock_meraki)
    subnets, strategy = service.classify_phone_subnets("net-austin-campus")

    assert strategy == "DEDICATED_VOICE"
    assert len(subnets) == 1
    assert subnets[0][0] == "10.236.14.0/24"
    assert "Voice" in subnets[0][1]


def test_classify_phone_subnets_teleworker_single_data_vlan():
    """Validates that a smaller site with only a single corporate data VLAN maps that single data VLAN."""
    mock_webex = MagicMock(spec=WebexCallingProvider)
    mock_meraki = MagicMock()
    mock_meraki.dashboard.appliance.getNetworkApplianceVlans.return_value = [
        {"id": 1, "name": "Default Data", "subnet": "192.168.10.0/24"},
        {"id": 99, "name": "Guest Wi-Fi", "subnet": "172.16.99.0/24"},
    ]

    service = WebexLocationProvisionService(webex_provider=mock_webex, meraki_provider=mock_meraki)
    subnets, strategy = service.classify_phone_subnets("net-teleworker-1")

    assert strategy == "SINGLE_DATA_VLAN"
    assert len(subnets) == 1
    assert subnets[0][0] == "192.168.10.0/24"


def test_classify_phone_subnets_teleworker_single_lan():
    """Validates teleworker Z3/Z4 appliances in routed single LAN mode (no VLANs)."""
    mock_webex = MagicMock(spec=WebexCallingProvider)
    mock_meraki = MagicMock()
    mock_meraki.dashboard.appliance.getNetworkApplianceVlans.side_effect = Exception("VLANs not enabled")
    mock_meraki.dashboard.appliance.getNetworkApplianceSingleLan.return_value = {
        "subnet": "192.168.128.0/24"
    }

    service = WebexLocationProvisionService(webex_provider=mock_webex, meraki_provider=mock_meraki)
    subnets, strategy = service.classify_phone_subnets("net-z4-home")

    assert strategy == "SINGLE_LAN"
    assert len(subnets) == 1
    assert subnets[0][0] == "192.168.128.0/24"


def test_wiremap_location_subnets_on_demand():
    """Validates on-demand wiremapping of location subnets to RedSky."""
    mock_webex = MagicMock(spec=WebexCallingProvider)
    mock_meraki = MagicMock()
    mock_redsky = MagicMock(spec=RedskyProvider)

    mock_redsky.is_configured.return_value = True
    mock_redsky.get_building_by_name.return_value = {"id": "bldg-wiremap-1", "name": "Austin HQ"}
    mock_meraki.dashboard.appliance.getNetworkApplianceVlans.return_value = [
        {"id": 200, "name": "Voice", "subnet": "10.236.4.0/24"}
    ]
    mock_redsky.map_subnet_to_building.return_value = {"id": "ip-123"}

    service = WebexLocationProvisionService(
        webex_provider=mock_webex,
        meraki_provider=mock_meraki,
        redsky_provider=mock_redsky,
    )

    result = service.wiremap_location_subnets(
        network_id="net-austin",
        location_name="Austin HQ",
    )

    assert result["status"] == "SUCCESS"
    assert result["strategy"] == "DEDICATED_VOICE"
    assert len(result["wiremapped_subnets"]) == 1
    mock_redsky.map_subnet_to_building.assert_called_once_with(
        "bldg-wiremap-1", "10.236.4.0/24", "Austin HQ - Voice"
    )


def test_api_wiremap_location_subnets_endpoint():
    """Validates POST /api/v1/telecom/locations/{location_id}/redsky-wiremap."""
    with patch("routers.telecom.WebexLocationProvisionService") as MockService:
        instance = MockService.return_value
        instance.wiremap_location_subnets.return_value = {
            "status": "SUCCESS",
            "location_name": "Austin HQ",
            "building_id": "bldg-wiremap-1",
            "strategy": "DEDICATED_VOICE",
            "wiremapped_subnets": ["10.236.4.0/24 (Voice)"],
            "details": "Wiremapped 1 subnet(s) to RedSky Building bldg-wiremap-1 [DEDICATED_VOICE].",
        }

        resp = client.post(
            "/api/v1/telecom/locations/loc-123/redsky-wiremap",
            json={
                "network_id": "net-austin",
                "location_name": "Austin HQ",
            },
            headers={"X-Webex-Token": "fake_token"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "SUCCESS"
        assert data["strategy"] == "DEDICATED_VOICE"
        assert "10.236.4.0/24 (Voice)" in data["wiremapped_subnets"]


def test_preview_meraki_locations_without_webex_token():
    """Validates that Meraki appliance locations can be fetched and previewed even if no Webex token is provided."""
    mock_meraki = MagicMock()
    mock_meraki.get_appliance_location_inventory.return_value = [
        {
            "network_id": "net-dallas-1",
            "network_name": "Dallas Clinic",
            "address": "123 Main St, Dallas, TX 75001",
            "time_zone": "America/Chicago",
            "appliance_models": ["MX68"],
            "serial_numbers": ["Q2AA-BBCC-DD11"],
            "device_count": 1,
            "lookup_error": "",
        }
    ]
    mock_meraki.dashboard.appliance.getNetworkApplianceVlans.return_value = [
        {"id": 10, "name": "Voice", "subnet": "10.236.50.0/24"}
    ]

    service = WebexLocationProvisionService(webex_provider=None, meraki_provider=mock_meraki)
    preview = service.preview_meraki_locations()

    assert preview.status == "SUCCESS"
    assert preview.total_meraki_networks == 1
    assert preview.locations[0].location_name == "Dallas Clinic"
    assert preview.locations[0].exists_in_webex is False
    # Subnet classification is deferred to sync / on-demand to prevent proxy timeouts across 1,100+ networks
    subnets, strat = service.classify_phone_subnets("net-dallas-1")
    assert strat == "DEDICATED_VOICE"


def test_api_get_network_phone_subnets():
    """Validates the on-demand endpoint for querying a single network's phone subnets."""
    mock_meraki = MagicMock()
    mock_meraki.dashboard.appliance.getNetworkApplianceVlans.return_value = [
        {"id": 100, "name": "Voice_VLAN", "subnet": "10.236.12.0/24"}
    ]

    try:
        app.dependency_overrides[get_meraki_provider] = lambda: mock_meraki
        response = client.get("/api/v1/telecom/networks/net-austin-lab/phone-subnets")
        assert response.status_code == 200
        data = response.json()
        assert data["network_id"] == "net-austin-lab"
        assert data["wiremap_strategy"] == "DEDICATED_VOICE"
        assert any("10.236.12.0/24" in s for s in data["subnets"])
    finally:
        app.dependency_overrides.pop(get_meraki_provider, None)


def test_service_sync_existing_location_unconfigured_redsky_preserves_subnets():
    """Validates that syncing an existing Webex location when RedSky is unconfigured preserves discovered subnets and adds an explicit note."""
    mock_webex = MagicMock(spec=WebexCallingProvider)
    mock_meraki = MagicMock()
    mock_redsky = MagicMock(spec=RedskyProvider)

    mock_meraki.get_appliance_location_inventory.return_value = [
        {
            "network_id": "L_721701840286129576",
            "network_name": "SIG_LouisGonzales",
            "address": "1620 Hawk Dr, Cedar Park, TX 78613",
            "time_zone": "America/Chicago",
            "appliance_models": ["Z4"],
            "serial_numbers": ["Q2MN-XXXX-YYYY"],
            "device_count": 1,
            "lookup_error": False,
        }
    ]
    mock_meraki.dashboard.appliance.getNetworkApplianceVlans.return_value = [
        {"id": 100, "name": "Sonic-VoIP", "subnet": "10.237.127.128/25"}
    ]
    mock_webex.get_locations.return_value = [
        {"id": "loc-webex-louis", "name": "SIG_LouisGonzales"}
    ]
    mock_webex.is_calling_enabled_for_location.return_value = True
    mock_webex.set_location_pstn_connection.return_value = {"status": "SUCCESS"}

    # RedSky is unconfigured
    mock_redsky.is_configured.return_value = False

    service = WebexLocationProvisionService(
        webex_provider=mock_webex,
        meraki_provider=mock_meraki,
        redsky_provider=mock_redsky,
    )

    response = service.sync_locations(
        network_ids=["L_721701840286129576"],
        dry_run=False,
        connection_type="LOCAL_GATEWAY",
        routing_group_id="CUCM",
        sync_redsky=True,
    )

    assert response.status == "SUCCESS"
    item = response.results[0]
    assert item.status == "CREATED"
    assert "10.237.127.128/25" in item.wiremapped_subnets
    assert item.wiremap_strategy == "DEDICATED_VOICE"
    assert "RedSky E911 skipped (REDSKY_API is unconfigured in .env)" in item.details









