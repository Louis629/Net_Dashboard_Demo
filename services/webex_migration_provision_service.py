"""Provision approved Webex Calling migration rows with dry-run protection."""

import logging
from typing import Any, Dict, List

from pydantic import BaseModel, Field

from providers.webex_calling_provider import WebexAPIError, WebexCallingProvider
from services.cucm_audit_service import SOFTPHONE_NAME_RE, is_softphone_device

logger = logging.getLogger("Webex_Migration_Provision_Service")


class WebexMigrationProvisionItem(BaseModel):
    source_file: str
    row_number: int
    target_type: str
    identity: str
    status: str
    details: str
    webex_id: str = ""


class WebexMigrationProvisionSummary(BaseModel):
    dry_run: bool
    approved: bool
    total_processed: int
    successful_count: int
    failed_count: int
    skipped_count: int
    items: List[WebexMigrationProvisionItem] = Field(default_factory=list)


class WebexMigrationProvisionService:
    def __init__(self, webex_provider: WebexCallingProvider):
        self.webex = webex_provider

    def provision(
        self,
        staging: Dict[str, Any],
        approved: bool = False,
        dry_run: bool = True,
        calling_license_id: str = "",
    ) -> WebexMigrationProvisionSummary:
        rows = [
            ("USER", row) for row in staging.get("users", [])
        ] + [
            ("WORKSPACE", row) for row in staging.get("workspaces", [])
        ]
        items: List[WebexMigrationProvisionItem] = []
        successful = failed = skipped = 0

        if not approved:
            return WebexMigrationProvisionSummary(
                dry_run=dry_run, approved=False, total_processed=len(rows),
                successful_count=0, failed_count=0, skipped_count=len(rows),
                items=[self._item(kind, row, "SKIPPED", "Explicit approval is required before Webex changes.") for kind, row in rows],
            )

        location_ids = self._location_ids()
        for kind, row in rows:
            if row.get("status") != "READY":
                items.append(self._item(kind, row, "SKIPPED", "Row has staging exceptions."))
                skipped += 1
                continue
            values = row.get("values", {})
            location_name = values.get("Location", values.get("Location Name", ""))
            location_id = location_ids.get(location_name)
            if not location_id:
                items.append(self._item(kind, row, "FAILED", f"Webex location '{location_name}' was not found."))
                failed += 1
                continue
            if dry_run:
                items.append(self._item(kind, row, "DRY_RUN", "Validated for provisioning; no Webex API mutation performed."))
                successful += 1
                continue
            try:
                if kind == "USER":
                    item = self._provision_user(values, location_id, calling_license_id)
                    msg = (
                        "Webex Calling user profile provisioned (softphone-only; no physical MPP device)."
                        if item.get("mode") == "SOFTPHONE_ONLY"
                        else "Webex resource provisioned."
                    )
                else:
                    item = self._provision_workspace(values, location_id)
                    msg = "Webex resource provisioned."
                items.append(self._item(kind, row, "SUCCESS", msg, item.get("id", "")))
                successful += 1
            except (WebexAPIError, KeyError, ValueError) as exc:
                items.append(self._item(kind, row, "FAILED", str(exc)))
                failed += 1

        return WebexMigrationProvisionSummary(
            dry_run=dry_run, approved=approved, total_processed=len(rows),
            successful_count=successful, failed_count=failed, skipped_count=skipped,
            items=items,
        )

    def _location_ids(self) -> Dict[str, str]:
        return {
            str(location.get("name", "")).strip(): str(location.get("id", ""))
            for location in self.webex.get_locations()
            if location.get("name") and location.get("id")
        }

    def _provision_user(self, values: Dict[str, str], location_id: str, calling_license_id: str) -> Dict[str, Any]:
        person = self.webex.get_person_by_email(values["Email"])
        if not person:
            raise ValueError(f"Webex person '{values['Email']}' was not found.")
        if not calling_license_id:
            raise ValueError("WEBEX_CALLING_LICENSE_ID is required for user provisioning.")
        ad_did = WebexCallingProvider.extract_existing_did(person)
        existing_ext = str(person.get("extension") or "").strip()
        self.webex.enable_webex_calling_for_person(
            person_id=person["id"], location_id=location_id,
            calling_license_id=calling_license_id,
            extension=existing_ext or values.get("Extension"),
            phone_number=ad_did or values.get("Phone Number"),
        )

        # Configure Caller ID if supported
        caller_id_type = values.get("Caller ID Type", "DIRECT_LINE")
        caller_id_name = values.get("Caller ID Name") or values.get("Display Name")
        if hasattr(self.webex, "configure_caller_id"):
            try:
                self.webex.configure_caller_id(
                    person_id=person["id"],
                    caller_id_type=caller_id_type,
                    external_caller_id_name=caller_id_name,
                )
            except Exception as cid_err:
                logger.warning("Could not set caller ID for %s: %s", values["Email"], cid_err)

        # Configure Voicemail if supported
        if hasattr(self.webex, "configure_voicemail"):
            try:
                self.webex.configure_voicemail(
                    person_id=person["id"],
                    enabled=True,
                    send_email_notification=True,
                    email_copy_of_message=True,
                    notification_email=values["Email"],
                )
            except Exception as vm_err:
                logger.warning("Could not configure voicemail for %s: %s", values["Email"], vm_err)

        raw_mac = values.get("MAC Address", "").strip()
        raw_model = values.get("Device Model", "").strip()
        if not raw_mac or SOFTPHONE_NAME_RE.match(raw_mac) or (not raw_mac and is_softphone_device(model_name=raw_model)):
            logger.info("Skipping physical MPP device registration for softphone-only user '%s'.", values["Email"])
            return {"id": person["id"], "mode": "SOFTPHONE_ONLY"}

        return self.webex.provision_mpp_device(raw_mac, raw_model, location_id, person["id"])

    def _provision_workspace(self, values: Dict[str, str], location_id: str) -> Dict[str, Any]:
        workspace = self.webex.get_workspace_by_name(values["Workspace Name"])
        if not workspace:
            capacity = int(values["Capacity"]) if values.get("Capacity", "").isdigit() else None
            workspace = self.webex.create_workspace(
                values["Workspace Name"], location_id, values.get("Type", "desk"), capacity,
            )
        return self.webex.provision_workspace_device(
            values["MAC Address"], values["Device Model"], location_id, workspace["id"],
        )

    @staticmethod
    def _item(kind: str, row: Dict[str, Any], status: str, details: str, webex_id: str = "") -> WebexMigrationProvisionItem:
        values = row.get("values", {})
        identity = values.get("Email", values.get("Workspace Name", "unknown"))
        return WebexMigrationProvisionItem(
            source_file=row.get("source_file", ""), row_number=row.get("row_number", 0),
            target_type=kind, identity=identity, status=status, details=details, webex_id=webex_id,
        )