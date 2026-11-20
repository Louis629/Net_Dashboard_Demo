# tests/test_multi_cluster_migration.py
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from main import app
from providers.telecom_factory import TelecomClusterRegistry, ClusterEnvironment
from providers.cucm_axl_provider import CUCMAXLProvider
from services.wxcc_flow_generator_service import WxCCFlowGeneratorService
from models.telecom import ClusterTarget

client = TestClient(app)

# -----------------------------------------------------------------------------
# 1. Test Factory & Cluster Registry
# -----------------------------------------------------------------------------

def test_telecom_factory_cluster_resolution(monkeypatch):
    monkeypatch.setenv("PRIMARY_CUCM_AXL_HOST", "10.100.10.30")
    monkeypatch.setenv("PRIMARY_CUCM_AXL_VERSION", "12.5")
    monkeypatch.setenv("HAWAII_CUCM_AXL_HOST", "10.240.10.30")
    monkeypatch.setenv("HAWAII_CUCM_AXL_VERSION", "11.5")
    monkeypatch.setenv("HAWAII_CUCM_AXL_TIMEOUT_SECONDS", "45")

    primary_cucm = TelecomClusterRegistry.get_cucm_provider(ClusterEnvironment.PRIMARY)
    assert primary_cucm.host == "10.100.10.30"
    assert primary_cucm.version == "12.5"
    assert primary_cucm.timeout == 30

    hawaii_cucm = TelecomClusterRegistry.get_cucm_provider(ClusterEnvironment.HAWAII)
    assert hawaii_cucm.host == "10.240.10.30"
    assert hawaii_cucm.version == "11.5"
    assert hawaii_cucm.timeout == 45


# -----------------------------------------------------------------------------
# 2. Test CUCM AXL SOAP Envelope Versioning (11.5 vs 12.5)
# -----------------------------------------------------------------------------

def test_cucm_axl_soap_version_envelope():
    provider_11 = CUCMAXLProvider(host="10.240.10.30", version="11.5")
    envelope_11 = provider_11._build_soap_envelope("SELECT name FROM device")
    assert 'xmlns:ns="http://www.cisco.com/AXL/API/11.5"' in envelope_11
    assert provider_11.session.headers["SOAPAction"] == '"CUCM:DB ver=11.5 executeSQLQuery"'

    provider_12 = CUCMAXLProvider(host="10.100.10.30", version="12.5")
    envelope_12 = provider_12._build_soap_envelope("SELECT name FROM device")
    assert 'xmlns:ns="http://www.cisco.com/AXL/API/12.5"' in envelope_12
    assert provider_12.session.headers["SOAPAction"] == '"CUCM:DB ver=12.5 executeSQLQuery"'


# -----------------------------------------------------------------------------
# 3. Test WxCC Flow Generator (CUC Call Handlers & UCCX Menus)
# -----------------------------------------------------------------------------

def test_wxcc_flow_generator_cuc_compatibility():
    generator = WxCCFlowGeneratorService()
    cuc_mock_handler = {
        "display_name": "Main_AutoAttendant",
        "extension": "5000",
        "dtmf_key_maps": [
            {"touch_tone_key": "1", "action": "Transfer", "target_handler_or_ext": "5001"},
            {"touch_tone_key": "2", "action": "Queue", "target_handler_or_ext": "CSQ_Support"}
        ],
        "downloaded_prompts": ["exports/prompts/Main_Standard.wav"]
    }
    flow_def = generator.convert_call_handler_to_wxcc_flow(cuc_mock_handler)
    assert flow_def.flow_name == "WxCC_Flow_Main_AutoAttendant"
    assert len(flow_def.nodes) == 6  # Entry, PlayMedia, Menu, Transfer, Queue, Timeout Disconnect


def test_wxcc_flow_generator_uccx_menu_flow():
    generator = WxCCFlowGeneratorService()
    mappings = {
        "1": {"type": "QUEUE", "destination": "QUEUE_HAWAII_BILLING"},
        "2": {"type": "TRANSFER", "destination": "+18085551234"}
    }
    flow = generator.generate_uccx_menu_flow(
        flow_name="Hawaii_Main_IVR",
        trigger_dn="8085551000",
        open_greeting_prompt="WPML_Welcome.wav",
        closed_greeting_prompt="WPML_Closed.wav",
        menu_dtmf_mappings=mappings,
        business_hours_id="BH_HAWAII_TIME"
    )
    assert flow.flow_name == "Hawaii_Main_IVR"
    node_types = [n.type for n in flow.nodes]
    assert "BusinessHour" in node_types
    assert "QueueContact" in node_types
    assert "BlindTransfer" in node_types


# -----------------------------------------------------------------------------
# 4. Test Telecom API Routing & Parameter Dispatch
# -----------------------------------------------------------------------------

@patch("routers.telecom.execute_cucm_migration_audit_task.delay")
def test_trigger_cucm_audit_hawaii_route(mock_celery_task):
    mock_celery_task.return_value = MagicMock(id="mock-task-id-1234")

    payload = {
        "cluster": "HAWAII",
        "device_scope": "PHYSICAL_ONLY",
        "device_pool_filter": "ALL",
        "default_country_code": "+1"
    }
    headers = {"X-API-Authorization": "Bearer dev-token"}
    response = client.post("/api/v1/telecom/cucm-audit", json=payload, headers=headers)

    assert response.status_code == 202
    res_data = response.json()
    assert res_data["status"] == "QUEUED"
    assert res_data["cluster"] == "HAWAII"
    assert res_data["task_id"] == "mock-task-id-1234"

    mock_celery_task.assert_called_once_with(
        cluster="HAWAII",
        default_country_code="+1",
        device_scope="PHYSICAL_ONLY",
        device_pool_filter="ALL"
    )