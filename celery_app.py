# celery_app.py
"""
Central Celery Application and Asynchronous Task Registry.
Handles dynamic broker fallback (Redis vs. Embedded SQLite) and defines all
background migration tasks for CUCM 11.5/12.5, UCCX 12.0/12.5, CUC 11.5/14, Webex Calling, and WxCC.
"""

import os
import socket
import logging
from typing import Optional
from urllib.parse import urlparse
from dotenv import load_dotenv
from celery import Celery

load_dotenv(override=True)
logger = logging.getLogger("Celery_Worker")


# -----------------------------------------------------------------------------
# Dynamic Broker & Result Backend Auto-Detection
# -----------------------------------------------------------------------------

def is_redis_available(broker_url: Optional[str] = None) -> bool:
    """Non-blocking socket check to verify if configured Redis/Valkey broker is listening."""
    raw_url = broker_url or os.getenv("VALKEY_URL") or os.getenv("REDIS_URL") or "redis://127.0.0.1:6379/0"
    try:
        parsed = urlparse(raw_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 6379
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            return sock.connect_ex((host, port)) == 0
    except Exception as e:
        logger.warning("Error probing Redis broker at %s: %s", raw_url, e)
        return False


CONFIGURED_BROKER = os.getenv("VALKEY_URL") or os.getenv("REDIS_URL") or "redis://127.0.0.1:6379/0"
current_env = os.getenv("ENVIRONMENT", "DEV").upper()

if is_redis_available(CONFIGURED_BROKER):
    BROKER_URL = CONFIGURED_BROKER
    BACKEND_URL = BROKER_URL
    logger.info("Redis/Valkey broker verified on %s.", BROKER_URL)
else:
    if current_env == "PROD":
        raise RuntimeError(
            f"CRITICAL STARTUP FAILURE: Redis/Valkey message broker at '{CONFIGURED_BROKER}' is unreachable. "
            "Embedded SQLite broker fallback is strictly prohibited in PRODUCTION environments. "
            "Please ensure Redis/Valkey service is running and accessible."
        )
    DB_PATH = os.path.abspath("celery_backend.sqlite")
    BROKER_URL = f"sqla+sqlite:///{DB_PATH}"
    BACKEND_URL = f"db+sqlite:///{DB_PATH}"
    logger.warning("Redis/Valkey not detected at %s. Using embedded SQLite broker/backend: %s", CONFIGURED_BROKER, BROKER_URL)

celery_app = Celery(
    "net_dashboard_tasks",
    broker=BROKER_URL,
    backend=BACKEND_URL
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=1800,
    broker_connection_retry_on_startup=True,
    task_store_eager_result=True,
)

eager_env = os.getenv("CELERY_ALWAYS_EAGER")
if eager_env is not None:
    is_eager = eager_env.strip().lower() in {"true", "1", "yes"}
else:
    is_eager = current_env in {"DEV", "LOCAL", "TEST"}

if is_eager:
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
    celery_app.conf.task_store_eager_result = True
    logger.info("Celery operating in eager mode (task_always_eager=True) for environment '%s'.", current_env)


# -----------------------------------------------------------------------------
# Provider & Service Dependencies
# -----------------------------------------------------------------------------

from providers.meraki_provider import MerakiProvider
from services.appliance_swap_service import ApplianceSwapService
from providers.webex_calling_provider import WebexCallingProvider
from services.webex_provision_service import WebexProvisionService
from providers.telecom_factory import TelecomClusterRegistry, ClusterEnvironment
from services.cucm_audit_service import CUCMAuditService
from services.uccx_migration_service import UCCXMigrationService
from services.uccx_prompt_export_service import UCCXPromptExportService
from services.voicemail_export_service import VoicemailExportService
from services.wxcc_flow_generator_service import WxCCFlowGeneratorService


# -----------------------------------------------------------------------------
# Task Definitions (Cluster-Aware & Backward Compatible)
# -----------------------------------------------------------------------------

@celery_app.task(bind=True, name="tasks.execute_meraki_appliance_swap")
def execute_meraki_appliance_swap_task(self, network_id: str, old_serial: str, new_serial: str):
    def update_progress(pct: int, msg: str):
        logger.info(f"[Task {self.request.id}] {pct}% - {msg}")
        self.update_state(state="PROGRESS", meta={"progress": pct, "message": msg})

    try:
        meraki_provider = MerakiProvider(
            api_key=os.getenv("MERAKI_API_KEY", ""),
            organization_id=os.getenv("MERAKI_ORG_ID", "")
        )
        swap_service = ApplianceSwapService(meraki_provider=meraki_provider)
        results = swap_service.execute_appliance_swap(
            network_id=network_id, old_serial=old_serial, new_serial=new_serial, progress_callback=update_progress
        )
        return {
            "status": "COMPLETED",
            "progress": 100,
            "message": f"Swap Complete! Replaced {old_serial} with {new_serial} in {results['network_name']}.",
            "details": results
        }
    except Exception as exc:
        logger.exception(f"Appliance swap task failed: {exc}")
        self.update_state(state="FAILURE", meta={"progress": 100, "message": f"Swap Aborted: {str(exc)}"})
        raise exc


@celery_app.task(bind=True, name="tasks.execute_webex_batch_provisioning")
def execute_webex_batch_provisioning_task(
    self,
    cucm_audit_data: dict,
    location_id: str = None,
    calling_license_id: str = None,
    webex_access_token: str = None
):
    def update_progress(pct: int, msg: str):
        logger.info(f"[Webex Provision Task {self.request.id}] {pct}% - {msg}")
        self.update_state(state="PROGRESS", meta={"progress": pct, "message": msg})

    try:
        update_progress(10, "Initializing Webex Control Hub Provider...")
        webex_provider = WebexCallingProvider(access_token=webex_access_token)
        provision_service = WebexProvisionService(webex_provider=webex_provider)

        update_progress(30, "Pushing calling profiles and Tier 1 devices to Control Hub...")
        summary = provision_service.process_cucm_audit_batch(
            cucm_audit_data=cucm_audit_data,
            location_id=location_id,
            calling_license_id=calling_license_id
        )

        update_progress(100, "Webex Calling batch provisioning complete.")
        return {
            "status": "COMPLETED",
            "progress": 100,
            "message": f"Successfully provisioned {summary.successful_count} Webex profiles ({summary.failed_count} failed, {summary.skipped_count} skipped).",
            "data": summary.model_dump()
        }
    except Exception as exc:
        logger.exception(f"Webex batch provisioning task failed: {exc}")
        self.update_state(state="FAILURE", meta={"progress": 100, "message": f"Provisioning Aborted: {str(exc)}"})
        raise exc


@celery_app.task(bind=True, name="tasks.execute_cucm_migration_audit")
def execute_cucm_migration_audit_task(
    self,
    cluster: str = "PRIMARY",
    default_country_code: str = "+1",
    device_scope: str = "PHYSICAL_ONLY",
    device_pool_filter: str = "ALL"
):
    def update_progress(pct: int, msg: str):
        logger.info(f"[{cluster} CUCM Audit {self.request.id}] {pct}% - {msg}")
        self.update_state(state="PROGRESS", meta={"progress": pct, "message": msg})

    try:
        env_cluster = ClusterEnvironment(cluster.upper())
        update_progress(10, f"Connecting to {env_cluster.value} CUCM...")
        
        axl_provider = TelecomClusterRegistry.get_cucm_provider(env_cluster)
        audit_service = CUCMAuditService(axl_provider=axl_provider)

        pool_filter = None if device_pool_filter == "ALL" else device_pool_filter
        summary = audit_service.run_full_migration_audit(
            default_country_code=default_country_code,
            device_scope=device_scope,
            device_pool_filter=pool_filter
        )

        update_progress(100, f"{env_cluster.value} CUCM audit complete.")
        return {
            "status": "COMPLETED",
            "cluster": env_cluster.value,
            "progress": 100,
            "message": f"Discovered {summary.total_devices} devices and {summary.total_lines} lines on {env_cluster.value}.",
            "data": summary.model_dump()
        }
    except Exception as exc:
        logger.exception(f"CUCM audit task for {cluster} failed: {exc}")
        self.update_state(state="FAILURE", meta={"progress": 100, "message": str(exc)})
        raise exc


@celery_app.task(bind=True, name="tasks.execute_uccx_migration_audit")
def execute_uccx_migration_audit_task(self, cluster: str = "PRIMARY", default_country_code: str = "+1"):
    def update_progress(pct: int, msg: str):
        logger.info(f"[{cluster} UCCX Audit {self.request.id}] {pct}% - {msg}")
        self.update_state(state="PROGRESS", meta={"progress": pct, "message": msg})

    try:
        env_cluster = ClusterEnvironment(cluster.upper())
        update_progress(10, f"Connecting to {env_cluster.value} UCCX Admin REST API...")
        
        uccx_provider = TelecomClusterRegistry.get_uccx_provider(env_cluster)
        migration_service = UCCXMigrationService(uccx_provider=uccx_provider)

        update_progress(40, f"Extracting CSQs, Skills, Agents on {env_cluster.value}...")
        summary = migration_service.run_full_contact_center_audit(default_country_code=default_country_code)

        update_progress(100, f"{env_cluster.value} UCCX Audit complete.")
        return {
            "status": "COMPLETED",
            "cluster": env_cluster.value,
            "progress": 100,
            "message": f"Audit complete! Discovered {summary.total_csqs} CSQs, {summary.total_skills} Skills, {summary.total_agents} Agents.",
            "data": summary.model_dump()
        }
    except Exception as exc:
        logger.exception(f"UCCX migration audit task failed: {exc}")
        self.update_state(state="FAILURE", meta={"progress": 100, "message": f"Audit Aborted: {str(exc)}"})
        raise exc


@celery_app.task(bind=True, name="tasks.execute_uccx_prompt_export")
def execute_uccx_prompt_export_task(
    self,
    cluster: str = "PRIMARY",
    target_language_folders: list = None,
    output_dir: str = None
):
    def update_progress(pct: int, msg: str):
        logger.info(f"[{cluster} Prompt Export {self.request.id}] {pct}% - {msg}")
        self.update_state(state="PROGRESS", meta={"progress": pct, "message": msg})

    try:
        env_cluster = ClusterEnvironment(cluster.upper())
        update_progress(10, f"Connecting to {env_cluster.value} Prompt Repository...")
        
        uccx_provider = TelecomClusterRegistry.get_uccx_provider(env_cluster)
        export_service = UCCXPromptExportService(uccx_provider=uccx_provider)

        langs = target_language_folders or ["en_US", "es_US"]
        base_export_dir = os.getenv("UCCX_PROMPT_EXPORT_DIR", "media/uccx_prompts")
        dest_dir = output_dir or os.path.join(base_export_dir, env_cluster.value.lower())

        update_progress(30, f"Crawling and downloading prompts for {langs} into '{dest_dir}'...")
        summary = export_service.export_prompts_recursively(
            target_language_folders=langs,
            output_dir=dest_dir
        )

        update_progress(100, f"{env_cluster.value} Prompt Export complete.")
        return {
            "status": "COMPLETED",
            "cluster": env_cluster.value,
            "progress": 100,
            "message": f"Export complete! Downloaded {summary.total_downloaded} prompts ({summary.total_megabytes_downloaded} MB, {summary.total_compliant} WxCC-compliant).",
            "data": summary.model_dump()
        }
    except Exception as exc:
        logger.exception(f"UCCX prompt export task failed: {exc}")
        self.update_state(state="FAILURE", meta={"progress": 100, "message": f"Export Aborted: {str(exc)}"})
        raise exc


@celery_app.task(bind=True, name="tasks.execute_cuc_voicemail_export")
def execute_cuc_voicemail_export_task(self, cluster: str = "PRIMARY"):
    def update_progress(pct: int, msg: str):
        logger.info(f"[{cluster} CUC Export {self.request.id}] {pct}% - {msg}")
        self.update_state(state="PROGRESS", meta={"progress": pct, "message": msg})

    try:
        env_cluster = ClusterEnvironment(cluster.upper())
        update_progress(10, f"Initializing {env_cluster.value} CUPI Provider...")
        
        cupi_provider = TelecomClusterRegistry.get_cupi_provider(env_cluster)
        export_service = VoicemailExportService(cupi_provider=cupi_provider)

        update_progress(30, f"Extracting mailboxes and call handlers from {env_cluster.value}...")
        summary = export_service.run_full_voicemail_export()

        update_progress(100, f"{env_cluster.value} Voicemail export complete.")
        return {
            "status": "COMPLETED",
            "cluster": env_cluster.value,
            "progress": 100,
            "message": f"Exported {summary.total_voicemail_users} users, {summary.total_call_handlers} call handlers, and {summary.total_prompts_downloaded} prompts.",
            "data": summary.model_dump()
        }
    except Exception as exc:
        logger.exception(f"CUC voicemail export task failed: {exc}")
        self.update_state(state="FAILURE", meta={"progress": 100, "message": f"Export Aborted: {str(exc)}"})
        raise exc


@celery_app.task(bind=True, name="tasks.execute_wxcc_flow_generation")
def execute_wxcc_flow_generation_task(self, cuc_export_data: dict, output_dir: str = "exports/wxcc_flows"):
    def update_progress(pct: int, msg: str):
        logger.info(f"[WxCC Flow Gen Task {self.request.id}] {pct}% - {msg}")
        self.update_state(state="PROGRESS", meta={"progress": pct, "message": msg})

    try:
        update_progress(10, "Initializing WxCC Flow Generator Engine...")
        generator = WxCCFlowGeneratorService()

        update_progress(40, "Converting Call Handler structures into WxCC Flow JSON schemas...")
        generated_files = generator.convert_cuc_export_batch(
            cuc_export_summary=cuc_export_data,
            output_dir=output_dir
        )

        update_progress(100, "WxCC Flow JSON generation complete.")
        return {
            "status": "COMPLETED",
            "progress": 100,
            "message": f"Successfully generated {len(generated_files)} WxCC Flow definitions.",
            "data": {"generated_files": generated_files}
        }
    except Exception as exc:
        logger.exception(f"WxCC Flow generation task failed: {exc}")
        self.update_state(state="FAILURE", meta={"progress": 100, "message": f"Flow Generation Aborted: {str(exc)}"})
        raise exc