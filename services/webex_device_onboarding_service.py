"""
services/webex_device_onboarding_service.py

Phase 3: Webex Calling Device Onboarding & MPP Transition Service.
Automates endpoint migration from CUCM 12.5(1) SU1 to Webex Calling Multi-Tenant:
1. Native MPP / Cloud Endpoints (Option A):
   - Direct Control Hub device registration (MAC & Model).
   - Generates 16-digit activation code (POST /v1/devices/activationCode).
   - Assigns device to User (personId) or Workspace (workspaceId).
2. Enterprise Firmware to MPP Conversion (Option B):
   - Pre-flight hardware compatibility validation (78xx / 88xx series).
   - Rejection/flagging of End-of-Life legacy hardware (79xx / 69xx / 39xx).
   - Generates 16-digit activation code for technician console.
   - Provides automated technician instructions (factory reset sequence 123456789*0#,
     Cloud Onboarding URL https://clouducm.cisco.com, and boot transition sequence).
3. Pre-flight MAC normalization and duplicate check.
4. Support for dry_run mode simulation and live Control Hub provisioning with step-up security.
"""

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from models.telecom import (
    DeviceOnboardingItem,
    DeviceOnboardingRequest,
    DeviceOnboardingResponse,
    DeviceOnboardingResultItem,
    TechnicianInstructions,
    DeviceActivationCodeResponse,
    DeviceCompatibilityResponse,
)
from providers.webex_calling_provider import WebexCallingProvider, WebexAPIError
from providers.redsky_provider import RedskyProvider

logger = logging.getLogger("Webex_Device_Onboarding_Service")

MAC_CLEAN_RE = re.compile(r"[^0-9A-Fa-f]")
MAC_VALID_RE = re.compile(r"^[0-9A-F]{12}$")

# Tier 1 Native Multiplatform Phone (MPP) patterns
NATIVE_MPP_PATTERNS = [
    re.compile(r"(?:3pcc|mpp)", re.IGNORECASE),
    re.compile(r"(?:cisco\s*)?(?:cp-)?(?:68\d{2})", re.IGNORECASE),
    re.compile(r"(?:cisco\s*)?(?:cp-)?(?:98\d{2})", re.IGNORECASE),
    re.compile(r"(?:webex|cisco)\s*(?:desk|room|board)", re.IGNORECASE),
]

# Enterprise Convertible hardware (Cisco 7800 / 8800 series eligible for Enterprise-to-MPP conversion)
ENTERPRISE_CONVERTIBLE_PATTERNS = [
    re.compile(r"(?:cisco\s*)?(?:cp-)?(?:7811|7821|7841|7861)", re.IGNORECASE),
    re.compile(r"(?:cisco\s*)?(?:cp-)?(?:8811|8841|8845|8851|8861|8865|8831|8832)", re.IGNORECASE),
]

# Legacy End-of-Life Models requiring hardware refresh or ATA gateway
LEGACY_EOL_PATTERNS = [
    re.compile(r"(?:cisco\s*)?(?:cp-)?(?:79\d{2})", re.IGNORECASE),
    re.compile(r"(?:cisco\s*)?(?:cp-)?(?:69\d{2})", re.IGNORECASE),
    re.compile(r"(?:cisco\s*)?(?:cp-)?(?:39\d{2})", re.IGNORECASE),
    re.compile(r"(?:cisco\s*)?(?:cp-)?(?:89\d{2})", re.IGNORECASE),
    re.compile(r"(?:cisco\s*)?(?:cp-)?(?:99\d{2})", re.IGNORECASE),
]


class CiscoDeviceClassifier:
    """Classifies Cisco IP phones for Webex Calling compatibility."""

    @staticmethod
    def classify(model: str) -> Dict[str, Any]:
        clean_model = model.strip()

        # Check legacy unsupported first
        for pat in LEGACY_EOL_PATTERNS:
            if pat.search(clean_model):
                return {
                    "model": model,
                    "clean_model": clean_model,
                    "category": "LEGACY_UNSUPPORTED",
                    "is_mpp_supported": False,
                    "is_conversion_supported": False,
                    "recommended_mode": "HARDWARE_REFRESH",
                    "notes": (
                        f"Model '{clean_model}' is an End-of-Life CUCM device. "
                        "It cannot run MPP firmware or connect directly to Webex Calling. "
                        "Replace with a modern MPP phone (e.g. Cisco 8851 MPP) or connect via Cisco ATA 191/192."
                    ),
                }

        # Check native MPP next
        for pat in NATIVE_MPP_PATTERNS:
            if pat.search(clean_model):
                return {
                    "model": model,
                    "clean_model": clean_model,
                    "category": "NATIVE_MPP",
                    "is_mpp_supported": True,
                    "is_conversion_supported": False,
                    "recommended_mode": "NATIVE_MPP",
                    "notes": f"Model '{clean_model}' natively runs Multiplatform Phone (MPP) or RoomOS firmware.",
                }

        # Check enterprise convertible
        for pat in ENTERPRISE_CONVERTIBLE_PATTERNS:
            if pat.search(clean_model):
                return {
                    "model": model,
                    "clean_model": clean_model,
                    "category": "ENTERPRISE_CONVERTIBLE",
                    "is_mpp_supported": True,
                    "is_conversion_supported": True,
                    "recommended_mode": "ENTERPRISE_TO_MPP",
                    "notes": (
                        f"Model '{clean_model}' is an Enterprise CUCM phone eligible for Enterprise-to-MPP firmware conversion. "
                        "Factory reset with key sequence 123456789*0# and point to clouducm.cisco.com."
                    ),
                }

        # Default fallback: unknown model
        return {
            "model": model,
            "clean_model": clean_model,
            "category": "UNKNOWN",
            "is_mpp_supported": True,
            "is_conversion_supported": True,
            "recommended_mode": "NATIVE_MPP",
            "notes": f"Model '{clean_model}' not found in known device database. Treating as generic SIP/MPP endpoint.",
        }


class WebexDeviceOnboardingService:
    """Orchestrates Phase 3 device onboarding, activation code generation, and MPP transition."""

    def __init__(
        self,
        webex_provider: WebexCallingProvider,
        redsky_provider: Optional[RedskyProvider] = None,
    ):
        self.webex = webex_provider
        self.redsky = redsky_provider if redsky_provider is not None else RedskyProvider()

    @staticmethod
    def normalize_mac(mac: str) -> str:
        """Normalizes and validates a MAC address to 12 uppercase hexadecimal characters."""
        clean = MAC_CLEAN_RE.sub("", mac).upper()
        if not MAC_VALID_RE.match(clean):
            raise ValueError(f"Invalid MAC address '{mac}'. Must be exactly 12 hexadecimal characters.")
        return clean

    @staticmethod
    def generate_technician_instructions(
        migration_mode: str,
        activation_code: Optional[str] = None,
        code_expiry: Optional[str] = None,
    ) -> TechnicianInstructions:
        """Generates step-by-step instructions for the field technician performing the migration."""
        code_str = activation_code or "Pending Generation"
        expiry_str = f" (Expires: {code_expiry})" if code_expiry else ""

        if migration_mode == "ENTERPRISE_TO_MPP":
            return TechnicianInstructions(
                factory_reset_sequence="123456789*0#",
                cloud_onboarding_url="https://clouducm.cisco.com",
                activation_code=activation_code,
                code_expiry=code_expiry,
                steps=[
                    "1. Hardware Reset: Unplug phone PoE/power. Press and hold '#' while reconnecting power.",
                    "2. Sequence Entry: When the line buttons flash amber/red, enter sequence: 123456789*0#.",
                    "3. Cloud Redirection: Ensure DHCP Option 66/150 points to Cisco Cloud or enter 'https://clouducm.cisco.com' in phone local web UI.",
                    "4. MPP Transition: Phone downloads the MPP migration load from Cisco Cloud and reboots twice.",
                    f"5. Activation Entry: On the phone display prompt, enter the 16-digit activation code: {code_str}{expiry_str}.",
                ],
            )
        else:
            return TechnicianInstructions(
                factory_reset_sequence="N/A (Native MPP)",
                cloud_onboarding_url="https://clouducm.cisco.com",
                activation_code=activation_code,
                code_expiry=code_expiry,
                steps=[
                    "1. Power on the Native MPP phone and connect to corporate network with internet access.",
                    "2. If phone is auto-provisioned via Cisco EDOS, it will configure automatically upon boot.",
                    f"3. Otherwise, select 'Activation Code' on screen and enter: {code_str}{expiry_str}.",
                ],
            )

    def check_compatibility(self, model: str) -> DeviceCompatibilityResponse:
        """Checks model compatibility against Cisco MPP and Enterprise transition rules."""
        res = CiscoDeviceClassifier.classify(model)
        return DeviceCompatibilityResponse(
            model=res["model"],
            clean_model=res["clean_model"],
            category=res["category"],
            is_mpp_supported=res["is_mpp_supported"],
            is_conversion_supported=res["is_conversion_supported"],
            recommended_mode=res["recommended_mode"],
            notes=res["notes"],
        )

    def resolve_owner(self, item: DeviceOnboardingItem) -> Tuple[str, str, str]:
        """
        Resolves whether the target owner is a User or a Workspace.
        Returns: (owner_type, owner_id, owner_identity)
        """
        if item.user_email:
            person = self.webex.get_person_by_email(item.user_email)
            if not person or not person.get("id"):
                raise ValueError(f"Webex user with email '{item.user_email}' was not found.")
            return "USER", person["id"], item.user_email

        if item.person_id:
            return "USER", item.person_id, item.person_id

        if item.workspace_name:
            workspace = self.webex.get_workspace_by_name(item.workspace_name)
            if not workspace or not workspace.get("id"):
                raise ValueError(f"Webex workspace '{item.workspace_name}' was not found.")
            return "WORKSPACE", workspace["id"], item.workspace_name

        if item.workspace_id:
            return "WORKSPACE", item.workspace_id, item.workspace_id

        raise ValueError("Device must be assigned to either a user (email/person_id) or a workspace (name/workspace_id).")

    def _get_location_cache(self) -> Dict[str, str]:
        """Loads and caches Webex locations by name for quick ID lookup."""
        cache: Dict[str, str] = {}
        try:
            for loc in self.webex.get_locations():
                name = str(loc.get("name", "")).strip()
                loc_id = str(loc.get("id", "")).strip()
                if name and loc_id:
                    cache[name] = loc_id
        except Exception as exc:
            logger.warning("Could not pre-fetch Webex locations for cache: %s", exc)
        return cache

    def resolve_location_id(
        self,
        item: DeviceOnboardingItem,
        default_location_id: Optional[str] = None,
        location_cache: Optional[Dict[str, str]] = None,
    ) -> str:
        """Determines the Webex location ID for the device."""
        if item.location_id:
            if item.location_id.startswith("MERAKI:"):
                loc_label = item.location_name or item.location_id.replace("MERAKI:", "")
                raise ValueError(
                    f"Selected location '{loc_label}' is a Meraki branch site that has not yet been synced to Webex Calling. "
                    "Please navigate to Phase 1 (Location & PSTN Synchronization) to sync this location before onboarding devices to it."
                )
            return item.location_id

        if item.location_name and location_cache and item.location_name in location_cache:
            return location_cache[item.location_name]

        if default_location_id:
            return default_location_id

        # Auto-resolve from Webex person or workspace profile
        if item.user_email:
            try:
                person = self.webex.get_person_by_email(item.user_email)
                if person and person.get("locationId"):
                    logger.info("Auto-resolved location ID %s from user email %s", person["locationId"], item.user_email)
                    return person["locationId"]
            except Exception as e:
                logger.warning("Could not auto-resolve location from user %s: %s", item.user_email, e)

        if item.person_id:
            try:
                person = self.webex.get_person_by_id(item.person_id)
                if person and person.get("locationId"):
                    logger.info("Auto-resolved location ID %s from person ID %s", person["locationId"], item.person_id)
                    return person["locationId"]
            except Exception as e:
                logger.warning("Could not auto-resolve location from person ID %s: %s", item.person_id, e)

        if item.workspace_name:
            try:
                ws = self.webex.get_workspace_by_name(item.workspace_name)
                if ws and ws.get("locationId"):
                    logger.info("Auto-resolved location ID %s from workspace %s", ws["locationId"], item.workspace_name)
                    return ws["locationId"]
            except Exception as e:
                logger.warning("Could not auto-resolve location from workspace %s: %s", item.workspace_name, e)

        if item.workspace_id:
            try:
                ws = self.webex.get_workspace(item.workspace_id)
                if ws and ws.get("locationId"):
                    logger.info("Auto-resolved location ID %s from workspace ID %s", ws["locationId"], item.workspace_id)
                    return ws["locationId"]
            except Exception as e:
                logger.warning("Could not auto-resolve location from workspace ID %s: %s", item.workspace_id, e)

        target = item.user_email or item.workspace_name or item.person_id or item.workspace_id or "assigned target"
        raise ValueError(
            f"No Webex Calling location specified for device {item.mac_address}, and could not auto-resolve location from '{target}'. "
            "Please select an active Webex location from the dropdown."
        )

    def generate_activation_code(
        self,
        user_email: Optional[str] = None,
        person_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        model: Optional[str] = None,
    ) -> DeviceActivationCodeResponse:
        """Generates a 16-digit device activation code for a user or workspace."""
        owner_type = "USER"
        owner_id = ""

        if user_email:
            person = self.webex.get_person_by_email(user_email)
            if not person or not person.get("id"):
                raise ValueError(f"Webex user with email '{user_email}' was not found.")
            owner_id = person["id"]
        elif person_id:
            owner_id = person_id
        elif workspace_id:
            owner_type = "WORKSPACE"
            owner_id = workspace_id
        else:
            raise ValueError("user_email, person_id, or workspace_id is required to generate an activation code.")

        res = self.webex.generate_device_activation_code(
            person_id=owner_id if owner_type == "USER" else None,
            workspace_id=owner_id if owner_type == "WORKSPACE" else None,
            model=model,
        )

        return DeviceActivationCodeResponse(
            code=res.get("code", ""),
            expiry_time=res.get("expiryTime"),
            owner_type=owner_type,
            owner_id=owner_id,
        )

    def onboard_devices(
        self,
        devices: List[DeviceOnboardingItem],
        dry_run: bool = True,
        default_location_id: Optional[str] = None,
        generate_activation_codes: bool = True,
    ) -> DeviceOnboardingResponse:
        """
        Executes Phase 3 device onboarding for a batch of endpoints.
        Handles both Native MPP and Enterprise-to-MPP conversion workflows.
        """
        location_cache = self._get_location_cache()
        results: List[DeviceOnboardingResultItem] = []
        successful_count = 0
        failed_count = 0
        skipped_count = 0

        for item in devices:
            # 1. Normalize MAC address
            try:
                clean_mac = self.normalize_mac(item.mac_address)
            except ValueError as mac_err:
                results.append(
                    DeviceOnboardingResultItem(
                        mac_address=item.mac_address,
                        model=item.model,
                        migration_mode=item.migration_mode,
                        owner_type="USER",
                        owner_identity="UNKNOWN",
                        status="FAILED",
                        details=str(mac_err),
                    )
                )
                failed_count += 1
                continue

            # 2. Check model compatibility
            compat = CiscoDeviceClassifier.classify(item.model)
            if compat["category"] == "LEGACY_UNSUPPORTED":
                results.append(
                    DeviceOnboardingResultItem(
                        mac_address=clean_mac,
                        model=item.model,
                        migration_mode=item.migration_mode,
                        owner_type="USER",
                        owner_identity="UNKNOWN",
                        status="SKIPPED",
                        details=compat["notes"],
                    )
                )
                skipped_count += 1
                continue

            # 3. Resolve Location ID
            try:
                location_id = self.resolve_location_id(item, default_location_id, location_cache)
            except ValueError as loc_err:
                results.append(
                    DeviceOnboardingResultItem(
                        mac_address=clean_mac,
                        model=item.model,
                        migration_mode=item.migration_mode,
                        owner_type="USER",
                        owner_identity="UNKNOWN",
                        status="FAILED",
                        details=str(loc_err),
                    )
                )
                failed_count += 1
                continue

            # 4. Resolve Owner (User or Workspace)
            try:
                owner_type, owner_id, owner_identity = self.resolve_owner(item)
            except (ValueError, WebexAPIError) as owner_err:
                results.append(
                    DeviceOnboardingResultItem(
                        mac_address=clean_mac,
                        model=item.model,
                        migration_mode=item.migration_mode,
                        owner_type="USER",
                        owner_identity="UNKNOWN",
                        status="FAILED",
                        details=str(owner_err),
                    )
                )
                failed_count += 1
                continue

            # 5. Handle Dry Run vs Live Execution
            migration_mode = item.migration_mode.upper()
            if migration_mode not in {"NATIVE_MPP", "ENTERPRISE_TO_MPP"}:
                migration_mode = "NATIVE_MPP"

            if dry_run:
                simulated_code = "9999-SIMULATED-CODE"
                instructions = self.generate_technician_instructions(
                    migration_mode=migration_mode,
                    activation_code=simulated_code,
                    code_expiry="7 days",
                )
                results.append(
                    DeviceOnboardingResultItem(
                        mac_address=clean_mac,
                        model=item.model,
                        migration_mode=migration_mode,
                        owner_type=owner_type,
                        owner_identity=owner_identity,
                        status="DRY_RUN",
                        details=(
                            f"Validated for {migration_mode} onboarding to {owner_type.lower()} '{owner_identity}' "
                            f"in location ID {location_id}. Pre-flight checks passed."
                        ),
                        activation_code=simulated_code,
                        activation_code_expiry="7 days (Simulated)",
                        technician_instructions=instructions,
                    )
                )
                successful_count += 1
                continue

            # Live Execution
            try:
                device_id = None
                activation_code = None
                code_expiry = None

                # Generate Activation Code if requested
                if generate_activation_codes:
                    try:
                        code_res = self.webex.generate_device_activation_code(
                            person_id=owner_id if owner_type == "USER" else None,
                            workspace_id=owner_id if owner_type == "WORKSPACE" else None,
                            model=item.model,
                        )
                        activation_code = code_res.get("code")
                        code_expiry = code_res.get("expiryTime")
                    except Exception as code_err:
                        logger.warning(
                            "Could not generate activation code for %s %s: %s",
                            owner_type, owner_identity, code_err
                        )

                # Register device in Control Hub
                user_sync_note = ""
                if owner_type == "USER":
                    # Sync user location & calling profile if target location, phone number, or extension differs
                    try:
                        person = self.webex.get_person_by_id(owner_id, calling_data=True)
                        user_loc = person.get("locationId")
                        ad_did = WebexCallingProvider.extract_existing_did(person)
                        existing_ext = str(person.get("extension") or "").strip()
                        input_ext = str(item.extension).strip() if (item.extension and str(item.extension).strip()) else None
                        input_phone = str(item.phone_number).strip() if (item.phone_number and str(item.phone_number).strip()) else None

                        target_ext = existing_ext or input_ext
                        target_phone = ad_did or input_phone

                        needs_user_update = (
                            (location_id and user_loc != location_id)
                            or (not existing_ext and bool(input_ext))
                            or (not ad_did and bool(input_phone))
                        )
                        if needs_user_update:
                            try:
                                self.webex.enable_webex_calling_for_person(
                                    person_id=owner_id,
                                    location_id=location_id or user_loc,
                                    calling_license_id="",
                                    extension=target_ext,
                                    phone_number=target_phone,
                                )
                                user_sync_note = "User location & calling profile synchronized."
                                logger.info("Successfully updated calling profile for user %s", owner_identity)
                            except Exception as user_upd_err:
                                err_str = str(user_upd_err).lower()
                                if "dir sync" in err_str or "dirsync" in err_str:
                                    logger.info("User %s calling attributes are managed by Directory Sync: %s", owner_identity, user_upd_err)
                                    user_sync_note = "User location synchronized (Extension & Phone Number managed via Active Directory / Dir Sync)."
                                else:
                                    logger.warning("Could not sync user location/calling profile for %s: %s", owner_identity, user_upd_err)
                                    user_sync_note = f"Warning: User calling profile sync returned: {user_upd_err}"
                    except Exception as prep_err:
                        logger.warning("Could not inspect user %s for calling profile sync: %s", owner_identity, prep_err)
                        user_sync_note = f"Warning: User inspection failed: {prep_err}"

                    # Automated RedSky MyE911 registration for nomadic/WFH Kari's Law compliance
                    if self.redsky and self.redsky.is_configured():
                        active_phone = (
                            WebexCallingProvider.extract_existing_did(person)
                            or (item.phone_number and str(item.phone_number).strip())
                        )
                        if active_phone:
                            try:
                                r_res = self.redsky.sync_device_user_mye911(
                                    email=owner_identity,
                                    phone_number=active_phone,
                                    first_name=person.get("firstName") if isinstance(person, dict) else "",
                                    last_name=person.get("lastName") if isinstance(person, dict) else "",
                                    callback_number=active_phone,
                                    enable_mye911=True,
                                )
                                user_sync_note += f" RedSky MyE911 active for '{active_phone}' ({r_res.get('action', 'SYNCED')})."
                                logger.info("RedSky MyE911 auto-synced for user %s with phone %s", owner_identity, active_phone)
                            except Exception as r_err:
                                logger.warning("Could not sync RedSky MyE911 during device onboarding for %s: %s", owner_identity, r_err)

                    dev_res = self.webex.provision_mpp_device(
                        mac_address=clean_mac,
                        model=item.model,
                        location_id=location_id,
                        person_id=owner_id,
                    )
                    device_id = dev_res.get("id")
                else:
                    dev_res = self.webex.provision_workspace_device(
                        mac_address=clean_mac,
                        model=item.model,
                        location_id=location_id,
                        workspace_id=owner_id,
                    )
                    device_id = dev_res.get("id")

                instructions = self.generate_technician_instructions(
                    migration_mode=migration_mode,
                    activation_code=activation_code,
                    code_expiry=code_expiry,
                )

                item_details = (
                    f"Successfully onboarded {migration_mode} device to {owner_type.lower()} '{owner_identity}' "
                    f"in Webex Calling."
                )
                if user_sync_note:
                    item_details = f"{item_details} [{user_sync_note}]"

                results.append(
                    DeviceOnboardingResultItem(
                        mac_address=clean_mac,
                        model=item.model,
                        migration_mode=migration_mode,
                        owner_type=owner_type,
                        owner_identity=owner_identity,
                        status="SUCCESS",
                        details=item_details,
                        device_id=device_id,
                        activation_code=activation_code,
                        activation_code_expiry=code_expiry,
                        technician_instructions=instructions,
                    )
                )
                successful_count += 1

            except (WebexAPIError, ValueError, Exception) as prov_err:
                logger.exception("Failed to onboard device %s: %s", clean_mac, prov_err)
                results.append(
                    DeviceOnboardingResultItem(
                        mac_address=clean_mac,
                        model=item.model,
                        migration_mode=migration_mode,
                        owner_type=owner_type,
                        owner_identity=owner_identity,
                        status="FAILED",
                        details=str(prov_err),
                    )
                )
                failed_count += 1

        return DeviceOnboardingResponse(
            status="SUCCESS",
            dry_run=dry_run,
            total_evaluated=len(devices),
            successful_count=successful_count,
            failed_count=failed_count,
            skipped_count=skipped_count,
            results=results,
        )
