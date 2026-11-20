"""
services/webex_provision_service.py

Batch Provisioning Orchestrator for Webex Calling.
Processes audited CUCM endpoints and provisions Webex Calling licenses, extensions, and MPP phones.
Includes multi-tier identity resolution (Email -> UPN Fallback).
"""

import logging
import os
from typing import Any, Dict, List, Optional
from pydantic import BaseModel

from core.config import get_settings
from providers.webex_calling_provider import WebexCallingProvider, WebexAPIError
from providers.redsky_provider import RedskyProvider
from services.cucm_audit_service import is_softphone_device

logger = logging.getLogger("Webex_Provision_Service")


class ProvisioningResultItem(BaseModel):
    device_name: str
    owner_email: str
    extension: str
    e164_phone_number: str
    status: str  # SUCCESS, FAILED, SKIPPED
    details: str


class WebexBatchProvisionSummary(BaseModel):
    total_processed: int
    successful_count: int
    failed_count: int
    skipped_count: int
    items: List[ProvisioningResultItem]


class WebexProvisionService:
    def __init__(
        self,
        webex_provider: WebexCallingProvider,
        redsky_provider: Optional[RedskyProvider] = None,
    ):
        settings = get_settings()
        self.webex = webex_provider
        self.redsky = redsky_provider if redsky_provider is not None else RedskyProvider()
        self.default_location_id = (
            getattr(webex_provider, "default_location_id", "")
            or os.getenv("WEBEX_DEFAULT_LOCATION_ID", "")
            or os.getenv("WEBEX_LOCATION_ID", "")
            or settings.WEBEX_DEFAULT_LOCATION_ID
        ).strip().strip('"').strip("'")
        self.calling_license_id = (
            getattr(webex_provider, "calling_license_id", "")
            or os.getenv("WEBEX_CALLING_LICENSE_ID", "")
            or os.getenv("WEBEX_LICENSE_ID", "")
            or settings.WEBEX_CALLING_LICENSE_ID
        ).strip().strip('"').strip("'")

    def resolve_person_identity(self, email: str, userid: str) -> Optional[Dict[str, Any]]:
        """
        Resolves a Control Hub Person ID using primary email, falling back to UPN matching.
        """
        if not email or email == "N/A":
            return None

        # 1. Primary Lookup via explicit email
        person = self.webex.get_person_by_email(email)
        if person:
            return person

        # 2. Fallback Lookup via CUCM User ID / UPN format
        if userid and userid != "Unassigned":
            default_domain = (os.getenv("DEFAULT_EMAIL_DOMAIN") or get_settings().DEFAULT_EMAIL_DOMAIN).strip()
            domain = email.split("@")[-1] if "@" in email else default_domain
            fallback_email = f"{userid}@{domain}"
            logger.info(
                "Primary email lookup failed for '%s'. Attempting fallback UPN lookup with '%s'...",
                email,
                fallback_email
            )
            person = self.webex.get_person_by_email(fallback_email)
            if person:
                return person

        return None

    def process_cucm_audit_batch(
        self,
        cucm_audit_data: Dict[str, Any],
        location_id: Optional[str] = None,
        calling_license_id: Optional[str] = None
    ) -> WebexBatchProvisionSummary:
        """
        Processes a CUCM Migration Audit result and provisions Webex Calling profiles.
        """
        target_location = location_id or self.default_location_id
        target_license = calling_license_id or self.calling_license_id

        devices = cucm_audit_data.get("devices", [])
        if not target_location and not any(dev.get("target_location_id") for dev in devices):
            raise ValueError("Webex Location ID is required for Webex Calling provisioning.")

        results: List[ProvisioningResultItem] = []
        
        success_c = 0
        failed_c = 0
        skipped_c = 0
        provisioned_softphone_users: set[tuple[str, str]] = set()

        for dev in devices:
            dev_name = dev.get("device_name", "UNKNOWN")
            dev_model = dev.get("device_model", "")
            device_location = dev.get("target_location_id") or target_location
            tier = dev.get("migration_tier", "")
            owner_email = dev.get("owner_email", "")
            owner_userid = dev.get("owner_userid", "")
            lines = dev.get("lines", [])

            # Extract Primary Line details (support raw CUCM audit rows and UI-staged rows)
            primary_dn = lines[0].get("dn", "") if lines else str(dev.get("extension", ""))
            primary_e164 = lines[0].get("e164_suggested", "") if lines else str(dev.get("e164_suggested", ""))
            e164_val = primary_e164 if primary_e164.startswith("+") else ""

            is_softphone = (
                dev.get("endpoint_type") == "SOFTPHONE"
                or (
                    tier == "TIER_2_GATEWAY_SOFTPHONE"
                    and is_softphone_device(device_name=dev_name, model_name=dev_model)
                )
            )

            # Skip analog gateways or Tier 3 incompatible devices; allow Tier 1 MPP and Softphones
            if tier != "TIER_1_MPP_DIRECT" and not is_softphone:
                results.append(ProvisioningResultItem(
                    device_name=dev_name,
                    owner_email=owner_email,
                    extension=primary_dn,
                    e164_phone_number=e164_val,
                    status="SKIPPED",
                    details=f"Device model is classified as {tier}. Requires Dedicated Instance or hardware refresh."
                ))
                skipped_c += 1
                continue

            if not owner_email or owner_email == "N/A":
                results.append(ProvisioningResultItem(
                    device_name=dev_name,
                    owner_email="N/A",
                    extension=primary_dn,
                    e164_phone_number=e164_val,
                    status="SKIPPED",
                    details="Device has no assigned owner email in CUCM."
                ))
                skipped_c += 1
                continue

            if not device_location:
                results.append(ProvisioningResultItem(
                    device_name=dev_name,
                    owner_email=owner_email,
                    extension=primary_dn,
                    e164_phone_number=e164_val,
                    status="FAILED",
                    details="Webex Location ID is required for this endpoint."
                ))
                failed_c += 1
                continue

            if is_softphone:
                user_key = (owner_email.strip().lower(), device_location)
                if user_key in provisioned_softphone_users:
                    results.append(ProvisioningResultItem(
                        device_name=dev_name,
                        owner_email=owner_email,
                        extension=primary_dn,
                        e164_phone_number=e164_val,
                        status="SKIPPED",
                        details=f"User '{owner_email}' already provisioned in this batch via another CUCM softphone device."
                    ))
                    skipped_c += 1
                    continue

            try:
                # Step 1: Resolve Person ID in Control Hub (Email -> UPN Fallback)
                person = self.resolve_person_identity(owner_email, owner_userid)
                if not person:
                    results.append(ProvisioningResultItem(
                        device_name=dev_name,
                        owner_email=owner_email,
                        extension=primary_dn,
                        e164_phone_number=e164_val,
                        status="FAILED",
                        details=f"User email '{owner_email}' (and fallback UPN '{owner_userid}') not found in Webex Control Hub."
                    ))
                    failed_c += 1
                    continue

                person_id = person["id"]
                ad_did = WebexCallingProvider.extract_existing_did(person)
                effective_e164 = ad_did or e164_val
                existing_ext = str(person.get("extension") or "").strip()
                effective_ext = existing_ext or primary_dn

                effective_license = target_license
                if is_softphone and not effective_license:
                    if hasattr(self.webex, "get_user_calling_licenses"):
                        try:
                            avail = self.webex.get_user_calling_licenses()
                            if avail:
                                effective_license = str(avail[0].get("id", "")).strip()
                        except Exception as lic_err:
                            logger.warning("Could not auto-detect calling license for softphone user %s: %s", owner_email, lic_err)
                    if not effective_license:
                        results.append(ProvisioningResultItem(
                            device_name=dev_name,
                            owner_email=owner_email,
                            extension=primary_dn,
                            e164_phone_number=e164_val,
                            status="FAILED",
                            details="Webex Calling license ID is required for softphone user provisioning."
                        ))
                        failed_c += 1
                        continue

                # Step 2: Enable Webex Calling for User (replaces Unified CM license if present)
                if effective_license:
                    self.webex.enable_webex_calling_for_person(
                        person_id=person_id,
                        location_id=device_location,
                        calling_license_id=effective_license,
                        extension=effective_ext,
                        phone_number=effective_e164
                    )

                if is_softphone:
                    if hasattr(self.webex, "configure_caller_id"):
                        try:
                            self.webex.configure_caller_id(
                                person_id=person_id,
                                caller_id_type="DIRECT_LINE" if effective_e164 else "LOCATION_NUMBER",
                                external_caller_id_name=person.get("displayName") or dev.get("display_name"),
                            )
                        except Exception as cid_err:
                            logger.warning("Could not set caller ID for softphone user %s: %s", owner_email, cid_err)

                    if hasattr(self.webex, "configure_voicemail"):
                        try:
                            self.webex.configure_voicemail(
                                person_id=person_id,
                                enabled=True,
                                send_email_notification=True,
                                email_copy_of_message=True,
                                notification_email=owner_email,
                            )
                        except Exception as vm_err:
                            logger.warning("Could not configure voicemail for softphone user %s: %s", owner_email, vm_err)

                    redsky_note = ""
                    if effective_e164 and self.redsky and self.redsky.is_configured():
                        try:
                            r_res = self.redsky.sync_device_user_mye911(
                                email=owner_email,
                                phone_number=effective_e164,
                                first_name=person.get("firstName") or "",
                                last_name=person.get("lastName") or "",
                                callback_number=effective_e164,
                                enable_mye911=True,
                            )
                            redsky_note = f" RedSky MyE911 {r_res.get('action', 'SYNCED')}."
                        except Exception as r_err:
                            logger.warning("RedSky MyE911 sync warning for softphone user %s: %s", owner_email, r_err)

                    provisioned_softphone_users.add((owner_email.strip().lower(), device_location))
                    results.append(ProvisioningResultItem(
                        device_name=dev_name,
                        owner_email=owner_email,
                        extension=effective_ext,
                        e164_phone_number=effective_e164,
                        status="SUCCESS",
                        details=(
                            "Provisioned Webex Calling user profile for softphone-only user "
                            "(Webex App cloud registration; no physical MPP device)." + redsky_note
                        )
                    ))
                    success_c += 1
                    continue

                # Step 3: Provision Tier 1 MPP Device
                # Extract MAC address from SEP Name (e.g., SEP001122334455 -> 001122334455)
                mac_addr = dev_name.replace("SEP", "") if dev_name.startswith("SEP") else dev_name
                model = dev_model or "CP-8841"

                self.webex.provision_mpp_device(
                    mac_address=mac_addr,
                    model=model,
                    location_id=device_location,
                    person_id=person_id
                )

                results.append(ProvisioningResultItem(
                    device_name=dev_name,
                    owner_email=owner_email,
                    extension=effective_ext,
                    e164_phone_number=effective_e164,
                    status="SUCCESS",
                    details="Provisioned Webex Calling user profile and Tier 1 MPP device."
                ))
                success_c += 1

            except WebexAPIError as exc:
                logger.error("Failed provisioning Webex Calling profile for %s: %s", owner_email, exc)
                results.append(ProvisioningResultItem(
                    device_name=dev_name,
                    owner_email=owner_email,
                    extension=primary_dn,
                    e164_phone_number=e164_val,
                    status="FAILED",
                    details=f"Webex API Rejection: {str(exc)}"
                ))
                failed_c += 1

        return WebexBatchProvisionSummary(
            total_processed=len(devices),
            successful_count=success_c,
            failed_count=failed_c,
            skipped_count=skipped_c,
            items=results
        )