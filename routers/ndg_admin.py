"""
routers/ndg_admin.py

Cisco ISE Network Device Group (NDG) Compliance Audit, Live Synchronization,
Zero-Touch Provisioning (ZTP), and Task Status Polling Router.
"""

import logging
import uuid
from typing import Any, Dict, Optional

from celery.result import AsyncResult
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from celery_app import celery_app
from core.dependencies import get_ise_provider, get_meraki_provider
from models.ndg import (
    TargetedAuditRequest,
    TargetedSyncRequest,
    ZtpProvisionRequest,
)
from providers.ise_provider import ISEProvider
from providers.meraki_provider import MerakiProvider
from security.auth import (
    CurrentUser,
    enforce_routing_auth,
    get_current_user,
)
from security.roles import Role
from services.ndg_audit_service import (
    ISENetworkDeviceClient,
    NDGAuditConfig,
    NDGAuditService,
)
from services.ztp_provisioning_service import ZtpProvisioningService

logger = logging.getLogger("Net_Dashboard.NDGAdmin")
router = APIRouter(tags=["ISE NDG Administration & ZTP"])

# In-memory tracking for background audit and sync tasks
active_tasks: Dict[str, Dict[str, Any]] = {}


@router.get("/api/v1/admin/ise-ndg-options", dependencies=[Depends(enforce_routing_auth)])
async def get_ise_ndg_options(
    ise_provider: ISEProvider = Depends(get_ise_provider),
):
    """Retrieves available ISE NDG configuration options and templates."""
    if not ise_provider:
        raise HTTPException(status_code=503, detail="ISE provider uninitialized.")
    return ise_provider.get_meraki_ndg_options()


@router.post("/api/v1/admin/ise-ndg-audit", dependencies=[Depends(enforce_routing_auth)])
async def run_ndg_audit(
    payload: TargetedAuditRequest,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(get_current_user),
    meraki_provider: Optional[MerakiProvider] = Depends(get_meraki_provider),
):
    """Schedules a dry-run NDG compliance audit comparing Meraki network inventory against ISE."""
    if current_user.role not in [Role.NETWORK_ADMIN, Role.LOCAL_TECH_SERVICES]:
        raise HTTPException(status_code=403, detail="Unauthorized.")
    if not meraki_provider:
        raise HTTPException(status_code=503, detail="Meraki provider uninitialized.")

    task_id = str(uuid.uuid4())
    active_tasks[task_id] = {"status": "INITIALIZED", "progress": 0, "message": "Queueing targeted audit..."}

    def bg_audit_runner(tid: str, audit_params: TargetedAuditRequest):
        def update_progress(pct: int, msg: str):
            if tid in active_tasks:
                active_tasks[tid]["progress"] = pct
                active_tasks[tid]["message"] = msg

        try:
            active_tasks[tid]["status"] = "RUNNING"
            config = NDGAuditConfig.from_env()
            config.target_template_id = audit_params.template_id
            config.hardware_filter = int(audit_params.hardware_filter)
            config.gateway_type = audit_params.gateway_type
            config.gateway_designation = audit_params.gateway_designation
            config.deployment_stage = audit_params.deployment_stage

            audit_service = NDGAuditService(meraki_provider=meraki_provider, config=config)
            audit_service.run_audit(dry_run=True, apply_updates=False, progress_callback=update_progress)
            active_tasks[tid] = {"status": "COMPLETED", "progress": 100, "message": "Audit complete!"}
        except Exception as exc:
            active_tasks[tid] = {"status": "FAILED", "progress": 100, "message": str(exc)}

    background_tasks.add_task(bg_audit_runner, task_id, payload)
    return {"status": "QUEUED", "task_id": task_id, "message": "Targeted compliance audit scheduled."}


@router.post("/api/v1/admin/ise-ndg-sync", dependencies=[Depends(enforce_routing_auth)])
async def run_ndg_sync(
    payload: TargetedSyncRequest,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(get_current_user),
    meraki_provider: Optional[MerakiProvider] = Depends(get_meraki_provider),
):
    """Schedules a live deployment synchronization pushing Meraki network device settings into ISE."""
    if current_user.role != Role.NETWORK_ADMIN:
        raise HTTPException(status_code=403, detail="Unauthorized.")
    if not meraki_provider:
        raise HTTPException(status_code=503, detail="Meraki provider uninitialized.")

    task_id = str(uuid.uuid4())
    active_tasks[task_id] = {"status": "INITIALIZED", "progress": 0, "message": "Queueing live deployment sync..."}

    def bg_sync_runner(tid: str, audit_params: TargetedSyncRequest):
        def update_progress(pct: int, msg: str):
            if tid in active_tasks:
                active_tasks[tid]["progress"] = pct
                active_tasks[tid]["message"] = msg

        try:
            active_tasks[tid]["status"] = "RUNNING"
            config = NDGAuditConfig.from_env()
            config.target_template_id = audit_params.template_id
            config.hardware_filter = int(audit_params.hardware_filter)
            config.gateway_type = audit_params.gateway_type
            config.gateway_designation = audit_params.gateway_designation
            config.deployment_stage = audit_params.deployment_stage

            audit_service = NDGAuditService(meraki_provider=meraki_provider, config=config)
            audit_service.run_audit(dry_run=False, apply_updates=True, progress_callback=update_progress)
            active_tasks[tid] = {"status": "COMPLETED", "progress": 100, "message": "Live Sync Complete!"}
        except Exception as exc:
            active_tasks[tid] = {"status": "FAILED", "progress": 100, "message": f"Deployment aborted: {str(exc)}"}

    background_tasks.add_task(bg_sync_runner, task_id, payload)
    return {"status": "QUEUED", "task_id": task_id, "message": "Live deployment authorized."}


@router.post("/api/v1/admin/ztp-provision", dependencies=[Depends(enforce_routing_auth)])
async def run_ztp_provision(
    payload: ZtpProvisionRequest,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(get_current_user),
    meraki_provider: Optional[MerakiProvider] = Depends(get_meraki_provider),
):
    """Executes end-to-end Zero-Touch Provisioning pipeline binding Meraki network to template and registering in ISE."""
    if current_user.role != Role.NETWORK_ADMIN:
        raise HTTPException(status_code=403, detail="Unauthorized.")
    if not meraki_provider:
        raise HTTPException(status_code=503, detail="Meraki provider uninitialized.")

    task_id = str(uuid.uuid4())
    active_tasks[task_id] = {"status": "INITIALIZED", "progress": 0, "message": "Initializing ZTP pipeline..."}

    def bg_ztp_runner(tid: str, ztp_params: ZtpProvisionRequest):
        def update_progress(pct: int, msg: str):
            if tid in active_tasks:
                active_tasks[tid]["progress"] = pct
                active_tasks[tid]["message"] = msg

        try:
            active_tasks[tid]["status"] = "RUNNING"
            config = NDGAuditConfig.from_env()
            config.gateway_type = ztp_params.gateway_type
            config.gateway_designation = ztp_params.gateway_designation
            config.deployment_stage = ztp_params.deployment_stage

            ise_client = ISENetworkDeviceClient(config)
            ztp_service = ZtpProvisioningService(meraki_provider=meraki_provider, ise_client=ise_client)
            ztp_service.run_ztp_pipeline(payload=ztp_params, progress_callback=update_progress)
            active_tasks[tid] = {"status": "COMPLETED", "progress": 100, "message": "ZTP Complete!"}
        except Exception as exc:
            active_tasks[tid] = {"status": "FAILED", "progress": 100, "message": str(exc)}

    background_tasks.add_task(bg_ztp_runner, task_id, payload)
    return {"status": "QUEUED", "task_id": task_id, "message": "ZTP pipeline authorized."}


@router.get("/api/v1/admin/ise-ndg-audit/status/{task_id}", dependencies=[Depends(enforce_routing_auth)])
async def get_audit_status(task_id: str):
    """Polls real-time execution status and progress for in-memory and Celery background tasks."""
    # 1. Check in-memory background tasks first (NDG audit, NDG sync, ZTP)
    if task_id in active_tasks:
        return active_tasks[task_id]

    # 2. Check Celery task results (e.g. Meraki appliance swap)
    try:
        task_result = AsyncResult(task_id, app=celery_app)
        if task_result.state == "PROGRESS":
            return {
                "status": "RUNNING",
                "progress": (task_result.info or {}).get("progress", 0),
                "message": (task_result.info or {}).get("message", ""),
            }
        elif task_result.state == "SUCCESS":
            return {
                "status": "COMPLETED",
                "progress": 100,
                "message": "Operation completed.",
                "details": task_result.result,
            }
        elif task_result.state == "FAILURE":
            return {
                "status": "FAILED",
                "progress": 100,
                "message": str(task_result.info),
            }
    except Exception as exc:
        logger.warning("Error resolving Celery task state for %s: %s", task_id, exc)

    # 3. If neither has the task, it was from a previous server session or invalid; fail to stop client polling
    return {
        "status": "FAILED",
        "progress": 0,
        "message": "Task expired or not found. Please re-run the action.",
    }
