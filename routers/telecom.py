"""
routers/telecom.py

Telecom Migration & Automation Router.
Handles CUCM AXL audits, Webex Calling batch provisioning, CUC voicemail extraction,
and interactive UCCX 12.0/12.5 CRS application to WxCC Flow Builder generation.
"""

import os
import re
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any, cast
from fastapi import APIRouter, HTTPException, Depends, Header, Query, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, SecretStr
from celery import Task
from celery.result import AsyncResult

from celery_app import (
    celery_app,
    execute_cuc_voicemail_export_task,
    execute_cucm_migration_audit_task,
    execute_uccx_migration_audit_task,
    execute_uccx_prompt_export_task,
    execute_webex_batch_provisioning_task,
    execute_wxcc_flow_generation_task,
)
from core.dependencies import get_meraki_provider
from models.telecom import (
    ClusterTarget,
    ClusterAuditRequest,
    ClusterUccxAuditRequest,
    ClusterPromptExportRequest,
    StagedEndpointRecord,
    WebexBatchStagingPayload,
    MerakiLocationPreviewResponse,
    MerakiLocationSyncRequest,
    MerakiLocationSyncResponse,
    LocationDidProvisionRequest,
    LocationDidProvisionResponse,
    UserCallingProfileProvisionRequest,
    UserCallingProfileProvisionResponse,
    DeviceOnboardingRequest,
    DeviceOnboardingResponse,
    DeviceActivationCodeRequest,
    DeviceActivationCodeResponse,
    DeviceCompatibilityResponse,
    CucmDialplanOptionsResponse,
    CucmRoutePatternRequest,
    CucmRoutePatternResponse,
    CucmTranslationPatternRequest,
    CucmTranslationPatternResponse,
    CucmDeviceDecommissionItem,
    CucmDecommissionRequest,
    CucmDecommissionResultItem,
    CucmDecommissionResponse,
    PostCutoverVerificationRequest,
    PostCutoverVerificationResponse,
    LicenseReconciliationItem,
    LicenseReconciliationResponse,
    RedskyMyE911SyncRequest,
    RedskyMyE911SyncResponse,
    RedskyWiremapRequest,
    RedskyWiremapResponse,
)
from providers.meraki_provider import MerakiProvider
from providers.webex_calling_provider import WebexCallingProvider, WebexAPIError
from providers.redsky_provider import RedskyProvider, RedskyAuthError, RedskyAPIError
from providers.telecom_factory import TelecomClusterRegistry, ClusterEnvironment
from services.wxcc_flow_generator_service import WxCCFlowGeneratorService, WxCCFlowDefinition
from services.uccx_script_migration_service import UCCXScriptMigrationService
from services.source_inventory_service import SourceInventoryService
from services.migration_staging_service import MigrationStagingService
from services.webex_migration_provision_service import WebexMigrationProvisionService
from services.webex_workspace_import_service import WebexWorkspaceImportService
from services.webex_workspace_location_service import WebexWorkspaceLocationService
from services.webex_location_provision_service import WebexLocationProvisionService
from services.webex_user_profile_service import WebexUserProfileService
from services.webex_device_onboarding_service import WebexDeviceOnboardingService, CiscoDeviceClassifier
from services.cucm_decommission_service import CucmDecommissionService
from services.webex_verification_service import WebexVerificationService
from providers.cucm_axl_provider import AXLRequestError, AXLProviderError
from security.roles import Role
from security.auth import authenticate_step_up_credentials, get_current_user, CurrentUser

from starlette.concurrency import run_in_threadpool
from providers.uccx_provider import UCCXConnectionError, UCCXAPIError

logger = logging.getLogger("Net_Dashboard.Telecom")
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def require_telecom_admin(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """Enforces strict Network Administrator access across all Telecom Migration tools."""
    if current_user.role != Role.NETWORK_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access Denied: Telecom Migration tools require Network Administrator privileges."
        )
    return current_user


router = APIRouter(
    prefix="/api/v1/telecom",
    tags=["Telecom & Migration Automation"],
    dependencies=[Depends(require_telecom_admin)]
)


# -----------------------------------------------------------------------------
# Request & Response Schemas
# -----------------------------------------------------------------------------

class TaskResponse(BaseModel):
    task_id: str
    status: str
    message: str
    cluster: Optional[str] = "PRIMARY"


class CUCMAuditRequest(BaseModel):
    cluster: ClusterTarget = Field(default=ClusterTarget.PRIMARY, description="Target cluster (PRIMARY or HAWAII)")
    default_country_code: str = "+1"
    device_scope: str = "PHYSICAL_ONLY"
    device_pool_filter: Optional[str] = None
    include_lines: bool = True


class UCCXAuditRequest(BaseModel):
    cluster: ClusterTarget = Field(default=ClusterTarget.PRIMARY, description="Target cluster (PRIMARY or HAWAII)")
    default_country_code: str = "+1"


class CUCExportRequest(BaseModel):
    cluster: ClusterTarget = Field(default=ClusterTarget.PRIMARY, description="Target cluster (PRIMARY or HAWAII)")


class WxCCFlowGenRequest(BaseModel):
    cuc_export_data: Dict[str, Any]
    output_dir: str = "exports/wxcc_flows"


class GenerateUCCXFlowRequest(BaseModel):
    cluster: str = Field(default="HAWAII", description="Target cluster (PRIMARY or HAWAII)")
    flow_name: str
    trigger_dn: str
    open_greeting_prompt: str
    closed_greeting_prompt: Optional[str] = None
    menu_dtmf_mappings: Dict[str, Dict[str, str]]
    business_hours_id: Optional[str] = None
    default_queue_id: Optional[str] = "WXCC_DEFAULT_QUEUE_ID"


class UCCXScriptConversionRequest(BaseModel):
    cluster: str = Field(default="HAWAII", description="Target cluster (PRIMARY or HAWAII)")
    script_name: str
    script_path: Optional[str] = None
    script_content: Optional[str] = None
    trigger_dn: Optional[str] = None


class SourceInventoryRequest(BaseModel):
    cluster: ClusterTarget = Field(default=ClusterTarget.PRIMARY, description="Target cluster (PRIMARY or HAWAII)")
    default_country_code: str = "+1"
    device_scope: str = "PHYSICAL_ONLY"
    device_pool_filter: Optional[str] = None
    cuc_page_size: int = 100


class MigrationStagingRequest(BaseModel):
    source_manifest: Dict[str, Any]
    csv_directory: str = "imports/migration_csv"


class WebexMigrationProvisionRequest(BaseModel):
    staging: Dict[str, Any]
    approved: bool = False
    dry_run: bool = True
    calling_license_id: Optional[str] = None


class WorkspaceCsvValidationRequest(BaseModel):
    csv_content: Optional[str] = None
    file_path: Optional[str] = None
    use_sample: bool = False


class WorkspaceBatchImportRequest(BaseModel):
    rows: List[Dict[str, Any]]
    dry_run: bool = True
    calling_license_id: Optional[str] = None
    step_up_user: Optional[str] = None
    step_up_pass: Optional[SecretStr] = None


class WorkspaceLocationCorrectionRequest(BaseModel):
    csv_content: str
    dry_run: bool = True
    step_up_user: Optional[str] = None
    step_up_pass: Optional[SecretStr] = None


async def require_workspace_step_up(
    username: Optional[str],
    password: Optional[SecretStr],
    action: str,
) -> None:
    """Re-authenticates live workspace mutations against the corporate directory."""
    await run_in_threadpool(
        authenticate_step_up_credentials,
        username or "",
        password.get_secret_value() if password else "",
        action,
    )


# -----------------------------------------------------------------------------
# CUCM & Webex Calling Routes
# -----------------------------------------------------------------------------

@router.post("/cucm-audit", response_model=TaskResponse, status_code=status.HTTP_202_ACCEPTED)
@router.post("/cucm/audit", response_model=TaskResponse, status_code=status.HTTP_202_ACCEPTED)
async def trigger_cucm_audit(
    payload: CUCMAuditRequest,
    current_user: CurrentUser = Depends(require_telecom_admin)
):

    try:
        target_cluster = payload.cluster.value if hasattr(payload.cluster, "value") else str(payload.cluster)
        task = await run_in_threadpool(
            cast(Task, execute_cucm_migration_audit_task).delay,
            cluster=target_cluster,
            default_country_code=payload.default_country_code,
            device_scope=payload.device_scope,
            device_pool_filter=payload.device_pool_filter or "ALL"
        )
        logger.info(f"Dispatched CUCM Audit task ID '{task.id}' [Cluster: {target_cluster}] for '{current_user.username}'.")
        return TaskResponse(
            task_id=task.id,
            status="QUEUED",
            cluster=target_cluster,
            message=f"CUCM Migration Audit queued for {target_cluster} cluster ({payload.device_scope})."
        )
    except Exception as exc:
        logger.exception("Failed to queue CUCM audit task: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to queue CUCM audit task: {str(exc)}")


def _clean_webex_token(token: Optional[str]) -> str:
    """Strips whitespace, surrounding quotes, and leading 'Bearer ' prefixes from token strings."""
    if not token:
        return ""
    clean = token.strip().strip('"').strip("'")
    return re.sub(r"^Bearer\s+", "", clean, flags=re.IGNORECASE).strip()


def _raise_for_webex_api_error(exc: WebexAPIError, operation: str) -> None:
    """Formats explicit HTTP 401 and 403 error details to distinguish token expiration from permission gaps."""
    err_str = str(exc)
    status_code = getattr(exc, "status_code", None)
    if status_code == 401 or "HTTP 401:" in err_str or "401" in err_str:
        raise HTTPException(
            status_code=401,
            detail="Webex rejected this token (HTTP 401 Unauthorized). The 12-hour Personal Access Token may be expired or invalid."
        ) from exc
    if status_code == 403 or "HTTP 403:" in err_str or "403" in err_str:
        raise HTTPException(
            status_code=403,
            detail=f"Webex permissions error (HTTP 403 Forbidden) during {operation}: {err_str}. Your Webex account requires Administrator privileges (e.g. spark-admin:locations_read, spark-admin:telephony_config_read)."
        ) from exc
    logger.exception("Webex API failure during %s: %s", operation, exc)
    raise HTTPException(status_code=502, detail=f"Webex API failure during {operation}: {err_str}") from exc


@router.get("/webex-locations")
async def get_webex_locations(
    webex_access_token: Optional[str] = Header(default=None, alias="X-Webex-Access-Token"),
    x_webex_token: Optional[str] = Header(default=None, alias="X-Webex-Token"),
    current_user: CurrentUser = Depends(require_telecom_admin)
):
    token = _clean_webex_token(webex_access_token or x_webex_token)
    if not token:
        raise HTTPException(status_code=401, detail="Paste your Webex bearer token to load locations.")

    try:
        webex_provider = WebexCallingProvider(access_token=token)
        locations = await run_in_threadpool(webex_provider.get_calling_locations)
        return [
            {
                "id": loc.get("id", ""),
                "name": loc.get("name", "Unknown"),
                "timeZone": loc.get("timeZone", "America/Chicago"),
                "city": (loc.get("address") or {}).get("city", "") if isinstance(loc.get("address"), dict) else "",
                "state": (loc.get("address") or {}).get("state", "") if isinstance(loc.get("address"), dict) else "",
            }
            for loc in locations
            if loc.get("id") and loc.get("name")
        ]
    except WebexAPIError as exc:
        _raise_for_webex_api_error(exc, "Webex location lookup")
    except Exception as exc:
        logger.exception("Could not retrieve live Webex locations: %s", exc)
        raise HTTPException(status_code=502, detail=f"Could not retrieve live Webex locations: {str(exc)}") from exc


@router.get("/pstn/routing-options")
async def get_pstn_routing_options(
    webex_access_token: Optional[str] = Header(default=None, alias="X-Webex-Access-Token"),
    x_webex_token: Optional[str] = Header(default=None, alias="X-Webex-Token"),
    current_user: CurrentUser = Depends(require_telecom_admin)
):
    """
    Phase 1: Retrieves available premises-based PSTN Route Groups and Trunks
    from Webex Control Hub.
    """
    token = _clean_webex_token(webex_access_token or x_webex_token)
    if not token:
        raise HTTPException(status_code=401, detail="Paste your Webex bearer token to load PSTN routing options.")

    try:
        webex_provider = WebexCallingProvider(access_token=token)
        return await run_in_threadpool(webex_provider.get_pstn_routing_options)
    except WebexAPIError as exc:
        _raise_for_webex_api_error(exc, "Webex PSTN routing options lookup")
    except Exception as exc:
        logger.exception("Could not retrieve live Webex PSTN routing options: %s", exc)
        raise HTTPException(status_code=502, detail=f"Could not retrieve live Webex PSTN routing options: {str(exc)}") from exc


@router.get("/locations/meraki-preview", response_model=MerakiLocationPreviewResponse)
async def preview_meraki_webex_locations(
    meraki_provider: Optional[MerakiProvider] = Depends(get_meraki_provider),
    webex_access_token: Optional[str] = Header(default=None, alias="X-Webex-Access-Token"),
    x_webex_token: Optional[str] = Header(default=None, alias="X-Webex-Token"),
    current_user: CurrentUser = Depends(require_telecom_admin),
):
    """
    Phase 1: Previews Meraki MX/Z4 branch physical addresses, parses E911-mandated fields,
    and cross-references against live Webex Control Hub locations.
    The Meraki appliance fetch proceeds independently of Webex token status.
    """
    token = _clean_webex_token(webex_access_token or x_webex_token)
    if not meraki_provider:
        raise HTTPException(status_code=503, detail="Meraki Provider is uninitialized or not configured.")

    try:
        webex_prov = WebexCallingProvider(access_token=token) if token else None
        service = WebexLocationProvisionService(webex_provider=webex_prov, meraki_provider=meraki_provider)
        return await run_in_threadpool(service.preview_meraki_locations)
    except WebexAPIError as exc:
        logger.warning("Webex API error during Meraki location cross-reference: %s. Continuing with Meraki data.", exc)
        # Fall back to Meraki-only preview if Webex lookup fails
        service = WebexLocationProvisionService(webex_provider=None, meraki_provider=meraki_provider)
        return await run_in_threadpool(service.preview_meraki_locations)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Meraki-to-Webex location preview failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Meraki-to-Webex location preview failed: {exc}")


@router.post("/locations/sync-meraki", response_model=MerakiLocationSyncResponse)
async def sync_meraki_webex_locations(
    payload: MerakiLocationSyncRequest,
    meraki_provider: Optional[MerakiProvider] = Depends(get_meraki_provider),
    webex_access_token: Optional[str] = Header(default=None, alias="X-Webex-Access-Token"),
    x_webex_token: Optional[str] = Header(default=None, alias="X-Webex-Token"),
    current_user: CurrentUser = Depends(require_telecom_admin),
):
    """
    Phase 1: Syncs reviewed Meraki branch locations into Webex Control Hub and configures Calling parameters.
    Supports dry_run=True (default). Live mutations require step-up authentication.
    """
    token = _clean_webex_token(webex_access_token or x_webex_token)
    if not token:
        raise HTTPException(status_code=401, detail="Paste your Webex bearer token before provisioning locations.")
    if not meraki_provider:
        raise HTTPException(status_code=503, detail="Meraki Provider is uninitialized or not configured.")

    if not payload.dry_run:
        await require_workspace_step_up(
            payload.step_up_user,
            SecretStr(payload.step_up_pass) if payload.step_up_pass else None,
            "Webex Calling Location Provisioning",
        )

    try:
        webex_prov = WebexCallingProvider(access_token=token)
        service = WebexLocationProvisionService(webex_provider=webex_prov, meraki_provider=meraki_provider)
        return await run_in_threadpool(
            service.sync_locations,
            network_ids=payload.network_ids,
            dry_run=payload.dry_run,
            connection_type=payload.connection_type,
            routing_group_id=payload.routing_group_id,
            internal_extension_length=payload.internal_extension_length,
            routing_prefix=payload.routing_prefix,
            sync_redsky=payload.sync_redsky,
        )
    except WebexAPIError as exc:
        if str(exc).startswith(("HTTP 401:", "HTTP 403:")):
            raise HTTPException(status_code=401, detail="Webex rejected this token. Paste a valid, unexpired bearer token.") from exc
        logger.exception("Webex location sync API failure: %s", exc)
        raise HTTPException(status_code=502, detail=f"Webex location sync API failure: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Meraki-to-Webex location sync failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Meraki-to-Webex location sync failed: {exc}")


@router.get("/redsky/status")
async def get_redsky_status(
    current_user: CurrentUser = Depends(require_telecom_admin),
):
    """
    Diagnostic endpoint to verify RedSky E911 configuration and API connectivity.
    """
    from providers.redsky_provider import RedskyProvider
    from pathlib import Path

    env_path = Path(__file__).resolve().parent.parent / ".env"
    prov = RedskyProvider()
    key_len = len(prov.api_key)
    key_masked = (
        f"{prov.api_key[:3]}...{prov.api_key[-3:]}"
        if key_len >= 8
        else ("***" if key_len > 0 else "")
    )

    auth_ok = False
    auth_err = None
    if prov.is_configured():
        try:
            token = await run_in_threadpool(prov._authenticate)
            auth_ok = bool(token)
        except Exception as e:
            auth_err = str(e)

    return {
        "is_configured": prov.is_configured(),
        "key_detected": key_len > 0,
        "key_length": key_len,
        "key_preview": key_masked,
        "base_url": prov.base_url,
        "auth_successful": auth_ok,
        "auth_error": auth_err,
        "env_file_path": str(env_path),
        "env_file_exists": env_path.exists(),
    }


@router.post("/locations/{location_id}/dids", response_model=LocationDidProvisionResponse)
async def provision_location_dids(
    location_id: str,
    payload: LocationDidProvisionRequest,
    webex_access_token: Optional[str] = Header(default=None, alias="X-Webex-Access-Token"),
    x_webex_token: Optional[str] = Header(default=None, alias="X-Webex-Token"),
    current_user: CurrentUser = Depends(require_telecom_admin),
):
    """
    Phase 1: Adds and activates external PSTN DIDs in E.164 format on a target Webex Calling location.
    Optionally sets the Location Main Number.
    Supports dry_run=True (default). Live mutations require step-up authentication.
    """
    token = (webex_access_token or x_webex_token or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Paste your Webex bearer token before adding DIDs.")

    if not payload.dry_run:
        await require_workspace_step_up(
            payload.step_up_user,
            SecretStr(payload.step_up_pass) if payload.step_up_pass else None,
            "Webex Calling DID Provisioning",
        )

    try:
        webex_prov = WebexCallingProvider(access_token=token)
        service = WebexLocationProvisionService(webex_provider=webex_prov)
        return await run_in_threadpool(
            service.provision_dids,
            location_id=location_id,
            phone_numbers=payload.phone_numbers,
            main_number=payload.main_number,
            dry_run=payload.dry_run,
        )
    except WebexAPIError as exc:
        if str(exc).startswith(("HTTP 401:", "HTTP 403:")):
            raise HTTPException(status_code=401, detail="Webex rejected this token. Paste a valid, unexpired bearer token.") from exc
        logger.exception("Webex location DID provisioning API failure: %s", exc)
        raise HTTPException(status_code=502, detail=f"Webex location DID provisioning API failure: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Webex location DID provisioning failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Webex location DID provisioning failed: {exc}")


@router.post("/locations/{location_id}/redsky-wiremap", response_model=RedskyWiremapResponse)
async def wiremap_location_subnets_to_redsky(
    location_id: str,
    payload: RedskyWiremapRequest,
    meraki_provider: Optional[MerakiProvider] = Depends(get_meraki_provider),
    webex_access_token: Optional[str] = Header(default=None, alias="X-Webex-Access-Token"),
    x_webex_token: Optional[str] = Header(default=None, alias="X-Webex-Token"),
    current_user: CurrentUser = Depends(require_telecom_admin),
):
    """
    On-Demand: Classifies and wiremaps Meraki phone subnets (Dedicated Voice supernet/named VLAN vs Teleworker Single Data VLAN)
    to a RedSky Building location for Ray Baum's Act compliance.
    """
    token = (webex_access_token or x_webex_token or "").strip()
    webex_prov = WebexCallingProvider(access_token=token) if token else None

    # Resolve location name if not provided in payload
    loc_name = payload.location_name
    if not loc_name and webex_prov and location_id and location_id != "auto":
        try:
            loc_data = webex_prov.get_location(location_id)
            if loc_data and loc_data.get("name"):
                loc_name = loc_data["name"]
        except Exception:
            pass

    try:
        service = WebexLocationProvisionService(
            webex_provider=webex_prov or WebexCallingProvider(access_token="anonymous"),
            meraki_provider=meraki_provider,
            redsky_provider=RedskyProvider(),
        )
        res = await run_in_threadpool(
            service.wiremap_location_subnets,
            network_id=payload.network_id,
            location_name=loc_name or location_id,
            building_id=payload.building_id,
            custom_subnets=payload.custom_subnets,
        )
        return RedskyWiremapResponse(**res)
    except RedskyAuthError as exc:
        raise HTTPException(status_code=401, detail=f"RedSky authentication failed: {exc}") from exc
    except RedskyAPIError as exc:
        raise HTTPException(status_code=502, detail=f"RedSky API error: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("RedSky subnet wiremapping failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"RedSky subnet wiremapping failed: {exc}")


@router.get("/networks/{network_id}/phone-subnets")
async def get_network_phone_subnets(
    network_id: str,
    meraki_provider: Optional[MerakiProvider] = Depends(get_meraki_provider),
    current_user: CurrentUser = Depends(require_telecom_admin),
):
    """
    On-Demand: Classifies Meraki appliance subnets for a single network
    (Dedicated Voice supernet/named VLAN vs Teleworker Single Data VLAN / Single LAN)
    to inspect E911 dispatch eligibility in ~500ms without blocking organizational preview.
    """
    if not meraki_provider:
        raise HTTPException(status_code=503, detail="Meraki Provider is uninitialized or not configured.")
    service = WebexLocationProvisionService(meraki_provider=meraki_provider)
    subnets, strategy = await run_in_threadpool(service.classify_phone_subnets, network_id)
    return {
        "network_id": network_id,
        "subnets": [f"{s} ({d})" for s, d in subnets],
        "raw_subnets": [{"cidr": s, "description": d} for s, d in subnets],
        "wiremap_strategy": strategy,
    }


@router.post("/users/provision-calling-profile", response_model=UserCallingProfileProvisionResponse)
async def provision_user_calling_profiles(
    payload: UserCallingProfileProvisionRequest,
    webex_access_token: Optional[str] = Header(default=None, alias="X-Webex-Access-Token"),
    x_webex_token: Optional[str] = Header(default=None, alias="X-Webex-Token"),
    current_user: CurrentUser = Depends(require_telecom_admin),
):
    """
    Phase 2: Provisions Webex Calling licenses, locations, internal extensions, direct DIDs,
    Caller ID policy (Direct Line vs Location Number), and Webex Cloud Voicemail with .wav audio attachment.
    Supports dry_run=True (default). Live mutations require step-up authentication.
    """
    token = (webex_access_token or x_webex_token or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Paste your Webex bearer token before configuring calling profiles.")

    if not payload.dry_run:
        await require_workspace_step_up(
            payload.step_up_user,
            SecretStr(payload.step_up_pass) if payload.step_up_pass else None,
            "Webex Calling User Profile Provisioning",
        )

    try:
        webex_prov = WebexCallingProvider(access_token=token)
        service = WebexUserProfileService(webex_provider=webex_prov, redsky_provider=RedskyProvider())
        return await run_in_threadpool(
            service.provision_user_profiles,
            users=payload.users,
            dry_run=payload.dry_run,
            default_license_id=payload.calling_license_id,
            default_location_id=payload.default_location_id,
            sync_redsky=payload.sync_redsky,
        )
    except WebexAPIError as exc:
        if str(exc).startswith(("HTTP 401:", "HTTP 403:")):
            raise HTTPException(status_code=401, detail="Webex rejected this token. Paste a valid, unexpired bearer token.") from exc
        logger.exception("Webex user profile provisioning API failure: %s", exc)
        raise HTTPException(status_code=502, detail=f"Webex user profile provisioning API failure: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("User calling profile provisioning failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"User calling profile provisioning failed: {exc}")


@router.get("/users/{email_or_person_id}/calling-profile")
async def get_user_calling_profile(
    email_or_person_id: str,
    webex_access_token: Optional[str] = Header(default=None, alias="X-Webex-Access-Token"),
    x_webex_token: Optional[str] = Header(default=None, alias="X-Webex-Token"),
    current_user: CurrentUser = Depends(require_telecom_admin),
):
    """
    Phase 2: Retrieves current Webex Calling profile, Caller ID policy, and Voicemail settings for a user.
    """
    token = (webex_access_token or x_webex_token or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Paste your Webex bearer token to view user profile.")

    try:
        webex_prov = WebexCallingProvider(access_token=token)
        service = WebexUserProfileService(webex_provider=webex_prov)
        return await run_in_threadpool(service.get_user_calling_profile, email_or_person_id)
    except WebexAPIError as exc:
        if str(exc).startswith(("HTTP 401:", "HTTP 403:")):
            raise HTTPException(status_code=401, detail="Webex rejected this token. Paste a valid, unexpired bearer token.") from exc
        logger.exception("Webex user profile lookup failure: %s", exc)
        raise HTTPException(status_code=502, detail=f"Webex user profile lookup failure: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.exception("Failed to retrieve user calling profile: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to retrieve user calling profile: {exc}")


@router.post("/users/{email}/redsky-mye911-sync", response_model=RedskyMyE911SyncResponse)
async def sync_user_redsky_mye911(
    email: str,
    payload: Optional[RedskyMyE911SyncRequest] = None,
    webex_access_token: Optional[str] = Header(default=None, alias="X-Webex-Access-Token"),
    x_webex_token: Optional[str] = Header(default=None, alias="X-Webex-Token"),
    current_user: CurrentUser = Depends(require_telecom_admin),
):
    """
    Dedicated on-demand synchronization of a user's direct DID, name, and MyE911 functionality
    to RedSky Anywhere. Enables 933 location readback and compliant emergency call dispatch.
    """
    clean_email = email.strip()
    phone_number = payload.phone_number if payload else None
    first_name = payload.first_name if payload else None
    last_name = payload.last_name if payload else None
    callback_num = payload.callback_number if payload else None
    enable_mye911 = payload.enable_mye911 if payload else True

    token = (webex_access_token or x_webex_token or "").strip()
    # Resolve AD-synced DID and missing profile details from Webex when a token is provided
    if token:
        try:
            webex_prov = WebexCallingProvider(access_token=token)
            person = webex_prov.get_person_by_email(clean_email)
            if person:
                ad_did = WebexCallingProvider.extract_existing_did(person)
                if ad_did:
                    if not callback_num or callback_num == phone_number:
                        callback_num = ad_did
                    phone_number = ad_did
                elif not phone_number:
                    phones = person.get("phoneNumbers", [])
                    if phones:
                        phone_number = phones[0].get("value")
                if not first_name:
                    first_name = person.get("firstName") or ""
                if not last_name:
                    last_name = person.get("lastName") or ""
        except Exception as wex_err:
            logger.debug("Could not enrich RedSky sync from Webex person for %s: %s", clean_email, wex_err)

    if not phone_number:
        raise HTTPException(
            status_code=400,
            detail=f"Phone number (DID) is required to configure RedSky MyE911 for user '{clean_email}'. None was provided or found in Webex.",
        )

    redsky = RedskyProvider()
    if not redsky.is_configured():
        raise HTTPException(
            status_code=503,
            detail="RedSky E911 is not configured. Set REDSKY_API in environment settings.",
        )

    try:
        result = await run_in_threadpool(
            redsky.sync_device_user_mye911,
            email=clean_email,
            phone_number=phone_number,
            first_name=first_name,
            last_name=last_name,
            callback_number=callback_num or phone_number,
            enable_mye911=enable_mye911,
        )
        return RedskyMyE911SyncResponse(
            status="SUCCESS",
            action=result.get("action", "SYNCED"),
            email=clean_email,
            phone_number=result.get("phone_number"),
            callback_number=result.get("callback_number"),
            enable_mye911=enable_mye911,
            details=f"RedSky MyE911 {result.get('action', 'SYNCED').lower()} successfully for '{clean_email}' with DID '{phone_number}'.",
        )
    except RedskyAuthError as exc:
        logger.exception("RedSky authentication failure: %s", exc)
        raise HTTPException(status_code=401, detail=f"RedSky authentication failed: {exc}") from exc
    except RedskyAPIError as exc:
        logger.exception("RedSky API failure: %s", exc)
        raise HTTPException(status_code=502, detail=f"RedSky API error: {exc}") from exc
    except Exception as exc:
        logger.exception("RedSky MyE911 sync failed for %s: %s", clean_email, exc)
        raise HTTPException(status_code=500, detail=f"RedSky MyE911 sync failed: {exc}")


@router.post("/devices/onboard", response_model=DeviceOnboardingResponse)
async def onboard_devices(
    payload: DeviceOnboardingRequest,
    webex_access_token: Optional[str] = Header(default=None, alias="X-Webex-Access-Token"),
    x_webex_token: Optional[str] = Header(default=None, alias="X-Webex-Token"),
    current_user: CurrentUser = Depends(require_telecom_admin),
):
    """
    Phase 3: Onboards IP phones to Webex Calling with support for:
    - Option A: Native MPP / Cloud endpoints (Control Hub registration + 16-digit activation code)
    - Option B: Enterprise Firmware to MPP Conversion (Hardware validation + factory reset instructions + activation code)
    Supports dry_run=True (default). Live mutations require step-up authentication.
    """
    token = (webex_access_token or x_webex_token or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Paste your Webex bearer token before onboarding devices.")

    if not payload.dry_run:
        await require_workspace_step_up(
            payload.step_up_user,
            SecretStr(payload.step_up_pass) if payload.step_up_pass else None,
            "Webex Device Onboarding & MPP Transition",
        )

    try:
        webex_prov = WebexCallingProvider(access_token=token)
        service = WebexDeviceOnboardingService(webex_provider=webex_prov, redsky_provider=RedskyProvider())
        return await run_in_threadpool(
            service.onboard_devices,
            devices=payload.devices,
            dry_run=payload.dry_run,
            default_location_id=payload.default_location_id,
            generate_activation_codes=payload.generate_activation_codes,
        )
    except WebexAPIError as exc:
        if str(exc).startswith(("HTTP 401:", "HTTP 403:")):
            raise HTTPException(status_code=401, detail="Webex rejected this token. Paste a valid, unexpired bearer token.") from exc
        logger.exception("Webex device onboarding API failure: %s", exc)
        raise HTTPException(status_code=502, detail=f"Webex device onboarding API failure: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Device onboarding failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Device onboarding failed: {exc}")


@router.post("/devices/activation-code", response_model=DeviceActivationCodeResponse)
async def generate_device_activation_code(
    payload: DeviceActivationCodeRequest,
    webex_access_token: Optional[str] = Header(default=None, alias="X-Webex-Access-Token"),
    x_webex_token: Optional[str] = Header(default=None, alias="X-Webex-Token"),
    current_user: CurrentUser = Depends(require_telecom_admin),
):
    """
    Phase 3: Generates a 16-digit activation code (POST /v1/devices/activationCode)
    for assigning an IP phone to a user or workspace.
    """
    token = (webex_access_token or x_webex_token or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Paste your Webex bearer token to generate activation codes.")

    try:
        webex_prov = WebexCallingProvider(access_token=token)
        service = WebexDeviceOnboardingService(webex_provider=webex_prov)
        return await run_in_threadpool(
            service.generate_activation_code,
            user_email=payload.user_email,
            person_id=payload.person_id,
            workspace_id=payload.workspace_id,
            model=payload.model,
        )
    except WebexAPIError as exc:
        if str(exc).startswith(("HTTP 401:", "HTTP 403:")):
            raise HTTPException(status_code=401, detail="Webex rejected this token. Paste a valid, unexpired bearer token.") from exc
        logger.exception("Webex activation code API failure: %s", exc)
        raise HTTPException(status_code=502, detail=f"Webex activation code API failure: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Failed to generate device activation code: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to generate device activation code: {exc}")


@router.get("/devices/check-compatibility/{model}", response_model=DeviceCompatibilityResponse)
async def check_device_compatibility(
    model: str,
    current_user: CurrentUser = Depends(require_telecom_admin),
):
    """
    Phase 3: Evaluates device model compatibility for Webex Calling:
    - Native MPP (68xx, 98xx, 3PCC)
    - Enterprise-to-MPP Convertible (78xx, 88xx)
    - Legacy Unsupported EOL (79xx, 69xx, 39xx)
    """
    try:
        return CiscoDeviceClassifier.classify(model)
    except Exception as exc:
        logger.exception("Failed to evaluate device compatibility: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to evaluate device compatibility: {exc}")


@router.get("/cucm/dialplan-options", response_model=CucmDialplanOptionsResponse)
async def get_cucm_dialplan_options(
    cluster: str = Query("PRIMARY", description="Target CUCM cluster environment"),
    current_user: CurrentUser = Depends(require_telecom_admin),
):
    """
    Phase 4: Returns configured Route Partitions and Gateways / Route Lists for the specified CUCM cluster.
    Used to dynamically populate dial-plan dropdowns.
    """
    try:
        env_cluster = ClusterEnvironment(cluster.strip().upper())
        axl_provider = TelecomClusterRegistry.get_cucm_provider(env_cluster)
        service = CucmDecommissionService(axl_provider=axl_provider, cluster_name=env_cluster.value)

        return await run_in_threadpool(service.get_dialplan_options)
    except Exception as exc:
        logger.exception("Failed to fetch CUCM dialplan options: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to fetch dialplan options from CUCM: {str(exc)}")


@router.post("/cucm/route-pattern", response_model=CucmRoutePatternResponse)
async def create_cucm_route_pattern(
    payload: CucmRoutePatternRequest,
    current_user: CurrentUser = Depends(require_telecom_admin),
):
    """
    Phase 4: Injects a Route Pattern in CUCM (e.g. extension 4199 in Route Partition)
    pointing to Gateway / Route List to redirect calls to Webex Calling / LGW during coexistence.
    Supports dry_run=True (default). Live mutations require step-up authentication.
    """
    if not payload.dry_run:
        await require_workspace_step_up(
            payload.step_up_user,
            SecretStr(payload.step_up_pass) if payload.step_up_pass else None,
            "CUCM Route Pattern Injection",
        )

    try:
        cluster = payload.cluster.value if hasattr(payload.cluster, "value") else str(payload.cluster)
        env_cluster = ClusterEnvironment(cluster.strip().upper())
        axl_provider = TelecomClusterRegistry.get_cucm_provider(env_cluster)
        service = CucmDecommissionService(axl_provider=axl_provider, cluster_name=env_cluster.value)

        return await run_in_threadpool(
            service.create_route_pattern,
            extension=payload.extension,
            partition=payload.partition,
            gateway_route_list=payload.gateway_route_list,
            description=payload.description,
            urgent_priority=payload.urgent_priority,
            dry_run=payload.dry_run,
        )
    except (AXLRequestError, AXLProviderError) as exc:
        logger.exception("CUCM AXL route pattern error: %s", exc)
        raise HTTPException(status_code=502, detail=f"CUCM AXL error: {str(exc)}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Failed to create CUCM route pattern: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to create route pattern: {str(exc)}")


@router.post("/cucm/translation-pattern", response_model=CucmTranslationPatternResponse)
async def create_cucm_translation_pattern(
    payload: CucmTranslationPatternRequest,
    current_user: CurrentUser = Depends(require_telecom_admin),
):
    """
    Phase 4: Injects a Translation Pattern in CUCM (e.g. extension 4199 in Internal_PT)
    pointing to the Local Gateway / Webex Calling SIP trunk to prevent split-brain routing.
    Supports dry_run=True (default). Live mutations require step-up authentication.
    """
    if not payload.dry_run:
        await require_workspace_step_up(
            payload.step_up_user,
            SecretStr(payload.step_up_pass) if payload.step_up_pass else None,
            "CUCM Translation Pattern Injection",
        )

    try:
        cluster = payload.cluster.value if hasattr(payload.cluster, "value") else str(payload.cluster)
        env_cluster = ClusterEnvironment(cluster.strip().upper())
        axl_provider = TelecomClusterRegistry.get_cucm_provider(env_cluster)
        service = CucmDecommissionService(axl_provider=axl_provider, cluster_name=env_cluster.value)

        return await run_in_threadpool(
            service.create_translation_pattern,
            extension=payload.extension,
            partition=payload.partition,
            calling_search_space=payload.calling_search_space,
            description=payload.description,
            urgent_priority=payload.urgent_priority,
            dry_run=payload.dry_run,
        )
    except (AXLRequestError, AXLProviderError) as exc:
        logger.exception("CUCM AXL translation pattern error: %s", exc)
        raise HTTPException(status_code=502, detail=f"CUCM AXL error: {str(exc)}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Failed to create CUCM translation pattern: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to create translation pattern: {str(exc)}")


@router.post("/cucm/decommission", response_model=CucmDecommissionResponse)
async def decommission_cucm_devices(
    payload: CucmDecommissionRequest,
    current_user: CurrentUser = Depends(require_telecom_admin),
):
    """
    Phase 4: Decommissions on-prem CUCM devices, disassociates lines, reclaims Smart Software Licensing
    (UCL/CUWL), and injects hybrid translation patterns routing calls to Webex Calling / LGW.
    Supports dry_run=True (default). Live mutations require step-up authentication.
    """
    if not payload.dry_run:
        await require_workspace_step_up(
            payload.step_up_user,
            SecretStr(payload.step_up_pass) if payload.step_up_pass else None,
            "CUCM Device Decommissioning & Smart License Release",
        )

    try:
        cluster = payload.cluster.value if hasattr(payload.cluster, "value") else str(payload.cluster)
        env_cluster = ClusterEnvironment(cluster.strip().upper())
        axl_provider = TelecomClusterRegistry.get_cucm_provider(env_cluster)
        service = CucmDecommissionService(axl_provider=axl_provider, cluster_name=env_cluster.value)

        return await run_in_threadpool(
            service.decommission_devices,
            items=payload.items,
            default_partition=payload.default_partition,
            default_calling_search_space=payload.default_calling_search_space,
            dry_run=payload.dry_run,
        )
    except (AXLRequestError, AXLProviderError) as exc:
        logger.exception("CUCM AXL decommission error: %s", exc)
        raise HTTPException(status_code=502, detail=f"CUCM AXL error: {str(exc)}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Failed to decommission devices from CUCM: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to decommission devices: {str(exc)}")


@router.get("/cucm/device/{device_name_or_mac}")
async def get_cucm_device_info(
    device_name_or_mac: str,
    cluster: ClusterTarget = ClusterTarget.PRIMARY,
    current_user: CurrentUser = Depends(require_telecom_admin),
):
    """
    Phase 4: Inspects an on-prem CUCM device, its line appearances, and its Smart Licensing tier.
    """
    try:
        cluster_val = cluster.value if hasattr(cluster, "value") else str(cluster)
        env_cluster = ClusterEnvironment(cluster_val.strip().upper())
        axl_provider = TelecomClusterRegistry.get_cucm_provider(env_cluster)
        service = CucmDecommissionService(axl_provider=axl_provider, cluster_name=env_cluster.value)

        device_name, clean_mac = service.normalize_device_name(device_name_or_mac)
        dev = await run_in_threadpool(axl_provider.get_device_by_name_or_mac, device_name)
        if not dev:
            raise HTTPException(status_code=404, detail=f"Device '{device_name}' not found in CUCM cluster {env_cluster.value}.")

        dev["license_reclaim_tier"] = service.determine_reclaimed_license(dev.get("model", ""))
        return {"status": "SUCCESS", "cluster": env_cluster.value, "device": dev}
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Failed to query device from CUCM: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to query CUCM device: {str(exc)}")


@router.get("/cucm/devices-by-extension/{extension}")
async def get_cucm_devices_by_extension(
    extension: str,
    partition: Optional[str] = None,
    cluster: ClusterTarget = ClusterTarget.PRIMARY,
    current_user: CurrentUser = Depends(require_telecom_admin),
):
    """
    Phase 4: Finds all CUCM devices (physical phones, BOT/TCT/CSF/named softphones)
    sharing a Directory Number so they can be decommissioned together before injecting a Route Pattern.
    """
    clean_ext = extension.strip()
    if not clean_ext:
        raise HTTPException(status_code=400, detail="Extension is required.")
    try:
        cluster_val = cluster.value if hasattr(cluster, "value") else str(cluster)
        env_cluster = ClusterEnvironment(cluster_val.strip().upper())
        axl_provider = TelecomClusterRegistry.get_cucm_provider(env_cluster)
        service = CucmDecommissionService(axl_provider=axl_provider, cluster_name=env_cluster.value)

        devices = await run_in_threadpool(axl_provider.get_devices_by_extension, clean_ext, partition)
        for dev in devices:
            dev["license_reclaim_tier"] = service.determine_reclaimed_license(dev.get("model", ""))
        return {
            "status": "SUCCESS",
            "cluster": env_cluster.value,
            "extension": clean_ext,
            "partition": partition,
            "devices": devices,
        }
    except Exception as exc:
        logger.exception("Failed to query CUCM devices for extension %s: %s", clean_ext, exc)
        raise HTTPException(status_code=500, detail=f"Failed to query CUCM devices by extension: {str(exc)}")


@router.post("/verification/post-cutover", response_model=PostCutoverVerificationResponse)
async def verify_post_cutover(
    payload: PostCutoverVerificationRequest,
    webex_access_token: Optional[str] = Header(default=None, alias="X-Webex-Access-Token"),
    x_webex_token: Optional[str] = Header(default=None, alias="X-Webex-Token"),
    current_user: CurrentUser = Depends(require_telecom_admin),
):
    """
    Phase 5: Executes automated 8-point post-cutover verification for a migrated user or endpoint:
    1. Online Device Registration (Control Hub status)
    2. MPP Firmware Release (>= 11.3 MPP)
    3. Inbound PSTN Direct DID (E.164 & internal extension mapping)
    4. Outbound PSTN & Caller ID (Calling Line ID policy)
    5. Internal Hybrid Dialing (CUCM translation pattern to LGW)
    6. Emergency Services E911 (Geocoded address & 933 test readiness)
    7. Cloud Voicemail & MWI (MWI enabled & notifications)
    8. CUCM Decommission & Licensing (Device removed, Smart License released)
    """
    token = (webex_access_token or x_webex_token or "").strip()
    try:
        webex_prov = WebexCallingProvider(access_token=token) if token else WebexCallingProvider()
    except Exception as exc:
        logger.warning("Could not initialize WebexCallingProvider: %s", exc)
        webex_prov = None

    cluster_name = "PRIMARY"
    axl_prov = None
    if payload.cluster:
        try:
            cluster_val = payload.cluster.value if hasattr(payload.cluster, "value") else str(payload.cluster)
            env_cluster = ClusterEnvironment(cluster_val.strip().upper())
            cluster_name = env_cluster.value
            axl_prov = TelecomClusterRegistry.get_cucm_provider(env_cluster)
        except Exception as exc:
            logger.warning("Could not initialize CUCM AXL provider for verification: %s", exc)

    if not webex_prov:
        raise HTTPException(status_code=400, detail="Webex provider configuration unavailable.")

    try:
        service = WebexVerificationService(
            webex_provider=webex_prov,
            axl_provider=axl_prov,
            cluster_name=cluster_name,
            redsky_provider=RedskyProvider(),
        )
        return await run_in_threadpool(
            service.verify_cutover,
            user_email=payload.user_email,
            mac_address=payload.mac_address,
            extension=payload.extension,
        )
    except WebexAPIError as exc:
        if str(exc).startswith(("HTTP 401:", "HTTP 403:")):
            raise HTTPException(status_code=401, detail="Webex rejected this token. Paste a valid, unexpired bearer token.") from exc
        logger.exception("Webex verification API error: %s", exc)
        raise HTTPException(status_code=502, detail=f"Webex verification API error: {exc}") from exc
    except Exception as exc:
        logger.exception("Post-cutover verification failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Post-cutover verification failed: {exc}")


@router.get("/verification/license-reconciliation", response_model=LicenseReconciliationResponse)
async def get_license_reconciliation(
    cluster: ClusterTarget = ClusterTarget.PRIMARY,
    webex_access_token: Optional[str] = Header(default=None, alias="X-Webex-Access-Token"),
    x_webex_token: Optional[str] = Header(default=None, alias="X-Webex-Token"),
    current_user: CurrentUser = Depends(require_telecom_admin),
):
    """
    Phase 5: Cross-platform licensing audit comparing CUCM Smart Software License reductions
    (reclaimed UCL/CUWL) against active Webex Calling cloud subscription licenses.
    """
    token = (webex_access_token or x_webex_token or "").strip()
    try:
        webex_prov = WebexCallingProvider(access_token=token) if token else WebexCallingProvider()
    except Exception as exc:
        logger.warning("Could not initialize WebexCallingProvider: %s", exc)
        webex_prov = None

    cluster_name = "PRIMARY"
    axl_prov = None
    try:
        cluster_val = cluster.value if hasattr(cluster, "value") else str(cluster)
        env_cluster = ClusterEnvironment(cluster_val.strip().upper())
        cluster_name = env_cluster.value
        axl_prov = TelecomClusterRegistry.get_cucm_provider(env_cluster)
    except Exception as exc:
        logger.warning("Could not initialize CUCM AXL provider for reconciliation: %s", exc)

    if not webex_prov:
        raise HTTPException(status_code=400, detail="Webex provider configuration unavailable.")

    try:
        service = WebexVerificationService(
            webex_provider=webex_prov,
            axl_provider=axl_prov,
            cluster_name=cluster_name,
        )
        return await run_in_threadpool(service.reconcile_licenses)
    except WebexAPIError as exc:
        if str(exc).startswith(("HTTP 401:", "HTTP 403:")):
            raise HTTPException(status_code=401, detail="Webex rejected this token. Paste a valid, unexpired bearer token.") from exc
        logger.exception("Webex license query error: %s", exc)
        raise HTTPException(status_code=502, detail=f"Webex license query error: {exc}") from exc
    except Exception as exc:
        logger.exception("License reconciliation failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"License reconciliation failed: {exc}")


@router.post("/webex-provision", response_model=TaskResponse, status_code=status.HTTP_202_ACCEPTED)
@router.post("/execute-staged-provisioning", response_model=TaskResponse, status_code=status.HTTP_202_ACCEPTED)
async def trigger_webex_provisioning(
    payload: Dict[str, Any],
    webex_access_token: Optional[str] = Header(default=None, alias="X-Webex-Access-Token"),
    current_user: CurrentUser = Depends(require_telecom_admin)
):
    token = (webex_access_token or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Paste your Webex bearer token before provisioning.")

    try:
        task = cast(Task, execute_webex_batch_provisioning_task).apply_async(
            kwargs={
                "cucm_audit_data": payload.get("cucm_audit_data", payload),
                "location_id": payload.get("location_id"),
                "calling_license_id": payload.get("calling_license_id"),
                "webex_access_token": token,
            },
            kwargsrepr="<Webex provisioning payload; bearer token redacted>",
        )
        logger.info("Dispatched Webex Calling Provisioning Task ID '%s'.", task.id)
        return TaskResponse(
            task_id=task.id,
            status="QUEUED",
            message="Webex Calling batch provisioning task successfully queued."
        )
    except Exception as exc:
        logger.exception("Failed to queue Webex Calling provisioning task: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to queue Webex Calling provisioning task: {str(exc)}") from exc


# -----------------------------------------------------------------------------
# UCCX & CUC Celery Migration Tasks
# -----------------------------------------------------------------------------

@router.post("/uccx/audit", response_model=TaskResponse, status_code=status.HTTP_202_ACCEPTED)
@router.post("/uccx-audit", response_model=TaskResponse, status_code=status.HTTP_202_ACCEPTED)
async def trigger_uccx_audit(
    payload: Optional[UCCXAuditRequest] = None,
    default_country_code: str = "+1",
    current_user: CurrentUser = Depends(require_telecom_admin)
):
    
    cluster_str = payload.cluster.value if (payload and hasattr(payload.cluster, "value")) else "PRIMARY"
    country_code = payload.default_country_code if payload else default_country_code

    task = await run_in_threadpool(
        cast(Task, execute_uccx_migration_audit_task).delay,
        cluster=cluster_str,
        default_country_code=country_code
    )
    return TaskResponse(
        task_id=task.id,
        status="QUEUED",
        cluster=cluster_str,
        message=f"UCCX Audit task queued successfully for {cluster_str}."
    )


@router.post("/export-prompts", response_model=TaskResponse, status_code=status.HTTP_202_ACCEPTED)
@router.post("/uccx/export-prompts", response_model=TaskResponse, status_code=status.HTTP_202_ACCEPTED)
@router.post("/uccx-prompt-export", response_model=TaskResponse, status_code=status.HTTP_202_ACCEPTED)
async def trigger_prompt_export(
    payload: ClusterPromptExportRequest,
    current_user: CurrentUser = Depends(require_telecom_admin)
):

    cluster_str = payload.cluster.value if hasattr(payload.cluster, "value") else str(payload.cluster)
    task = await run_in_threadpool(
        cast(Task, execute_uccx_prompt_export_task).delay,
        cluster=cluster_str,
        target_language_folders=payload.languages
    )
    return TaskResponse(
        task_id=task.id,
        status="QUEUED",
        cluster=cluster_str,
        message=f"Prompt export queued for {cluster_str} cluster."
    )


@router.post("/cuc/export", response_model=TaskResponse, status_code=status.HTTP_202_ACCEPTED)
@router.post("/cuc-export", response_model=TaskResponse, status_code=status.HTTP_202_ACCEPTED)
async def trigger_cuc_export(
    payload: Optional[CUCExportRequest] = None,
    current_user: CurrentUser = Depends(require_telecom_admin)
):

    cluster_str = payload.cluster.value if (payload and hasattr(payload.cluster, "value")) else "PRIMARY"
    task = await run_in_threadpool(
        cast(Task, execute_cuc_voicemail_export_task).delay,
        cluster=cluster_str
    )
    return TaskResponse(
        task_id=task.id,
        status="QUEUED",
        cluster=cluster_str,
        message=f"CUC Voicemail export task queued successfully for {cluster_str}."
    )


@router.post("/wxcc/generate-flows", response_model=TaskResponse, status_code=status.HTTP_202_ACCEPTED)
@router.post("/wxcc-flow-generation", response_model=TaskResponse, status_code=status.HTTP_202_ACCEPTED)
async def trigger_flow_generation(
    payload: WxCCFlowGenRequest,
    current_user: CurrentUser = Depends(require_telecom_admin)
):

    task = await run_in_threadpool(
        cast(Task, execute_wxcc_flow_generation_task).delay,
        cuc_export_data=payload.cuc_export_data,
        output_dir=payload.output_dir
    )
    return TaskResponse(
        task_id=task.id,
        status="QUEUED",
        message="WxCC Flow JSON generation task queued."
    )


# -----------------------------------------------------------------------------
# Interactive UCCX Menu-to-Flow Studio Endpoints
# -----------------------------------------------------------------------------

@router.get("/uccx/applications")
@router.get("/uccx-applications")
async def list_uccx_applications(
    cluster: str = "HAWAII",
    current_user: CurrentUser = Depends(require_telecom_admin)
):
    """
    Direct non-blocking discovery of UCCX Applications, Scripts, Triggers, and CSQs.
    Fails fast with 504 Gateway Timeout if the target cluster is unreachable.
    """

    try:
        clean_cluster = cluster.strip().upper()
        env_cluster = ClusterEnvironment(clean_cluster)
        uccx_provider = TelecomClusterRegistry.get_uccx_provider(env_cluster)

        # Offload synchronous network I/O to a worker thread pool
        def fetch_uccx_inventory():
            # Quick 2-second Layer 4 socket check
            if not uccx_provider.is_reachable(port=8443, timeout_sec=2.0):
                raise UCCXConnectionError(
                    f"UCCX node at {uccx_provider.host}:8443 is unreachable. "
                    f"Check WAN/VPN connectivity or verify Cisco Tomcat is running."
                )
            return uccx_provider.get_applications(), uccx_provider.get_csqs()

        applications, csqs = await run_in_threadpool(fetch_uccx_inventory)

        logger.info(f"Discovered {len(applications)} applications and {len(csqs)} CSQs on {env_cluster.value}.")
        return {
            "status": "SUCCESS",
            "cluster": env_cluster.value,
            "total_applications": len(applications),
            "applications": applications,
            "total_csqs": len(csqs),
            "available_csqs": csqs
        }

    except UCCXConnectionError as conn_err:
        logger.warning("UCCX connection failure for cluster %s: %s", cluster, conn_err)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=str(conn_err)
        )

    except Exception as exc:
        logger.exception("Failed querying UCCX applications for cluster %s: %s", cluster, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed querying UCCX applications for {cluster}: {str(exc)}"
        )


@router.post("/uccx/generate-flow")
@router.post("/generate-wxcc-flow")
async def generate_uccx_wxcc_flow(
    payload: GenerateUCCXFlowRequest,
    current_user: CurrentUser = Depends(require_telecom_admin)
):
    """
    Synchronously converts UCCX script routing logic and DTMF parameters
    into an importable WxCC Flow Builder JSON schema.
    """

    try:
        generator = WxCCFlowGeneratorService()
        flow_def: WxCCFlowDefinition = generator.generate_uccx_menu_flow(
            flow_name=payload.flow_name,
            trigger_dn=payload.trigger_dn,
            open_greeting_prompt=payload.open_greeting_prompt,
            closed_greeting_prompt=payload.closed_greeting_prompt,
            menu_dtmf_mappings=payload.menu_dtmf_mappings,
            business_hours_id=payload.business_hours_id,
            default_queue_id=payload.default_queue_id or "WXCC_DEFAULT_QUEUE_ID"
        )

        cluster_str = payload.cluster.strip().upper()
        return {
            "status": "SUCCESS",
            "cluster": cluster_str,
            "flow_name": flow_def.flow_name,
            "total_nodes": len(flow_def.nodes),
            "total_edges": len(flow_def.edges),
            "flow_schema": flow_def.model_dump()
        }
    except Exception as exc:
        logger.exception("WxCC Flow generation failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"WxCC Flow generation failed: {str(exc)}"
        )


@router.post("/uccx/convert-script")
@router.post("/uccx/scripts/convert")
async def convert_uccx_script(
    payload: UCCXScriptConversionRequest,
    current_user: CurrentUser = Depends(require_telecom_admin)
):
    """Convert a supplied or remotely acquired UCCX script into reviewable WxCC JSON."""
    if not payload.script_content and not payload.script_path:
        raise HTTPException(status_code=400, detail="Provide script_content or script_path.")

    try:
        cluster = ClusterEnvironment(payload.cluster.strip().upper())
        provider = TelecomClusterRegistry.get_uccx_provider(cluster)
        service = UCCXScriptMigrationService(uccx_provider=provider)
        content = payload.script_content
        if content is None:
            if payload.script_path is None:
                raise HTTPException(status_code=400, detail="Provide script_content or script_path.")
            content = await run_in_threadpool(service.acquire_script, payload.script_path)
        result = service.convert(payload.script_name, content, payload.trigger_dn)
        return {
            "status": "SUCCESS",
            "cluster": cluster.value,
            "script_name": payload.script_name,
            "conversion": result.model_dump(),
        }
    except (UCCXAPIError, ValueError) as exc:
        logger.warning("UCCX script conversion rejected: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("UCCX script conversion failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"UCCX script conversion failed: {exc}")


@router.get("/uccx/scripts")
async def list_uccx_scripts(
    cluster: str = "HAWAII",
    current_user: CurrentUser = Depends(require_telecom_admin)
):
    """List scripts when the target UCCX deployment exposes a configured inventory endpoint."""
    try:
        env_cluster = ClusterEnvironment(cluster.strip().upper())
        provider = TelecomClusterRegistry.get_uccx_provider(env_cluster)
        scripts = await run_in_threadpool(provider.get_scripts)
        return {"status": "SUCCESS", "cluster": env_cluster.value, "scripts": scripts}
    except (UCCXAPIError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/source-inventory")
@router.post("/migration/source-inventory")
async def collect_source_inventory(
    payload: SourceInventoryRequest,
    current_user: CurrentUser = Depends(require_telecom_admin)
):
    """Collect a normalized CUCM, UCCX, and CUC migration source manifest."""

    try:
        cluster = payload.cluster.value if hasattr(payload.cluster, "value") else str(payload.cluster)
        env_cluster = ClusterEnvironment(cluster.strip().upper())
        cucm_provider = TelecomClusterRegistry.get_cucm_provider(env_cluster)
        uccx_provider = TelecomClusterRegistry.get_uccx_provider(env_cluster)
        cuc_provider = TelecomClusterRegistry.get_cupi_provider(env_cluster)
        service = SourceInventoryService(cucm_provider, uccx_provider, cuc_provider)
        manifest = await run_in_threadpool(
            service.collect,
            env_cluster.value,
            payload.default_country_code,
            payload.device_scope,
            payload.device_pool_filter,
            payload.cuc_page_size,
        )
        return {"status": "SUCCESS", "inventory": manifest.model_dump()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Source inventory collection failed for cluster %s", payload.cluster)
        raise HTTPException(status_code=500, detail=f"Source inventory collection failed: {exc}")


@router.post("/migration/stage-csvs")
@router.post("/source-inventory/stage")
async def stage_migration_csvs(
    payload: MigrationStagingRequest,
    current_user: CurrentUser = Depends(require_telecom_admin)
):
    """Validate vendor Webex CSV templates against a source inventory manifest."""
    try:
        service = MigrationStagingService()
        result = await run_in_threadpool(
            service.stage,
            payload.source_manifest,
            Path(payload.csv_directory),
        )
        return {
            "status": "SUCCESS",
            "ready_rows": result.ready_rows,
            "exception_count": len(result.exceptions),
            "staging": result.model_dump(),
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Migration CSV staging failed")
        raise HTTPException(status_code=500, detail=f"Migration CSV staging failed: {exc}")


@router.post("/migration/provision-webex")
@router.post("/migration/provision")
async def provision_webex_migration(
    payload: WebexMigrationProvisionRequest,
    webex_access_token: Optional[str] = Header(default=None, alias="X-Webex-Access-Token"),
    current_user: CurrentUser = Depends(require_telecom_admin)
):
    """Provision reviewed migration rows into Webex Calling; dry-run is the default."""
    token = (webex_access_token or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Paste your Webex bearer token before provisioning.")

    try:
        service = WebexMigrationProvisionService(WebexCallingProvider(access_token=token))
        summary = await run_in_threadpool(
            service.provision,
            payload.staging,
            payload.approved,
            payload.dry_run,
            payload.calling_license_id or os.getenv("WEBEX_CALLING_LICENSE_ID", ""),
        )
        return {"status": "SUCCESS", "provisioning": summary.model_dump()}
    except WebexAPIError as exc:
        if str(exc).startswith(("HTTP 401:", "HTTP 403:")):
            raise HTTPException(status_code=401, detail="Webex rejected this token. Paste a valid, unexpired bearer token.") from exc
        logger.exception("Webex API migration provisioning failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"Webex API migration provisioning failed: {str(exc)}") from exc
    except Exception as exc:
        logger.exception("Webex migration provisioning failed")
        raise HTTPException(status_code=500, detail=f"Webex migration provisioning failed: {exc}") from exc


# -----------------------------------------------------------------------------
# Webex Calling Workspace & Device Import (Vendor Postman Alternative)
# -----------------------------------------------------------------------------

@router.get("/webex/licenses")
async def get_webex_licenses(
    webex_access_token: Optional[str] = Header(default=None, alias="X-Webex-Access-Token"),
    current_user: CurrentUser = Depends(require_telecom_admin)
):
    """Retrieves all Webex organization licenses and identifies workspace calling licenses."""
    token = (webex_access_token or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Paste your Webex bearer token to load calling licenses.")

    try:
        provider = WebexCallingProvider(access_token=token)
        if not provider.access_token:
            logger.error("Webex Calling Provider initialized with empty access token.")
            raise HTTPException(status_code=401, detail="Webex bearer token is empty.")

        licenses = await run_in_threadpool(provider.get_licenses)
        workspace_licenses = await run_in_threadpool(provider.get_workspace_calling_licenses)
        user_licenses = await run_in_threadpool(provider.get_user_calling_licenses)
        ws_ids = {lic.get("id") for lic in workspace_licenses}
        user_ids = {lic.get("id") for lic in user_licenses}

        return {
            "status": "SUCCESS",
            "total_licenses": len(licenses),
            "licenses": [
                {
                    "id": lic.get("id"),
                    "name": lic.get("name"),
                    "totalUnits": lic.get("totalUnits", 0),
                    "consumedUnits": lic.get("consumedUnits", 0),
                    "is_workspace_eligible": lic.get("id") in ws_ids,
                    "is_user_eligible": lic.get("id") in user_ids,
                }
                for lic in licenses
            ]
        }
    except WebexAPIError as exc:
        if str(exc).startswith(("HTTP 401:", "HTTP 403:")):
            raise HTTPException(status_code=401, detail="Webex rejected this token. Paste a valid, unexpired bearer token.") from exc
        logger.exception("Failed retrieving Webex licenses: %s", exc)
        raise HTTPException(status_code=502, detail=f"Failed retrieving Webex licenses: {str(exc)}") from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed retrieving Webex licenses: %s", exc)
        raise HTTPException(status_code=502, detail=f"Failed retrieving Webex licenses: {str(exc)}") from exc


@router.get("/workspaces/sample-csv")
async def get_sample_workspace_csv(
    preset: str = "sample",
    current_user: CurrentUser = Depends(require_telecom_admin)
):
    """Returns sample vendor or clean template CSV content."""
    preset_files = {
        "sample": ("imports/migration_csv/sample_vendor_workspaces.csv", "sample_vendor_workspaces.csv"),
        "template": ("imports/migration_csv/templates/workspaces_template.csv", "sample_webex_workspaces_template.csv"),
        "vendor": ("imports/migration_csv/webex_workspaces_import.csv", "webex_workspaces_import.csv"),
    }
    file_rel, filename = preset_files.get(preset.lower(), preset_files["sample"])
    sample_path = PROJECT_ROOT / file_rel
    if os.path.exists(sample_path):
        with open(sample_path, "r", encoding="utf-8-sig") as f:
            content = f.read()
        return {"status": "SUCCESS", "csv_content": content, "filename": filename}
    raise HTTPException(status_code=404, detail="Sample CSV not found.")


@router.get("/migration/templates/{template_type}")
async def download_migration_template(
    template_type: str,
    current_user: CurrentUser = Depends(require_telecom_admin)
):
    """
    Downloads clean sample CSV templates for Webex Calling migration phases:
    - 'workspaces': Clean workspace & MPP device template
    - 'users': Clean calling user & MPP phone template
    - 'locations': Clean Webex location provisioning template
    - 'vendor_workspaces': Original vendor workspace format
    """
    templates_map = {
        "workspaces": ("imports/migration_csv/templates/workspaces_template.csv", "sample_webex_workspaces_template.csv"),
        "users": ("imports/migration_csv/templates/calling_users_template.csv", "sample_webex_calling_users_template.csv"),
        "locations": ("imports/migration_csv/templates/locations_template.csv", "sample_webex_locations_template.csv"),
        "vendor_workspaces": ("imports/migration_csv/webex_workspaces_import.csv", "vendor_webex_workspaces_import.csv"),
    }

    entry = templates_map.get(template_type.lower())
    if not entry:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown template type '{template_type}'. Available: {list(templates_map.keys())}"
        )

    file_path, download_name = entry
    abs_path = PROJECT_ROOT / file_path
    if not os.path.exists(abs_path):
        raise HTTPException(status_code=404, detail="Template file not found.")

    return FileResponse(
        path=str(abs_path),
        media_type="text/csv",
        filename=download_name,
        headers={"Content-Disposition": f'attachment; filename="{download_name}"'}
    )


@router.post("/workspaces/validate-csv")
async def validate_workspace_csv(
    payload: WorkspaceCsvValidationRequest,
    webex_access_token: Optional[str] = Header(default=None, alias="X-Webex-Access-Token"),
    current_user: CurrentUser = Depends(require_telecom_admin)
):
    """Validates CSV input against Webex Locations and Licenses, flagging syntax, duplicates, and model tiers."""
    try:
        csv_text = payload.csv_content
        if payload.use_sample:
            sample_path = PROJECT_ROOT / "imports/migration_csv/sample_vendor_workspaces.csv"
            if os.path.exists(sample_path):
                with open(sample_path, "r", encoding="utf-8") as f:
                    csv_text = f.read()
        elif payload.file_path:
            clean_path = os.path.abspath(payload.file_path)
            if os.path.exists(clean_path):
                with open(clean_path, "r", encoding="utf-8-sig") as f:
                    csv_text = f.read()

        if not csv_text or not csv_text.strip():
            raise HTTPException(status_code=400, detail="No CSV content provided.")

        token = (webex_access_token or "").strip()
        if not token:
            raise HTTPException(status_code=401, detail="Paste your Webex bearer token before validating the CSV.")
        service = WebexWorkspaceImportService(WebexCallingProvider(access_token=token))
        raw_rows = service.parse_csv(csv_text)
        summary = await run_in_threadpool(service.validate_batch, raw_rows)
        return {"status": "SUCCESS", "validation": summary.model_dump()}
    except WebexAPIError as exc:
        if str(exc).startswith(("HTTP 401:", "HTTP 403:")):
            raise HTTPException(status_code=401, detail="Webex rejected this token. Paste a valid, unexpired bearer token.") from exc
        logger.exception("Webex API CSV validation failure: %s", exc)
        raise HTTPException(status_code=502, detail=f"Webex API CSV validation failure: {str(exc)}") from exc
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("CSV validation failure: %s", exc)
        raise HTTPException(status_code=500, detail=f"CSV validation failure: {str(exc)}")


@router.post("/workspaces/import")
async def import_workspace_batch(
    payload: WorkspaceBatchImportRequest,
    webex_access_token: Optional[str] = Header(default=None, alias="X-Webex-Access-Token"),
    current_user: CurrentUser = Depends(require_telecom_admin)
):
    """
    Executes batch import into Webex Calling.
    dry_run=True validates payloads without mutations.
    dry_run=False provisions workspaces (POST /v1/workspaces) and associates MPP devices (POST /v1/devices).
    """

    try:
        token = (webex_access_token or "").strip()
        if not token:
            raise HTTPException(status_code=401, detail="Paste your Webex bearer token before importing.")
        if not payload.dry_run:
            await require_workspace_step_up(
                payload.step_up_user,
                payload.step_up_pass,
                "Webex Workspace Live Import",
            )
        service = WebexWorkspaceImportService(WebexCallingProvider(access_token=token))
        result = await run_in_threadpool(
            service.execute_batch,
            payload.rows,
            None,
            payload.dry_run,
            payload.calling_license_id
        )
        return {"status": "SUCCESS", "execution": result.model_dump()}
    except WebexAPIError as exc:
        if str(exc).startswith(("HTTP 401:", "HTTP 403:")):
            raise HTTPException(status_code=401, detail="Webex rejected this token. Paste a valid, unexpired bearer token.") from exc
        logger.exception("Webex API workspace batch import failure: %s", exc)
        raise HTTPException(status_code=502, detail=f"Webex API workspace batch import failure: {str(exc)}") from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Workspace batch import execution failure: %s", exc)
        raise HTTPException(status_code=500, detail=f"Workspace batch import execution failure: {str(exc)}")


@router.post("/workspaces/correct-locations")
async def correct_workspace_locations(
    payload: WorkspaceLocationCorrectionRequest,
    webex_access_token: Optional[str] = Header(default=None, alias="X-Webex-Access-Token")
):
    """Previews or migrates workspaces and their MPP devices to corrected locations."""
    token = (webex_access_token or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Paste your Webex bearer token to continue.")
    if not payload.dry_run:
        await require_workspace_step_up(
            payload.step_up_user,
            payload.step_up_pass,
            "Webex Workspace Location Migration",
        )

    try:
        provider = WebexCallingProvider(access_token=token)
        service = WebexWorkspaceLocationService(provider)
        result = await run_in_threadpool(
            service.correct_locations,
            payload.csv_content,
            payload.dry_run,
        )
        return {"status": "SUCCESS", "execution": result}
    except WebexAPIError as exc:
        if str(exc).startswith(("HTTP 401:", "HTTP 403:")):
            raise HTTPException(
                status_code=401,
                detail="Webex rejected this token. Paste a valid, unexpired bearer token with workspace and device access.",
            ) from exc
        logger.exception("Webex API rejected workspace location migration: %s", exc)
        raise HTTPException(status_code=502, detail=f"Webex API workspace location migration failure: {str(exc)}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Workspace location migration failure: %s", exc)
        raise HTTPException(status_code=500, detail=f"Workspace location migration failure: {str(exc)}")


@router.post("/workspaces/recover-device-assignments")
async def recover_workspace_device_assignments(
    payload: WorkspaceLocationCorrectionRequest,
    webex_access_token: Optional[str] = Header(default=None, alias="X-Webex-Access-Token")
):
    """Previews or restores detached MPP device assignments to their original workspaces."""
    token = (webex_access_token or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Paste your Webex bearer token to continue.")
    if not payload.dry_run:
        await require_workspace_step_up(
            payload.step_up_user,
            payload.step_up_pass,
            "Webex Workspace Device Recovery",
        )

    try:
        provider = WebexCallingProvider(access_token=token)
        service = WebexWorkspaceLocationService(provider)
        result = await run_in_threadpool(
            service.recover_device_assignments,
            payload.csv_content,
            payload.dry_run,
        )
        return {"status": "SUCCESS", "execution": result}
    except WebexAPIError as exc:
        if str(exc).startswith(("HTTP 401:", "HTTP 403:")):
            raise HTTPException(
                status_code=401,
                detail="Webex rejected this token. Paste a valid, unexpired bearer token with workspace and device access.",
            ) from exc
        logger.exception("Webex API rejected device assignment recovery: %s", exc)
        raise HTTPException(status_code=502, detail=f"Webex API device assignment recovery failure: {str(exc)}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Workspace device assignment recovery failure: %s", exc)
        raise HTTPException(status_code=500, detail=f"Workspace device assignment recovery failure: {str(exc)}")


@router.post("/workspaces/export-postman")
async def export_workspace_postman_collection(
    payload: WorkspaceBatchImportRequest,
    current_user: CurrentUser = Depends(require_telecom_admin),
    x_webex_token: Optional[str] = Header(None, alias="X-Webex-Token"),
):
    """Generates a downloadable Postman Collection v2.1 JSON replicating the vendor's automation."""
    try:
        webex_prov = WebexCallingProvider(access_token=x_webex_token) if x_webex_token else None
        service = WebexWorkspaceImportService(webex_provider=webex_prov)
        collection = service.generate_postman_collection(payload.rows)
        return {"status": "SUCCESS", "collection": collection}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed generating Postman collection: {str(exc)}")


# -----------------------------------------------------------------------------
# Universal Task Status Polling
# -----------------------------------------------------------------------------

@router.get("/tasks/{task_id}")
@router.get("/cucm-audit/status/{task_id}")
@router.get("/webex-provision/status/{task_id}")
@router.get("/uccx-audit/status/{task_id}")
@router.get("/cuc-export/status/{task_id}")
async def get_universal_telecom_task_status(
    task_id: str,
    current_user: CurrentUser = Depends(require_telecom_admin)
):
    res = AsyncResult(task_id, app=celery_app)

    if res.state in ["PENDING", "RECEIVED"]:
        return {
            "task_id": task_id,
            "status": "PENDING",
            "progress": 10,
            "message": "Task queued in Celery worker pool..."
        }
    elif res.state == "PROGRESS":
        meta = res.info or {}
        return {
            "task_id": task_id,
            "status": "RUNNING",
            "progress": meta.get("progress", 50),
            "message": meta.get("message", "Processing migration task...")
        }
    elif res.state == "SUCCESS":
        result_data = res.result or {}
        if isinstance(result_data, dict) and "status" in result_data:
            return result_data
        return {
            "task_id": task_id,
            "status": "COMPLETED",
            "progress": 100,
            "message": "Operation completed successfully.",
            "data": result_data
        }
    elif res.state in ["FAILURE", "REVOKED"]:
        err_msg = str(res.info or res.result or "Task failed during execution.")
        return {
            "task_id": task_id,
            "status": "FAILED",
            "progress": 100,
            "message": err_msg
        }

    return {"task_id": task_id, "status": res.state, "progress": 50, "message": "Processing..."}