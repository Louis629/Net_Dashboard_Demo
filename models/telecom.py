# models/telecom.py
from enum import Enum
from typing import Any, List, Optional
from pydantic import BaseModel, Field, field_validator


# -----------------------------------------------------------------------------
# Multi-Cluster Target Definitions
# -----------------------------------------------------------------------------

class ClusterTarget(str, Enum):
    PRIMARY = "PRIMARY"
    HAWAII = "HAWAII"


class ClusterAuditRequest(BaseModel):
    cluster: ClusterTarget = Field(
        default=ClusterTarget.PRIMARY,
        description="Target collaboration cluster environment"
    )
    device_scope: str = "PHYSICAL_ONLY"
    device_pool_filter: Optional[str] = "ALL"
    default_country_code: str = "+1"


class ClusterUccxAuditRequest(BaseModel):
    cluster: ClusterTarget = Field(
        default=ClusterTarget.PRIMARY,
        description="Target UCCX cluster"
    )
    default_country_code: str = "+1"


class ClusterPromptExportRequest(BaseModel):
    cluster: ClusterTarget = Field(
        default=ClusterTarget.PRIMARY,
        description="Target UCCX cluster for prompt repository export"
    )
    languages: List[str] = Field(default_factory=lambda: ["en_US", "es_US"])


# -----------------------------------------------------------------------------
# Webex Calling Staging & Batch Provisioning Models (Existing Pipeline)
# -----------------------------------------------------------------------------

class StagedEndpointRecord(BaseModel):
    device_name: str
    clean_mac: str
    device_model: str
    migration_tier: str  # TIER_1_MPP_DIRECT, TIER_2_GATEWAY, TIER_3_INCOMPATIBLE
    entity_type: str = "PEOPLE"  # PEOPLE or WORKSPACE
    owner_userid: Optional[str] = ""
    owner_email: Optional[str] = ""
    display_name: str
    extension: str
    e164_suggested: str
    target_location_id: str
    target_location_name: str
    calling_license_type: str = "PROFESSIONAL"  # PROFESSIONAL, STANDARD, or WORKSPACE
    voicemail_enabled: bool = True


class WebexBatchStagingPayload(BaseModel):
    dry_run_only: bool = False
    endpoints: List[StagedEndpointRecord]


# -----------------------------------------------------------------------------
# Phase 1: Meraki to Webex Calling Location & DID Provisioning Models
# -----------------------------------------------------------------------------

class MerakiLocationPreviewItem(BaseModel):
    network_id: str
    location_name: str
    source_address: str
    address_line_1: str
    city: str
    state: str
    zip_code: str
    country: str
    time_zone: str
    appliance_models: List[str] = Field(default_factory=list)
    serial_numbers: List[str] = Field(default_factory=list)
    device_count: int = 1
    exists_in_webex: bool = False
    webex_location_id: Optional[str] = None
    is_valid: bool = True
    missing_fields: List[str] = Field(default_factory=list)
    redsky_building_id: Optional[str] = None
    redsky_status: Optional[str] = None  # e.g. VALID, AMBIGUOUS, NOT_REGISTERED, UNCONFIGURED
    phone_subnets: List[str] = Field(default_factory=list)
    wiremap_strategy: Optional[str] = None  # DEDICATED_VOICE, SINGLE_DATA_VLAN, SINGLE_LAN, NO_SUBNETS_FOUND

    @field_validator("appliance_models", "serial_numbers", mode="before")
    @classmethod
    def _coerce_to_list(cls, v: Any) -> List[str]:
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        if isinstance(v, (list, tuple, set)):
            return [str(x).strip() for x in v if str(x).strip()]
        return []


class MerakiLocationPreviewResponse(BaseModel):
    status: str = "SUCCESS"
    total_meraki_networks: int
    existing_in_webex_count: int
    new_to_create_count: int
    invalid_address_count: int
    locations: List[MerakiLocationPreviewItem]


class MerakiLocationSyncRequest(BaseModel):
    network_ids: Optional[List[str]] = Field(
        default=None,
        description="List of Meraki network IDs to sync. If omitted or empty, all valid networks are synced."
    )
    dry_run: bool = True
    connection_type: str = Field(
        default="LOCAL_GATEWAY",
        description="Calling connection type: LOCAL_GATEWAY, CLOUD_CONNECTED_PSTN, or CISCO_CALLING_PLAN"
    )
    routing_group_id: Optional[str] = Field(
        default=None,
        description="Optional SIP trunk routing group ID when connection_type is LOCAL_GATEWAY"
    )
    internal_extension_length: int = Field(
        default=4,
        description="Internal extension digit length (typically 4, 5, or 6 to match CUCM)"
    )
    routing_prefix: Optional[str] = Field(
        default=None,
        description="Optional site steering / routing prefix"
    )
    sync_redsky: bool = Field(
        default=True,
        description="Whether to register and MSAG-validate the location in RedSky E911 Anywhere"
    )
    step_up_user: Optional[str] = None
    step_up_pass: Optional[str] = None


class MerakiLocationSyncItem(BaseModel):
    network_id: str
    location_name: str
    status: str  # CREATED, EXISTING, DRY_RUN, FAILED, SKIPPED
    webex_location_id: Optional[str] = None
    details: str
    wiremapped_subnets: List[str] = Field(default_factory=list)
    wiremap_strategy: Optional[str] = None  # DEDICATED_VOICE, SINGLE_DATA_VLAN, SINGLE_LAN, MANUAL


class RedskyWiremapRequest(BaseModel):
    network_id: Optional[str] = Field(
        default=None,
        description="Meraki network ID to discover subnets from"
    )
    location_name: Optional[str] = Field(
        default=None,
        description="Location/Building name in RedSky"
    )
    webex_location_id: Optional[str] = Field(
        default=None,
        description="Optional Webex location ID for cross-reference"
    )
    building_id: Optional[str] = Field(
        default=None,
        description="Optional RedSky Building ID if already known"
    )
    custom_subnets: Optional[List[str]] = Field(
        default=None,
        description="Optional manual subnet list in CIDR notation (e.g. ['10.236.4.0/24'])"
    )


class RedskyWiremapResponse(BaseModel):
    status: str = "SUCCESS"
    location_name: str
    building_id: Optional[str] = None
    strategy: str  # DEDICATED_VOICE, SINGLE_DATA_VLAN, SINGLE_LAN, MANUAL
    wiremapped_subnets: List[str] = Field(default_factory=list)
    details: str


class MerakiLocationSyncResponse(BaseModel):
    status: str = "SUCCESS"
    dry_run: bool
    total_evaluated: int
    created_count: int
    existing_count: int
    failed_count: int
    skipped_count: int
    results: List[MerakiLocationSyncItem]


class LocationDidProvisionRequest(BaseModel):
    phone_numbers: List[str] = Field(
        description="List of external PSTN DIDs in E.164 format (e.g. +15125550199)"
    )
    main_number: Optional[str] = Field(
        default=None,
        description="Optional primary pilot DID to designate as the Location Main Number"
    )
    dry_run: bool = True
    step_up_user: Optional[str] = None
    step_up_pass: Optional[str] = None


class LocationDidProvisionResponse(BaseModel):
    status: str = "SUCCESS"
    dry_run: bool
    location_id: str
    location_name: Optional[str] = None
    phone_numbers_added: List[str] = Field(default_factory=list)
    main_number_set: Optional[str] = None
    details: str


# -----------------------------------------------------------------------------
# Phase 2: User Entitlement & Calling Profile Provisioning Models
# -----------------------------------------------------------------------------

class UserCallingProfileItem(BaseModel):
    email: str = Field(description="User primary email address registered in Control Hub")
    userid: Optional[str] = Field(default=None, description="Optional CUCM user ID / UPN fallback for identity resolution")
    location_id: str = Field(description="Target Webex Calling location ID")
    extension: Optional[str] = Field(default=None, description="Optional internal extension digits (e.g. 4199) - omit if managed by Dir Sync")
    phone_number: Optional[str] = Field(default=None, description="External E.164 direct DID (e.g. +15125550199)")
    calling_license_id: Optional[str] = Field(default=None, description="Webex Calling Professional license ID")
    caller_id_type: str = Field(default="DIRECT_LINE", description="DIRECT_LINE or LOCATION_NUMBER")
    caller_id_name: Optional[str] = Field(default=None, description="Custom external caller ID name (e.g. Department / Company)")
    voicemail_enabled: bool = Field(default=True, description="Enable Webex Cloud Voicemail")
    voicemail_email_notification: bool = Field(default=True, description="Send voicemail notifications via email")
    voicemail_attach_audio: bool = Field(default=True, description="Attach .wav audio recording to voicemail email notification")
    voicemail_pin: Optional[str] = Field(default=None, description="Optional default voicemail PIN passcode")


class UserCallingProfileProvisionRequest(BaseModel):
    users: List[UserCallingProfileItem] = Field(description="List of users to configure calling profiles for")
    dry_run: bool = True
    calling_license_id: Optional[str] = Field(default=None, description="Default Webex Calling license ID applied if not specified per item")
    default_location_id: Optional[str] = Field(default=None, description="Default Location ID applied if not specified per item")
    sync_redsky: bool = Field(default=True, description="Automatically sync emergency dispatch & MyE911 profile to RedSky")
    step_up_user: Optional[str] = None
    step_up_pass: Optional[str] = None


class UserCallingProfileResultItem(BaseModel):
    email: str
    display_name: Optional[str] = None
    person_id: Optional[str] = None
    location_id: Optional[str] = None
    extension: Optional[str] = None
    phone_number: Optional[str] = None
    status: str  # SUCCESS, DRY_RUN, FAILED, SKIPPED
    caller_id_status: Optional[str] = None
    voicemail_status: Optional[str] = None
    redsky_mye911_status: Optional[str] = None
    details: str


class UserCallingProfileProvisionResponse(BaseModel):
    status: str = "SUCCESS"
    dry_run: bool
    total_evaluated: int
    successful_count: int
    failed_count: int
    skipped_count: int
    results: List[UserCallingProfileResultItem]


class RedskyMyE911SyncRequest(BaseModel):
    phone_number: Optional[str] = Field(default=None, description="External DID to assign to MyE911 (auto-detected from Webex if omitted)")
    callback_number: Optional[str] = Field(default=None, description="Emergency callback number (defaults to phone_number)")
    enable_mye911: bool = Field(default=True, description="Enable MyE911 functionality")
    first_name: Optional[str] = None
    last_name: Optional[str] = None


class RedskyMyE911SyncResponse(BaseModel):
    status: str = "SUCCESS"
    action: str  # UPDATED, CREATED, SKIPPED, FAILED
    email: str
    phone_number: Optional[str] = None
    callback_number: Optional[str] = None
    enable_mye911: bool
    details: str



# -----------------------------------------------------------------------------
# Phase 3: Device Onboarding & MPP Transition Models
# -----------------------------------------------------------------------------

class TechnicianInstructions(BaseModel):
    factory_reset_sequence: str = "123456789*0#"
    cloud_onboarding_url: str = "https://clouducm.cisco.com"
    activation_code: Optional[str] = None
    code_expiry: Optional[str] = None
    steps: List[str] = Field(default_factory=list)


class DeviceOnboardingItem(BaseModel):
    mac_address: str = Field(description="12-digit hexadecimal MAC address of the physical IP phone")
    model: str = Field(description="Cisco Phone model, e.g., 'Cisco 8851', 'CP-8851-3PCC', 'CP-7841'")
    migration_mode: str = Field(
        default="NATIVE_MPP",
        description="Migration mode: 'NATIVE_MPP' or 'ENTERPRISE_TO_MPP'"
    )
    location_id: Optional[str] = Field(default=None, description="Webex location ID where device will reside")
    location_name: Optional[str] = Field(default=None, description="Webex location name for lookup if ID not provided")
    user_email: Optional[str] = Field(default=None, description="Email of the assigned user (for single-user device)")
    person_id: Optional[str] = Field(default=None, description="Webex personId if known")
    workspace_name: Optional[str] = Field(default=None, description="Workspace display name if shared/common area device")
    workspace_id: Optional[str] = Field(default=None, description="Webex workspaceId if known")
    phone_number: Optional[str] = Field(default=None, description="Primary direct phone number in E.164 format (e.g. +15124391662)")
    extension: Optional[str] = Field(default=None, description="Internal extension number (e.g. 801662)")


class DeviceOnboardingRequest(BaseModel):
    devices: List[DeviceOnboardingItem] = Field(description="List of devices to onboard")
    dry_run: bool = True
    default_location_id: Optional[str] = Field(default=None, description="Default location ID if not set per device")
    generate_activation_codes: bool = Field(default=True, description="Generate 16-digit activation codes for users/workspaces")
    step_up_user: Optional[str] = None
    step_up_pass: Optional[str] = None


class DeviceOnboardingResultItem(BaseModel):
    mac_address: str
    model: str
    migration_mode: str
    owner_type: str = "USER"  # "USER" or "WORKSPACE"
    owner_identity: str
    status: str  # "SUCCESS", "DRY_RUN", "FAILED", "SKIPPED"
    details: str
    device_id: Optional[str] = None
    activation_code: Optional[str] = None
    activation_code_expiry: Optional[str] = None
    technician_instructions: Optional[TechnicianInstructions] = None


class DeviceOnboardingResponse(BaseModel):
    status: str = "SUCCESS"
    dry_run: bool
    total_evaluated: int
    successful_count: int
    failed_count: int
    skipped_count: int
    results: List[DeviceOnboardingResultItem]


class DeviceActivationCodeRequest(BaseModel):
    user_email: Optional[str] = None
    person_id: Optional[str] = None
    workspace_id: Optional[str] = None
    model: Optional[str] = None


class DeviceActivationCodeResponse(BaseModel):
    code: str
    expiry_time: Optional[str] = None
    owner_type: str
    owner_id: str


class DeviceCompatibilityResponse(BaseModel):
    model: str
    clean_model: str
    category: str  # "NATIVE_MPP", "ENTERPRISE_CONVERTIBLE", "LEGACY_UNSUPPORTED"
    is_mpp_supported: bool
    is_conversion_supported: bool
    recommended_mode: str
    notes: str


# -----------------------------------------------------------------------------
# Phase 4: CUCM Dial-Plan & Decommissioning Models
# -----------------------------------------------------------------------------

class CucmRoutePartitionItem(BaseModel):
    name: str
    description: Optional[str] = None


class CucmGatewayRouteListItem(BaseModel):
    name: str
    description: Optional[str] = None
    class_name: Optional[str] = None
    is_route_list: bool = False


class CucmDialplanOptionsResponse(BaseModel):
    cluster: str
    partitions: List[CucmRoutePartitionItem] = []
    gateways_and_route_lists: List[CucmGatewayRouteListItem] = []


class CucmRoutePatternRequest(BaseModel):
    extension: str = Field(description="Extension or directory number to redirect to Webex Calling (e.g. 4199)")
    partition: str = Field(description="Route Partition where pattern will reside")
    gateway_route_list: str = Field(description="Gateway, SIP Trunk, or Route List routing calls to LGW / Webex")
    description: Optional[str] = Field(default=None, description="Optional description for the route pattern")
    urgent_priority: bool = Field(default=True, description="Whether pattern has urgent priority")
    cluster: Optional[ClusterTarget] = Field(default=ClusterTarget.PRIMARY, description="Target CUCM cluster environment")
    dry_run: bool = True
    step_up_user: Optional[str] = None
    step_up_pass: Optional[str] = None


class CucmRoutePatternResponse(BaseModel):
    status: str = "SUCCESS"
    dry_run: bool
    cluster: str
    pattern: str
    partition: str
    gateway_route_list: str
    destination_type: Optional[str] = None
    uuid: Optional[str] = None
    details: str


class CucmTranslationPatternRequest(BaseModel):
    extension: str = Field(description="Extension or directory number to redirect to Webex Calling (e.g. 4199)")
    partition: str = Field(default="Internal_PT", description="Route Partition where pattern will reside")
    calling_search_space: str = Field(default="To_LGW_CSS", description="CSS routing calls to Local Gateway / Webex SIP Trunk")
    description: Optional[str] = Field(default=None, description="Optional description for the translation pattern")
    urgent_priority: bool = Field(default=True, description="Whether pattern has urgent priority")
    cluster: Optional[ClusterTarget] = Field(default=ClusterTarget.PRIMARY, description="Target CUCM cluster environment")
    dry_run: bool = True
    step_up_user: Optional[str] = None
    step_up_pass: Optional[str] = None


class CucmTranslationPatternResponse(BaseModel):
    status: str = "SUCCESS"
    dry_run: bool
    cluster: str
    pattern: str
    partition: str
    calling_search_space: str
    uuid: Optional[str] = None
    details: str


class CucmDeviceDecommissionItem(BaseModel):
    mac_address: str = Field(description="Device MAC address or CUCM device name (e.g. SEP001122334455)")
    extension: Optional[str] = Field(default=None, description="Optional primary extension associated with the phone")
    partition: Optional[str] = Field(default=None, description="Partition for extension / route / translation pattern")
    calling_search_space: Optional[str] = Field(default=None, description="CSS routing to Local Gateway / Webex Trunk")
    gateway_route_list: Optional[str] = Field(default=None, description="Gateway or Route List for hybrid route pattern")
    create_translation_pattern: bool = Field(
        default=True,
        description="Whether to inject a Translation/Route Pattern pointing the extension to Webex Calling"
    )
    create_route_pattern: bool = Field(
        default=True,
        description="Whether to inject a Route Pattern pointing the extension to Gateway/Route List"
    )
    delete_unassigned_dn: bool = Field(
        default=True,
        description="Whether to delete the Directory Number from numplan if unassigned after device deletion"
    )


class CucmDecommissionRequest(BaseModel):
    items: List[CucmDeviceDecommissionItem] = Field(description="List of devices to decommission from CUCM")
    default_partition: str = Field(default="Internal_PT", description="Default partition applied if not set per item")
    default_calling_search_space: str = Field(
        default="To_LGW_CSS",
        description="Default CSS routing to Local Gateway applied if not set per item"
    )
    default_gateway_route_list: Optional[str] = Field(
        default=None,
        description="Default Gateway or Route List routing calls to Local Gateway"
    )
    cluster: Optional[ClusterTarget] = Field(default=ClusterTarget.PRIMARY, description="Target CUCM cluster environment")
    dry_run: bool = True
    step_up_user: Optional[str] = None
    step_up_pass: Optional[str] = None


class CucmDecommissionResultItem(BaseModel):
    device_name: str
    mac_address: str
    model: Optional[str] = None
    extension: Optional[str] = None
    status: str  # SUCCESS, DRY_RUN, FAILED, SKIPPED
    translation_pattern_status: Optional[str] = None
    route_pattern_status: Optional[str] = None
    device_deletion_status: Optional[str] = None
    line_deletion_status: Optional[str] = None
    license_reclaimed: Optional[str] = None
    details: str


class CucmDecommissionResponse(BaseModel):
    status: str = "SUCCESS"
    dry_run: bool
    cluster: str
    total_evaluated: int
    successful_count: int
    failed_count: int
    skipped_count: int
    results: List[CucmDecommissionResultItem]


# -----------------------------------------------------------------------------
# Phase 5: Post-Cutover Verification & License Reconciliation Models
# -----------------------------------------------------------------------------

class VerificationCheckItem(BaseModel):
    check_id: str
    name: str
    status: str  # PASS, WARNING, FAIL, MANUAL_ACTION
    details: str
    recommended_action: Optional[str] = None


class PostCutoverVerificationRequest(BaseModel):
    user_email: Optional[str] = Field(default=None, description="Migrated user email to verify")
    mac_address: Optional[str] = Field(default=None, description="Physical phone MAC address or SEP name")
    extension: Optional[str] = Field(default=None, description="Internal extension digits (e.g. 4199)")
    cluster: Optional[ClusterTarget] = Field(default=ClusterTarget.PRIMARY, description="CUCM cluster for decommission check")


class PostCutoverVerificationResponse(BaseModel):
    status: str = "SUCCESS"
    readiness_status: str  # CUTOVER_VERIFIED, ACTION_REQUIRED, INCOMPLETE
    user_identity: Optional[str] = None
    device_name: Optional[str] = None
    checks_passed: int
    checks_warning: int
    checks_failed: int
    checks: List[VerificationCheckItem]


class LicenseReconciliationItem(BaseModel):
    category: str
    cucm_smart_license: str
    reclaimed_count: int
    webex_equivalent_license: str
    webex_consumed_count: int
    status: str


class LicenseReconciliationResponse(BaseModel):
    status: str = "SUCCESS"
    cluster: str
    total_reclaimed_cucm_licenses: int
    total_active_webex_licenses: int
    items: List[LicenseReconciliationItem]
    summary: str



