"""
routers/diagnostics.py

Network endpoint diagnostics, remote Windows PowerShell telemetry, iPerf throughput tests,
and client identity tracking with Cisco Meraki enrichment.
"""

import json
import logging
import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from core.config import get_settings
from core.dependencies import get_diagnostic_service, get_meraki_provider
from models.common import MAC_PATTERN
from models.diagnostics import (
    IdentityTrackerRequest,
    IPerfRequest,
    NetTestRequest,
    WindowsDiagRequest,
)
from providers.meraki_provider import MerakiProvider
from security.auth import enforce_routing_auth
from services.endpoint_diagnostic_service import EndpointDiagnosticService

logger = logging.getLogger("Net_Dashboard.Diagnostics")
router = APIRouter(tags=["Diagnostics"])


def cleanup_temp_dir(path: str) -> None:
    try:
        if os.path.exists(path):
            shutil.rmtree(path)
            logger.info("Cleaned temporary directory path=%s", path)
    except Exception as exc:
        logger.warning("Failed to clean temporary directory path=%s error=%s", path, exc)


def run_powershell_script(
    command: List[str],
    timeout_seconds: int,
    input_data: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    try:
        sub_env = os.environ.copy()
        if env:
            sub_env.update(env)
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            input=input_data,
            timeout=timeout_seconds,
            env=sub_env,
        )
    except subprocess.TimeoutExpired as exc:
        logger.error("PowerShell execution timed out after %s seconds: %s", timeout_seconds, exc)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"Execution timed out after {timeout_seconds} seconds waiting for target computer.",
        ) from exc


def safe_json_loads(raw_output: str, error_message: str) -> Any:
    try:
        return json.loads(raw_output)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"{error_message}: {exc}") from exc


def _build_windows_script_env(settings: Any) -> Dict[str, str]:
    """Builds non-empty AD/LDAP environment overrides for remote Windows PowerShell scripts."""
    env_overrides: Dict[str, str] = {}
    bind_user = os.getenv("LDAP_BIND_USER") or settings.LDAP_BIND_USER
    bind_password = os.getenv("LDAP_BIND_PASSWORD") or settings.LDAP_BIND_PASSWORD
    ldap_domain = os.getenv("LDAP_DOMAIN") or settings.LDAP_DOMAIN
    netbios_domain = os.getenv("AD_NETBIOS_DOMAIN") or settings.AD_NETBIOS_DOMAIN
    if bind_user:
        env_overrides["LDAP_BIND_USER"] = bind_user
    if bind_password:
        env_overrides["LDAP_BIND_PASSWORD"] = bind_password
    if ldap_domain:
        env_overrides["LDAP_DOMAIN"] = ldap_domain
    if netbios_domain:
        env_overrides["AD_NETBIOS_DOMAIN"] = netbios_domain
    return env_overrides


def fallback_adapter_enrichment(adapter: Dict[str, Any], reason: str) -> Dict[str, Any]:
    return {
        "adapter_name": str(adapter.get("Name", "Unknown")),
        "adapter_type": str(adapter.get("Type", "Unknown")),
        "mac_address": str(adapter.get("MacAddress", "N/A")),
        "ip_address": str(adapter.get("IPAddress", "No IP Assigned")),
        "os_status": str(adapter.get("Status", "Unknown")),
        "meraki_found": False,
        "network_name": "N/A",
        "switch_ap_name": "N/A",
        "switch_ap_serial": "N/A",
        "switch_ap_mac": "N/A",
        "port_ssid": "N/A",
        "vlan": "N/A",
        "meraki_status": "Provider Disabled",
        "manufacturer": "N/A",
        "os": "N/A",
        "user": "N/A",
        "description": "N/A",
        "first_seen": "N/A",
        "last_seen": "N/A",
        "lookup_source": "fallback",
        "meraki_error": reason,
    }


@router.get("/api/v1/diagnose/{target}", status_code=status.HTTP_200_OK, dependencies=[Depends(enforce_routing_auth)])
async def run_diagnostics(
    target: str,
    diagnostic_service: EndpointDiagnosticService = Depends(get_diagnostic_service),
):
    """Executes multi-layer diagnostics against target IP, MAC, or hostname across ISE, Meraki, and DNAC."""
    cleaned_target = target.strip()
    if not cleaned_target:
        raise HTTPException(status_code=400, detail="Target is required.")

    is_ip = re.match(r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$", cleaned_target)
    is_mac = MAC_PATTERN.match(cleaned_target)

    if not is_ip and not is_mac:
        ps_script_path = os.path.abspath("scripts/get_client_macs.ps1")
        if os.path.exists(ps_script_path):
            command = ["powershell.exe", "-ExecutionPolicy", "Bypass", "-File", ps_script_path, "-TargetComputer", cleaned_target]
            try:
                settings = get_settings()
                env_overrides = _build_windows_script_env(settings)
                process = await run_in_threadpool(run_powershell_script, command, 10, env=env_overrides)
                if process.returncode == 0 and process.stdout:
                    parsed = json.loads(process.stdout.strip())
                    adapters = [parsed] if isinstance(parsed, dict) else parsed
                    for adapter in adapters:
                        if adapter.get("Status") == "Up" and adapter.get("MacAddress"):
                            cleaned_target = adapter.get("MacAddress")
                            break
            except Exception:
                pass

    return await run_in_threadpool(diagnostic_service.diagnose_target, cleaned_target)


@router.post("/api/v1/windows-diag", dependencies=[Depends(enforce_routing_auth)])
async def run_windows_diag(payload: WindowsDiagRequest, background_tasks: BackgroundTasks):
    """Gathers TEAP/802.1X diagnostic logs from remote Windows host and returns a compressed archive."""
    hostname = payload.hostname.strip()
    ps_script_path = os.path.abspath("scripts/get_teap_logs.ps1")

    if not os.path.exists(ps_script_path):
        raise HTTPException(status_code=500, detail=f"Required script not found: {ps_script_path}")

    command = ["powershell.exe", "-ExecutionPolicy", "Bypass", "-File", ps_script_path, "-TargetComputer", hostname]

    settings = get_settings()
    input_payload = None
    if settings.LDAP_BIND_USER and settings.LDAP_BIND_PASSWORD:
        command.append("-UseStdinCredentials")
        input_payload = json.dumps({
            "username": settings.LDAP_BIND_USER,
            "password": settings.LDAP_BIND_PASSWORD,
        })

    env_overrides = _build_windows_script_env(settings)
    teap_xml_profiles = os.getenv("TEAP_XML_PROFILES") or settings.TEAP_XML_PROFILES
    teap_ca_filter = os.getenv("TEAP_CA_FILTER") or settings.TEAP_CA_FILTER
    if teap_xml_profiles:
        env_overrides["TEAP_XML_PROFILES"] = teap_xml_profiles
    if teap_ca_filter:
        env_overrides["TEAP_CA_FILTER"] = teap_ca_filter

    process = await run_in_threadpool(
        run_powershell_script,
        command,
        120,
        input_data=input_payload,
        env=env_overrides,
    )

    if process.returncode != 0:
        raise HTTPException(status_code=500, detail=f"WinRM Error: {process.stderr or process.stdout}")

    zip_path = os.path.join("temp_downloads", hostname, "Network_Diagnostics.zip")
    if not os.path.exists(zip_path):
        raise HTTPException(status_code=500, detail="Script executed, but ZIP file was not returned.")

    background_tasks.add_task(cleanup_temp_dir, os.path.join("temp_downloads", hostname))
    return FileResponse(path=zip_path, filename=f"{hostname}_TEAP_Logs.zip", media_type="application/zip")


@router.post("/api/v1/iperf-test", dependencies=[Depends(enforce_routing_auth)])
async def run_iperf_test(payload: IPerfRequest):
    """Executes distributed iPerf network throughput benchmarks against regional core aggregation hubs."""
    hostname = payload.hostname.strip()
    settings = get_settings()
    server_string = (os.getenv("IPERF_SERVERS") or settings.IPERF_SERVERS).strip()
    if not server_string:
        raise HTTPException(
            status_code=503,
            detail="IPERF_SERVERS is not configured. Set IPERF_SERVERS (format: 'Hub1|10.x.x.x,Hub2|10.y.y.y').",
        )
    ps_script_path = os.path.abspath("scripts/run_iperf_test.ps1")

    if not os.path.exists(ps_script_path):
        raise HTTPException(status_code=500, detail=f"Required script not found: {ps_script_path}")

    command = ["powershell.exe", "-ExecutionPolicy", "Bypass", "-File", ps_script_path, "-TargetComputer", hostname, "-ServerList", server_string]
    process = await run_in_threadpool(
        run_powershell_script,
        command,
        120,
        env=_build_windows_script_env(settings),
    )

    if process.returncode != 0:
        raise HTTPException(status_code=500, detail=f"Execution Failed: {process.stderr or process.stdout}")

    stdout_str = process.stdout.strip()
    metrics_array = safe_json_loads(stdout_str, "Failed to parse regional performance data") if stdout_str else []
    return {"hostname": hostname, "metrics": [metrics_array] if isinstance(metrics_array, dict) else metrics_array}


@router.post("/api/v1/network-test", dependencies=[Depends(enforce_routing_auth)])
async def run_network_test(payload: NetTestRequest):
    """Initiates remote ping/traceroute network telemetry tests from Windows endpoint."""
    hostname = payload.hostname.strip()
    destination = payload.destination.strip()
    ps_script_path = os.path.abspath("scripts/run_network_tests.ps1")

    if not os.path.exists(ps_script_path):
        raise HTTPException(status_code=500, detail=f"Required script not found: {ps_script_path}")

    settings = get_settings()
    command = ["powershell.exe", "-ExecutionPolicy", "Bypass", "-File", ps_script_path, "-TargetComputer", hostname, "-Destination", destination]
    process = await run_in_threadpool(
        run_powershell_script,
        command,
        60,
        env=_build_windows_script_env(settings),
    )

    if process.returncode != 0:
        raise HTTPException(status_code=500, detail=f"Execution Failed: {process.stderr or process.stdout}")

    parsed_data = safe_json_loads(process.stdout.strip(), "Failed to parse remote network telemetry packet")
    return {"hostname": hostname, "destination": destination, "data": parsed_data}


@router.post("/api/v1/identity-tracker", dependencies=[Depends(enforce_routing_auth)])
async def run_identity_tracker(
    payload: IdentityTrackerRequest,
    meraki_provider: Optional[MerakiProvider] = Depends(get_meraki_provider),
):
    """Inventories host network adapters via WinRM and enriches telemetry with Cisco Meraki switch/AP details."""
    hostname = payload.hostname.strip()
    ps_script_path = os.path.abspath("scripts/get_client_macs.ps1")

    if not os.path.exists(ps_script_path):
        raise HTTPException(status_code=500, detail=f"Required script not found: {ps_script_path}")

    settings = get_settings()
    command = ["powershell.exe", "-ExecutionPolicy", "Bypass", "-File", ps_script_path, "-TargetComputer", hostname]
    process = await run_in_threadpool(
        run_powershell_script,
        command,
        60,
        env=_build_windows_script_env(settings),
    )

    if process.returncode != 0:
        raise HTTPException(status_code=500, detail=process.stderr or process.stdout or "WinRM adapter inventory failed.")

    parsed = safe_json_loads(process.stdout.strip(), "Failed to parse adapter inventory JSON") if process.stdout.strip() else []
    adapter_records = [parsed] if isinstance(parsed, dict) else parsed

    enriched_adapters = []
    for adapter in adapter_records:
        if isinstance(adapter, dict):
            enriched = (
                meraki_provider.enrich_adapter_record(adapter)
                if meraki_provider
                else fallback_adapter_enrichment(adapter, "Meraki provider uninitialized.")
            )
            enriched_adapters.append(enriched)

    return {"hostname": hostname, "adapters": enriched_adapters}
