"""
services/cucm_decommission_service.py

Phase 4: CUCM Dial-Plan & Voicemail Routing Hand-Off Service.
Automates decommissioning and hybrid coexistence routing in CUCM 12.5(1) SU1:
1. Inter-Cluster / Hybrid Routing (Translation Pattern):
   - Injects Translation Pattern in CUCM (e.g. extension 4199) in Internal_PT.
   - Assigns Calling Search Space pointing to Local Gateway (CUBE/LGW SIP Trunk)
     to route on-prem calls to Webex Calling without split-brain blackholes.
2. Device & Line Decommissioning:
   - Removes physical IP phone (SEP<MAC>) from CUCM via AXL removePhone.
   - Releases Cisco Smart Software Licensing (Enhanced / Basic UCL or CUWL).
   - Deletes or unassigns Directory Number from numplan via removeLine so CUCM
     does not intercept calls locally.
3. Supports dry-run simulation and live AXL execution with step-up authentication.
"""

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from models.telecom import (
    CucmRoutePartitionItem,
    CucmGatewayRouteListItem,
    CucmDialplanOptionsResponse,
    CucmRoutePatternRequest,
    CucmRoutePatternResponse,
    CucmTranslationPatternRequest,
    CucmTranslationPatternResponse,
    CucmDeviceDecommissionItem,
    CucmDecommissionRequest,
    CucmDecommissionResultItem,
    CucmDecommissionResponse,
)
from providers.cucm_axl_provider import CUCMAXLProvider, AXLRequestError, AXLProviderError
from services.cucm_audit_service import SOFTPHONE_NAME_RE, is_softphone_device

logger = logging.getLogger("CUCM_Decommission_Service")

MAC_CLEAN_RE = re.compile(r"[^0-9A-Fa-f]")
MAC_VALID_RE = re.compile(r"^[0-9A-F]{12}$")
NAMED_DEVICE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{1,63}$")


class CucmDecommissionService:
    """Manages Phase 4 CUCM Dial-Plan routing and device/line decommissioning."""

    def __init__(self, axl_provider: CUCMAXLProvider, cluster_name: str = "PRIMARY"):
        self.axl = axl_provider
        self.cluster_name = cluster_name

    @staticmethod
    def normalize_device_name(mac_or_device_name: str) -> Tuple[str, str]:
        """
        Normalizes input to standard CUCM device name and identifier.
        Accepts 12-digit MACs (SEP<MAC>), standard softphones (CSF*, TCT*, BOT*, TAB*),
        or named CUCM devices (e.g. ThomasJabber).
        Returns: (cucm_device_name, clean_identifier)
        """
        raw_orig = mac_or_device_name.strip()
        raw = raw_orig.upper()
        if SOFTPHONE_NAME_RE.match(raw):
            return raw, raw

        if raw.startswith("SEP"):
            clean_mac = MAC_CLEAN_RE.sub("", raw[3:])
        else:
            clean_mac = MAC_CLEAN_RE.sub("", raw)
            if NAMED_DEVICE_RE.match(raw_orig) and not MAC_VALID_RE.match(clean_mac):
                return raw_orig, raw_orig

        if not MAC_VALID_RE.match(clean_mac):
            raise ValueError(
                f"Invalid device MAC address or name '{mac_or_device_name}'. "
                "Must be a 12-digit hexadecimal MAC (e.g. 001122334455 / SEP001122334455) "
                "or a CUCM device name (e.g. CSFjdoe, TCTjdoe, BOTjdoe, TABjdoe, ThomasJabber)."
            )

        return f"SEP{clean_mac}", clean_mac

    @staticmethod
    def determine_reclaimed_license(model_name: str) -> str:
        """Determines the Cisco Smart Software Licensing tier released by deleting the phone."""
        clean = (model_name or "").strip()

        # High-end desktop & conference phones -> Enhanced UCL / CUWL
        if re.search(r"88\d{2}|7841|7861", clean):
            return "Enhanced UCL / CUWL Standard"
        # Mid-range phones -> Basic UCL
        elif re.search(r"7821|7811|68\d{2}", clean):
            return "Basic UCL / CUWL Standard"
        # Entry-level / legacy -> Essential UCL
        elif re.search(r"39\d{2}|69\d{2}|79\d{2}", clean):
            return "Essential UCL"
        # Video systems -> TelePresence Room
        elif re.search(r"Room|Desk|Board|DX|SX|MX", clean, re.IGNORECASE):
            return "TelePresence Room / CUWL Advanced"
        # Softphones (CSF / TCT / BOT / TAB / Jabber / Webex App)
        elif is_softphone_device(device_name=clean, model_name=clean):
            return "Essential / Basic UCL (Softphone Device Released)"
        return "Cisco Smart Software License (Device Token Released)"

    def get_dialplan_options(self) -> CucmDialplanOptionsResponse:
        """
        Retrieves route partitions and gateways/route lists from CUCM AXL provider.
        """
        partitions_data = []
        try:
            partitions_data = self.axl.get_route_partitions()
        except Exception as exc:
            logger.warning("Could not fetch route partitions from CUCM '%s': %s", self.cluster_name, exc)

        gateways_data = []
        try:
            gateways_data = self.axl.get_gateways_and_route_lists()
        except Exception as exc:
            logger.warning("Could not fetch gateways/route lists from CUCM '%s': %s", self.cluster_name, exc)

        # Fallback defaults only during simulated/mock testing without a configured CUCM host
        if not partitions_data and not getattr(self.axl, "host", None):
            partitions_data = [
                {"name": "Internal_PT", "description": "Default Internal Partition"},
            ]
        if not gateways_data and not getattr(self.axl, "host", None):
            gateways_data = [
                {"name": "Local_Gateway_RL", "description": "Default Route List", "class_name": "Route List", "is_route_list": True},
            ]

        partitions = [
            CucmRoutePartitionItem(
                name=p["name"],
                description=p.get("description"),
            )
            for p in partitions_data if p.get("name")
        ]

        gateways = [
            CucmGatewayRouteListItem(
                name=g["name"],
                description=g.get("description"),
                class_name=g.get("class_name", "Gateway"),
                is_route_list=bool(g.get("is_route_list", False)),
            )
            for g in gateways_data if g.get("name")
        ]

        return CucmDialplanOptionsResponse(
            cluster=self.cluster_name,
            partitions=partitions,
            gateways_and_route_lists=gateways,
        )

    def create_route_pattern(
        self,
        extension: str,
        partition: str,
        gateway_route_list: str,
        is_route_list: Optional[bool] = None,
        description: Optional[str] = None,
        urgent_priority: bool = True,
        dry_run: bool = True,
    ) -> CucmRoutePatternResponse:
        """
        Creates or validates a Route Pattern pointing an on-prem extension directly to a Gateway or Route List.
        """
        clean_ext = extension.strip()
        clean_pt = partition.strip()
        clean_gw = gateway_route_list.strip()
        desc = description or f"Migrated to Webex Calling - Ext {clean_ext}"

        # 1. Pre-check if route pattern already exists in given partition
        existing = None
        try:
            existing = self.axl.get_route_pattern(clean_ext, clean_pt)
        except Exception as check_err:
            logger.warning("Could not pre-check route pattern '%s': %s", clean_ext, check_err)

        if existing:
            return CucmRoutePatternResponse(
                status="SKIPPED",
                dry_run=dry_run,
                cluster=self.cluster_name,
                pattern=clean_ext,
                partition=clean_pt,
                gateway_route_list=existing.get("destination_name") or clean_gw,
                destination_type=existing.get("destination_type"),
                uuid=existing.get("pkid"),
                details=f"Route Pattern '{clean_ext}' already exists in partition '{clean_pt}'.",
            )

        # 2. Dry run simulation
        if dry_run:
            return CucmRoutePatternResponse(
                status="DRY_RUN",
                dry_run=True,
                cluster=self.cluster_name,
                pattern=clean_ext,
                partition=clean_pt,
                gateway_route_list=clean_gw,
                details=(
                    f"Validated Route Pattern creation in partition '{clean_pt}' pointing to Gateway/Route List '{clean_gw}'. "
                    "Pre-flight verification passed; no AXL mutations performed."
                ),
            )

        # 3. Live creation via AXL SOAP
        try:
            res = self.axl.add_route_pattern(
                pattern=clean_ext,
                partition=clean_pt,
                gateway_route_list=clean_gw,
                is_route_list=is_route_list,
                description=desc,
                urgent_priority=urgent_priority,
            )
            return CucmRoutePatternResponse(
                status="SUCCESS",
                dry_run=False,
                cluster=self.cluster_name,
                pattern=clean_ext,
                partition=clean_pt,
                gateway_route_list=clean_gw,
                destination_type=res.get("destination_type"),
                uuid=res.get("uuid"),
                details=f"Route Pattern '{clean_ext}' created in CUCM routing calls to '{clean_gw}'.",
            )
        except (AXLRequestError, AXLProviderError, Exception) as exc:
            logger.exception("Failed to add route pattern in CUCM: %s", exc)
            err_lower = str(exc).lower()
            if ("dn exists" in err_lower or "same pattern and partition" in err_lower) and hasattr(
                self.axl, "get_devices_by_extension"
            ):
                try:
                    holders = self.axl.get_devices_by_extension(clean_ext, clean_pt)
                except Exception:
                    holders = []
                if holders:
                    holder_list = ", ".join(
                        f"{h.get('device_name')} ({h.get('model') or 'Device'})" for h in holders
                    )
                    raise ValueError(
                        f"CUCM AXL rejected Route Pattern '{clean_ext}': DN '{clean_ext}' still exists in partition "
                        f"'{clean_pt}' on device(s): {holder_list}. Decommission those device(s) & remove the DN in Step 1 first. ({exc})"
                    )
                raise ValueError(
                    f"CUCM AXL rejected Route Pattern '{clean_ext}': An unassigned DN '{clean_ext}' still exists in "
                    f"partition '{clean_pt}'. Remove the unassigned DN in Step 1 (or CUCM Route Plan Report) first. ({exc})"
                )
            raise ValueError(f"CUCM AXL rejected Route Pattern '{clean_ext}': {str(exc)}")

    def create_translation_pattern(
        self,
        extension: str,
        partition: str = "Internal_PT",
        calling_search_space: str = "To_LGW_CSS",
        description: Optional[str] = None,
        urgent_priority: bool = True,
        dry_run: bool = True,
    ) -> CucmTranslationPatternResponse:
        """
        Creates or validates a Translation Pattern pointing an on-prem extension to Webex Calling / LGW.
        """
        clean_ext = extension.strip()
        clean_pt = partition.strip()
        clean_css = calling_search_space.strip()
        desc = description or f"Migrated to Webex Calling - Ext {clean_ext}"

        # 1. Pre-check if pattern already exists in the given partition
        existing = None
        try:
            existing = self.axl.get_translation_pattern(clean_ext, clean_pt)
        except Exception as check_err:
            logger.warning("Could not pre-check translation pattern '%s': %s", clean_ext, check_err)

        if existing:
            return CucmTranslationPatternResponse(
                status="SKIPPED",
                dry_run=dry_run,
                cluster=self.cluster_name,
                pattern=clean_ext,
                partition=clean_pt,
                calling_search_space=existing.get("css_name") or clean_css,
                uuid=existing.get("pkid"),
                details=f"Translation Pattern '{clean_ext}' already exists in partition '{clean_pt}'.",
            )

        # 2. Dry run simulation
        if dry_run:
            return CucmTranslationPatternResponse(
                status="DRY_RUN",
                dry_run=True,
                cluster=self.cluster_name,
                pattern=clean_ext,
                partition=clean_pt,
                calling_search_space=clean_css,
                details=(
                    f"Validated Translation Pattern creation in partition '{clean_pt}' pointing to CSS '{clean_css}'. "
                    "Pre-flight verification passed; no AXL mutations performed."
                ),
            )

        # 3. Live creation via AXL SOAP
        try:
            res = self.axl.add_translation_pattern(
                pattern=clean_ext,
                partition=clean_pt,
                calling_search_space=clean_css,
                description=desc,
                urgent_priority=urgent_priority,
            )
            return CucmTranslationPatternResponse(
                status="SUCCESS",
                dry_run=False,
                cluster=self.cluster_name,
                pattern=clean_ext,
                partition=clean_pt,
                calling_search_space=clean_css,
                uuid=res.get("uuid"),
                details=f"Translation Pattern '{clean_ext}' created in CUCM routing calls to '{clean_css}'.",
            )
        except (AXLRequestError, AXLProviderError, Exception) as exc:
            logger.exception("Failed to add translation pattern in CUCM: %s", exc)
            raise ValueError(f"CUCM AXL rejected Translation Pattern '{clean_ext}': {str(exc)}")

    def decommission_devices(
        self,
        items: List[CucmDeviceDecommissionItem],
        default_partition: str = "Internal_PT",
        default_calling_search_space: str = "To_LGW_CSS",
        default_gateway_route_list: Optional[str] = None,
        dry_run: bool = True,
    ) -> CucmDecommissionResponse:
        """
        Executes Phase 4 device decommissioning, line cleanup, and hybrid route/translation pattern injection.
        Live execution always runs in CUCM-safe order:
        1. Delete device(s) (removePhone)
        2. Delete unassigned DN(s) (removeLine) once shared devices are removed
        3. Inject Route Pattern or Translation Pattern (addRoutePattern / addTranslationPattern)
        """
        results: List[Optional[CucmDecommissionResultItem]] = [None] * len(items)
        successful_count = 0
        failed_count = 0
        skipped_count = 0

        # Track live contexts so shared-DN multi-device batches remove ALL devices before removeLine & addRoutePattern
        live_contexts: List[Dict[str, Any]] = []

        for idx, item in enumerate(items):
            # 1. Normalize device name
            try:
                device_name, clean_mac = self.normalize_device_name(item.mac_address)
            except ValueError as mac_err:
                results[idx] = CucmDecommissionResultItem(
                    device_name="UNKNOWN",
                    mac_address=item.mac_address,
                    status="FAILED",
                    details=str(mac_err),
                )
                failed_count += 1
                continue

            # 2. Query CUCM for existing device and line associations
            device_info = None
            try:
                device_info = self.axl.get_device_by_name_or_mac(device_name)
            except Exception as query_err:
                logger.warning("Could not query device '%s' from CUCM: %s", device_name, query_err)

            actual_device_name = (
                str(device_info.get("device_name")).strip()
                if isinstance(device_info, dict) and device_info.get("device_name")
                else device_name
            )
            model_name = device_info.get("model", "Cisco IP Phone") if device_info else "Unknown"
            reclaimed_lic = self.determine_reclaimed_license(model_name)

            assigned_lines = device_info.get("lines", []) if device_info else []
            primary_line_dn = None
            primary_line_pt = None
            if assigned_lines:
                primary_line_dn = assigned_lines[0].get("line_dn")
                primary_line_pt = assigned_lines[0].get("partition_name")

            extension = item.extension or primary_line_dn
            partition = item.partition or primary_line_pt or default_partition
            calling_search_space = item.calling_search_space or default_calling_search_space
            gateway_route_list = item.gateway_route_list or default_gateway_route_list

            should_create_pattern = (item.create_route_pattern or item.create_translation_pattern) and bool(extension)

            # 3. Dry run simulation
            if dry_run:
                if not device_info:
                    if should_create_pattern:
                        try:
                            pat_res = (
                                self.create_route_pattern(
                                    extension=extension,
                                    partition=partition,
                                    gateway_route_list=gateway_route_list,
                                    dry_run=True,
                                )
                                if gateway_route_list
                                else self.create_translation_pattern(
                                    extension=extension,
                                    partition=partition,
                                    calling_search_space=calling_search_space,
                                    dry_run=True,
                                )
                            )
                            results[idx] = CucmDecommissionResultItem(
                                device_name=actual_device_name,
                                mac_address=clean_mac,
                                model=model_name,
                                extension=extension,
                                status=pat_res.status,
                                translation_pattern_status=pat_res.status,
                                route_pattern_status=pat_res.status,
                                device_deletion_status="NOT_FOUND",
                                license_reclaimed="None (Device not found in CUCM)",
                                details=f"Device not found in CUCM. {pat_res.details}",
                            )
                            if pat_res.status in {"SUCCESS", "DRY_RUN", "SKIPPED"}:
                                successful_count += 1
                            else:
                                failed_count += 1
                        except Exception as pat_err:
                            results[idx] = CucmDecommissionResultItem(
                                device_name=actual_device_name,
                                mac_address=clean_mac,
                                model=model_name,
                                extension=extension,
                                status="FAILED",
                                device_deletion_status="NOT_FOUND",
                                details=f"Device not found in CUCM. Pattern injection failed: {pat_err}",
                            )
                            failed_count += 1
                    else:
                        results[idx] = CucmDecommissionResultItem(
                            device_name=actual_device_name,
                            mac_address=clean_mac,
                            model=model_name,
                            extension=extension,
                            status="SKIPPED",
                            device_deletion_status="NOT_FOUND",
                            details=f"Device '{actual_device_name}' does not exist in CUCM cluster. Already decommissioned.",
                        )
                        skipped_count += 1
                else:
                    pat_desc = (
                        f"Route pattern will route '{extension}' to '{gateway_route_list}'."
                        if gateway_route_list
                        else f"Translation pattern will route '{extension}' to '{calling_search_space}'."
                    )
                    results[idx] = CucmDecommissionResultItem(
                        device_name=actual_device_name,
                        mac_address=clean_mac,
                        model=model_name,
                        extension=extension,
                        status="DRY_RUN",
                        translation_pattern_status="DRY_RUN" if should_create_pattern else "SKIPPED",
                        route_pattern_status="DRY_RUN" if should_create_pattern else "SKIPPED",
                        device_deletion_status="DRY_RUN",
                        line_deletion_status="DRY_RUN" if item.delete_unassigned_dn and extension else "SKIPPED",
                        license_reclaimed=reclaimed_lic,
                        details=(
                            f"Validated decommissioning for '{actual_device_name}' ({model_name}). "
                            f"Smart License to reclaim: '{reclaimed_lic}'. {pat_desc}"
                        ),
                    )
                    successful_count += 1
                continue

            live_contexts.append(
                {
                    "idx": idx,
                    "item": item,
                    "device_name": actual_device_name,
                    "clean_mac": clean_mac,
                    "device_info": device_info,
                    "model_name": model_name,
                    "reclaimed_lic": reclaimed_lic,
                    "extension": extension,
                    "partition": partition,
                    "calling_search_space": calling_search_space,
                    "gateway_route_list": gateway_route_list,
                    "should_create_pattern": should_create_pattern,
                    "dev_status": "NOT_FOUND" if not device_info else "FAILED",
                    "dev_error": None,
                }
            )

        # 4. Live Execution (Step A: Delete all devices first -> Step B: Delete unassigned DNs -> Step C: Create Patterns)
        if not dry_run and live_contexts:
            for ctx in live_contexts:
                if not ctx["device_info"]:
                    continue
                try:
                    self.axl.remove_phone(ctx["device_name"])
                    ctx["dev_status"] = "SUCCESS"
                except (AXLRequestError, AXLProviderError, Exception) as del_err:
                    logger.exception("Device deletion failed for %s: %s", ctx["device_name"], del_err)
                    ctx["dev_status"] = "FAILED"
                    ctx["dev_error"] = str(del_err)

            dn_cleanup_status: Dict[Tuple[str, str], str] = {}
            pattern_results: Dict[Tuple[str, str], Dict[str, Any]] = {}

            for ctx in live_contexts:
                ext = ctx["extension"]
                pt = ctx["partition"]
                if not ext:
                    continue
                dn_key = (ext, pt)

                if ctx["item"].delete_unassigned_dn and ctx["device_info"] and ctx["dev_status"] == "SUCCESS":
                    if dn_key not in dn_cleanup_status:
                        try:
                            self.axl.remove_line(ext, pt)
                            dn_cleanup_status[dn_key] = "SUCCESS"
                        except Exception as dn_err:
                            logger.warning("Could not remove line '%s' (Partition: %s): %s", ext, pt, dn_err)
                            dn_cleanup_status[dn_key] = f"SKIPPED ({dn_err})"

            for ctx in live_contexts:
                ext = ctx["extension"]
                pt = ctx["partition"]
                if not ctx["should_create_pattern"] or not ext:
                    continue
                dn_key = (ext, pt)
                if dn_key in pattern_results:
                    continue
                try:
                    if ctx["gateway_route_list"]:
                        pat_resp = self.create_route_pattern(
                            extension=ext,
                            partition=pt,
                            gateway_route_list=ctx["gateway_route_list"],
                            dry_run=False,
                        )
                    else:
                        pat_resp = self.create_translation_pattern(
                            extension=ext,
                            partition=pt,
                            calling_search_space=ctx["calling_search_space"],
                            dry_run=False,
                        )
                    pattern_results[dn_key] = {"status": pat_resp.status, "details": pat_resp.details, "error": None}
                except (AXLRequestError, AXLProviderError, Exception) as pat_err:
                    logger.exception("Pattern injection failed for %s (%s): %s", ext, pt, pat_err)
                    pattern_results[dn_key] = {"status": "FAILED", "details": "", "error": str(pat_err)}

            for ctx in live_contexts:
                idx = ctx["idx"]
                ext = ctx["extension"]
                pt = ctx["partition"]
                dn_key = (ext, pt) if ext else None
                dev_status = ctx["dev_status"]
                line_status = dn_cleanup_status.get(dn_key, "SKIPPED") if dn_key else "SKIPPED"
                pat_info = pattern_results.get(dn_key) if (ctx["should_create_pattern"] and dn_key) else None
                pat_status = pat_info["status"] if pat_info else "SKIPPED"

                if ctx["dev_error"]:
                    results[idx] = CucmDecommissionResultItem(
                        device_name=ctx["device_name"],
                        mac_address=ctx["clean_mac"],
                        model=ctx["model_name"],
                        extension=ext,
                        status="FAILED",
                        device_deletion_status="FAILED",
                        details=f"Decommissioning failed: {ctx['dev_error']}",
                    )
                    failed_count += 1
                    continue

                if not ctx["device_info"]:
                    if pat_info and pat_info["error"]:
                        results[idx] = CucmDecommissionResultItem(
                            device_name=ctx["device_name"],
                            mac_address=ctx["clean_mac"],
                            model=ctx["model_name"],
                            extension=ext,
                            status="FAILED",
                            device_deletion_status="NOT_FOUND",
                            details=f"Device not found in CUCM. Pattern injection failed: {pat_info['error']}",
                        )
                        failed_count += 1
                    elif pat_info:
                        results[idx] = CucmDecommissionResultItem(
                            device_name=ctx["device_name"],
                            mac_address=ctx["clean_mac"],
                            model=ctx["model_name"],
                            extension=ext,
                            status=pat_status,
                            translation_pattern_status=pat_status,
                            route_pattern_status=pat_status,
                            device_deletion_status="NOT_FOUND",
                            license_reclaimed="None (Device not found in CUCM)",
                            details=f"Device not found in CUCM. {pat_info['details']}",
                        )
                        if pat_status in {"SUCCESS", "DRY_RUN", "SKIPPED"}:
                            successful_count += 1
                        else:
                            failed_count += 1
                    else:
                        results[idx] = CucmDecommissionResultItem(
                            device_name=ctx["device_name"],
                            mac_address=ctx["clean_mac"],
                            model=ctx["model_name"],
                            extension=ext,
                            status="SKIPPED",
                            device_deletion_status="NOT_FOUND",
                            details=f"Device '{ctx['device_name']}' does not exist in CUCM cluster. Already decommissioned.",
                        )
                        skipped_count += 1
                    continue

                if pat_info and pat_info["error"]:
                    results[idx] = CucmDecommissionResultItem(
                        device_name=ctx["device_name"],
                        mac_address=ctx["clean_mac"],
                        model=ctx["model_name"],
                        extension=ext,
                        status="FAILED",
                        translation_pattern_status="FAILED",
                        route_pattern_status="FAILED",
                        device_deletion_status=dev_status,
                        line_deletion_status=line_status,
                        license_reclaimed=ctx["reclaimed_lic"],
                        details=(
                            f"Device '{ctx['device_name']}' deleted from CUCM, but pattern injection failed: "
                            f"{pat_info['error']}"
                        ),
                    )
                    failed_count += 1
                    continue

                dest_label = ctx["gateway_route_list"] or ctx["calling_search_space"]
                results[idx] = CucmDecommissionResultItem(
                    device_name=ctx["device_name"],
                    mac_address=ctx["clean_mac"],
                    model=ctx["model_name"],
                    extension=ext,
                    status="SUCCESS",
                    translation_pattern_status=pat_status,
                    route_pattern_status=pat_status,
                    device_deletion_status=dev_status,
                    line_deletion_status=line_status,
                    license_reclaimed=ctx["reclaimed_lic"],
                    details=(
                        f"Device '{ctx['device_name']}' deleted from CUCM. "
                        f"Reclaimed '{ctx['reclaimed_lic']}'. "
                        + (
                            f"Hybrid routing active for extension '{ext}' -> '{dest_label}'."
                            if pat_status == "SUCCESS"
                            else (pat_info["details"] if pat_info else "Line cleanup completed.")
                        )
                    ),
                )
                successful_count += 1

        final_results = [r for r in results if r is not None]
        return CucmDecommissionResponse(
            status="SUCCESS",
            dry_run=dry_run,
            cluster=self.cluster_name,
            total_evaluated=len(items),
            successful_count=successful_count,
            failed_count=failed_count,
            skipped_count=skipped_count,
            results=final_results,
        )
