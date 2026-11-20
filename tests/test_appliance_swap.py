# tests/test_appliance_swap.py
import pytest
from unittest.mock import MagicMock
from services.appliance_swap_service import ApplianceSwapService


@pytest.fixture
def mock_meraki_provider():
    """
    Constructs a mock MerakiProvider instance with mocked Dashboard API interfaces.
    """
    provider = MagicMock()
    dashboard = MagicMock()
    provider.dashboard = dashboard
    return provider


def test_swap_safety_check_blocks_invalid_serial(mock_meraki_provider):
    """
    SAFETY TEST: Verifies that if old_serial is NOT assigned to target network_id,
    the service aborts immediately with a ValueError before calling claim or swap APIs.
    """
    network_id = "N_1234567890"
    old_serial = "Q2XX-OLDD-1111"
    new_serial = "Q2YY-NEWW-2222"

    # Mock network device inventory WITHOUT the target old_serial
    mock_meraki_provider.dashboard.networks.getNetworkDevices.return_value = [
        {"serial": "Q2ZZ-OTHER-3333", "model": "MX68"}
    ]

    service = ApplianceSwapService(meraki_provider=mock_meraki_provider)

    # Execution must raise ValueError
    with pytest.raises(ValueError) as exc_info:
        service.execute_appliance_swap(network_id, old_serial, new_serial)

    assert "Safety Violation" in str(exc_info.value)

    # Verify no modification endpoints were invoked
    mock_meraki_provider.dashboard.networks.claimNetworkDevices.assert_not_called()
    mock_meraki_provider.dashboard.appliance.swapNetworkAppliance.assert_not_called()


def test_swap_execution_successful_pipeline(mock_meraki_provider):
    """
    SUCCESS TEST: Verifies full 4-step replacement pipeline:
    1. Verify old device presence
    2. Claim replacement hardware
    3. Invoke Meraki swapNetworkAppliance
    4. Remove old serial from network
    """
    network_id = "N_1234567890"
    old_serial = "Q2XX-OLDD-1111"
    new_serial = "Q2YY-NEWW-2222"

    # Mock network inventory containing old_serial
    mock_meraki_provider.dashboard.networks.getNetworkDevices.return_value = [
        {"serial": "Q2XX-OLDD-1111", "model": "MX68"}
    ]
    mock_meraki_provider.dashboard.networks.getNetwork.return_value = {
        "id": network_id,
        "name": "CPL_Austin_PSC"
    }
    mock_meraki_provider.dashboard.appliance.swapNetworkAppliance.return_value = {
        "success": True
    }

    progress_ticks = []
    def track_progress(pct, msg):
        progress_ticks.append((pct, msg))

    service = ApplianceSwapService(meraki_provider=mock_meraki_provider)
    results = service.execute_appliance_swap(
        network_id=network_id,
        old_serial=old_serial,
        new_serial=new_serial,
        progress_callback=track_progress
    )

    # Assert response payload integrity
    assert results["status"] == "SUCCESS"
    assert results["network_name"] == "CPL_Austin_PSC"
    assert results["old_serial"] == old_serial
    assert results["new_serial"] == new_serial

    # Assert API invocation sequence
    mock_meraki_provider.dashboard.networks.claimNetworkDevices.assert_called_once_with(
        networkId=network_id,
        serials=[new_serial]
    )
    mock_meraki_provider.dashboard.appliance.swapNetworkAppliance.assert_called_once_with(
        networkId=network_id,
        oldSerial=old_serial,
        newSerial=new_serial
    )
    mock_meraki_provider.dashboard.networks.removeNetworkDevices.assert_called_once_with(
        networkId=network_id,
        serial=old_serial
    )

    # Assert 100% progress callback completion
    assert progress_ticks[-1][0] == 100


def test_swap_handles_claim_warning_gracefully(mock_meraki_provider):
    """
    RESILIENCE TEST: Confirms that if claimNetworkDevices throws a minor warning
    (e.g., hardware already claimed at Organization scope), execution proceeds safely to swap settings.
    """
    network_id = "N_1234567890"
    old_serial = "Q2XX-OLDD-1111"
    new_serial = "Q2YY-NEWW-2222"

    mock_meraki_provider.dashboard.networks.getNetworkDevices.return_value = [
        {"serial": "Q2XX-OLDD-1111", "model": "MX68"}
    ]
    mock_meraki_provider.dashboard.networks.getNetwork.return_value = {
        "id": network_id,
        "name": "CPL_Austin_PSC"
    }
    
    # Simulate non-fatal claim warning from Meraki API
    mock_meraki_provider.dashboard.networks.claimNetworkDevices.side_effect = Exception("Device already claimed to org")

    service = ApplianceSwapService(meraki_provider=mock_meraki_provider)
    results = service.execute_appliance_swap(network_id, old_serial, new_serial)

    assert results["status"] == "SUCCESS"
    mock_meraki_provider.dashboard.appliance.swapNetworkAppliance.assert_called_once()