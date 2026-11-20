"""
tests/test_cucm_decommission_service.py

Comprehensive unit tests for Phase 4:
CUCM Dial-Plan Translation Pattern Injection & Device Decommissioning Service and API Endpoints.
"""

import pytest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient

from main import app
from models.telecom import (
    ClusterTarget,
    CucmTranslationPatternRequest,
    CucmDeviceDecommissionItem,
    CucmDecommissionRequest,
)
from providers.cucm_axl_provider import CUCMAXLProvider, AXLRequestError
from services.cucm_decommission_service import CucmDecommissionService
from security.roles import Role
from security.auth import get_current_user, CurrentUser


@pytest.fixture
def mock_admin_user():
    return CurrentUser(
        username="netadmin",
        role=Role.NETWORK_ADMIN,
        permissions=["telecom:audit", "telecom:provision", "telecom:write"],
    )


@pytest.fixture
def mock_axl_provider():
    axl = MagicMock(spec=CUCMAXLProvider)
    axl.version = "12.5"

    # Default translation pattern lookup: None unless pattern == "9999"
    def mock_get_tp(pattern, partition=None):
        if pattern == "9999":
            return {
                "pkid": "uuid_tp_9999",
                "dnorpattern": "9999",
                "partition_name": partition or "Internal_PT",
                "css_name": "To_LGW_CSS",
            }
        return None

    axl.get_translation_pattern.side_effect = mock_get_tp
    axl.add_translation_pattern.return_value = {
        "status": "SUCCESS",
        "uuid": "uuid_tp_new_123",
        "pattern": "4199",
        "partition": "Internal_PT",
        "calling_search_space": "To_LGW_CSS",
    }

    axl.get_route_partitions.return_value = [
        {"pkid": "uuid_pt_1", "name": "Internal_PT", "description": "Internal Lines"},
        {"pkid": "uuid_pt_2", "name": "PSTN_PT", "description": "PSTN Calls"},
    ]
    axl.get_gateways_and_route_lists.return_value = [
        {"pkid": "uuid_gw_1", "name": "CUBE_LGW_Trunk", "description": "SIP Trunk to CUBE", "class_name": "Trunk", "is_route_list": False},
        {"pkid": "uuid_rl_1", "name": "RL_Local_Gateway", "description": "Route List to LGW", "class_name": "Route List", "is_route_list": True},
    ]

    def mock_get_rp(pattern, partition=None):
        if pattern == "9999":
            return {
                "pkid": "uuid_rp_9999",
                "dnorpattern": "9999",
                "partition_name": partition or "Internal_PT",
                "destination_name": "RL_Local_Gateway",
                "destination_type": "Route List",
            }
        return None

    axl.get_route_pattern.side_effect = mock_get_rp
    axl.add_route_pattern.return_value = {
        "status": "SUCCESS",
        "uuid": "uuid_rp_new_123",
        "pattern": "4199",
        "partition": "Internal_PT",
        "gateway_route_list": "RL_Local_Gateway",
        "destination_type": "Route List",
    }
    axl.remove_route_pattern.return_value = True

    # Device lookup
    def mock_get_device(name_or_mac):
        clean = name_or_mac.upper()
        if "001122334455" in clean:
            return {
                "pkid": "uuid_dev_8851",
                "device_name": "SEP001122334455",
                "model": "Cisco 8851",
                "description": "John Doe Desk",
                "device_pool": "Austin_DP",
                "owner_userid": "jdoe",
                "lines": [
                    {"line_dn": "4199", "partition_name": "Internal_PT", "numplanindex": "1"}
                ],
            }
        elif "AABBCCDDEEFF" in clean:
            return {
                "pkid": "uuid_dev_7821",
                "device_name": "SEPAABBCCDDEEFF",
                "model": "Cisco 7821",
                "description": "Breakroom Phone",
                "device_pool": "Dallas_DP",
                "owner_userid": "Unassigned",
                "lines": [
                    {"line_dn": "5200", "partition_name": "Internal_Lines_PT", "numplanindex": "1"}
                ],
            }
        return None

    axl.get_device_by_name_or_mac.side_effect = mock_get_device
    axl.remove_phone.return_value = True
    axl.remove_line.return_value = True
    axl.remove_translation_pattern.return_value = True

    return axl


def test_device_name_normalization():
    norm = CucmDecommissionService.normalize_device_name

    assert norm("001122334455") == ("SEP001122334455", "001122334455")
    assert norm("SEP001122334455") == ("SEP001122334455", "001122334455")
    assert norm("00:11:22:33:44:55") == ("SEP001122334455", "001122334455")
    assert norm("00-11-22-33-44-55") == ("SEP001122334455", "001122334455")
    assert norm("aabbccddeeff") == ("SEPAABBCCDDEEFF", "AABBCCDDEEFF")

    with pytest.raises(ValueError, match="Invalid device MAC address"):
        norm("0011223344")

    with pytest.raises(ValueError, match="Invalid device MAC address"):
        norm("00112233445566")

    with pytest.raises(ValueError, match="Invalid device MAC address"):
        norm("0011223344ZZ")


def test_license_reclamation_tiers():
    det = CucmDecommissionService.determine_reclaimed_license

    assert "Enhanced UCL" in det("Cisco 8851")
    assert "Enhanced UCL" in det("Cisco 8865")
    assert "Enhanced UCL" in det("Cisco 7841")
    assert "Basic UCL" in det("Cisco 7821")
    assert "Basic UCL" in det("Cisco 6851")
    assert "Essential UCL" in det("Cisco 3905")
    assert "Essential UCL" in det("Cisco 7960")
    assert "TelePresence Room" in det("Cisco Webex Room Kit")


def test_translation_pattern_dry_run_and_live(mock_axl_provider):
    service = CucmDecommissionService(mock_axl_provider, cluster_name="PRIMARY")

    # 1. Existing pattern -> SKIPPED
    res_skip = service.create_translation_pattern(extension="9999", partition="Internal_PT", dry_run=True)
    assert res_skip.status == "SKIPPED"
    assert "already exists" in res_skip.details

    # 2. Dry run for new pattern -> DRY_RUN
    res_dry = service.create_translation_pattern(extension="4199", partition="Internal_PT", calling_search_space="To_LGW_CSS", dry_run=True)
    assert res_dry.status == "DRY_RUN"
    assert res_dry.dry_run is True
    mock_axl_provider.add_translation_pattern.assert_not_called()

    # 3. Live execution -> SUCCESS
    res_live = service.create_translation_pattern(extension="4199", partition="Internal_PT", calling_search_space="To_LGW_CSS", dry_run=False)
    assert res_live.status == "SUCCESS"
    assert res_live.dry_run is False
    assert res_live.uuid == "uuid_tp_new_123"
    mock_axl_provider.add_translation_pattern.assert_called_once_with(
        pattern="4199",
        partition="Internal_PT",
        calling_search_space="To_LGW_CSS",
        description="Migrated to Webex Calling - Ext 4199",
        urgent_priority=True,
    )


def test_decommission_devices_dry_run(mock_axl_provider):
    service = CucmDecommissionService(mock_axl_provider, cluster_name="PRIMARY")

    items = [
        CucmDeviceDecommissionItem(
            mac_address="001122334455",
            extension="4199",
            partition="Internal_PT",
            calling_search_space="To_LGW_CSS",
            create_translation_pattern=True,
            delete_unassigned_dn=True,
        ),
        CucmDeviceDecommissionItem(
            mac_address="AABBCCDDEEFF",
            extension="5200",
            create_translation_pattern=True,
            delete_unassigned_dn=True,
        ),
        CucmDeviceDecommissionItem(
            mac_address="112233445566",  # Not in CUCM
            extension="6300",
            create_translation_pattern=True,
        ),
    ]

    resp = service.decommission_devices(items=items, dry_run=True)

    assert resp.status == "SUCCESS"
    assert resp.dry_run is True
    assert resp.total_evaluated == 3
    assert resp.successful_count == 3  # All validated in dry run

    # Verify no mutating AXL operations executed during dry-run
    mock_axl_provider.remove_phone.assert_not_called()
    mock_axl_provider.remove_line.assert_not_called()
    mock_axl_provider.add_translation_pattern.assert_not_called()

    # Verify Smart Licensing tier reported
    assert "Enhanced UCL" in resp.results[0].license_reclaimed
    assert "Basic UCL" in resp.results[1].license_reclaimed


def test_decommission_devices_live_execution(mock_axl_provider):
    service = CucmDecommissionService(mock_axl_provider, cluster_name="PRIMARY")

    items = [
        CucmDeviceDecommissionItem(
            mac_address="00:11:22:33:44:55",
            extension="4199",
            partition="Internal_PT",
            calling_search_space="To_LGW_CSS",
            create_translation_pattern=True,
            delete_unassigned_dn=True,
        )
    ]

    resp = service.decommission_devices(items=items, dry_run=False)

    assert resp.status == "SUCCESS"
    assert resp.dry_run is False
    assert resp.successful_count == 1
    assert resp.results[0].status == "SUCCESS"
    assert resp.results[0].device_deletion_status == "SUCCESS"
    assert resp.results[0].translation_pattern_status == "SUCCESS"
    assert resp.results[0].line_deletion_status == "SUCCESS"

    # Verify AXL mutations were called
    mock_axl_provider.add_translation_pattern.assert_called_once()
    mock_axl_provider.remove_phone.assert_called_once_with("SEP001122334455")
    mock_axl_provider.remove_line.assert_called_once_with("4199", "Internal_PT")


def test_api_translation_pattern_endpoint(mock_admin_user, mock_axl_provider):
    app.dependency_overrides[get_current_user] = lambda: mock_admin_user
    client = TestClient(app)

    try:
        from unittest.mock import patch
        with patch("providers.telecom_factory.TelecomClusterRegistry.get_cucm_provider", return_value=mock_axl_provider):
            resp = client.post(
                "/api/v1/telecom/cucm/translation-pattern",
                json={
                    "extension": "4199",
                    "partition": "Internal_PT",
                    "calling_search_space": "To_LGW_CSS",
                    "dry_run": True,
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "DRY_RUN"
            assert data["pattern"] == "4199"
    finally:
        app.dependency_overrides.clear()


def test_api_decommission_endpoint(mock_admin_user, mock_axl_provider):
    app.dependency_overrides[get_current_user] = lambda: mock_admin_user
    client = TestClient(app)

    try:
        from unittest.mock import patch
        with patch("providers.telecom_factory.TelecomClusterRegistry.get_cucm_provider", return_value=mock_axl_provider):
            resp = client.post(
                "/api/v1/telecom/cucm/decommission",
                json={
                    "items": [
                        {
                            "mac_address": "001122334455",
                            "extension": "4199",
                        }
                    ],
                    "dry_run": True,
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "SUCCESS"
            assert data["dry_run"] is True
            assert data["successful_count"] == 1
    finally:
        app.dependency_overrides.clear()


def test_api_get_device_info(mock_admin_user, mock_axl_provider):
    app.dependency_overrides[get_current_user] = lambda: mock_admin_user
    client = TestClient(app)

    try:
        from unittest.mock import patch
        with patch("providers.telecom_factory.TelecomClusterRegistry.get_cucm_provider", return_value=mock_axl_provider):
            resp = client.get("/api/v1/telecom/cucm/device/001122334455")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "SUCCESS"
            assert data["device"]["device_name"] == "SEP001122334455"
            assert "Enhanced UCL" in data["device"]["license_reclaim_tier"]
    finally:
        app.dependency_overrides.clear()


def test_get_dialplan_options(mock_axl_provider):
    service = CucmDecommissionService(mock_axl_provider, cluster_name="PRIMARY")
    options = service.get_dialplan_options()

    assert options.cluster == "PRIMARY"
    assert len(options.partitions) == 2
    assert options.partitions[0].name == "Internal_PT"
    assert options.partitions[1].name == "PSTN_PT"

    assert len(options.gateways_and_route_lists) == 2
    assert options.gateways_and_route_lists[0].name == "CUBE_LGW_Trunk"
    assert options.gateways_and_route_lists[0].class_name == "Trunk"
    assert options.gateways_and_route_lists[0].is_route_list is False
    assert options.gateways_and_route_lists[1].name == "RL_Local_Gateway"
    assert options.gateways_and_route_lists[1].class_name == "Route List"
    assert options.gateways_and_route_lists[1].is_route_list is True


def test_route_pattern_dry_run_and_live(mock_axl_provider):
    service = CucmDecommissionService(mock_axl_provider, cluster_name="PRIMARY")

    # 1. Existing pattern -> SKIPPED
    res_skip = service.create_route_pattern(
        extension="9999", partition="Internal_PT", gateway_route_list="RL_Local_Gateway", dry_run=True
    )
    assert res_skip.status == "SKIPPED"
    assert "already exists" in res_skip.details

    # 2. Dry run -> DRY_RUN
    res_dry = service.create_route_pattern(
        extension="4199", partition="Internal_PT", gateway_route_list="RL_Local_Gateway", dry_run=True
    )
    assert res_dry.status == "DRY_RUN"
    assert res_dry.dry_run is True
    assert "Pre-flight verification passed" in res_dry.details
    mock_axl_provider.add_route_pattern.assert_not_called()

    # 3. Live execution -> SUCCESS
    res_live = service.create_route_pattern(
        extension="4199", partition="Internal_PT", gateway_route_list="RL_Local_Gateway", dry_run=False
    )
    assert res_live.status == "SUCCESS"
    assert res_live.dry_run is False
    assert res_live.uuid == "uuid_rp_new_123"
    assert res_live.destination_type == "Route List"
    mock_axl_provider.add_route_pattern.assert_called_once_with(
        pattern="4199",
        partition="Internal_PT",
        gateway_route_list="RL_Local_Gateway",
        is_route_list=None,
        description="Migrated to Webex Calling - Ext 4199",
        urgent_priority=True,
    )


def test_api_dialplan_options_endpoint(mock_admin_user, mock_axl_provider):
    app.dependency_overrides[get_current_user] = lambda: mock_admin_user
    client = TestClient(app)

    try:
        from unittest.mock import patch
        with patch("providers.telecom_factory.TelecomClusterRegistry.get_cucm_provider", return_value=mock_axl_provider):
            resp = client.get("/api/v1/telecom/cucm/dialplan-options?cluster=PRIMARY")
            assert resp.status_code == 200
            data = resp.json()
            assert data["cluster"] == "PRIMARY"
            assert len(data["partitions"]) == 2
            assert data["partitions"][0]["name"] == "Internal_PT"
            assert len(data["gateways_and_route_lists"]) == 2
            assert data["gateways_and_route_lists"][1]["name"] == "RL_Local_Gateway"
            assert data["gateways_and_route_lists"][1]["is_route_list"] is True
    finally:
        app.dependency_overrides.clear()


def test_api_route_pattern_endpoint(mock_admin_user, mock_axl_provider):
    app.dependency_overrides[get_current_user] = lambda: mock_admin_user
    client = TestClient(app)

    try:
        from unittest.mock import patch
        with patch("providers.telecom_factory.TelecomClusterRegistry.get_cucm_provider", return_value=mock_axl_provider):
            resp = client.post(
                "/api/v1/telecom/cucm/route-pattern",
                json={
                    "extension": "4199",
                    "partition": "Internal_PT",
                    "gateway_route_list": "RL_Local_Gateway",
                    "dry_run": True,
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "DRY_RUN"
            assert data["pattern"] == "4199"
            assert data["partition"] == "Internal_PT"
            assert data["gateway_route_list"] == "RL_Local_Gateway"
    finally:
        app.dependency_overrides.clear()


def test_decommission_with_route_pattern(mock_axl_provider):
    service = CucmDecommissionService(mock_axl_provider, cluster_name="PRIMARY")

    items = [
        CucmDeviceDecommissionItem(
            mac_address="001122334455",
            extension="4199",
            partition="Internal_PT",
            gateway_route_list="RL_Local_Gateway",
            create_route_pattern=True,
            delete_unassigned_dn=True,
        )
    ]

    resp = service.decommission_devices(items=items, dry_run=False)

    assert resp.status == "SUCCESS"
    assert resp.results[0].route_pattern_status == "SUCCESS"
    assert resp.results[0].device_deletion_status == "SUCCESS"
    mock_axl_provider.add_route_pattern.assert_called_once()


def test_axl_provider_get_gateways_and_route_lists_schema():
    """Verifies that CUCMAXLProvider queries device, routelist, and sipdevice using correct Informix schema."""
    provider = CUCMAXLProvider(host="10.212.16.10", username="axladmin", password="password")

    def mock_sql(query):
        assert "tc.val" not in query, "tc.val is invalid in CUCM Informix schema; must use tc.enum"
        if "device d" in query and "routelist rl" in query:
            assert "rl.fkdevice = d.pkid" in query
            return [
                {"pkid": "uuid_rl_1", "name": "RL_Webex_LGW", "description": "Webex RL", "tkclass": "7"}
            ]
        elif "device d" in query and "sipdevice sd" in query:
            assert "sd.fkdevice = d.pkid" in query
            return [
                {"pkid": "uuid_sd_1", "name": "CUBE_SIP_TRUNK", "description": "CUBE Trunk", "tkclass": "18"}
            ]
        elif "device d" in query and "typeclass tc" in query:
            assert "d.tkclass = tc.enum" in query
            return [
                {"pkid": "uuid_gw_1", "name": "VG224_GATEWAY", "description": "Analog VG", "class_name": "Gateway", "tkclass": "2"},
                {"pkid": "uuid_rl_1", "name": "RL_Webex_LGW", "description": "Webex RL", "class_name": "Route List", "tkclass": "7"},
            ]
        return []

    provider.execute_sql_query = mock_sql
    items = provider.get_gateways_and_route_lists()

    assert len(items) == 3
    names = [i["name"] for i in items]
    assert "CUBE_SIP_TRUNK" in names
    assert "RL_Webex_LGW" in names
    assert "VG224_GATEWAY" in names

    rl_item = next(i for i in items if i["name"] == "RL_Webex_LGW")
    assert rl_item["is_route_list"] is True
    assert rl_item["class_name"] == "Route List"

    trunk_item = next(i for i in items if i["name"] == "CUBE_SIP_TRUNK")
    assert trunk_item["is_route_list"] is False
    assert trunk_item["class_name"] == "Trunk"


def test_axl_provider_add_route_pattern_destination_tag():
    """Verifies that add_route_pattern creates routeListName for route lists and gatewayName for gateways/trunks."""
    provider = CUCMAXLProvider(host="10.212.16.10", username="axladmin", password="password")

    captured_bodies = []

    def mock_soap(action, body):
        captured_bodies.append((action, body))
        return "<return>{uuid_test_123}</return>"

    provider.execute_axl_soap = mock_soap
    provider.execute_sql_query = lambda q: [{"tkclass": "7", "class_name": "Route List"}]

    # Case 1: Route List
    res_rl = provider.add_route_pattern("4199", "Internal_PT", "RL_Webex_LGW")
    assert res_rl["destination_type"] == "Route List"
    assert "<routeListName>RL_Webex_LGW</routeListName>" in captured_bodies[0][1]

    # Case 2: Gateway / Trunk
    provider.execute_sql_query = lambda q: [{"tkclass": "18", "class_name": "Trunk"}]
    res_gw = provider.add_route_pattern("4200", "Internal_PT", "CUBE_SIP_TRUNK")
    assert res_gw["destination_type"] == "Gateway/Trunk"
    assert "<gatewayName>CUBE_SIP_TRUNK</gatewayName>" in captured_bodies[1][1]


def test_axl_provider_get_device_by_name_or_mac_schema():
    """Verifies that get_device_by_name_or_mac queries typemodel using tm.enum and retrieves lines."""
    provider = CUCMAXLProvider(host="10.212.16.10", username="axladmin", password="password")

    def mock_sql(query):
        assert "tm.val" not in query, "tm.val is invalid in CUCM Informix schema; must use tm.enum"
        assert "typeproduct" not in query, "device.tkmodel joins typemodel, not typeproduct"
        if "device d" in query:
            assert "tm.enum" in query
            return [{
                "pkid": "uuid_dev_bcc49351fb60",
                "device_name": "SEPBCC49351FB60",
                "model": "Cisco 8851",
                "description": "User Deskphone",
                "device_pool": "Default_DP",
                "owner_userid": "jdoe",
            }]
        elif "devicenumplanmap" in query:
            return [{
                "line_dn": "4199",
                "partition_name": "Internal_PT",
                "numplanindex": 1,
            }]
        return []

    provider.execute_sql_query = mock_sql

    # Test with full SEP name
    dev = provider.get_device_by_name_or_mac("SEPBCC49351FB60")
    assert dev is not None
    assert dev["device_name"] == "SEPBCC49351FB60"
    assert dev["model"] == "Cisco 8851"
    assert len(dev["lines"]) == 1
    assert dev["lines"][0]["line_dn"] == "4199"

    # Test with plain MAC
    dev2 = provider.get_device_by_name_or_mac("bcc49351fb60")
    assert dev2 is not None
    assert dev2["device_name"] == "SEPBCC49351FB60"


def test_axl_provider_get_translation_pattern_schema():
    """Verifies that get_translation_pattern uses valid Informix columns fkcallingsearchspace_translation and tkpatternusage = 3."""
    provider = CUCMAXLProvider(host="10.212.16.10", username="axladmin", password="password")

    captured_queries = []

    def mock_sql(query):
        captured_queries.append(query)
        assert "fkcallingsearchspace_sharedinput" not in query, (
            "fkcallingsearchspace_sharedinput does not exist in CUCM numplan table; "
            "must use fkcallingsearchspace_translation"
        )
        assert "fkcallingsearchspace_translation" in query
        assert "tkpatternusage = 3" in query
        return [{
            "pkid": "uuid_tp_801662",
            "dnorpattern": "801662",
            "partition_name": "Internal_PT",
            "css_name": "To_Webex_LGW_CSS",
            "description": "Migrated to Webex",
        }]

    provider.execute_sql_query = mock_sql

    res = provider.get_translation_pattern(" 801662 ", partition="Internal_PT")
    assert res is not None
    assert res["dnorpattern"] == "801662"
    assert res["css_name"] == "To_Webex_LGW_CSS"
    assert "dnorpattern = '801662'" in captured_queries[0]
    assert "rp.name = 'Internal_PT'" in captured_queries[0]


def test_axl_provider_get_route_pattern_schema():
    """Verifies that get_route_pattern uses tkpatternusage = 5 for Route Patterns."""
    provider = CUCMAXLProvider(host="10.212.16.10", username="axladmin", password="password")

    captured_queries = []

    def mock_sql(query):
        captured_queries.append(query)
        assert "fkroutelist" not in query, "fkroutelist does not exist in numplan; must use devicenumplanmap"
        assert "devicenumplanmap" in query
        assert "tkpatternusage = 5" in query
        return [{
            "pkid": "uuid_rp_801662",
            "pattern": "801662",
            "partition_name": "Internal_PT",
            "destination_name": "WebexCUBE_RL",
            "destination_type": "Route List",
            "description": "LGW Route Pattern",
        }]

    provider.execute_sql_query = mock_sql

    res = provider.get_route_pattern(" 801662 ")
    assert res is not None
    assert res["destination_name"] == "WebexCUBE_RL"
    assert "dnorpattern = '801662'" in captured_queries[0]


def test_api_cucm_decommission_step_up_auth(mock_admin_user, mock_axl_provider):
    """Verifies that live decommission enforces LDAP step-up auth and accepts credentials."""
    app.dependency_overrides[get_current_user] = lambda: mock_admin_user
    client = TestClient(app)

    payload_no_step_up = {
        "items": [
            {
                "mac_address": "001122334455",
                "extension": "4199",
                "partition": "Internal_PT",
                "gateway_route_list": "RL_Local_Gateway",
                "create_route_pattern": True,
                "delete_unassigned_dn": True,
            }
        ],
        "dry_run": False,
    }

    try:
        from unittest.mock import patch
        with patch("providers.telecom_factory.TelecomClusterRegistry.get_cucm_provider", return_value=mock_axl_provider):
            # 1. Live execution without step-up credentials -> 401
            resp_fail = client.post("/api/v1/telecom/cucm/decommission", json=payload_no_step_up)
            assert resp_fail.status_code == 401
            assert "authentication failed" in resp_fail.json()["detail"].lower()

            # 2. Live execution with step-up credentials -> 200
            payload_with_step_up = {
                **payload_no_step_up,
                "step_up_user": "netadmin@corp.internal",
                "step_up_pass": "secret-ldap-pass",
            }
            with patch("routers.telecom.authenticate_step_up_credentials") as mock_auth:
                resp_ok = client.post("/api/v1/telecom/cucm/decommission", json=payload_with_step_up)
                assert resp_ok.status_code == 200
                mock_auth.assert_called_once_with(
                    "netadmin@corp.internal",
                    "secret-ldap-pass",
                    "CUCM Device Decommissioning & Smart License Release",
                )
    finally:
        app.dependency_overrides.clear()




