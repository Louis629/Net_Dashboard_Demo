from unittest.mock import MagicMock

from services.webex_migration_provision_service import WebexMigrationProvisionService


def _staging():
    return {
        "users": [{
            "source_file": "users.csv", "row_number": 2, "status": "READY",
            "values": {
                "Email": "user@example.com", "Extension": "5001",
                "Phone Number": "+15125555001", "MAC Address": "001122334455",
                "Device Model": "Cisco 7821", "Location": "Corporate HQ",
            },
        }],
        "workspaces": [{
            "source_file": "workspaces.csv", "row_number": 2, "status": "EXCEPTION",
            "values": {"Workspace Name": "Lab 1"},
        }],
    }


def test_requires_explicit_approval_before_changes():
    provider = MagicMock()
    summary = WebexMigrationProvisionService(provider).provision(_staging())

    assert summary.approved is False
    assert summary.skipped_count == 2
    provider.get_locations.assert_not_called()


def test_dry_run_reads_locations_but_does_not_mutate():
    provider = MagicMock()
    provider.get_locations.return_value = [{"id": "loc1", "name": "Corporate HQ"}]
    summary = WebexMigrationProvisionService(provider).provision(_staging(), approved=True, dry_run=True)

    assert summary.successful_count == 1
    assert summary.skipped_count == 1
    assert summary.items[0].status == "DRY_RUN"
    provider.get_person_by_email.assert_not_called()
    provider.provision_mpp_device.assert_not_called()


def test_approved_user_is_provisioned():
    provider = MagicMock()
    provider.get_locations.return_value = [{"id": "loc1", "name": "Corporate HQ"}]
    provider.get_person_by_email.return_value = {"id": "person1"}
    provider.provision_mpp_device.return_value = {"id": "device1"}
    summary = WebexMigrationProvisionService(provider).provision(
        _staging(), approved=True, dry_run=False, calling_license_id="license1"
    )

    assert summary.successful_count == 1
    provider.enable_webex_calling_for_person.assert_called_once()
    provider.provision_mpp_device.assert_called_once()