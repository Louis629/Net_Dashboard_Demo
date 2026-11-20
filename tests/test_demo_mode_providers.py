"""
tests/test_demo_mode_providers.py

Unit and integration tests verifying DEMO_MODE configuration, provider factory
switching, self-contained Demo*Provider contracts, lifespan behavior, and FastAPI routes.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from core.config import Settings, get_settings
from core.dependencies import (
    get_diagnostic_service,
    get_dnac_provider,
    get_ise_provider,
    get_ldap_provider,
    get_meraki_provider,
)
import main as main_module
from main import app
from models.telecom import CucmDeviceDecommissionItem
from providers.catalyst_center_provider import CatalystCenterProvider
from providers.cucm_axl_provider import CUCMAXLProvider
from providers.cupi_provider import CUPIProvider
from providers.demo_providers import (
    DemoCUCMProvider,
    DemoCUPIProvider,
    DemoDNACProvider,
    DemoISEProvider,
    DemoLDAPProvider,
    DemoMerakiProvider,
    DemoUCCXProvider,
)
from providers.ise_provider import ISEProvider
from providers.meraki_provider import MerakiProvider
from providers.telecom_factory import ClusterEnvironment, TelecomClusterRegistry
from providers.uccx_provider import UCCXProvider
from services.appliance_swap_service import ApplianceSwapService
from services.cucm_audit_service import CUCMAuditService
from services.cucm_decommission_service import CucmDecommissionService
from services.endpoint_diagnostic_service import EndpointDiagnosticService
from services.source_inventory_service import SourceInventoryService
from services.uccx_migration_service import UCCXMigrationService
from services.uccx_prompt_export_service import UCCXPromptExportService
from services.uccx_script_migration_service import UCCXScriptMigrationService
from services.voicemail_export_service import VoicemailExportService
from services.webex_location_provision_service import WebexLocationProvisionService

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _clear_all_caches() -> None:
    get_settings.cache_clear()
    get_ise_provider.cache_clear()
    get_meraki_provider.cache_clear()
    get_dnac_provider.cache_clear()
    get_ldap_provider.cache_clear()


@pytest.fixture(autouse=True)
def reset_provider_caches(monkeypatch):
    _clear_all_caches()
    yield
    monkeypatch.undo()
    _clear_all_caches()
    main_module.ise_provider = get_ise_provider()
    main_module.meraki_provider = get_meraki_provider()
    main_module.dnac_provider = get_dnac_provider()
    main_module.ldap_provider = get_ldap_provider()


@pytest.fixture
def demo_mode_env(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("ENVIRONMENT", "LOCAL")
    monkeypatch.setenv("ISE_PAN_IP", "")
    monkeypatch.setenv("ISE_ERS_USER", "")
    monkeypatch.setenv("ISE_ERS_PASS", "")
    monkeypatch.setenv("DNAC_IP", "")
    monkeypatch.setenv("MERAKI_API_KEY", "")
    monkeypatch.setenv("MERAKI_ORG_ID", "")
    monkeypatch.setenv("LDAP_SERVER_URI", "")
    monkeypatch.setenv("PRIMARY_CUCM_AXL_HOST", "")
    monkeypatch.setenv("HAWAII_CUCM_AXL_HOST", "")
    monkeypatch.setenv("UCCX_HOST", "")
    monkeypatch.setenv("CUC_HOST", "")
    _clear_all_caches()
    yield


# -----------------------------------------------------------------------------
# 1. Configuration & Factory Switching
# -----------------------------------------------------------------------------


def test_demo_mode_setting_default_and_env_override(monkeypatch):
    default_settings = Settings(_env_file=None)
    assert default_settings.DEMO_MODE is False

    monkeypatch.setenv("DEMO_MODE", "true")
    _clear_all_caches()
    assert get_settings().DEMO_MODE is True

    env_example_text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "DEMO_MODE=false" in env_example_text


def test_dependencies_return_demo_providers_when_demo_mode_enabled(demo_mode_env):
    assert isinstance(get_ise_provider(), DemoISEProvider)
    assert isinstance(get_meraki_provider(), DemoMerakiProvider)
    assert isinstance(get_dnac_provider(), DemoDNACProvider)
    assert isinstance(get_ldap_provider(), DemoLDAPProvider)


def test_dependencies_return_live_providers_when_demo_mode_disabled(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("ISE_PAN_IP", "127.0.0.1")
    monkeypatch.setenv("ISE_ERS_USER", "dummy_ise_user")
    monkeypatch.setenv("ISE_ERS_PASS", "dummy_ise_pass")
    monkeypatch.setenv("MERAKI_API_KEY", "dummy_api_key_123456789")
    monkeypatch.setenv("MERAKI_ORG_ID", "dummy_org_id_987654321")
    monkeypatch.setenv("DNAC_IP", "")
    monkeypatch.setenv("LDAP_SERVER_URI", "")
    _clear_all_caches()

    assert isinstance(get_ise_provider(), ISEProvider)
    assert isinstance(get_meraki_provider(), MerakiProvider)
    assert get_dnac_provider() is None
    assert get_ldap_provider() is None


def test_telecom_factory_switches_between_demo_and_live(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    _clear_all_caches()

    pri_cucm = TelecomClusterRegistry.get_cucm_provider(ClusterEnvironment.PRIMARY)
    hnl_cucm = TelecomClusterRegistry.get_cucm_provider(ClusterEnvironment.HAWAII)
    pri_uccx = TelecomClusterRegistry.get_uccx_provider(ClusterEnvironment.PRIMARY)
    hnl_uccx = TelecomClusterRegistry.get_uccx_provider(ClusterEnvironment.HAWAII)
    pri_cuc = TelecomClusterRegistry.get_cupi_provider(ClusterEnvironment.PRIMARY)
    hnl_cuc = TelecomClusterRegistry.get_cupi_provider(ClusterEnvironment.HAWAII)

    assert isinstance(pri_cucm, DemoCUCMProvider)
    assert pri_cucm.version == "12.5"
    assert isinstance(hnl_cucm, DemoCUCMProvider)
    assert hnl_cucm.version == "11.5"
    assert isinstance(pri_uccx, DemoUCCXProvider)
    assert isinstance(hnl_uccx, DemoUCCXProvider)
    assert isinstance(pri_cuc, DemoCUPIProvider)
    assert isinstance(hnl_cuc, DemoCUPIProvider)

    monkeypatch.setenv("DEMO_MODE", "false")
    _clear_all_caches()
    assert isinstance(TelecomClusterRegistry.get_cucm_provider(ClusterEnvironment.PRIMARY), CUCMAXLProvider)
    assert isinstance(TelecomClusterRegistry.get_uccx_provider(ClusterEnvironment.PRIMARY), UCCXProvider)
    assert isinstance(TelecomClusterRegistry.get_cupi_provider(ClusterEnvironment.PRIMARY), CUPIProvider)


# -----------------------------------------------------------------------------
# 2. Infrastructure Demo Providers (ISE / DNAC / Meraki / LDAP)
# -----------------------------------------------------------------------------


def test_demo_ise_provider_operations():
    ise = DemoISEProvider()

    active_diag = ise.diagnose_endpoint_session("aa-bb-cc-11-22-33")
    assert active_diag["mac_address"] == "AA:BB:CC:11:22:33"
    assert active_diag["status"] == "ACTIVE_SESSION_FOUND"
    assert len(active_diag["recent_history"]) >= 1

    denied_diag = ise.diagnose_endpoint_session("AA:BB:CC:44:55:66")
    assert denied_diag["status"] == "NO_ACTIVE_SESSION"
    assert "multiple matching AD accounts" in denied_diag["triage_hint"]

    bypass = ise.provision_imaging_bypass("11:22:33:44:55:66", "operator@corp.internal")
    assert bypass["status"] == "SUCCESS"
    assert bypass["group"] == "Staging_Imaging_Bypass"
    assert bypass["coa_applied"] is True

    removed = ise.remove_endpoint("11:22:33:44:55:66")
    assert removed["status"] == "SUCCESS"
    assert removed["deleted"] is True

    missing = ise.remove_endpoint("11:22:33:44:55:66")
    assert missing["status"] == "NOT_FOUND"
    assert missing["deleted"] is False

    ndg_opts = ise.get_meraki_ndg_options()
    assert "device_types" in ndg_opts and len(ndg_opts["device_types"]) > 0
    assert "roles" in ndg_opts and len(ndg_opts["roles"]) > 0


def test_demo_dnac_and_ldap_providers():
    dnac = DemoDNACProvider()
    wired = dnac.get_client_topology_by_mac("AA:BB:CC:11:22:33")
    assert wired["dnac_found"] is True
    assert wired["client_type"] == "WIRED"
    assert wired["health_score"] == 9

    wireless = dnac.get_client_topology_by_mac("DE:AD:BE:EF:00:01")
    assert wireless["dnac_found"] is True
    assert wireless["client_type"] == "WIRELESS"
    assert wireless["ssid"] == "Corp-Secure-WiFi"

    vpn = dnac.get_client_topology_by_mac("00:50:56:AA:BB:CC")
    assert vpn["dnac_found"] is False

    ldap = DemoLDAPProvider()
    assert ldap.authenticate("jdoe", "any-pass") is True
    assert ldap.authenticate("", "any-pass") is False
    assert ldap.authenticate("jdoe", "") is False

    unique = ldap.check_identity_collisions("jdoe@corp.internal")
    assert unique.has_collision is False
    assert unique.match_count == 1

    collision = ldap.check_identity_collisions("msmith@corp.internal")
    assert collision.has_collision is True
    assert collision.match_count == 2


def test_demo_meraki_provider_and_services():
    meraki = DemoMerakiProvider()
    assert meraki.health_check()["status"] == "OK"
    assert len(meraki.get_organization_networks()) == 3
    assert len(meraki.get_organization_templates()) == 3
    assert len(meraki.get_appliance_location_inventory()) == 3

    ctx_mac = meraki.get_client_by_mac("de-ad-be-ef-00-01")
    assert ctx_mac.meraki_found is True
    assert ctx_mac.network_id == "N_DEMO_AUS_002"

    ctx_ip = meraki.get_client_by_ip("10.10.20.88")
    assert ctx_ip.meraki_found is True
    assert ctx_ip.mac_address == "AA:BB:CC:44:55:66"

    enriched = meraki.enrich_adapter_record(
        {"Name": "Wi-Fi", "Type": "Wireless", "MacAddress": "DE:AD:BE:EF:00:01", "IPAddress": "10.20.30.105", "Status": "Up"}
    )
    assert enriched["meraki_found"] is True
    assert enriched["port_ssid"] == "Corp-Secure-WiFi"

    loc_service = WebexLocationProvisionService(meraki_provider=meraki)
    preview = loc_service.preview_meraki_locations()
    assert preview.total_meraki_networks == 3
    assert preview.invalid_address_count == 0

    dal_subs, dal_strat = loc_service.classify_phone_subnets("N_DEMO_DAL_001")
    assert dal_strat == "DEDICATED_VOICE"
    assert dal_subs[0][0] == "10.10.21.0/24"

    hnl_subs, hnl_strat = loc_service.classify_phone_subnets("N_DEMO_HNL_003")
    assert hnl_strat == "SINGLE_LAN"
    assert hnl_subs[0][0] == "10.30.10.0/24"

    swap_service = ApplianceSwapService(meraki_provider=meraki)
    swap_res = swap_service.execute_appliance_swap(
        network_id="N_DEMO_DAL_001",
        old_serial="Q2XX-DEMO-MX01",
        new_serial="Q2XX-DEMO-NEW9",
    )
    assert swap_res["status"] == "SUCCESS"
    assert swap_res["network_name"] == "Dallas HQ - Corporate"


def test_endpoint_diagnostic_service_with_demo_providers(demo_mode_env):
    service = EndpointDiagnosticService(
        ise_provider=get_ise_provider(),
        meraki_provider=get_meraki_provider(),
        dnac_provider=get_dnac_provider(),
        ldap_provider=get_ldap_provider(),
    )

    by_mac = service.diagnose_target("AA:BB:CC:44:55:66")
    assert by_mac["status"] == "NO_ACTIVE_SESSION"
    assert by_mac["catalyst_telemetry"]["dnac_found"] is True
    assert by_mac["meraki_telemetry"]["meraki_found"] is True
    assert by_mac["identity_collision"]["has_collision"] is True
    assert by_mac["identity_collision"]["match_count"] == 2

    by_ip = service.diagnose_target("10.10.20.45")
    assert by_ip["mac_address"] == "AA:BB:CC:11:22:33"
    assert by_ip["status"] == "ACTIVE_SESSION_FOUND"

    by_user = service.diagnose_target("asmith")
    assert by_user["mac_address"] == "DE:AD:BE:EF:00:01"
    assert by_user["catalyst_telemetry"]["client_type"] == "WIRELESS"


# -----------------------------------------------------------------------------
# 3. Telecom Demo Providers (CUCM / UCCX / CUC) & Services
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("cluster", ["PRIMARY", "HAWAII"])
def test_demo_cucm_audit_and_decommission_services(cluster):
    axl = DemoCUCMProvider(cluster=cluster)
    audit = CUCMAuditService(axl_provider=axl)

    physical = audit.run_full_migration_audit(device_scope="PHYSICAL_ONLY")
    softphones = audit.run_full_migration_audit(device_scope="SOFTPHONES_ONLY")
    gateways = audit.run_full_migration_audit(device_scope="ANALOG_GATEWAYS")
    all_devs = audit.run_full_migration_audit(device_scope="ALL")

    assert physical.total_devices >= 2
    assert physical.tier_1_mpp_direct_count >= 1
    assert physical.tier_3_incompatible_count >= 1
    assert physical.e164_compliant_lines_count >= 1
    assert physical.e164_non_compliant_lines_count >= 1
    assert softphones.total_devices >= 1
    assert softphones.tier_2_gateway_softphone_count >= 1
    assert gateways.total_devices >= 1
    assert all_devs.total_devices == (
        physical.total_devices + softphones.total_devices + gateways.total_devices
    )

    decomm = CucmDecommissionService(axl_provider=axl, cluster_name=cluster)
    opts = decomm.get_dialplan_options()
    assert len(opts.partitions) >= 1
    assert len(opts.gateways_and_route_lists) >= 1

    ext = "5101" if cluster == "HAWAII" else "4101"
    pt = opts.partitions[0].name
    gw = opts.gateways_and_route_lists[0].name

    rp_dry = decomm.create_route_pattern(extension=ext, partition=pt, gateway_route_list=gw, dry_run=True)
    assert rp_dry.status == "DRY_RUN"

    tp_live = decomm.create_translation_pattern(
        extension="4999",
        partition=pt,
        calling_search_space="To_LGW_CSS",
        dry_run=False,
    )
    assert tp_live.status == "SUCCESS"
    assert axl.get_translation_pattern("4999", pt) is not None

    target_mac = "SEPAABBCCDDEE01" if cluster == "HAWAII" else "SEP001122334455"
    res = decomm.decommission_devices(
        items=[
            CucmDeviceDecommissionItem(
                mac_address=target_mac,
                extension=ext,
                partition=pt,
                gateway_route_list=gw,
                create_route_pattern=True,
                delete_unassigned_dn=True,
            )
        ],
        dry_run=False,
    )
    assert res.successful_count == 1
    assert res.results[0].device_deletion_status == "SUCCESS"
    assert axl.get_device_by_name_or_mac(target_mac) is None
    assert axl.get_route_pattern(ext, pt) is not None


@pytest.mark.parametrize("cluster", ["PRIMARY", "HAWAII"])
def test_demo_uccx_and_cupi_services(cluster, tmp_path):
    uccx = DemoUCCXProvider(cluster=cluster)
    assert uccx.is_reachable() is True

    uccx_audit = UCCXMigrationService(uccx_provider=uccx).run_full_contact_center_audit()
    assert uccx_audit.total_csqs >= 2
    assert uccx_audit.total_skills >= 2
    assert uccx_audit.total_agents >= 2
    assert uccx_audit.total_applications >= 1

    prompt_dir = tmp_path / "uccx_prompts" / cluster.lower()
    prompt_summary = UCCXPromptExportService(uccx_provider=uccx).export_prompts_recursively(
        target_language_folders=["en_US", "es_US"],
        output_dir=str(prompt_dir),
    )
    assert prompt_summary.total_discovered >= 4
    assert prompt_summary.total_downloaded == prompt_summary.total_discovered
    assert prompt_summary.total_compliant == prompt_summary.total_downloaded
    assert prompt_summary.total_non_compliant == 0

    script_svc = UCCXScriptMigrationService(uccx_provider=uccx)
    scripts = uccx.get_scripts()
    content = script_svc.acquire_script(scripts[0]["path"])
    conv = script_svc.convert(scripts[0]["name"], content, trigger_dn="8085550100")
    assert len(conv.flow_schema["nodes"]) >= 4

    cupi = DemoCUPIProvider(cluster=cluster)
    vm_dir = tmp_path / "voicemail" / cluster.lower()
    vm_summary = VoicemailExportService(cupi_provider=cupi, export_dir=str(vm_dir)).run_full_voicemail_export()
    assert vm_summary.total_voicemail_users >= 1
    assert vm_summary.total_call_handlers >= 1
    assert vm_summary.total_prompts_downloaded == 2

    manifest = SourceInventoryService(
        DemoCUCMProvider(cluster=cluster),
        DemoUCCXProvider(cluster=cluster),
        DemoCUPIProvider(cluster=cluster),
    ).collect(cluster=cluster)
    assert manifest.cluster == cluster
    assert manifest.errors == []
    assert manifest.cucm["total_devices"] >= 2
    assert manifest.uccx["total_csqs"] >= 2
    assert manifest.cuc["counts"]["users"] >= 1


# -----------------------------------------------------------------------------
# 4. FastAPI Lifespan & Route Integration in DEMO_MODE
# -----------------------------------------------------------------------------


def test_lifespan_skips_meraki_cache_warmup_in_demo_mode(demo_mode_env):
    with patch("main.warm_meraki_cache") as mock_warm:
        with TestClient(app) as client:
            resp = client.get("/api/v1/meraki-health")
            assert resp.status_code == 200
            assert resp.json()["status"] == "OK"
            assert resp.json()["mode"] == "DEMO"
        mock_warm.assert_not_called()


def test_fastapi_routes_work_end_to_end_in_demo_mode(demo_mode_env):
    with TestClient(app) as client:
        ndg_resp = client.get("/api/v1/admin/ise-ndg-options")
        assert ndg_resp.status_code == 200
        assert "device_types" in ndg_resp.json()

        nets_resp = client.get("/api/v1/admin/meraki-networks")
        assert nets_resp.status_code == 200
        assert len(nets_resp.json()) == 3

        diag_resp = client.get("/api/v1/diagnose/AA:BB:CC:44:55:66")
        assert diag_resp.status_code == 200
        diag_data = diag_resp.json()
        assert diag_data["catalyst_telemetry"]["dnac_found"] is True
        assert diag_data["meraki_telemetry"]["meraki_found"] is True
        assert diag_data["identity_collision"]["has_collision"] is True

        dialplan_resp = client.get("/api/v1/telecom/cucm/dialplan-options?cluster=HAWAII")
        assert dialplan_resp.status_code == 200
        assert dialplan_resp.json()["cluster"] == "HAWAII"
        assert len(dialplan_resp.json()["partitions"]) >= 1

        uccx_resp = client.get("/api/v1/telecom/uccx/applications?cluster=PRIMARY")
        assert uccx_resp.status_code == 200
        assert uccx_resp.json()["total_applications"] >= 1

        inv_resp = client.post("/api/v1/telecom/source-inventory", json={"cluster": "HAWAII"})
        assert inv_resp.status_code == 200
        inv_data = inv_resp.json()["inventory"]
        assert inv_data["cluster"] == "HAWAII"
        assert inv_data["errors"] == []
