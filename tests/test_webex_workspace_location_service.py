from unittest.mock import MagicMock, call

import pytest

from providers.webex_calling_provider import WebexAPIError, WebexCallingProvider
from services.webex_workspace_location_service import WebexWorkspaceLocationService


SAMPLE_CSV = """displayName,locatioName,type,capacity,callingType,extension,mac,model
Lanihuli Lounge Phone,CLH Aiea Heights,desk,1,webexCalling,851845,70:BD:96:EC:99:06,Cisco 9841
"""


def make_provider():
    provider = MagicMock(spec=WebexCallingProvider)
    workspace = {
        "id": "workspace_1",
        "displayName": "Lanihuli Lounge Phone",
        "locationId": "loc_old",
        "type": "desk",
        "capacity": 1,
        "calling": {
            "type": "webexCalling",
            "webexCalling": {"locationId": "loc_old", "extension": "851845"},
        },
    }
    replacement = {
        "id": "replacement_1",
        "displayName": "Lanihuli Lounge Phone [move-test]",
        "locationId": "loc_new",
        "type": "desk",
        "capacity": 1,
        "calling": {
            "type": "webexCalling",
            "webexCalling": {"locationId": "loc_new", "extension": "851845"},
        },
    }
    replacement_devices = []
    provider.get_locations.return_value = [
        {"id": "loc_old", "name": "CLH - Billing Lanihuli"},
        {"id": "loc_new", "name": "CLH Aiea Heights"},
    ]
    provider.get_workspaces_by_name.return_value = [workspace]
    provider.get_workspace.side_effect = lambda workspace_id: (
        workspace if workspace_id == "workspace_1" else replacement
    )
    provider.get_devices_by_workspace.return_value = [
        {"id": "device_1", "mac": "70BD96EC9906", "product": "Cisco 9841"}
    ]
    provider.create_workspace_with_calling.return_value = replacement
    def add_device_to_workspace(*, mac_address, model, workspace_id):
        if workspace_id == "replacement_1":
            replacement_devices.append(
                {"id": "device_replacement", "mac": mac_address, "product": model}
            )
        return {"id": "device_replacement"}

    provider.add_device_to_workspace.side_effect = add_device_to_workspace
    provider.update_workspace.side_effect = lambda workspace_id, payload: (
        replacement.update(payload) or replacement
    )
    provider.update_workspace_location.return_value = {"id": "workspace_1"}
    return provider


def test_preview_resolves_existing_workspace_without_mutation():
    provider = make_provider()
    provider.get_workspaces_by_name.return_value[0].pop("calling")
    service = WebexWorkspaceLocationService(provider)

    result = service.correct_locations(SAMPLE_CSV, dry_run=True)

    assert result["dry_run"] is True
    assert result["successful_count"] == 1
    assert result["items"][0]["status"] == "DRY_RUN"
    assert result["items"][0]["current_location"] == "CLH - Billing Lanihuli"
    assert result["items"][0]["target_location"] == "CLH Aiea Heights"
    assert result["items"][0]["mac"] == "70BD96EC9906"
    provider.update_workspace_location.assert_not_called()


def test_live_correction_moves_existing_workspace_once():
    provider = make_provider()
    provider.get_workspaces_by_name.return_value[0].pop("calling")
    service = WebexWorkspaceLocationService(provider)

    result = service.correct_locations(SAMPLE_CSV, dry_run=False)

    assert result["successful_count"] == 1
    assert result["items"][0]["status"] == "SUCCESS"
    provider.update_workspace_location.assert_called_once()
    args = provider.update_workspace_location.call_args.args
    assert args[0]["id"] == "workspace_1"
    assert args[1] == "loc_new"


def test_preview_allows_unassigned_csv_phone_for_workspace_location_migration():
    provider = make_provider()
    provider.get_workspaces_by_name.return_value[0].pop("calling")
    provider.get_devices_by_workspace.return_value = []
    provider.get_all_devices.return_value = []
    service = WebexWorkspaceLocationService(provider)

    result = service.correct_locations(SAMPLE_CSV, dry_run=True)

    assert result["items"][0]["status"] == "DRY_RUN"
    assert "unassigned CSV phone will be registered" in result["items"][0]["error_message"]
    provider.get_all_devices.assert_called_once()
    provider.update_workspace_location.assert_not_called()
    provider.add_device_to_workspace.assert_not_called()


def test_live_location_migration_registers_unassigned_csv_phone_in_target_workspace():
    provider = make_provider()
    provider.get_workspaces_by_name.return_value[0].pop("calling")
    provider.get_devices_by_workspace.return_value = []
    provider.get_all_devices.return_value = []
    service = WebexWorkspaceLocationService(provider)

    result = service.correct_locations(SAMPLE_CSV, dry_run=False)

    assert result["items"][0]["status"] == "SUCCESS"
    provider.update_workspace_location.assert_called_once_with(
        provider.get_workspaces_by_name.return_value[0],
        "loc_new",
    )
    provider.add_device_to_workspace.assert_called_once_with(
        mac_address="70BD96EC9906",
        model="Cisco 9841",
        workspace_id="workspace_1",
    )


def test_live_location_migration_registers_unassigned_phone_when_workspace_already_at_target():
    provider = make_provider()
    workspace = provider.get_workspaces_by_name.return_value[0]
    workspace.pop("calling")
    workspace["locationId"] = "loc_new"
    provider.get_devices_by_workspace.return_value = []
    provider.get_all_devices.return_value = []
    service = WebexWorkspaceLocationService(provider)

    result = service.correct_locations(SAMPLE_CSV, dry_run=False)

    assert result["items"][0]["status"] == "SUCCESS"
    provider.update_workspace_location.assert_not_called()
    provider.add_device_to_workspace.assert_called_once_with(
        mac_address="70BD96EC9906",
        model="Cisco 9841",
        workspace_id="workspace_1",
    )


def test_location_migration_blocks_phone_assigned_to_another_workspace():
    provider = make_provider()
    provider.get_workspaces_by_name.return_value[0].pop("calling")
    provider.get_devices_by_workspace.return_value = []
    provider.get_all_devices.return_value = [
        {"id": "device_other", "mac": "70BD96EC9906", "workspaceId": "other_workspace"}
    ]
    service = WebexWorkspaceLocationService(provider)

    result = service.correct_locations(SAMPLE_CSV, dry_run=False)

    assert result["items"][0]["status"] == "FAILED"
    assert "already assigned" in result["items"][0]["error_message"]
    provider.update_workspace_location.assert_not_called()
    provider.add_device_to_workspace.assert_not_called()


def test_calling_workspace_preview_plans_safe_migration_without_mutation():
    provider = make_provider()
    service = WebexWorkspaceLocationService(provider)

    preview = service.correct_locations(SAMPLE_CSV, dry_run=True)

    assert preview["items"][0]["status"] == "DRY_RUN"
    assert "create and verify a replacement workspace" in preview["items"][0]["error_message"]
    provider.get_workspace.assert_called_once_with("workspace_1")
    provider.update_workspace_location.assert_not_called()
    provider.delete_device.assert_not_called()
    provider.update_workspace.assert_not_called()
    provider.add_device_to_workspace.assert_not_called()


def test_live_calling_workspace_migration_runs_state_machine(monkeypatch):
    provider = make_provider()
    provider.get_devices_by_workspace.side_effect = lambda workspace_id: (
        [{"id": "device_1", "mac": "70BD96EC9906", "product": "Cisco 9841"}]
        if workspace_id == "workspace_1"
        else [{"id": "device_replacement", "mac": "70BD96EC9906", "product": "Cisco 9841"}]
    )
    service = WebexWorkspaceLocationService(provider)

    result = service.correct_locations(SAMPLE_CSV, dry_run=False)

    assert result["items"][0]["status"] == "SUCCESS"
    provider.create_workspace_with_calling.assert_called_once()
    assert provider.update_workspace.call_count == 2
    replacement_setup = provider.update_workspace.call_args_list[0].args[1]
    final_payload = provider.update_workspace.call_args_list[1].args[1]
    assert replacement_setup["locationId"] == "loc_new"
    assert replacement_setup["calling"]["type"] == "webexCalling"
    assert final_payload["displayName"] == "Lanihuli Lounge Phone"
    assert final_payload["locationId"] == "loc_new"
    assert final_payload["calling"]["webexCalling"]["extension"] == "851845"
    provider.delete_device.assert_called_once_with("device_1")
    provider.add_device_to_workspace.assert_called_once_with(
        mac_address="70BD96EC9906",
        model="Cisco 9841",
        workspace_id="replacement_1",
    )
    provider.delete_workspace.assert_called_once_with("workspace_1")
    assert result["items"][0]["workspace_id"] == "replacement_1"
    verify_target_index = provider.mock_calls.index(
        call.get_devices_by_workspace("replacement_1")
    )
    delete_original_index = provider.mock_calls.index(
        call.delete_workspace("workspace_1")
    )
    rename_replacement_index = provider.mock_calls.index(
        call.update_workspace("replacement_1", final_payload)
    )
    assert verify_target_index < delete_original_index < rename_replacement_index
    provider.update_workspace_location.assert_not_called()


def test_calling_migration_adds_unassigned_csv_phone_to_replacement():
    provider = make_provider()
    provider.get_devices_by_workspace.side_effect = lambda workspace_id: (
        []
        if workspace_id == "workspace_1"
        else [{"id": "device_replacement", "mac": "70BD96EC9906", "product": "Cisco 9841"}]
    )
    provider.get_all_devices.return_value = []
    service = WebexWorkspaceLocationService(provider)

    result = service.correct_locations(SAMPLE_CSV, dry_run=False)

    assert result["items"][0]["status"] == "SUCCESS"
    provider.delete_device.assert_not_called()
    provider.add_device_to_workspace.assert_called_once_with(
        mac_address="70BD96EC9906",
        model="Cisco 9841",
        workspace_id="replacement_1",
    )
    provider.delete_workspace.assert_called_once_with("workspace_1")


def test_migration_rolls_back_original_workspace_if_replacement_device_attach_fails():
    provider = make_provider()
    provider.get_devices_by_workspace.side_effect = lambda workspace_id: (
        [{"id": "device_1", "mac": "70BD96EC9906", "product": "Cisco 9841"}]
        if workspace_id == "workspace_1"
        else []
    )
    provider.add_device_to_workspace.side_effect = [
        WebexAPIError("HTTP 400: Device could not be assigned"),
        {"id": "device_restored"},
    ]
    service = WebexWorkspaceLocationService(provider)

    result = service.correct_locations(SAMPLE_CSV, dry_run=False)

    assert result["items"][0]["status"] == "FAILED"
    assert "original workspace and device assignments were preserved" in result["items"][0]["error_message"]
    provider.delete_device.assert_called_once_with("device_1")
    assert provider.add_device_to_workspace.call_args_list == [
        call(
            mac_address="70BD96EC9906",
            model="Cisco 9841",
            workspace_id="replacement_1",
        ),
        call(
            mac_address="70BD96EC9906",
            model="Cisco 9841",
            workspace_id="workspace_1",
        ),
    ]
    provider.delete_workspace.assert_called_once_with("replacement_1")
    provider.update_workspace.assert_called_once()


def test_migration_reports_failed_rollback_if_original_assignment_cannot_be_restored():
    provider = make_provider()
    provider.get_devices_by_workspace.side_effect = lambda workspace_id: (
        [{"id": "device_1", "mac": "70BD96EC9906", "product": "Cisco 9841"}]
        if workspace_id == "workspace_1"
        else []
    )
    provider.add_device_to_workspace.side_effect = [
        WebexAPIError("HTTP 400: Device could not be assigned"),
        WebexAPIError("HTTP 400: Device restore failed"),
    ]
    service = WebexWorkspaceLocationService(provider)

    result = service.correct_locations(SAMPLE_CSV, dry_run=False)

    assert result["items"][0]["status"] == "FAILED"
    assert "rollback was incomplete" in result["items"][0]["error_message"]
    assert "could not restore 70BD96EC9906" in result["items"][0]["error_message"]


def test_migration_does_not_delete_original_until_target_is_verified():
    provider = make_provider()
    provider.get_devices_by_workspace.side_effect = lambda workspace_id: (
        [{"id": "device_1", "mac": "70BD96EC9906", "product": "Cisco 9841"}]
        if workspace_id == "workspace_1"
        else []
    )
    service = WebexWorkspaceLocationService(provider)

    result = service.correct_locations(SAMPLE_CSV, dry_run=False)

    assert result["items"][0]["status"] == "FAILED"
    assert "original workspace and device assignments were preserved" in result["items"][0]["error_message"]
    provider.delete_workspace.assert_called_once_with("replacement_1")
    assert "workspace_1" not in [
        call.args[0] for call in provider.delete_workspace.call_args_list
    ]


def test_migration_reports_replacement_for_manual_completion_after_original_deletion():
    provider = make_provider()
    provider.get_devices_by_workspace.side_effect = lambda workspace_id: (
        [{"id": "device_1", "mac": "70BD96EC9906", "product": "Cisco 9841"}]
        if workspace_id == "workspace_1"
        else [{"id": "device_replacement", "mac": "70BD96EC9906", "product": "Cisco 9841"}]
    )
    provider.update_workspace.side_effect = [
        {"id": "replacement_1"},
        WebexAPIError("HTTP 400: Replacement rename failed"),
    ]
    service = WebexWorkspaceLocationService(provider)

    result = service.correct_locations(SAMPLE_CSV, dry_run=False)

    assert result["items"][0]["status"] == "FAILED"
    assert "original workspace was deleted" in result["items"][0]["error_message"]
    assert "replacement_1" in result["items"][0]["error_message"]
    provider.delete_workspace.assert_called_once_with("workspace_1")
    provider.delete_device.assert_called_once_with("device_1")
    provider.add_device_to_workspace.assert_called_once_with(
        mac_address="70BD96EC9906",
        model="Cisco 9841",
        workspace_id="replacement_1",
    )


def test_migration_transfers_existing_phone_number_after_original_workspace_is_deleted():
    provider = make_provider()
    provider.get_workspaces_by_name.return_value[0]["calling"]["webexCalling"]["phoneNumber"] = "+18085551212"
    provider.get_devices_by_workspace.side_effect = lambda workspace_id: (
        [{"id": "device_1", "mac": "70BD96EC9906", "product": "Cisco 9841"}]
        if workspace_id == "workspace_1"
        else [{"id": "device_replacement", "mac": "70BD96EC9906", "product": "Cisco 9841"}]
    )
    service = WebexWorkspaceLocationService(provider)

    result = service.correct_locations(SAMPLE_CSV, dry_run=False)

    assert result["items"][0]["status"] == "SUCCESS"
    assert provider.create_workspace_with_calling.call_args.kwargs["phone_number"] is None
    initial_calling = provider.update_workspace.call_args_list[0].args[1]["calling"]["webexCalling"]
    final_calling = provider.update_workspace.call_args_list[1].args[1]["calling"]["webexCalling"]
    assert "phoneNumber" not in initial_calling
    assert final_calling["phoneNumber"] == "+18085551212"
    delete_original_index = provider.mock_calls.index(
        call.delete_workspace("workspace_1")
    )
    restore_number_index = provider.mock_calls.index(
        call.update_workspace("replacement_1", provider.update_workspace.call_args_list[1].args[1])
    )
    assert delete_original_index < restore_number_index


def test_live_migration_uses_csv_extension_and_preserves_calling_licenses(monkeypatch):
    provider = make_provider()
    provider.get_workspaces_by_name.return_value[0]["calling"]["webexCalling"]["licenses"] = ["license_1"]
    csv_content = SAMPLE_CSV.replace(",851845,", ",851999,")
    service = WebexWorkspaceLocationService(provider)

    result = service.correct_locations(csv_content, dry_run=False)

    assert result["items"][0]["status"] == "SUCCESS"
    replacement_calling = provider.create_workspace_with_calling.call_args.kwargs
    assert replacement_calling["extension"] == "851999"
    assert replacement_calling["calling_license_id"] == "license_1"
    calling = provider.update_workspace.call_args_list[0].args[1]["calling"]["webexCalling"]
    assert calling["extension"] == "851999"
    assert calling["licenses"] == ["license_1"]


def test_live_migration_uses_extension_from_workspace_when_csv_omits_it(monkeypatch):
    provider = make_provider()
    csv_content = SAMPLE_CSV.replace(",851845,", ",,")
    service = WebexWorkspaceLocationService(provider)

    result = service.correct_locations(csv_content, dry_run=False)

    assert result["items"][0]["status"] == "SUCCESS"
    calling = provider.update_workspace.call_args_list[0].args[1]["calling"]["webexCalling"]
    assert calling["extension"] == "851845"


def test_live_migration_blocks_roomos_device_before_mutation():
    provider = make_provider()
    provider.get_devices_by_workspace.return_value[0]["type"] = "roomdesk"
    service = WebexWorkspaceLocationService(provider)

    result = service.correct_locations(SAMPLE_CSV, dry_run=False)

    assert result["items"][0]["status"] == "FAILED"
    assert "RoomOS device" in result["items"][0]["error_message"]
    provider.delete_device.assert_not_called()
    provider.create_workspace_with_calling.assert_not_called()


def test_live_migration_blocks_missing_extension_before_mutation():
    provider = make_provider()
    provider.get_workspaces_by_name.return_value[0]["calling"]["webexCalling"].pop("extension")
    service = WebexWorkspaceLocationService(provider)
    csv_content = SAMPLE_CSV.replace(",851845,", ",,")

    result = service.correct_locations(csv_content, dry_run=False)

    assert result["items"][0]["status"] == "FAILED"
    assert "No target extension" in result["items"][0]["error_message"]
    provider.delete_device.assert_not_called()
    provider.create_workspace_with_calling.assert_not_called()


def test_recovery_preview_identifies_unassigned_device_without_mutation():
    provider = make_provider()
    provider.get_devices_by_workspace.return_value = []
    provider.get_all_devices.return_value = []
    service = WebexWorkspaceLocationService(provider)

    result = service.recover_device_assignments(SAMPLE_CSV, dry_run=True)

    assert result["items"][0]["status"] == "DRY_RUN"
    assert "can be restored" in result["items"][0]["error_message"]
    assert result["items"][0]["workspace_id"] == "workspace_1"
    provider.add_device_to_workspace.assert_not_called()
    provider.delete_device.assert_not_called()
    provider.update_workspace.assert_not_called()


def test_live_recovery_reassociates_unassigned_device_to_original_workspace():
    provider = make_provider()
    provider.get_devices_by_workspace.return_value = []
    provider.get_all_devices.return_value = []
    service = WebexWorkspaceLocationService(provider)

    result = service.recover_device_assignments(SAMPLE_CSV, dry_run=False)

    assert result["items"][0]["status"] == "SUCCESS"
    provider.add_device_to_workspace.assert_called_once_with(
        mac_address="70BD96EC9906",
        model="Cisco 9841",
        workspace_id="workspace_1",
    )


def test_recovery_is_unchanged_when_device_is_already_assigned_to_original_workspace():
    provider = make_provider()
    service = WebexWorkspaceLocationService(provider)

    result = service.recover_device_assignments(SAMPLE_CSV, dry_run=False)

    assert result["items"][0]["status"] == "UNCHANGED"
    provider.get_all_devices.assert_not_called()
    provider.add_device_to_workspace.assert_not_called()


def test_recovery_blocks_mac_registered_to_another_workspace():
    provider = make_provider()
    provider.get_devices_by_workspace.return_value = []
    provider.get_all_devices.return_value = [
        {
            "id": "device_elsewhere",
            "mac": "70:BD:96:EC:99:06",
            "workspaceId": "other_workspace",
        }
    ]
    service = WebexWorkspaceLocationService(provider)

    result = service.recover_device_assignments(SAMPLE_CSV, dry_run=False)

    assert result["items"][0]["status"] == "FAILED"
    assert "still registered elsewhere" in result["items"][0]["error_message"]
    provider.add_device_to_workspace.assert_not_called()


def test_recovery_blocks_workspace_without_webex_calling():
    provider = make_provider()
    provider.get_workspaces_by_name.return_value[0]["calling"] = {"type": "freeCalling"}
    provider.get_devices_by_workspace.return_value = []
    service = WebexWorkspaceLocationService(provider)

    result = service.recover_device_assignments(SAMPLE_CSV, dry_run=False)

    assert result["items"][0]["status"] == "FAILED"
    assert "not currently configured for Webex Calling" in result["items"][0]["error_message"]
    provider.get_all_devices.assert_not_called()
    provider.add_device_to_workspace.assert_not_called()


def test_recovery_requires_csv_model_before_webex_lookup():
    provider = make_provider()
    csv_content = SAMPLE_CSV.replace(",Cisco 9841", ",")
    service = WebexWorkspaceLocationService(provider)

    result = service.recover_device_assignments(csv_content, dry_run=False)

    assert result["items"][0]["status"] == "FAILED"
    assert "Device model is required" in result["items"][0]["error_message"]
    provider.get_workspaces_by_name.assert_not_called()
    provider.add_device_to_workspace.assert_not_called()


def test_preview_rejects_unknown_location_without_workspace_lookup():
    provider = make_provider()
    csv_content = "displayName,locationName,mac\nLanihuli Lounge Phone,Unknown Location,70BD96EC9906\n"
    service = WebexWorkspaceLocationService(provider)

    result = service.correct_locations(csv_content, dry_run=True)

    assert result["items"][0]["status"] == "FAILED"
    assert "not found" in result["items"][0]["error_message"]
    provider.get_workspaces_by_name.assert_not_called()
    provider.update_workspace_location.assert_not_called()


def test_preview_rejects_ambiguous_workspace_name():
    provider = make_provider()
    provider.get_workspaces_by_name.return_value = [
        {"id": "ws_1", "displayName": "Lanihuli Lounge Phone", "locationId": "loc_old"},
        {"id": "ws_2", "displayName": "Lanihuli Lounge Phone", "locationId": "loc_old"},
    ]
    service = WebexWorkspaceLocationService(provider)

    result = service.correct_locations(SAMPLE_CSV, dry_run=True)

    assert result["items"][0]["status"] == "FAILED"
    assert "Multiple existing workspaces" in result["items"][0]["error_message"]
    provider.update_workspace_location.assert_not_called()


def test_preview_accepts_csv_phone_not_yet_assigned_to_workspace():
    provider = make_provider()
    provider.get_devices_by_workspace.return_value = [
        {"id": "other_device", "mac": "001122334455", "product": "Cisco 9841"}
    ]
    service = WebexWorkspaceLocationService(provider)

    result = service.correct_locations(SAMPLE_CSV, dry_run=True)

    assert result["items"][0]["status"] == "DRY_RUN"
    assert "register the unassigned CSV phone" in result["items"][0]["error_message"]
    provider.add_device_to_workspace.assert_not_called()
    provider.update_workspace_location.assert_not_called()


def test_workspace_already_at_target_location_is_unchanged():
    provider = make_provider()
    provider.get_workspaces_by_name.return_value[0]["locationId"] = "loc_new"
    service = WebexWorkspaceLocationService(provider)

    result = service.correct_locations(SAMPLE_CSV, dry_run=False)

    assert result["items"][0]["status"] == "UNCHANGED"
    assert result["unchanged_count"] == 1
    provider.update_workspace_location.assert_not_called()


def test_provider_refuses_calling_enabled_workspace_location_update(monkeypatch):
    provider = WebexCallingProvider(access_token="token")
    request = MagicMock(return_value={"id": "workspace_1"})
    monkeypatch.setattr(provider, "_request", request)
    workspace = {
        "id": "workspace_1",
        "displayName": "Lanihuli Lounge Phone",
        "locationId": "loc_old",
        "type": "desk",
        "capacity": 1,
        "calling": {
            "type": "webexCalling",
            "webexCalling": {"locationId": "loc_old", "extension": "851845"},
        },
    }

    with pytest.raises(WebexAPIError, match="calling-enabled workspace"):
        provider.update_workspace_location(workspace, "loc_new")

    request.assert_not_called()
    assert workspace["calling"]["webexCalling"]["locationId"] == "loc_old"


def test_provider_updates_non_calling_workspace_location(monkeypatch):
    provider = WebexCallingProvider(access_token="token")
    request = MagicMock(return_value={"id": "workspace_1"})
    monkeypatch.setattr(provider, "_request", request)
    workspace = {
        "id": "workspace_1",
        "displayName": "Lanihuli Lounge Phone",
        "locationId": "loc_old",
        "type": "desk",
        "capacity": 1,
    }

    provider.update_workspace_location(workspace, "loc_new")

    request.assert_called_once_with(
        "PUT",
        "/workspaces/workspace_1",
        json={
            "displayName": "Lanihuli Lounge Phone",
            "type": "desk",
            "capacity": 1,
            "locationId": "loc_new",
        },
    )


def test_provider_exposes_workspace_migration_api_calls(monkeypatch):
    provider = WebexCallingProvider(access_token="token")
    request = MagicMock(return_value={"id": "workspace_1"})
    monkeypatch.setattr(provider, "_request", request)

    assert provider.get_workspace("workspace_1") == {"id": "workspace_1"}
    request.assert_called_with("GET", "/workspaces/workspace_1")
    provider.delete_device("device_1")
    request.assert_called_with("DELETE", "/devices/device_1")
    provider.add_device_to_workspace("70:bd:96:ec:99:06", "Cisco 9841", "workspace_1")
    request.assert_called_with(
        "POST",
        "/devices",
        json={
            "mac": "70BD96EC9906",
            "model": "Cisco 9841",
            "workspaceId": "workspace_1",
        },
    )


def test_provider_lists_all_devices_across_pages(monkeypatch):
    provider = WebexCallingProvider(access_token="token")
    request_page = MagicMock(
        side_effect=[
            ({"items": [{"id": "device_1"}]}, "https://webexapis.com/v1/devices?cursor=next"),
            ({"items": [{"id": "device_2"}]}, None),
        ]
    )
    monkeypatch.setattr(provider, "_request_page", request_page)

    assert provider.get_all_devices() == [
        {"id": "device_1"},
        {"id": "device_2"},
    ]
    assert request_page.call_args_list[0].args == ("GET", "/devices")
    assert request_page.call_args_list[1].args == (
        "GET",
        "https://webexapis.com/v1/devices?cursor=next",
    )


def test_empty_csv_is_reported_as_invalid_input():
    service = WebexWorkspaceLocationService(make_provider())

    with pytest.raises(ValueError, match="no workspace rows"):
        service.correct_locations("displayName,locationName,mac\n")
