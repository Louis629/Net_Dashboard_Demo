"""Tests for migrating CUCM-registered softphone-only users to Webex Calling Cloud."""

import csv
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from models.telecom import CucmDeviceDecommissionItem, UserCallingProfileItem
from providers.cucm_axl_provider import AXLRequestError, CUCMAXLProvider
from providers.webex_calling_provider import WebexAPIError, WebexCallingProvider
from services.cucm_audit_service import CUCMAuditService, is_softphone_device
from services.cucm_decommission_service import CucmDecommissionService
from services.migration_staging_service import MigrationStagingService
from services.webex_migration_provision_service import WebexMigrationProvisionService
from services.webex_provision_service import WebexProvisionService
from services.webex_user_profile_service import WebexUserProfileService
from services.webex_verification_service import WebexVerificationService


def test_softphone_classification_distinguishes_softphones_from_gateways_and_hardware():
    assert is_softphone_device("CSFjdoe", "Cisco Unified Client Services Framework") is True
    assert is_softphone_device("TCTjdoe", "Cisco Dual Mode for iPhone") is True
    assert is_softphone_device("BOTjdoe", "Cisco Dual Mode for Android") is True
    assert is_softphone_device("TABjdoe", "Cisco Jabber for Tablet") is True
    assert is_softphone_device("", "Webex App (Softphone)") is True

    assert is_softphone_device("SEP001122334455", "Cisco 8841") is False
    assert is_softphone_device("ATA19100112233", "Cisco ATA 191") is False
    assert is_softphone_device("VG320_GW", "Cisco VG320") is False
    assert is_softphone_device("SEP001122334466", "Webex Desk Pro") is False

    assert CUCMAuditService._classify_device_tier("Cisco Unified Client Services Framework", "CSFJDOE") == "TIER_2_GATEWAY_SOFTPHONE"
    assert CUCMAuditService._classify_device_tier("Cisco 8841", "SEP001122334455") == "TIER_1_MPP_DIRECT"
    assert CUCMAuditService._classify_device_tier("Cisco ATA 191", "ATA001122334455") == "TIER_2_GATEWAY_SOFTPHONE"


def test_enable_webex_calling_replaces_ucm_calling_license_and_preserves_other_attrs():
    provider = WebexCallingProvider(access_token="test_token")
    org_licenses = [
        {"id": "lic_ucm_calling", "name": "Calling in Webex (Unified CM)", "licenseId": "C_UCM_CALL"},
        {"id": "lic_reg_ucm", "name": "Register to Unified Communication Manager (UCM)"},
        {"id": "lic_meetings", "name": "Webex Meetings", "licenseId": "M_MEET"},
        {"id": "lic_wxc_pro", "name": "Webex Calling - Professional", "licenseId": "WXC_PRO"},
    ]
    user_record = {
        "id": "person_soft_1",
        "firstName": "Jane",
        "lastName": "Softphone",
        "displayName": "Jane Softphone",
        "emails": ["jsoft@example.com"],
        "licenses": ["lic_ucm_calling", "lic_reg_ucm", "lic_meetings"],
        "locationId": "loc_hq",
        "roles": ["role_admin"],
        "siteAdmin": True,
        "orgId": "org_123",
    }

    with patch.object(provider, "get_licenses", return_value=org_licenses), \
         patch.object(provider, "_request") as mock_req:
        def fake_request(method, endpoint, **kwargs):
            if method == "GET":
                return user_record
            if method == "PUT":
                return {"id": "person_soft_1", **kwargs["json"]}
            return {}

        mock_req.side_effect = fake_request

        eligible = provider.get_user_calling_licenses()
        assert [lic["id"] for lic in eligible] == ["lic_wxc_pro"]

        provider.enable_webex_calling_for_person(
            person_id="person_soft_1",
            location_id="loc_hq",
            calling_license_id="lic_wxc_pro",
            extension="4123",
            phone_number="+15125554123",
        )

        put_call = next(c for c in mock_req.call_args_list if c[0][0] == "PUT")
        assert put_call[0][1] == "/people/person_soft_1?callingData=true"
        payload = put_call[1]["json"]
        assert "lic_ucm_calling" not in payload["licenses"]
        assert "lic_reg_ucm" not in payload["licenses"]
        assert "lic_meetings" in payload["licenses"]
        assert "lic_wxc_pro" in payload["licenses"]
        assert payload["roles"] == ["role_admin"]
        assert payload["siteAdmin"] is True

        # Also verify stripping Register to UCM even if calling_license_id is omitted for an already-licensed WxC user
        user_record["licenses"] = ["lic_reg_ucm", "lic_wxc_pro", "lic_meetings"]
        mock_req.reset_mock()
        provider.enable_webex_calling_for_person(
            person_id="person_soft_1",
            location_id="loc_hq",
            calling_license_id="",
            extension="4123",
        )
        put_call2 = next(c for c in mock_req.call_args_list if c[0][0] == "PUT")
        assert "lic_reg_ucm" not in put_call2[1]["json"]["licenses"]
        assert "lic_wxc_pro" in put_call2[1]["json"]["licenses"]


def test_enable_webex_calling_dir_sync_keeps_extension_when_phone_numbers_rejected():
    provider = WebexCallingProvider(access_token="test_token")
    user_record = {
        "id": "person_dir_1",
        "firstName": "Tom",
        "lastName": "Mustwillo",
        "displayName": "Tom Mustwillo",
        "emails": ["tmustwillo@example.com"],
        "licenses": [],
    }
    sent_payloads = []

    def fake_request(method, endpoint, **kwargs):
        if method == "GET":
            return user_record
        if method == "PUT":
            body = dict(kwargs["json"])
            sent_payloads.append(body)
            if "phoneNumbers" in body:
                raise WebexAPIError("HTTP 400: Phone numbers are managed by Dir Sync", status_code=400)
            if "extension" not in body and "phoneNumbers" not in body:
                raise WebexAPIError("HTTP 400: Create Calling user either Phone number or Extension is required", status_code=400)
            return {"id": "person_dir_1", **body}
        return {}

    with patch.object(provider, "get_licenses", return_value=[]), \
         patch.object(provider, "_request", side_effect=fake_request):
        res = provider.enable_webex_calling_for_person(
            person_id="person_dir_1",
            location_id="loc_1",
            calling_license_id="lic_wxc_pro",
            extension="801682",
            phone_number="+15124391682",
        )

    assert len(sent_payloads) == 2
    assert "phoneNumbers" in sent_payloads[0] and "extension" in sent_payloads[0]
    assert "phoneNumbers" not in sent_payloads[1]
    assert sent_payloads[1]["extension"] == "801682"
    assert res["extension"] == "801682"


def test_enable_webex_calling_dir_sync_keeps_phone_when_extension_rejected():
    provider = WebexCallingProvider(access_token="test_token")
    user_record = {
        "id": "person_dir_2",
        "firstName": "Tom",
        "lastName": "Mustwillo",
        "emails": ["tmustwillo@example.com"],
        "licenses": [],
    }
    sent_payloads = []

    def fake_request(method, endpoint, **kwargs):
        if method == "GET":
            return user_record
        if method == "PUT":
            body = dict(kwargs["json"])
            sent_payloads.append(body)
            if "extension" in body:
                raise WebexAPIError("HTTP 400: Extension cannot be modified because Dir Sync is enabled", status_code=400)
            return {"id": "person_dir_2", **body}
        return {}

    with patch.object(provider, "get_licenses", return_value=[]), \
         patch.object(provider, "_request", side_effect=fake_request):
        res = provider.enable_webex_calling_for_person(
            person_id="person_dir_2",
            location_id="loc_1",
            calling_license_id="lic_wxc_pro",
            extension="801682",
            phone_number="+15124391682",
        )

    assert len(sent_payloads) == 2
    assert "extension" not in sent_payloads[1]
    assert sent_payloads[1]["phoneNumbers"] == [{"type": "work", "value": "+15124391682"}]
    assert res["phoneNumbers"] == [{"type": "work", "value": "+15124391682"}]


def test_enable_webex_calling_strips_dir_sync_manager_without_dropping_phone_or_extension():
    provider = WebexCallingProvider(access_token="test_token")
    ad_user = {
        "id": "person_mgr_1",
        "firstName": "Thomas",
        "lastName": "Mustwillo",
        "emails": ["tmustwillo@corp.internal"],
        "licenses": ["lic_ucm"],
        "manager": "Boss Name",
        "managerId": "mgr_123",
        "department": "IT",
        "phoneNumbers": [{"type": "work", "value": "+15124391682"}],
    }
    sent_payloads = []

    def fake_request(method, endpoint, **kwargs):
        if method == "GET":
            return ad_user
        if method == "PUT":
            body = dict(kwargs["json"])
            sent_payloads.append(body)
            if "manager" in body or "managerId" in body:
                raise WebexAPIError(
                    "HTTP 400: {\"message\":\"Failed to update people. Invalid Request. Manager is part of Dir Sync attributes and cannot be updated manually.\"}",
                    status_code=400,
                )
            if "department" in body:
                raise WebexAPIError(
                    "HTTP 400: {\"message\":\"Department is part of Dir Sync attributes and cannot be updated manually.\"}",
                    status_code=400,
                )
            return {"id": "person_mgr_1", **body}
        return {}

    with patch.object(provider, "get_licenses", return_value=[]), \
         patch.object(provider, "_request", side_effect=fake_request):
        res = provider.enable_webex_calling_for_person(
            person_id="person_mgr_1",
            location_id="loc_1",
            calling_license_id="lic_wxc_pro",
            extension="801682",
            phone_number="+15124391682",
        )

    assert len(sent_payloads) == 3
    assert "manager" not in sent_payloads[1] and "managerId" not in sent_payloads[1]
    assert "department" not in sent_payloads[2]
    assert sent_payloads[2]["phoneNumbers"] == [{"type": "work", "value": "+15124391682"}]
    assert sent_payloads[2]["extension"] == "801682"
    assert res["extension"] == "801682"


def test_enable_webex_calling_prioritizes_ad_synced_did_and_ext_over_step2_input():
    provider = WebexCallingProvider(access_token="test_token")
    ad_user = {
        "id": "person_ad_1",
        "firstName": "Tom",
        "lastName": "Mustwillo",
        "emails": ["tmustwillo@corp.internal"],
        "licenses": [],
        "phoneNumbers": [
            {"type": "work_extension", "value": "801682"},
            {"type": "work", "value": "+19015550100"},
        ],
        "extension": "9010",
    }
    sent_payloads = []

    def fake_request(method, endpoint, **kwargs):
        if method == "GET":
            return ad_user
        if method == "PUT":
            body = dict(kwargs["json"])
            sent_payloads.append(body)
            return {"id": "person_ad_1", **body}
        return {}

    with patch.object(provider, "get_licenses", return_value=[]), \
         patch.object(provider, "_request", side_effect=fake_request):
        res = provider.enable_webex_calling_for_person(
            person_id="person_ad_1",
            location_id="loc_1",
            calling_license_id="lic_wxc_pro",
            extension="801682",
            phone_number="+15124391682",
        )

    assert len(sent_payloads) == 1
    assert sent_payloads[0]["phoneNumbers"] == [
        {"type": "work_extension", "value": "801682"},
        {"type": "work", "value": "+19015550100"},
    ]
    assert sent_payloads[0]["extension"] == "9010"
    assert res["extension"] == "9010"


def test_user_profile_service_prioritizes_ad_synced_did_for_calling_and_redsky():
    mock_webex = MagicMock(spec=WebexCallingProvider)
    mock_webex.get_locations.return_value = [{"id": "loc_1", "name": "SIG_ThomasMustwillo"}]
    mock_webex.get_person_by_email.return_value = {
        "id": "person_ad_2",
        "displayName": "Thomas Mustwillo",
        "firstName": "Thomas",
        "lastName": "Mustwillo",
        "emails": ["tmustwillo@corp.internal"],
        "phoneNumbers": [{"type": "work", "value": "+19015550100"}],
    }
    mock_webex.provision_user_calling_profile.return_value = {"status": "SUCCESS"}

    mock_redsky = MagicMock()
    mock_redsky.is_configured.return_value = True
    mock_redsky.sync_device_user_mye911.return_value = {"action": "UPDATED"}

    service = WebexUserProfileService(mock_webex, redsky_provider=mock_redsky)
    items = [
        UserCallingProfileItem(
            email="tmustwillo@corp.internal",
            location_id="loc_1",
            phone_number="+15124391682",
            extension="801682",
            calling_license_id="lic_wxc_pro",
        )
    ]

    dry_res = service.provision_user_profiles(items, dry_run=True, sync_redsky=True)
    assert dry_res.results[0].phone_number == "+19015550100"
    assert "AD synced; priority over Step 2 '+15124391682'" in dry_res.results[0].details

    live_res = service.provision_user_profiles(items, dry_run=False, sync_redsky=True)
    assert live_res.successful_count == 1
    assert live_res.results[0].phone_number == "+19015550100"
    mock_webex.provision_user_calling_profile.assert_called_once_with(
        person_id="person_ad_2",
        location_id="loc_1",
        calling_license_id="lic_wxc_pro",
        extension="801682",
        phone_number="+19015550100",
        caller_id_type="DIRECT_LINE",
        caller_id_name=None,
        voicemail_enabled=True,
        voicemail_email_notification=True,
        voicemail_attach_audio=True,
        voicemail_pin=None,
    )
    mock_redsky.sync_device_user_mye911.assert_called_once_with(
        email="tmustwillo@corp.internal",
        phone_number="+19015550100",
        first_name="Thomas",
        last_name="Mustwillo",
        callback_number="+19015550100",
        enable_mye911=True,
    )


def test_audit_batch_provisions_softphone_users_without_mpp_device_and_deduplicates():
    provider = MagicMock(spec=WebexCallingProvider)
    provider.default_location_id = ""
    provider.calling_license_id = "lic_wxc_pro"
    provider.get_person_by_email.return_value = {
        "id": "person_soft_1",
        "displayName": "Jane Doe",
        "firstName": "Jane",
        "lastName": "Doe",
    }

    redsky = MagicMock()
    redsky.is_configured.return_value = True
    redsky.sync_device_user_mye911.return_value = {"action": "UPDATED"}

    service = WebexProvisionService(provider, redsky_provider=redsky)
    summary = service.process_cucm_audit_batch({
        "devices": [
            {
                "device_name": "CSFJDOE",
                "device_model": "Cisco Unified Client Services Framework",
                "migration_tier": "TIER_2_GATEWAY_SOFTPHONE",
                "owner_email": "jdoe@example.com",
                "target_location_id": "loc_1",
                "lines": [{"dn": "4101", "e164_suggested": "+15125554101"}],
            },
            {
                # Second softphone for same user in same batch
                "device_name": "BOTJDOE",
                "device_model": "Cisco Dual Mode for Android",
                "migration_tier": "TIER_2_GATEWAY_SOFTPHONE",
                "owner_email": "jdoe@example.com",
                "target_location_id": "loc_1",
                "lines": [{"dn": "4101", "e164_suggested": "+15125554101"}],
            },
            {
                # Analog gateway should still be skipped
                "device_name": "ATA001122334455",
                "device_model": "Cisco ATA 191",
                "migration_tier": "TIER_2_GATEWAY_SOFTPHONE",
                "owner_email": "lobby@example.com",
                "target_location_id": "loc_1",
                "lines": [{"dn": "4199", "e164_suggested": "+15125554199"}],
            },
        ]
    })

    assert summary.successful_count == 1
    assert summary.skipped_count == 2
    assert summary.failed_count == 0
    assert summary.items[0].status == "SUCCESS"
    assert "softphone-only user" in summary.items[0].details
    assert "already provisioned" in summary.items[1].details

    provider.enable_webex_calling_for_person.assert_called_once_with(
        person_id="person_soft_1",
        location_id="loc_1",
        calling_license_id="lic_wxc_pro",
        extension="4101",
        phone_number="+15125554101",
    )
    provider.provision_mpp_device.assert_not_called()
    provider.configure_caller_id.assert_called_once()
    provider.configure_voicemail.assert_called_once()
    redsky.sync_device_user_mye911.assert_called_once()


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_csv_staging_and_migration_provision_support_softphone_only_users(tmp_path: Path):
    _write_csv(
        tmp_path / "webex_locations_import.csv",
        ["Location Name", "Address Line 1", "City", "State", "ZIP", "Country", "Time Zone"],
        [{
            "Location Name": "Corporate HQ", "Address Line 1": "100 Main St",
            "City": "Austin", "State": "TX", "ZIP": "78701",
            "Country": "US", "Time Zone": "America/Chicago",
        }],
    )
    _write_csv(
        tmp_path / "webex_calling_user_import.csv",
        ["First Name", "Last Name", "Display Name", "Email", "Extension", "Phone Number", "Location", "Device Model", "MAC Address"],
        [
            {
                "First Name": "Jane", "Last Name": "Soft", "Display Name": "Jane Soft",
                "Email": "jsoft@example.com", "Extension": "5001", "Phone Number": "+15125555001",
                "Location": "Corporate HQ", "Device Model": "Webex App (Softphone)", "MAC Address": "",
            },
            {
                "First Name": "Bob", "Last Name": "Csf", "Display Name": "Bob Csf",
                "Email": "bcsf@example.com", "Extension": "5002", "Phone Number": "",
                "Location": "Corporate HQ", "Device Model": "", "MAC Address": "CSFBCSF",
            },
            {
                # Physical phone missing MAC must still fail validation
                "First Name": "Tim", "Last Name": "Hard", "Display Name": "Tim Hard",
                "Email": "thard@example.com", "Extension": "5003", "Phone Number": "",
                "Location": "Corporate HQ", "Device Model": "Cisco 8841", "MAC Address": "",
            },
        ],
    )
    _write_csv(
        tmp_path / "webex_workspaces_import.csv",
        ["Workspace Name", "Location Name", "Type", "Calling Behavior", "Extension", "Device Model", "MAC Address"],
        [],
    )

    manifest = {
        "cucm": {
            "devices": [{
                "device_name": "CSFJSOFT",
                "device_model": "Cisco Unified Client Services Framework",
                "owner_email": "jsoft@example.com",
            }]
        }
    }

    staged = MigrationStagingService().stage(manifest, csv_directory=tmp_path)
    assert staged.users[0].status == "READY"
    assert staged.users[0].source_match["device_name"] == "CSFJSOFT"
    assert staged.users[1].status == "READY"
    assert staged.users[1].values["Device Model"] == "Webex App (Softphone)"
    assert staged.users[2].status == "EXCEPTION"

    provider = MagicMock()
    provider.get_locations.return_value = [{"id": "loc1", "name": "Corporate HQ"}]
    provider.get_person_by_email.side_effect = lambda email: {"id": f"p_{email}"}

    provision = WebexMigrationProvisionService(provider).provision(
        staged.model_dump(), approved=True, dry_run=False, calling_license_id="lic_pro"
    )
    assert provision.successful_count == 2
    assert provision.skipped_count == 1
    assert "softphone-only" in provision.items[0].details
    provider.provision_mpp_device.assert_not_called()
    assert provider.enable_webex_calling_for_person.call_count == 2


def test_cucm_decommission_and_axl_support_softphone_device_names():
    assert CucmDecommissionService.normalize_device_name("csfjdoe") == ("CSFJDOE", "CSFJDOE")
    assert CucmDecommissionService.normalize_device_name("TCTjdoe") == ("TCTJDOE", "TCTJDOE")
    assert CucmDecommissionService.normalize_device_name("BOTjdoe") == ("BOTJDOE", "BOTJDOE")
    assert CucmDecommissionService.normalize_device_name("TABjdoe") == ("TABJDOE", "TABJDOE")
    assert CucmDecommissionService.normalize_device_name("001122334455") == ("SEP001122334455", "001122334455")

    with pytest.raises(ValueError, match="Invalid device MAC address"):
        CucmDecommissionService.normalize_device_name("0011223344")

    assert "Softphone" in CucmDecommissionService.determine_reclaimed_license("Cisco Unified Client Services Framework")
    assert "TelePresence Room" in CucmDecommissionService.determine_reclaimed_license("Cisco Webex Room Kit")

    axl = MagicMock(spec=CUCMAXLProvider)
    axl.get_route_pattern.return_value = None
    axl.add_route_pattern.return_value = {"uuid": "rp_1", "destination_type": "Route List"}
    axl.get_device_by_name_or_mac.return_value = {
        "pkid": "dev_csf_1",
        "device_name": "CSFJDOE",
        "model": "Cisco Unified Client Services Framework",
        "lines": [{"line_dn": "4199", "partition_name": "Internal_PT"}],
    }

    service = CucmDecommissionService(axl, cluster_name="PRIMARY")
    resp = service.decommission_devices(
        items=[
            CucmDeviceDecommissionItem(
                mac_address="CSFjdoe",
                extension="4199",
                partition="Internal_PT",
                gateway_route_list="RL_Local_Gateway",
                create_route_pattern=True,
                delete_unassigned_dn=True,
            )
        ],
        dry_run=False,
    )

    assert resp.successful_count == 1
    assert resp.results[0].device_name == "CSFJDOE"
    assert resp.results[0].device_deletion_status == "SUCCESS"
    assert "Softphone" in resp.results[0].license_reclaimed
    axl.remove_phone.assert_called_once_with("CSFJDOE")
    axl.remove_line.assert_called_once_with("4199", "Internal_PT")

    # Verify AXL provider does not rewrite CSF+12-hex into SEP+12-hex
    real_axl = CUCMAXLProvider(host="10.0.0.1", username="u", password="p")
    captured_sql = []
    real_axl.execute_sql_query = lambda q: captured_sql.append(q) or []
    real_axl.get_device_by_name_or_mac("CSF1234567890AB")
    assert "CSF1234567890AB" in captured_sql[0]
    assert "SEP1234567890AB" not in captured_sql[0]


def test_cucm_decommission_multi_device_and_named_jabber_order_before_route_pattern():
    # Custom named Jabber softphone without CSF prefix must be accepted
    assert CucmDecommissionService.normalize_device_name("ThomasJabber") == ("ThomasJabber", "ThomasJabber")

    call_log = []
    axl = MagicMock(spec=CUCMAXLProvider)
    axl.get_route_pattern.return_value = None

    def fake_get_dev(name):
        upper = name.upper()
        if upper == "BOTSTTM":
            return {
                "pkid": "pk_bot",
                "device_name": "BOTSTTM",
                "model": "Cisco Dual Mode for Android",
                "lines": [{"line_dn": "801682", "partition_name": "Global-PT-Internal"}],
            }
        if upper == "THOMASJABBER":
            return {
                "pkid": "pk_jab",
                "device_name": "ThomasJabber",
                "model": "Cisco Unified Client Services Framework",
                "lines": [{"line_dn": "801682", "partition_name": "Global-PT-Internal"}],
            }
        return None

    axl.get_device_by_name_or_mac.side_effect = fake_get_dev
    axl.remove_phone.side_effect = lambda name: call_log.append(("remove_phone", name)) or True
    axl.remove_line.side_effect = lambda dn, pt=None: call_log.append(("remove_line", dn, pt)) or True
    axl.add_route_pattern.side_effect = lambda **kw: call_log.append(("add_route_pattern", kw["pattern"], kw["partition"])) or {
        "uuid": "rp_801682",
        "destination_type": "Route List",
    }

    service = CucmDecommissionService(axl, cluster_name="PRIMARY")
    resp = service.decommission_devices(
        items=[
            CucmDeviceDecommissionItem(
                mac_address="BOTSTTM",
                extension="801682",
                partition="Global-PT-Internal",
                gateway_route_list="WebexCUBE_RL",
                create_route_pattern=True,
                delete_unassigned_dn=True,
            ),
            CucmDeviceDecommissionItem(
                mac_address="ThomasJabber",
                extension="801682",
                partition="Global-PT-Internal",
                gateway_route_list="WebexCUBE_RL",
                create_route_pattern=True,
                delete_unassigned_dn=True,
            ),
        ],
        dry_run=False,
    )

    assert resp.successful_count == 2
    assert resp.failed_count == 0
    # Verify exact CUCM-safe order: delete both devices -> delete DN once -> inject route pattern once
    assert call_log == [
        ("remove_phone", "BOTSTTM"),
        ("remove_phone", "ThomasJabber"),
        ("remove_line", "801682", "Global-PT-Internal"),
        ("add_route_pattern", "801682", "Global-PT-Internal"),
    ]

    # Verify create_route_pattern names blocking devices when a DN still exists in numplan
    axl2 = MagicMock(spec=CUCMAXLProvider)
    axl2.get_route_pattern.return_value = None
    axl2.add_route_pattern.side_effect = AXLRequestError(
        "SOAP Fault on addRoutePattern: soapenv:Client: Cannot insert or update pattern. A DN exists with the same pattern and partition."
    )
    axl2.get_devices_by_extension.return_value = [
        {"device_name": "BOTSTTM", "model": "Cisco Dual Mode for Android"},
        {"device_name": "ThomasJabber", "model": "Cisco Unified Client Services Framework"},
    ]
    svc2 = CucmDecommissionService(axl2, cluster_name="PRIMARY")
    with pytest.raises(ValueError, match="BOTSTTM.*ThomasJabber"):
        svc2.create_route_pattern(
            extension="801682",
            partition="Global-PT-Internal",
            gateway_route_list="WebexCUBE_RL",
            dry_run=False,
        )


def test_post_cutover_verification_for_softphone_only_user():
    webex = MagicMock(spec=WebexCallingProvider)
    webex.get_person_by_email.return_value = {"id": "p_1", "emails": ["jdoe@example.com"]}
    webex.get_person_by_id.return_value = {
        "id": "p_1",
        "displayName": "Jane Doe",
        "extension": "4199",
        "locationId": "loc_1",
        "phoneNumbers": [{"type": "work", "value": "+15125554199", "primary": True}],
        "callingData": {"location": {"id": "loc_1"}},
    }
    webex.get_caller_id.return_value = {"selected": "DIRECT_LINE", "customExternalCallerIdName": "Jane Doe"}
    webex.get_location_details.return_value = {
        "id": "loc_1",
        "name": "Austin HQ",
        "address": {"address1": "100 Main", "city": "Austin", "state": "TX", "postalCode": "78701"},
    }
    webex.get_voicemail_settings.return_value = {"enabled": True, "messageStorage": {"mwi": {"enabled": True}}, "emailCopyOfMessage": True}

    axl = MagicMock(spec=CUCMAXLProvider)
    axl.get_route_pattern.return_value = {
        "pkid": "rp_1",
        "pattern": "4199",
        "partition_name": "Internal_PT",
        "destination_name": "RL_Local_Gateway",
        "destination_type": "Route List",
    }
    axl.get_device_by_name_or_mac.return_value = None

    redsky = MagicMock()
    redsky.is_configured.return_value = True
    redsky.get_device_user_by_email.return_value = {"phoneNumber": "+15125554199", "userOptions": "MYE911_ENABLED"}

    service = WebexVerificationService(webex_provider=webex, axl_provider=axl, redsky_provider=redsky)
    resp = service.verify_cutover(user_email="jdoe@example.com", mac_address="CSFjdoe", extension="4199")

    assert resp.readiness_status == "CUTOVER_VERIFIED"
    assert resp.device_name == "CSFJDOE"
    assert resp.checks_passed == 8
    assert resp.checks_failed == 0
    assert resp.checks_warning == 0
    axl.get_device_by_name_or_mac.assert_called_once_with("CSFJDOE")
