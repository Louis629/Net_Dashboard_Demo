"""
services/webex_user_profile_service.py

Phase 2 User Entitlement & Calling Profile Orchestration Service.
Assigns Webex Calling Professional licenses, binds users to Meraki-synced locations,
assigns internal extensions and direct DIDs, configures Caller ID policies
(Direct Line vs Location Main Number), and provisions Webex Cloud Voicemail
with email notifications and .wav audio attachments.
"""

import logging
import os
import re
from typing import Any, Dict, List, Optional

from core.config import get_settings
from models.telecom import (
    UserCallingProfileItem,
    UserCallingProfileProvisionResponse,
    UserCallingProfileResultItem,
)
from providers.webex_calling_provider import WebexAPIError, WebexCallingProvider
from providers.redsky_provider import RedskyProvider

logger = logging.getLogger("Net_Dashboard.WebexUserProfileService")

E164_PATTERN = re.compile(r"^\+[1-9]\d{6,14}$")


class WebexUserProfileService:
    """Manages Phase 2 User Entitlements, Calling Profile configurations, and RedSky MyE911 synchronization."""

    def __init__(
        self,
        webex_provider: WebexCallingProvider,
        redsky_provider: Optional[RedskyProvider] = None,
    ):
        settings = get_settings()
        self.webex = webex_provider
        self.redsky = redsky_provider if redsky_provider is not None else RedskyProvider()
        self.server_license_id = (
            getattr(webex_provider, "calling_license_id", "")
            or os.getenv("WEBEX_CALLING_LICENSE_ID", "")
            or os.getenv("WEBEX_LICENSE_ID", "")
            or settings.WEBEX_CALLING_LICENSE_ID
        ).strip().strip('"').strip("'")
        self.server_location_id = (
            getattr(webex_provider, "default_location_id", "")
            or os.getenv("WEBEX_DEFAULT_LOCATION_ID", "")
            or os.getenv("WEBEX_LOCATION_ID", "")
            or settings.WEBEX_DEFAULT_LOCATION_ID
        ).strip().strip('"').strip("'")

    def resolve_person(self, email: str, userid: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Resolves a Control Hub Person ID using primary email, falling back to UPN matching.
        """
        clean_email = (email or "").strip()
        if not clean_email or clean_email == "N/A":
            return None

        # 1. Primary Lookup via explicit email
        person = self.webex.get_person_by_email(clean_email)
        if person:
            return person

        # 2. Fallback Lookup via CUCM User ID / UPN format
        if userid and userid.strip() and userid.strip().lower() != "unassigned":
            default_domain = (os.getenv("DEFAULT_EMAIL_DOMAIN") or get_settings().DEFAULT_EMAIL_DOMAIN).strip()
            domain = clean_email.split("@")[-1] if "@" in clean_email else default_domain
            fallback_email = f"{userid.strip()}@{domain}"
            logger.info("Email lookup failed for '%s'. Attempting fallback UPN '%s'...", clean_email, fallback_email)
            person = self.webex.get_person_by_email(fallback_email)
            if person:
                return person

        return None

    def provision_user_profiles(
        self,
        users: List[UserCallingProfileItem],
        dry_run: bool = True,
        default_license_id: Optional[str] = None,
        default_location_id: Optional[str] = None,
        sync_redsky: bool = True,
    ) -> UserCallingProfileProvisionResponse:
        """
        Processes batch User Calling Profile requests with dry-run protection and automated RedSky MyE911 provisioning.
        """
        results: List[UserCallingProfileResultItem] = []
        successful_count = 0
        failed_count = 0
        skipped_count = 0

        # Cache locations to validate location existence by ID or Name
        locations = self.webex.get_locations()
        location_by_id: Dict[str, str] = {
            str(loc.get("id", "")): str(loc.get("name", ""))
            for loc in locations
            if loc.get("id")
        }
        location_by_name: Dict[str, str] = {
            str(loc.get("name", "")).strip().lower(): str(loc.get("id", ""))
            for loc in locations
            if loc.get("name") and loc.get("id")
        }

        for item in users:
            target_email = item.email.strip()
            raw_loc = (item.location_id or default_location_id or self.server_location_id).strip()
            target_lic = (item.calling_license_id or default_license_id or self.server_license_id).strip()
            extension = item.extension.strip() if item.extension else None
            phone_num = item.phone_number.strip() if item.phone_number else None

            # Validate basic parameters
            if not target_email:
                results.append(
                    UserCallingProfileResultItem(
                        email="UNKNOWN",
                        status="SKIPPED",
                        details="Missing user email address.",
                    )
                )
                skipped_count += 1
                continue

            # Resolve target location by ID or Name
            resolved_loc_id = None
            if raw_loc in location_by_id:
                resolved_loc_id = raw_loc
            elif raw_loc.lower() in location_by_name:
                resolved_loc_id = location_by_name[raw_loc.lower()]

            if not resolved_loc_id:
                results.append(
                    UserCallingProfileResultItem(
                        email=target_email,
                        location_id=raw_loc,
                        extension=extension,
                        phone_number=phone_num,
                        status="FAILED",
                        details=f"Location '{raw_loc}' not found in Webex Control Hub. Complete Phase 1 location setup first.",
                    )
                )
                failed_count += 1
                continue
            target_loc = resolved_loc_id

            if phone_num:
                clean_phone = phone_num.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
                if not clean_phone.startswith("+"):
                    clean_phone = f"+{clean_phone}"
                if not E164_PATTERN.match(clean_phone):
                    results.append(
                        UserCallingProfileResultItem(
                            email=target_email,
                            location_id=target_loc,
                            extension=extension,
                            phone_number=phone_num,
                            status="FAILED",
                            details=f"Phone number '{phone_num}' is not valid E.164 (e.g. +15125550199).",
                        )
                    )
                    failed_count += 1
                    continue
                phone_num = clean_phone

            # Resolve Control Hub Person
            person = self.resolve_person(target_email, item.userid)
            if not person:
                results.append(
                    UserCallingProfileResultItem(
                        email=target_email,
                        location_id=target_loc,
                        extension=extension,
                        phone_number=phone_num,
                        status="FAILED",
                        details=f"User '{target_email}' does not exist in Webex Control Hub. Verify Directory Sync/SCIM status.",
                    )
                )
                failed_count += 1
                continue

            # Auto-detect calling license from existing user profile or org
            if not target_lic:
                user_lics = person.get("licenses", [])
                if user_lics:
                    target_lic = user_lics[0]
                else:
                    try:
                        avail = self.webex.get_user_calling_licenses() if hasattr(self.webex, "get_user_calling_licenses") else []
                        if avail:
                            target_lic = avail[0].get("id", "")
                    except Exception as lic_err:
                        logger.warning("Could not auto-detect user calling licenses: %s", lic_err)

            if not target_lic:
                results.append(
                    UserCallingProfileResultItem(
                        email=target_email,
                        location_id=target_loc,
                        extension=extension,
                        phone_number=phone_num,
                        status="FAILED",
                        details="Webex Calling license ID is required and none was found on user profile or organization.",
                    )
                )
                failed_count += 1
                continue

            person_id = person.get("id", "")
            display_name = person.get("displayName", target_email)

            # AD / Directory Sync values already present on the Webex user take priority over Step 2 inputs
            ad_did = WebexCallingProvider.extract_existing_did(person)
            did_overridden = bool(ad_did and phone_num and ad_did != phone_num)
            effective_phone = ad_did or phone_num
            existing_ext = str(person.get("extension") or "").strip()
            effective_ext = existing_ext or extension

            if dry_run:
                redsky_dry = ""
                redsky_status = None
                if sync_redsky and self.redsky and self.redsky.is_configured() and effective_phone:
                    redsky_status = "READY"
                    redsky_dry = f", RedSky MyE911 sync ready for '{effective_phone}' (Nomadic WFH routing enabled)"

                if did_overridden:
                    did_dry_desc = f"DID '{effective_phone}' (AD synced; priority over Step 2 '{phone_num}')"
                elif ad_did:
                    did_dry_desc = f"DID '{effective_phone}' (AD synced)"
                else:
                    did_dry_desc = f"DID '{effective_phone or 'None'}'"

                results.append(
                    UserCallingProfileResultItem(
                        email=target_email,
                        display_name=display_name,
                        person_id=person_id,
                        location_id=target_loc,
                        extension=effective_ext,
                        phone_number=effective_phone,
                        status="DRY_RUN",
                        caller_id_status=f"Ready ({item.caller_id_type})",
                        voicemail_status=f"Ready (MWI=True, emailNotify={item.voicemail_email_notification})",
                        redsky_mye911_status=redsky_status,
                        details=(
                            f"Validated for profile provisioning: Location '{location_by_id.get(target_loc, target_loc)}', "
                            f"Extension '{effective_ext or 'Managed via Dir Sync'}', {did_dry_desc}, "
                            f"Caller ID '{item.caller_id_type}', Voicemail '{item.voicemail_enabled}'{redsky_dry}."
                        ),
                    )
                )
                successful_count += 1
                continue

            # Live Provisioning
            try:
                res = self.webex.provision_user_calling_profile(
                    person_id=person_id,
                    location_id=target_loc,
                    calling_license_id=target_lic,
                    extension=effective_ext,
                    phone_number=effective_phone,
                    caller_id_type=item.caller_id_type,
                    caller_id_name=item.caller_id_name,
                    voicemail_enabled=item.voicemail_enabled,
                    voicemail_email_notification=item.voicemail_email_notification,
                    voicemail_attach_audio=item.voicemail_attach_audio,
                    voicemail_pin=item.voicemail_pin,
                )

                ext_summary = f"Extension '{effective_ext}'" if effective_ext else "Extension (Dir Sync)"
                did_note = ""
                if did_overridden:
                    did_note = f", AD-synced DID '{effective_phone}' prioritized over Step 2 '{phone_num}'"
                elif ad_did:
                    did_note = f", AD-synced DID '{effective_phone}' preserved"

                # RedSky MyE911 automated provisioning
                redsky_status = None
                redsky_note = ""
                if sync_redsky and self.redsky and self.redsky.is_configured() and effective_phone:
                    try:
                        first_name = person.get("firstName") or ""
                        last_name = person.get("lastName") or ""
                        if not first_name and not last_name and display_name:
                            parts = display_name.split(" ", 1)
                            first_name = parts[0]
                            last_name = parts[1] if len(parts) > 1 else ""

                        r_res = self.redsky.sync_device_user_mye911(
                            email=target_email,
                            phone_number=effective_phone,
                            first_name=first_name,
                            last_name=last_name,
                            callback_number=effective_phone,
                            enable_mye911=True,
                        )
                        redsky_status = "CONFIGURED"
                        redsky_note = f", RedSky MyE911 active for '{effective_phone}' ({r_res.get('action', 'SYNCED')})"
                        logger.info("RedSky MyE911 active for '%s' (%s)", target_email, effective_phone)
                    except Exception as redsky_err:
                        logger.warning("RedSky MyE911 sync warning for '%s': %s", target_email, redsky_err)
                        redsky_status = "WARNING"
                        redsky_note = f" (RedSky MyE911 warning: {redsky_err})"

                results.append(
                    UserCallingProfileResultItem(
                        email=target_email,
                        display_name=display_name,
                        person_id=person_id,
                        location_id=target_loc,
                        extension=effective_ext,
                        phone_number=effective_phone,
                        status="SUCCESS",
                        caller_id_status="CONFIGURED",
                        voicemail_status="CONFIGURED",
                        redsky_mye911_status=redsky_status,
                        details=f"Webex Calling license assigned; Location, {ext_summary}, Caller ID, and Cloud Voicemail active{did_note}{redsky_note}.",
                    )
                )
                successful_count += 1

            except (WebexAPIError, ValueError, KeyError) as exc:
                logger.exception("Calling profile provisioning failed for '%s': %s", target_email, exc)
                results.append(
                    UserCallingProfileResultItem(
                        email=target_email,
                        display_name=display_name,
                        person_id=person_id,
                        location_id=target_loc,
                        extension=effective_ext,
                        phone_number=effective_phone,
                        status="FAILED",
                        details=str(exc),
                    )
                )
                failed_count += 1

        return UserCallingProfileProvisionResponse(
            status="SUCCESS",
            dry_run=dry_run,
            total_evaluated=len(results),
            successful_count=successful_count,
            failed_count=failed_count,
            skipped_count=skipped_count,
            results=results,
        )

    def get_user_calling_profile(self, email_or_id: str) -> Dict[str, Any]:
        """
        Retrieves current Calling entitlements, Caller ID, and Voicemail settings for a user.
        """
        clean_input = email_or_id.strip()
        person: Optional[Dict[str, Any]] = None

        if "@" in clean_input:
            person = self.webex.get_person_by_email(clean_input)
        else:
            try:
                person = self.webex._request("GET", f"/people/{clean_input}?callingData=true")
            except Exception:
                person = None

        if not person:
            raise ValueError(f"Webex person '{email_or_id}' was not found.")

        person_id = person["id"]
        caller_id_data = {}
        voicemail_data = {}

        try:
            caller_id_data = self.webex.get_caller_id(person_id)
        except Exception as exc:
            logger.debug("Could not retrieve caller ID for %s: %s", person_id, exc)

        try:
            voicemail_data = self.webex.get_voicemail_settings(person_id)
        except Exception as exc:
            logger.debug("Could not retrieve voicemail for %s: %s", person_id, exc)

        return {
            "person_id": person_id,
            "display_name": person.get("displayName", ""),
            "emails": person.get("emails", []),
            "licenses": person.get("licenses", []),
            "location_id": person.get("locationId", ""),
            "extension": person.get("extension", ""),
            "phone_numbers": person.get("phoneNumbers", []),
            "caller_id": caller_id_data,
            "voicemail": voicemail_data,
        }
