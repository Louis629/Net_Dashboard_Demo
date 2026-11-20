"""
routers/bypass.py

802.1X Staging Bypass and Multi-System Device Reimaging Automation Router.
Handles Cisco ISE MAB staging, SCCM asset unbinding, Active Directory computer account deletion,
and Cisco ISE endpoint record purging with secondary authorization enforcement.
"""

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, status
from starlette.concurrency import run_in_threadpool

from core.dependencies import get_ise_provider, get_sccm_provider
from models.bypass import BypassRequest
from models.common import normalize_mac
from providers.ise_provider import ISEProvider
from providers.sccm_provider import SCCMProvider
from security.auth import (
    AuthFactory,
    CurrentUser,
    enforce_routing_auth,
    get_current_user,
)
from security.roles import Role

logger = logging.getLogger("Net_Dashboard.Bypass")
router = APIRouter(tags=["Bypass & Re-imaging"])


@router.post("/api/v1/bypass", status_code=status.HTTP_200_OK, dependencies=[Depends(enforce_routing_auth)])
async def trigger_bypass(
    payload: BypassRequest,
    current_user: CurrentUser = Depends(get_current_user),
    ise_provider: ISEProvider = Depends(get_ise_provider),
    sccm_provider: SCCMProvider = Depends(get_sccm_provider),
):
    """Triggers 802.1X bypass staging or complete device re-imaging across SCCM, AD, and ISE."""
    if current_user.role not in [Role.NETWORK_ADMIN, Role.LOCAL_TECH_SERVICES]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access Denied: Imaging 802.1X Bypass & Re-imaging is restricted to Network Admin and Tech Services groups.",
        )
    try:
        if payload.device_type == "new":
            result = await run_in_threadpool(
                ise_provider.provision_imaging_bypass,
                mac_address=payload.mac_address,
                operator_teams_id=payload.operator_id,
            )
            if result.get("status") in {"ERROR", "CRITICAL_FAILURE"}:
                raise HTTPException(status_code=500, detail=result.get("message", "ISE bypass failed."))
            return {
                "device_type": "new",
                "mac": payload.mac_address,
                "ise_bypass": result,
                "message": f"Successfully authorized MAC '{payload.mac_address}' in ISE staging group.",
            }

        # Device Type: 'reimage'
        # Mandatory secondary authentication & authorization prior to running
        if not payload.step_up_user or not payload.step_up_pass:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Secondary authentication required: Re-imaging deletes devices across SCCM, Active Directory, and Cisco ISE. Please provide secondary credentials.",
            )

        auth_provider = AuthFactory.get_step_up_provider()
        is_authorized, reason = auth_provider.verify_reimaging_authorization(
            username=payload.step_up_user,
            password=payload.step_up_pass,
        )
        if not is_authorized:
            if "invalid" in reason.lower() or "credential" in reason.lower():
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=f"Secondary authentication failed: {reason}",
                )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Secondary authorization failed: {reason}",
            )

        logger.info(
            "Re-imaging secondary authorization granted for user '%s' (operator_id='%s', device='%s', mac='%s')",
            payload.step_up_user,
            payload.operator_id,
            payload.device_name,
            payload.mac_address,
        )

        is_bulk = bool(payload.device_names and len(payload.device_names) > 1)

        if is_bulk:
            removal_output = await run_in_threadpool(
                sccm_provider.remove_device,
                device_name=payload.device_name,
                device_names=payload.device_names,
                mac_address=payload.mac_address,
                operator_id=payload.step_up_user or payload.operator_id,
                remove_sccm=payload.remove_sccm,
                remove_ad=payload.remove_ad,
                credential_user=payload.step_up_user,
                credential_pass=payload.step_up_pass,
            )

            bulk_devices = removal_output.get("devices", [])
            ise_purged_count = 0
            ise_staged_count = 0

            # Process Cisco ISE for each device if requested
            for dev in bulk_devices:
                dev_mac = dev.get("resolved_mac")
                dev["ise_removal"] = None
                dev["ise_bypass"] = None

                if payload.remove_ise and dev_mac:
                    try:
                        clean_dev_mac = normalize_mac(dev_mac)
                        ise_del = await run_in_threadpool(ise_provider.remove_endpoint, clean_dev_mac)
                        dev["ise_removal"] = ise_del
                        if ise_del.get("status") == "SUCCESS":
                            ise_purged_count += 1
                    except Exception as ise_err:
                        dev["ise_removal"] = {"status": "ERROR", "message": str(ise_err)}

                if payload.stage_in_ise and dev_mac:
                    try:
                        clean_dev_mac = normalize_mac(dev_mac)
                        ise_byp = await run_in_threadpool(
                            ise_provider.provision_imaging_bypass,
                            mac_address=clean_dev_mac,
                            operator_teams_id=payload.operator_id,
                        )
                        dev["ise_bypass"] = ise_byp
                        if ise_byp.get("status") == "SUCCESS":
                            ise_staged_count += 1
                    except Exception as ise_err:
                        dev["ise_bypass"] = {"status": "ERROR", "message": str(ise_err)}

            summary = removal_output.get("summary", {})
            summary["ise_purged"] = ise_purged_count
            if payload.stage_in_ise:
                summary["ise_staged"] = ise_staged_count

            return {
                "device_type": "reimage",
                "is_bulk": True,
                "total": len(bulk_devices),
                "summary": summary,
                "devices": bulk_devices,
                "operator_id": payload.step_up_user or payload.operator_id,
                "messages": [
                    f"Bulk re-imaging workflow completed for {len(bulk_devices)} devices."
                ],
            }

        response_data: Dict[str, Any] = {
            "device_type": "reimage",
            "device_name": payload.device_name,
            "mac": payload.mac_address,
            "sccm": None,
            "ad": None,
            "ise_removal": None,
            "ise_bypass": None,
            "messages": [],
        }

        resolved_mac = payload.mac_address

        # 1. SCCM & Active Directory Removal
        if payload.remove_sccm or payload.remove_ad:
            removal_output = await run_in_threadpool(
                sccm_provider.remove_device,
                device_name=payload.device_name,
                mac_address=payload.mac_address,
                operator_id=payload.step_up_user or payload.operator_id,
                remove_sccm=payload.remove_sccm,
                remove_ad=payload.remove_ad,
                credential_user=payload.step_up_user,
                credential_pass=payload.step_up_pass,
            )
            response_data["sccm"] = removal_output.get("sccm")
            response_data["ad"] = removal_output.get("ad")

            # Extract resolved MAC from SCCM if tech didn't supply one
            if not resolved_mac and removal_output.get("resolved_mac"):
                try:
                    resolved_mac = normalize_mac(removal_output.get("resolved_mac"))
                    response_data["mac"] = resolved_mac
                    response_data["messages"].append(f"Auto-resolved MAC '{resolved_mac}' from SCCM asset inventory.")
                except ValueError:
                    pass

            if payload.remove_sccm:
                sccm_info = removal_output.get("sccm", {})
                if sccm_info.get("removed"):
                    response_data["messages"].append(sccm_info.get("message", "Removed from SCCM."))
                elif sccm_info.get("error") or "error" in (sccm_info.get("message") or "").lower():
                    response_data["messages"].append(sccm_info.get("message"))
                elif sccm_info.get("found") is False:
                    response_data["messages"].append("Device not found in SCCM inventory.")
                elif sccm_info.get("message"):
                    response_data["messages"].append(f"SCCM Notice: {sccm_info.get('message')}")

            if payload.remove_ad:
                ad_info = removal_output.get("ad", {})
                if ad_info.get("removed"):
                    response_data["messages"].append(ad_info.get("message", "Removed from Active Directory."))
                elif ad_info.get("error") or "error" in (ad_info.get("message") or "").lower():
                    response_data["messages"].append(ad_info.get("message"))
                elif ad_info.get("found") is False:
                    response_data["messages"].append("Computer account not found in Active Directory.")
                elif ad_info.get("message"):
                    response_data["messages"].append(f"Active Directory Notice: {ad_info.get('message')}")

        # 2. Cisco ISE Removal
        if payload.remove_ise:
            if resolved_mac:
                ise_del_result = await run_in_threadpool(ise_provider.remove_endpoint, resolved_mac)
                response_data["ise_removal"] = ise_del_result
                if ise_del_result.get("status") == "SUCCESS":
                    response_data["messages"].append(f"Purged endpoint '{resolved_mac}' from Cisco ISE.")
                elif ise_del_result.get("status") == "NOT_FOUND":
                    response_data["messages"].append(f"Endpoint '{resolved_mac}' was not present in Cisco ISE.")
                else:
                    response_data["messages"].append(f"ISE Removal Notice: {ise_del_result.get('message')}")
            else:
                response_data["messages"].append("Skipped ISE removal: No MAC address provided or resolved from SCCM.")

        # 3. Optional Cisco ISE Staging Bypass
        if payload.stage_in_ise:
            if resolved_mac:
                ise_bypass_result = await run_in_threadpool(
                    ise_provider.provision_imaging_bypass,
                    mac_address=resolved_mac,
                    operator_teams_id=payload.operator_id,
                )
                response_data["ise_bypass"] = ise_bypass_result
                if ise_bypass_result.get("status") == "SUCCESS":
                    response_data["messages"].append(
                        f"Staged MAC '{resolved_mac}' in ISE group '{ise_bypass_result.get('group', 'Staging_Imaging_Bypass')}'."
                    )
                else:
                    response_data["messages"].append(f"ISE Staging Notice: {ise_bypass_result.get('message')}")
            else:
                response_data["messages"].append("Skipped ISE staging bypass: No MAC address available.")

        return response_data
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Bypass / Reimage workflow failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
