# services/appliance_swap_service.py
import logging
from typing import Dict, Any, Callable, Optional
from providers.meraki_provider import MerakiProvider

logger = logging.getLogger("Appliance_Swap_Service")


class ApplianceSwapService:
    """
    Business logic for safely replacing Meraki MX/Z security appliances.
    Prevents accidental configuration wipes by verifying device-to-network assignment
    before executing the Meraki Swap API pipeline.
    """

    def __init__(self, meraki_provider: MerakiProvider):
        self.meraki = meraki_provider

    def execute_appliance_swap(
        self,
        network_id: str,
        old_serial: str,
        new_serial: str,
        progress_callback: Optional[Callable[[int, str], None]] = None
    ) -> Dict[str, Any]:
        """
        Executes a 4-step appliance swap pipeline:
        1. Verifies that `old_serial` actually belongs to `network_id`.
        2. Claims `new_serial` into the Meraki Organization and Network.
        3. Invokes Meraki `swapNetworkAppliance` API to clone interface/DHCP/VPN settings.
        4. Removes the `old_serial` from the network.
        """
        def update_progress(pct: int, msg: str):
            logger.info(f"[Swap Pipeline {pct}%] {msg}")
            if progress_callback:
                progress_callback(pct, msg)

        update_progress(10, f"Verifying old serial '{old_serial}' in target network...")

        # STEP 1: Verify Old Device Assignment
        network_devices = self.meraki.dashboard.networks.getNetworkDevices(network_id)
        existing_serials = [d.get("serial", "").upper() for d in network_devices]

        if old_serial.upper() not in existing_serials:
            raise ValueError(
                f"Safety Violation: Old device '{old_serial}' is NOT currently assigned to network '{network_id}'. "
                f"Swap aborted to prevent misconfiguration."
            )

        # Retrieve network metadata for logging
        net_info = self.meraki.dashboard.networks.getNetwork(network_id)
        network_name = net_info.get("name", network_id)

        # STEP 2: Claim New Serial to Organization & Network
        update_progress(35, f"Claiming replacement serial '{new_serial}' into network '{network_name}'...")
        try:
            self.meraki.dashboard.networks.claimNetworkDevices(
                networkId=network_id,
                serials=[new_serial]
            )
        except Exception as claim_err:
            logger.warning(f"Organization claim note for '{new_serial}': {claim_err}")

        # STEP 3: Execute Meraki Appliance Swap API
        update_progress(65, f"Swapping configuration from '{old_serial}' to '{new_serial}'...")
        swap_response = self.meraki.dashboard.appliance.swapNetworkAppliance(
            networkId=network_id,
            oldSerial=old_serial,
            newSerial=new_serial
        )

        # STEP 4: Remove Old Device from Network
        update_progress(90, f"Removing old serial '{old_serial}' from network '{network_name}'...")
        try:
            self.meraki.dashboard.networks.removeNetworkDevices(
                networkId=network_id,
                serial=old_serial
            )
        except Exception as remove_err:
            logger.warning(f"Could not automatically unclaim old serial '{old_serial}': {remove_err}")

        update_progress(100, f"Appliance swap complete for network '{network_name}'.")

        return {
            "status": "SUCCESS",
            "network_id": network_id,
            "network_name": network_name,
            "old_serial": old_serial,
            "new_serial": new_serial,
            "details": swap_response
        }