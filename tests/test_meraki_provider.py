from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from providers.meraki_provider import MerakiLookupError, MerakiProvider, TTLCache


def test_appliance_location_inventory_groups_mx_and_z4_networks():
    provider = MerakiProvider.__new__(MerakiProvider)
    provider.cache = TTLCache(ttl_seconds=300)
    provider.get_organization_devices = Mock(return_value=[
        {"model": "MX68", "networkId": "N1", "networkName": "Clinic", "serial": "MX-1"},
        {"model": "MX68", "networkId": "N1", "networkName": "Clinic", "serial": "MX-2"},
        {"model": "Z4C", "networkId": "N2", "networkName": "Remote", "serial": "Z4-1"},
        {"model": "MR46", "networkId": "N3", "networkName": "Wireless", "serial": "MR-1"},
    ])
    provider.dashboard = SimpleNamespace(networks=SimpleNamespace(
        getNetwork=Mock(side_effect=lambda network_id: {
            "N1": {"name": "Clinic", "address": "10 Main St, Town, ST 12345", "timeZone": "America/Chicago"},
            "N2": {"name": "Remote", "address": "20 Oak Rd, City, ST 54321", "timeZone": "America/New_York"},
        }[network_id])
    ))

    inventory = provider.get_appliance_location_inventory()

    assert [row["network_id"] for row in inventory] == ["N1", "N2"]
    assert inventory[0]["address"] == "10 Main St, Town, ST 12345"
    assert inventory[0]["time_zone"] == "America/Chicago"
    assert inventory[0]["serial_numbers"] == "MX-1, MX-2"
    assert inventory[1]["appliance_models"] == "Z4C"
    provider.dashboard.networks.getNetwork.assert_has_calls([call("N1"), call("N2")])


def test_network_details_retries_transient_service_unavailable(monkeypatch):
    class ServiceUnavailable(Exception):
        status = 503

    provider = MerakiProvider.__new__(MerakiProvider)
    provider.cache = TTLCache(ttl_seconds=300)
    get_network = Mock(side_effect=[ServiceUnavailable("503"), {"name": "Clinic"}])
    provider.dashboard = SimpleNamespace(networks=SimpleNamespace(getNetwork=get_network))
    monkeypatch.setattr("providers.meraki_provider.time.sleep", Mock())

    result = provider.get_network_details("N1")

    assert result == {"name": "Clinic"}
    assert get_network.call_count == 2


def test_network_details_stops_after_one_transient_retry(monkeypatch):
    class ServiceUnavailable(Exception):
        status = 503

    provider = MerakiProvider.__new__(MerakiProvider)
    provider.cache = TTLCache(ttl_seconds=300)
    get_network = Mock(side_effect=ServiceUnavailable("503"))
    provider.dashboard = SimpleNamespace(networks=SimpleNamespace(getNetwork=get_network))
    monkeypatch.setattr("providers.meraki_provider.time.sleep", Mock())

    with pytest.raises(MerakiLookupError):
        provider.get_network_details("N1")

    assert get_network.call_count == 2


def test_appliance_inventory_uses_ztp_device_address_and_counts_devices():
    provider = MerakiProvider.__new__(MerakiProvider)
    provider.cache = TTLCache(ttl_seconds=300)
    provider.get_organization_devices = Mock(return_value=[
        {
            "model": "MX68",
            "networkId": "N1",
            "networkName": "Clinic",
            "serial": "MX-1",
            "address": "2230 Liliha St, Honolulu, HI 96817",
        },
        {
            "model": "Z4",
            "networkId": "N1",
            "networkName": "Clinic",
            "serial": "Z4-1",
            "address": "2230 Liliha St, Honolulu, HI 96817",
        },
    ])
    provider.get_organization_networks = Mock(return_value=[{
        "id": "N1",
        "name": "Clinic",
        "timeZone": "Pacific/Honolulu",
    }])
    provider.dashboard = SimpleNamespace(networks=SimpleNamespace(getNetwork=Mock()))

    inventory = provider.get_appliance_location_inventory()

    assert len(inventory) == 1
    assert inventory[0]["address"] == "2230 Liliha St, Honolulu, HI 96817"
    assert inventory[0]["address_source"] == "device"
    assert inventory[0]["device_count"] == 2
    assert inventory[0]["address_conflict"] is False
    provider.get_organization_devices.assert_called_once_with(force_refresh=True)
    provider.dashboard.networks.getNetwork.assert_not_called()