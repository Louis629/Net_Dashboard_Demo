from unittest.mock import MagicMock

from providers.webex_calling_provider import WebexCallingProvider
from services.webex_provision_service import WebexProvisionService


def test_cucm_endpoint_uses_staged_location_without_server_default():
    provider = MagicMock(spec=WebexCallingProvider)
    provider.default_location_id = ""
    provider.calling_license_id = "license_1"
    provider.get_person_by_email.return_value = {"id": "person_1"}
    service = WebexProvisionService(provider)

    result = service.process_cucm_audit_batch({
        "devices": [{
            "device_name": "SEP001122334455",
            "device_model": "Cisco 8841",
            "migration_tier": "TIER_1_MPP_DIRECT",
            "owner_email": "engineer@example.com",
            "target_location_id": "location_from_staging",
            "lines": [{"dn": "5001", "e164_suggested": "+18085550100"}],
        }]
    })

    assert result.successful_count == 1
    provider.enable_webex_calling_for_person.assert_called_once_with(
        person_id="person_1",
        location_id="location_from_staging",
        calling_license_id="license_1",
        extension="5001",
        phone_number="+18085550100",
    )
    provider.provision_mpp_device.assert_called_once_with(
        mac_address="001122334455",
        model="Cisco 8841",
        location_id="location_from_staging",
        person_id="person_1",
    )
