"""
tests/test_webex_workspace_import.py

Unit and integration tests for WebexWorkspaceImportService:
- Vendor CSV parsing (handles 'locatioName' typo)
- MAC normalization & regex validation
- Phone model tiering (Tier 1 MPP direct vs. Legacy 79xx warnings)
- Batch pre-flight validation against Webex Control Hub locations
- Dry-run simulation vs. Live provisioning (workspaces + devices)
- Postman Collection v2.1 JSON export
"""

import os
from unittest.mock import MagicMock
import pytest

from services.webex_workspace_import_service import (
    WebexWorkspaceImportService,
    WorkspaceImportRow,
    WorkspaceValidationSummary,
)
from providers.webex_calling_provider import WebexCallingProvider


SAMPLE_CSV_SNIPPET = """displayName,locatioName,type,capacity,callingType,extension,mac,model
CLH Lanihuli Lounge Phone,CLH - Billing Lanihuli,desk,1,webexCalling,851845,70BD96EC9906,Cisco 9841
CLH Lanihuli Processing,CLH - Billing Lanihuli,desk,1,webexCalling,851847,70BD96EC9915,Cisco 9841
CLH Cyto Reading Room 1,CLH Aiea Heights,desk,1,webexCalling,845100,10E3760139EB,Cisco 7821
"CLH Lim, Roman",CLH Aiea Heights,desk,1,webexCalling,844102,081FF36349AC,Cisco 7942
"""

ASSIGNED_DEVICE_CSV = """macAddress,model,ownerType,ownerEmail,personId,workspaceId,locationName,locationId,displayName,notes
AA11BB22CC33,Cisco 8841,USER,alex@example.com,,,Office,loc_1,Alex Example,
DD44EE55FF66,Cisco 8841,WORKSPACE,,,,Office,loc_1,Conference Room,
"""


def test_parse_vendor_csv_snippet():
    service = WebexWorkspaceImportService()
    parsed = service.parse_csv(SAMPLE_CSV_SNIPPET)

    assert len(parsed) == 4
    # Check vendor column mapping with locatioName typo
    assert parsed[0]["displayName"] == "CLH Lanihuli Lounge Phone"
    assert parsed[0]["locatioName"] == "CLH - Billing Lanihuli"
    assert parsed[0]["mac"] == "70BD96EC9906"
    assert parsed[0]["model"] == "Cisco 9841"
    assert parsed[0]["extension"] == "851845"

    # Check quoted name handling
    assert parsed[3]["displayName"] == "CLH Lim, Roman"
    assert parsed[3]["model"] == "Cisco 7942"


def test_existing_owner_csv_validates_and_assigns_devices_without_extensions():
    mock_provider = MagicMock(spec=WebexCallingProvider)
    mock_provider.get_locations.return_value = [{"id": "loc_1", "name": "Office"}]
    mock_provider.get_person_by_email.return_value = {"id": "person_1"}
    mock_provider.get_workspace_by_name.return_value = {"id": "ws_1"}

    service = WebexWorkspaceImportService(webex_provider=mock_provider)
    raw_rows = service.parse_csv(ASSIGNED_DEVICE_CSV)
    summary = service.validate_batch(raw_rows, mock_provider)

    assert len(raw_rows) == 2
    assert raw_rows[0]["ownerType"] == "USER"
    assert raw_rows[0]["ownerEmail"] == "alex@example.com"
    assert raw_rows[0]["callingType"] == "none"
    assert [row.status for row in summary.rows] == ["READY", "READY"]
    assert summary.rows[0].person_id == "person_1"
    assert summary.rows[1].workspace_id == "ws_1"
    assert all(not row.extension for row in summary.rows)

    mock_provider.provision_mpp_device.return_value = {"id": "device_user"}
    mock_provider.add_device_to_workspace.return_value = {"id": "device_workspace"}
    execution = service.execute_batch(
        [row.model_dump() for row in summary.rows],
        mock_provider,
        dry_run=False,
    )

    assert execution.successful_count == 2
    mock_provider.provision_mpp_device.assert_called_once_with(
        mac_address="AA11BB22CC33",
        model="Cisco 8841",
        location_id="loc_1",
        person_id="person_1",
    )
    mock_provider.add_device_to_workspace.assert_called_once_with(
        mac_address="DD44EE55FF66",
        model="Cisco 8841",
        workspace_id="ws_1",
    )
    mock_provider.get_workspace_by_name.assert_called_once_with("Conference Room")
    mock_provider.create_workspace_with_calling.assert_not_called()


def test_parse_full_sample_file_if_present():
    sample_path = os.path.abspath("imports/migration_csv/sample_vendor_workspaces.csv")
    if os.path.exists(sample_path):
        service = WebexWorkspaceImportService()
        with open(sample_path, "r", encoding="utf-8") as f:
            content = f.read()
        rows = service.parse_csv(content)
        assert len(rows) == 222
        assert rows[0]["mac"] == "70BD96EC9906"


def test_mac_normalization_and_validation():
    service = WebexWorkspaceImportService()
    assert service.normalize_mac("70:BD:96:EC:99:06") == "70BD96EC9906"
    assert service.normalize_mac("70-bd-96-ec-99-06") == "70BD96EC9906"
    assert service.normalize_mac("70bd.96ec.9906") == "70BD96EC9906"
    assert service.normalize_mac("  70BD96EC9906  ") == "70BD96EC9906"


def test_model_tier_classification():
    service = WebexWorkspaceImportService()

    # Tier 1 MPP Direct
    tier, warn = service.classify_model_tier("Cisco 9841")
    assert tier == "TIER_1_MPP_DIRECT"
    assert warn is None

    tier, warn = service.classify_model_tier("Cisco 7821")
    assert tier == "TIER_1_MPP_DIRECT"
    assert warn is None

    tier, warn = service.classify_model_tier("CP-8841")
    assert tier == "TIER_1_MPP_DIRECT"
    assert warn is None

    # Tier 3 Legacy Refresh
    tier, warn = service.classify_model_tier("Cisco 7942")
    assert tier == "TIER_3_LEGACY"
    assert "legacy Cisco hardware" in warn

    tier, warn = service.classify_model_tier("Cisco 7936")
    assert tier == "TIER_3_LEGACY"
    assert "legacy Cisco hardware" in warn


def test_validation_batch_with_mock_provider():
    mock_provider = MagicMock(spec=WebexCallingProvider)
    mock_provider.get_locations.return_value = [
        {"id": "loc_lanihuli_123", "name": "CLH - Billing Lanihuli"},
        {"id": "loc_aiea_456", "name": "CLH Aiea Heights"},
    ]
    mock_provider.get_workspace_calling_licenses.return_value = [
        {"id": "lic_wxc_workspace_789", "name": "Webex Calling - Workspaces", "totalUnits": 500, "consumedUnits": 120}
    ]

    service = WebexWorkspaceImportService(webex_provider=mock_provider)
    raw_rows = service.parse_csv(SAMPLE_CSV_SNIPPET)
    summary: WorkspaceValidationSummary = service.validate_batch(raw_rows, mock_provider)

    assert summary.total_rows == 4
    assert summary.locations_mapped.get("CLH - Billing Lanihuli") == "loc_lanihuli_123"
    assert summary.locations_mapped.get("CLH Aiea Heights") == "loc_aiea_456"
    assert summary.default_calling_license_id == "lic_wxc_workspace_789"

    # Rows 1, 2, 3 should be READY
    assert summary.rows[0].status == "READY"
    assert summary.rows[0].location_id == "loc_lanihuli_123"
    assert summary.rows[1].status == "READY"
    assert summary.rows[2].status == "READY"

    # Row 4 is a legacy 7942 phone -> should have WARNING status
    assert summary.rows[3].status == "WARNING"
    assert any("legacy Cisco hardware" in issue for issue in summary.rows[3].issues)


def test_dry_run_batch_import():
    mock_provider = MagicMock(spec=WebexCallingProvider)
    mock_provider.get_locations.return_value = [
        {"id": "loc_lanihuli_123", "name": "CLH - Billing Lanihuli"}
    ]
    mock_provider.get_workspace_calling_licenses.return_value = [
        {"id": "lic_1", "name": "Webex Calling - Workspaces"}
    ]

    service = WebexWorkspaceImportService(webex_provider=mock_provider)
    rows_to_import = [
        {
            "row_number": 1,
            "display_name": "CLH Lanihuli Lounge Phone",
            "location_name": "CLH - Billing Lanihuli",
            "location_id": "loc_lanihuli_123",
            "extension": "851845",
            "mac": "70BD96EC9906",
            "model": "Cisco 9841",
            "selected": True,
        }
    ]

    result = service.execute_batch(rows_to_import, mock_provider, dry_run=True)
    assert result.dry_run is True
    assert result.successful_count == 1
    assert result.failed_count == 0
    assert result.items[0].status == "DRY_RUN"

    # Provider mutation methods should NOT be called during dry run
    mock_provider.create_workspace_with_calling.assert_not_called()
    mock_provider.add_device_to_workspace.assert_not_called()


def test_live_batch_import():
    mock_provider = MagicMock(spec=WebexCallingProvider)
    mock_provider.get_locations.return_value = [
        {"id": "loc_lanihuli_123", "name": "CLH - Billing Lanihuli"}
    ]
    mock_provider.get_workspace_calling_licenses.return_value = [
        {"id": "lic_1", "name": "Webex Calling - Workspaces"}
    ]
    mock_provider.create_workspace_with_calling.return_value = {
        "id": "ws_uuid_101",
        "displayName": "CLH Lanihuli Lounge Phone"
    }
    mock_provider.add_device_to_workspace.return_value = {
        "id": "dev_uuid_202",
        "mac": "70BD96EC9906"
    }

    service = WebexWorkspaceImportService(webex_provider=mock_provider)
    rows_to_import = [
        {
            "row_number": 1,
            "display_name": "CLH Lanihuli Lounge Phone",
            "location_name": "CLH - Billing Lanihuli",
            "location_id": "loc_lanihuli_123",
            "extension": "851845",
            "mac": "70BD96EC9906",
            "model": "Cisco 9841",
            "selected": True,
        }
    ]

    result = service.execute_batch(rows_to_import, mock_provider, dry_run=False, calling_license_id="lic_1")
    assert result.dry_run is False
    assert result.successful_count == 1
    assert result.items[0].status == "SUCCESS"
    assert result.items[0].workspace_id == "ws_uuid_101"
    assert result.items[0].device_id == "dev_uuid_202"

    # Verify calls made
    mock_provider.create_workspace_with_calling.assert_called_once_with(
        display_name="CLH Lanihuli Lounge Phone",
        location_id="loc_lanihuli_123",
        workspace_type="desk",
        capacity=1,
        calling_type="webexCalling",
        extension="851845",
        phone_number=None,
        calling_license_id="lic_1"
    )
    mock_provider.add_device_to_workspace.assert_called_once_with(
        mac_address="70BD96EC9906",
        model="Cisco 9841",
        workspace_id="ws_uuid_101"
    )


def test_postman_collection_generation():
    service = WebexWorkspaceImportService()
    rows = service.parse_csv(SAMPLE_CSV_SNIPPET)
    collection = service.generate_postman_collection(rows)

    assert collection["info"]["name"] == "WxC Workspaces Import (Generated)"
    assert len(collection["item"]) == 4

    item_names = [item["name"] for item in collection["item"]]
    assert "1. GET All Location Info" in item_names
    assert "2. GET License Information for Workspaces" in item_names
    assert "3. POST Create Workspace Ext Standard" in item_names
    assert "4. POST Add Device to New Workspace" in item_names


def test_postman_collection_rejects_existing_owner_rows():
    service = WebexWorkspaceImportService()
    with pytest.raises(ValueError, match="existing-owner devices"):
        service.generate_postman_collection([{"owner_type": "USER"}])
