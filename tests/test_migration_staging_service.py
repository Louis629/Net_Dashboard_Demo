from services.migration_staging_service import MigrationStagingService


def test_stages_vendor_csvs_and_flags_duplicates():
    manifest = {
        "cucm": {"devices": [{"device_name": "SEP10E37601381C", "owner_email": "user@example.com"}]}
    }
    result = MigrationStagingService().stage(manifest)

    assert len(result.workspaces) == 177
    assert result.locations[0].status == "READY"
    assert any(error.code == "DUPLICATE_WORKSPACE_EXTENSION" for error in result.exceptions)
    assert any(error.code == "SOURCE_DEVICE_NOT_FOUND" for error in result.exceptions)
    assert result.ready_rows < 179