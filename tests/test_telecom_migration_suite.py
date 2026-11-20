"""
tests/test_telecom_migration_suite.py

Comprehensive Unit and Integration Test Suite for NetDashboard Telecom Migration Modules.
Tests CUCM AXL Data Extraction, Webex Calling Provisioning, and CUC Voicemail Exports.
"""

import os
import pytest
from unittest.mock import MagicMock, patch

from providers.cucm_axl_provider import CUCMAXLProvider, AXLRequestError
from services.cucm_audit_service import CUCMAuditService
from providers.webex_calling_provider import WebexCallingProvider, WebexAPIError
from services.webex_provision_service import WebexProvisionService
from providers.cupi_provider import CUPIProvider
from services.voicemail_export_service import VoicemailExportService

IS_LIVE_TEST = os.getenv("LIVE_TEST", "false").strip().lower() in {"1", "true", "yes"}


# =============================================================================
# Mock Fixtures & XML/JSON Payloads
# =============================================================================

MOCK_AXL_SOAP_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
   <soapenv:Body>
      <ns:executeSQLQueryResponse xmlns:ns="http://www.cisco.com/AXL/API/12.5">
         <return>
            <row>
               <device_name>SEP001122334455</device_name>
               <device_description>Louis Gonzales - Desk Phone</device_description>
               <device_model>Cisco 8841</device_model>
               <owner_userid>lgonzales</owner_userid>
               <owner_email>louis.gonzales@corp.internal</owner_email>
               <line_dn>5125555001</line_dn>
               <partition_name>PT_Internal</partition_name>
               <line_display>Louis Gonzales</line_display>
               <line_label>5001</line_label>
            </row>
            <row>
               <device_name>SEP009988776655</device_name>
               <device_description>Legacy Breakroom Phone</device_description>
               <device_model>Cisco 7942</device_model>
               <owner_userid>Unassigned</owner_userid>
               <owner_email></owner_email>
               <line_dn>5099</line_dn>
               <partition_name>PT_Internal</partition_name>
               <line_display>Breakroom</line_display>
               <line_label>5099</line_label>
            </row>
         </return>
      </ns:executeSQLQueryResponse>
   </soapenv:Body>
</soapenv:Envelope>"""

MOCK_CUPI_USER_RESPONSE = {
    "User": [
        {
            "ObjectId": "usr-uuid-111",
            "Alias": "lgonzales",
            "DisplayName": "Louis Gonzales",
            "DtmfAccessId": "5001",
            "SmtpAddress": "louis.gonzales@corp.internal",
            "PartitionString": "PT_Voicemail"
        }
    ]
}

MOCK_CUPI_HANDLER_RESPONSE = {
    "Callhandler": [
        {
            "ObjectId": "ch-uuid-999",
            "DisplayName": "Main_Auto_Attendant",
            "Extension": "5000"
        }
    ]
}

MOCK_WEBEX_PERSON_RESPONSE = {
    "items": [
        {
            "id": "person-uuid-12345",
            "emails": ["louis.gonzales@corp.internal"],
            "firstName": "Louis",
            "lastName": "Gonzales",
            "licenses": []
        }
    ]
}


# =============================================================================
# 1. CUCM AXL Provider & Audit Service Tests
# =============================================================================

class TestCUCMAuditModule:

    @patch("requests.Session.post")
    def test_axl_query_parsing_and_audit_classification(self, mock_post):
        """Validates SOAP XML parsing, device tier classification, and E.164 line audit logic."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = MOCK_AXL_SOAP_RESPONSE
        mock_post.return_value = mock_resp

        axl = CUCMAXLProvider(host="10.212.16.10", username="test", password="test")
        audit_service = CUCMAuditService(axl_provider=axl)

        summary = audit_service.run_full_migration_audit(default_country_code="+1")

        assert summary.total_devices == 2
        assert summary.tier_1_mpp_direct_count == 1  # Cisco 8841
        assert summary.tier_3_incompatible_count == 1  # Cisco 7942
        assert summary.total_unique_users == 1  # lgonzales

        # Verify device 1 (Cisco 8841 with 10-digit DID 5125555001 -> +15125555001)
        dev1 = next(d for d in summary.devices if d.device_name == "SEP001122334455")
        assert dev1.migration_tier == "TIER_1_MPP_DIRECT"
        assert dev1.owner_userid == "lgonzales"
        assert len(dev1.lines) == 1
        assert dev1.lines[0].is_e164_compliant is True  # Corrected assertion
        assert dev1.lines[0].e164_suggested == "+15125555001"

        # Verify device 2 (Cisco 7942 with 4-digit extension 5099 -> EXT_5099)
        dev2 = next(d for d in summary.devices if d.device_name == "SEP009988776655")
        assert dev2.migration_tier == "TIER_3_INCOMPATIBLE_REFRESH"
        assert dev2.lines[0].is_e164_compliant is False
        assert dev2.lines[0].e164_suggested == "EXT_5099"

    @patch("requests.Session.post")
    def test_axl_soap_fault_handling(self, mock_post):
        """Ensures AXL SOAP Faults raise expected AXLRequestError exceptions."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = """<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
           <soapenv:Body><soapenv:Fault><faultstring>Database lock error</faultstring></soapenv:Fault></soapenv:Body>
        </soapenv:Envelope>"""
        mock_post.return_value = mock_resp

        axl = CUCMAXLProvider(host="10.212.16.10", username="test", password="test")
        with pytest.raises(AXLRequestError):
            axl.execute_sql_query("SELECT * FROM device")


# =============================================================================
# 2. Webex Calling Provisioning Tests
# =============================================================================

class TestWebexProvisioningModule:

    @patch("requests.Session.request")
    def test_webex_provisioning_flow(self, mock_request):
        """Verifies Webex Control Hub Person lookup and MPP device registration."""
        def mock_webex_responses(method, url, **kwargs):
            mock_r = MagicMock()
            mock_r.status_code = 200
            if "/people" in url and kwargs.get("params", {}).get("email"):
                mock_r.json.return_value = MOCK_WEBEX_PERSON_RESPONSE
            elif "/people/person-uuid-12345" in url:
                mock_r.json.return_value = MOCK_WEBEX_PERSON_RESPONSE["items"][0]
            elif "/telephony/config/devices" in url:
                mock_r.json.return_value = {"id": "device-uuid-8888"}
            else:
                mock_r.json.return_value = {}
            return mock_r

        mock_request.side_effect = mock_webex_responses

        provider = WebexCallingProvider(access_token="fake_token", org_id="fake_org")
        service = WebexProvisionService(webex_provider=provider)

        mock_cucm_data = {
            "devices": [
                {
                    "device_name": "SEP001122334455",
                    "device_model": "CP-8841",
                    "migration_tier": "TIER_1_MPP_DIRECT",
                    "owner_email": "louis.gonzales@corp.internal",
                    "lines": [{"dn": "5001", "e164_suggested": "+15125555001"}]
                }
            ]
        }

        summary = service.process_cucm_audit_batch(
            cucm_audit_data=mock_cucm_data,
            location_id="loc-uuid-111",
            calling_license_id="lic-uuid-222"
        )

        assert summary.successful_count == 1
        assert summary.failed_count == 0
        assert summary.items[0].status == "SUCCESS"


# =============================================================================
# 3. CUC CUPI Voicemail & Call Handler Export Tests
# =============================================================================

class TestCUCVoicemailExportModule:

    @patch("requests.Session.get")
    @patch("requests.Session.request")
    def test_cupi_user_and_handler_export(self, mock_request, mock_get):
        """Tests CUPI array normalization and call handler prompt downloads."""
        def mock_cupi_responses(method, url, **kwargs):
            mock_r = MagicMock()
            mock_r.status_code = 200
            if "/vmrest/users" in url:
                mock_r.json.return_value = MOCK_CUPI_USER_RESPONSE
            elif "/handlers/callhandlers?" in url:
                mock_r.json.return_value = MOCK_CUPI_HANDLER_RESPONSE
            elif "/menuentries" in url:
                mock_r.json.return_value = {"MenuEntry": [{"Touchtone": "1", "Action": "Transfer", "TransferExtension": "5001"}]}
            else:
                mock_r.json.return_value = {}
            return mock_r

        mock_request.side_effect = mock_cupi_responses

        mock_audio_r = MagicMock()
        mock_audio_r.status_code = 200
        mock_audio_r.iter_content.return_value = [b"RIFF_MOCK_WAV_HEADER_DATA"]
        mock_get.return_value = mock_audio_r

        cupi = CUPIProvider(host="10.212.16.12", username="test", password="test")
        service = VoicemailExportService(cupi_provider=cupi, export_dir="temp_test_export")

        summary = service.run_full_voicemail_export()

        assert summary.total_voicemail_users == 1
        assert summary.total_call_handlers == 1
        assert summary.users[0].alias == "lgonzales"
        assert summary.call_handlers[0].display_name == "Main_Auto_Attendant"
        assert len(summary.call_handlers[0].dtmf_key_maps) == 1


# =============================================================================
# 4. Optional Live Integration Suite (Executed when LIVE_TEST=true)
# =============================================================================

@pytest.mark.skipif(not IS_LIVE_TEST, reason="Skipping Live Integration Tests. Set LIVE_TEST=true to run against live lab environment.")
class TestLiveTelecomIntegration:

    def test_live_cucm_axl_connection(self):
        """Live test validating CUCM AXL connectivity and basic query execution."""
        axl = CUCMAXLProvider()
        res = axl.execute_sql_query("SELECT FIRST 1 name, description FROM device")
        assert isinstance(res, list)
        print("\n[LIVE TEST] CUCM Device Sample:", res)

    def test_live_cuc_cupi_connection(self):
        """Live test validating CUC CUPI connectivity."""
        cupi = CUPIProvider()
        users = cupi.get_users_paginated(page_size=5)
        assert isinstance(users, list)
        print("\n[LIVE TEST] CUC User Sample Count:", len(users))