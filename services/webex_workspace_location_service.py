"""Preview and safely apply Webex Calling workspace location corrections."""

import logging
from collections import Counter
from copy import deepcopy
from typing import Any, Dict, List, Optional
from uuid import uuid4

from providers.webex_calling_provider import WebexCallingProvider
from services.webex_workspace_import_service import WebexWorkspaceImportService

logger = logging.getLogger("Webex_Workspace_Location_Service")


class WebexWorkspaceLocationService:
    """Migrates existing workspaces and their assigned devices using CSV rows."""

    def __init__(self, webex_provider: Optional[WebexCallingProvider] = None):
        self.webex = webex_provider or WebexCallingProvider()
        self.csv_importer = WebexWorkspaceImportService(self.webex)

    def correct_locations(self, csv_content: str, dry_run: bool = True) -> Dict[str, Any]:
        raw_rows = self.csv_importer.parse_csv(csv_content)
        if not raw_rows:
            raise ValueError("CSV contains no workspace rows.")

        locations = self.webex.get_locations()
        locations_by_name: Dict[str, Dict[str, str]] = {}
        locations_by_id: Dict[str, str] = {}
        for location in locations:
            name = str(location.get("name", "")).strip()
            location_id = str(location.get("id", "")).strip()
            if name and location_id:
                locations_by_name[name.casefold()] = {"id": location_id, "name": name}
                locations_by_id[location_id] = name

        rows = self._prepare_rows(raw_rows, locations_by_name, locations_by_id)
        self._mark_duplicates(rows)
        all_devices: Optional[List[Dict[str, Any]]] = None

        for row in rows:
            if row["status"] == "FAILED":
                continue
            try:
                matches = self.webex.get_workspaces_by_name(row["display_name"])
                exact_matches = [
                    workspace for workspace in matches
                    if str(workspace.get("displayName", "")).strip().casefold()
                    == row["display_name"].casefold()
                ]
                if len(exact_matches) != 1:
                    row["status"] = "FAILED"
                    row["error_message"] = (
                        "No existing workspace matched this name."
                        if not exact_matches
                        else "Multiple existing workspaces matched this name; no change was made."
                    )
                    continue

                workspace = exact_matches[0]
                workspace_id = str(workspace.get("id", "")).strip()
                if workspace_id and workspace.get("calling"):
                    workspace = self.webex.get_workspace(workspace_id)
                current_location_id = self._workspace_location_id(workspace)
                if not workspace_id or not current_location_id:
                    row["status"] = "FAILED"
                    row["error_message"] = "Workspace ID or current location could not be verified."
                    continue

                devices = self.webex.get_devices_by_workspace(workspace_id)
                matching_devices = [
                    device for device in devices
                    if self.csv_importer.normalize_mac(
                        str(device.get("mac") or device.get("macAddress") or "")
                    ) == row["mac"]
                ]
                if len(matching_devices) > 1:
                    row["status"] = "FAILED"
                    row["error_message"] = "Multiple devices with this MAC were found in the workspace."
                    continue

                row["workspace_id"] = workspace_id
                row["current_location"] = locations_by_id.get(current_location_id, current_location_id)
                unassigned_device: Optional[Dict[str, Any]] = None
                if not matching_devices:
                    if all_devices is None:
                        all_devices = self.webex.get_all_devices()
                    registered_matches = [
                        device for device in all_devices
                        if self.csv_importer.normalize_mac(
                            str(device.get("mac") or device.get("macAddress") or "")
                        ) == row["mac"]
                    ]
                    assigned_elsewhere = [
                        device for device in registered_matches
                        if str(device.get("workspaceId") or "").strip()
                        and str(device.get("workspaceId") or "").strip() != workspace_id
                    ]
                    assignment_disagreement = [
                        device for device in registered_matches
                        if str(device.get("workspaceId") or "").strip() == workspace_id
                    ]
                    assigned_to_person = [
                        device for device in registered_matches
                        if device.get("personId") or device.get("person")
                    ]
                    if assigned_elsewhere or assignment_disagreement or assigned_to_person:
                        row["status"] = "FAILED"
                        row["error_message"] = (
                            "The CSV device MAC is already assigned or its assignment conflicts "
                            "with the workspace device lookup; no reassignment was attempted."
                        )
                        continue
                    model = row["model"] or next(
                        (
                            str(device.get("product") or device.get("model") or "").strip()
                            for device in registered_matches
                            if device.get("product") or device.get("model")
                        ),
                        "",
                    )
                    if not model:
                        row["status"] = "FAILED"
                        row["error_message"] = (
                            "The CSV device MAC is not assigned to this workspace, and no model "
                            "is available to register it."
                        )
                        continue
                    unassigned_device = {"mac": row["mac"], "product": model}
                    row["_unassigned_device"] = unassigned_device

                if current_location_id == row["target_location_id"] and not unassigned_device:
                    row["status"] = "UNCHANGED"
                    row["error_message"] = "Workspace is already in the requested location."
                    continue

                if (
                    current_location_id != row["target_location_id"]
                    and workspace.get("calling", {}).get("type") == "webexCalling"
                ):
                    current_extension = workspace.get("calling", {}).get(
                        "webexCalling", {}
                    ).get("extension")
                    target_extension = row["extension"] or str(current_extension or "").strip()
                    if not target_extension:
                        row["status"] = "FAILED"
                        row["error_message"] = (
                            "No target extension is present in the CSV or current Webex Calling setup."
                        )
                        continue

                    device_error = self._validate_mpp_devices(devices)
                    if device_error:
                        row["status"] = "FAILED"
                        row["error_message"] = device_error
                        continue

                    row["_migration"] = {
                        "workspace": workspace,
                        "devices": devices,
                        "unassigned_device": unassigned_device,
                        "unassigned_device": unassigned_device,
                        "target_extension": target_extension,
                        "target_phone_number": row["phone_number"]
                        or workspace.get("calling", {}).get("webexCalling", {}).get("phoneNumber"),
                    }
                    row["status"] = "DRY_RUN" if dry_run else "READY"
                    row["error_message"] = (
                        "Preview only; will create and verify a replacement workspace with Calling "
                        "and its MPP devices at the target location, register the unassigned CSV phone "
                        "if needed, then delete the original."
                        if dry_run
                        else ""
                    )
                    continue

                if current_location_id != row["target_location_id"] and workspace.get("calling"):
                    row["status"] = "FAILED"
                    row["error_message"] = (
                        f"Calling type '{workspace['calling'].get('type', 'unknown')}' is not supported "
                        "by this migration workflow."
                    )
                    continue

                row["_workspace"] = workspace
                row["status"] = "DRY_RUN" if dry_run else "READY"
                row["error_message"] = (
                    (
                        "Preview only; the unassigned CSV phone will be registered to this workspace."
                        if unassigned_device
                        else "Preview only; no changes have been made."
                    )
                    if dry_run
                    else ""
                )
            except Exception as exc:
                logger.exception(
                    "Failed resolving workspace location for CSV row %d (%s)",
                    row["row_number"], row["display_name"]
                )
                row["status"] = "FAILED"
                row["error_message"] = str(exc)

        if not dry_run:
            for row in rows:
                if row["status"] != "READY":
                    continue
                try:
                    migration = row.pop("_migration", None)
                    registering_unassigned_device = bool(row.get("_unassigned_device"))
                    if migration:
                        self._migrate_calling_workspace(row, migration)
                    else:
                        workspace = row.pop("_workspace")
                        if row["current_location"] != row["target_location"]:
                            self.webex.update_workspace_location(
                                workspace,
                                row["target_location_id"],
                            )
                        unassigned_device = row.pop("_unassigned_device", None)
                        if unassigned_device:
                            self.webex.add_device_to_workspace(
                                mac_address=unassigned_device["mac"],
                                model=unassigned_device["product"],
                                workspace_id=row["workspace_id"],
                            )
                    row["status"] = "SUCCESS"
                    if migration:
                        row["error_message"] = (
                            "Workspace location updated; Calling restored and assigned MPP devices re-associated."
                            + (
                                " Previously unassigned CSV phone registered to the replacement workspace."
                                if registering_unassigned_device
                                else ""
                            )
                        )
                    elif registering_unassigned_device:
                        row["error_message"] = (
                            (
                                "Workspace location confirmed"
                                if row["current_location"] == row["target_location"]
                                else "Workspace location updated"
                            )
                            + " and previously unassigned CSV phone registered."
                        )
                    else:
                        row["error_message"] = "Workspace location updated successfully."
                except Exception as exc:
                    logger.exception(
                        "Workspace location migration failed for CSV row %d (%s) at stage %s",
                        row["row_number"], row["display_name"], row.get("migration_stage", "location update")
                    )
                    row["status"] = "FAILED"
                    row["error_message"] = (
                        f"Migration failed during {row.get('migration_stage', 'location update')}: {exc}. "
                        "The operation may be partially complete; inspect the workspace and device "
                        "assignments in Control Hub before retrying."
                    )
                finally:
                    row.pop("_workspace", None)
                    row.pop("_migration", None)
                    row.pop("_unassigned_device", None)
        else:
            for row in rows:
                row.pop("_workspace", None)
                row.pop("_migration", None)
                row.pop("_unassigned_device", None)

        successful_count = sum(row["status"] in {"DRY_RUN", "SUCCESS"} for row in rows)
        unchanged_count = sum(row["status"] == "UNCHANGED" for row in rows)
        failed_count = sum(row["status"] == "FAILED" for row in rows)
        return {
            "dry_run": dry_run,
            "total_processed": len(rows),
            "successful_count": successful_count,
            "unchanged_count": unchanged_count,
            "failed_count": failed_count,
            "items": rows,
        }

    def recover_device_assignments(
        self,
        csv_content: str,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        raw_rows = self.csv_importer.parse_csv(csv_content)
        if not raw_rows:
            raise ValueError("CSV contains no workspace rows.")

        rows: List[Dict[str, Any]] = []
        for row_number, raw in enumerate(raw_rows, start=2):
            row = {
                "row_number": row_number,
                "display_name": raw.get("displayName", "").strip(),
                "mac": self.csv_importer.normalize_mac(raw.get("mac", "")),
                "model": raw.get("model", "").strip(),
                "phone_number": raw.get("phoneNumber", "").strip(),
                "current_location": "",
                "status": "READY",
                "error_message": "",
            }
            if not row["display_name"]:
                row["status"] = "FAILED"
                row["error_message"] = "Workspace displayName is required."
            elif not row["mac"] or len(row["mac"]) != 12 or any(
                ch not in "0123456789ABCDEF" for ch in row["mac"]
            ):
                row["status"] = "FAILED"
                row["error_message"] = f"Invalid MAC address: '{raw.get('mac', '')}'."
            elif not row["model"]:
                row["status"] = "FAILED"
                row["error_message"] = "Device model is required to restore its assignment."
            rows.append(row)

        self._mark_duplicates(rows)
        all_devices: Optional[List[Dict[str, Any]]] = None
        for row in rows:
            if row["status"] == "FAILED":
                continue
            try:
                matches = self.webex.get_workspaces_by_name(row["display_name"])
                exact_matches = [
                    workspace for workspace in matches
                    if str(workspace.get("displayName", "")).strip().casefold()
                    == row["display_name"].casefold()
                ]
                if len(exact_matches) != 1:
                    row["status"] = "FAILED"
                    row["error_message"] = (
                        "No existing workspace matched this name."
                        if not exact_matches
                        else "Multiple existing workspaces matched this name; no change was made."
                    )
                    continue

                workspace_id = str(exact_matches[0].get("id", "")).strip()
                if not workspace_id:
                    row["status"] = "FAILED"
                    row["error_message"] = "Matched workspace has no ID."
                    continue

                workspace = self.webex.get_workspace(workspace_id)
                if workspace.get("calling", {}).get("type") != "webexCalling":
                    row["status"] = "FAILED"
                    row["error_message"] = (
                        "The original workspace is not currently configured for Webex Calling."
                    )
                    continue
                row["workspace_id"] = workspace_id
                row["current_location"] = self._workspace_location_id(workspace)

                assigned_devices = self.webex.get_devices_by_workspace(workspace_id)
                mac_matches = [
                    device for device in assigned_devices
                    if self.csv_importer.normalize_mac(
                        str(device.get("mac") or device.get("macAddress") or "")
                    ) == row["mac"]
                ]
                if mac_matches:
                    row["status"] = "UNCHANGED"
                    row["error_message"] = "Device is already assigned to this workspace."
                    continue

                if all_devices is None:
                    all_devices = self.webex.get_all_devices()
                elsewhere = [
                    device for device in all_devices
                    if self.csv_importer.normalize_mac(
                        str(device.get("mac") or device.get("macAddress") or "")
                    ) == row["mac"]
                ]
                if elsewhere:
                    row["status"] = "FAILED"
                    row["error_message"] = (
                        "This MAC is still registered elsewhere in Webex; no reassignment was attempted."
                    )
                    continue

                row["status"] = "DRY_RUN" if dry_run else "READY"
                row["error_message"] = (
                    "Preview only; device is unassigned and can be restored to this workspace."
                    if dry_run
                    else ""
                )
            except Exception as exc:
                logger.exception(
                    "Failed validating device recovery for CSV row %d (%s)",
                    row["row_number"], row["display_name"],
                )
                row["status"] = "FAILED"
                row["error_message"] = str(exc)

        if not dry_run:
            for row in rows:
                if row["status"] != "READY":
                    continue
                try:
                    self.webex.add_device_to_workspace(
                        mac_address=row["mac"],
                        model=row["model"],
                        workspace_id=row["workspace_id"],
                    )
                    row["status"] = "SUCCESS"
                    row["error_message"] = "Device assignment restored to the original workspace."
                except Exception as exc:
                    logger.exception(
                        "Failed restoring device %s to workspace %s",
                        row["mac"], row["workspace_id"],
                    )
                    row["status"] = "FAILED"
                    row["error_message"] = str(exc)

        successful_count = sum(row["status"] in {"DRY_RUN", "SUCCESS"} for row in rows)
        unchanged_count = sum(row["status"] == "UNCHANGED" for row in rows)
        failed_count = sum(row["status"] == "FAILED" for row in rows)
        return {
            "dry_run": dry_run,
            "total_processed": len(rows),
            "successful_count": successful_count,
            "unchanged_count": unchanged_count,
            "failed_count": failed_count,
            "items": rows,
        }

    def _prepare_rows(
        self,
        raw_rows: List[Dict[str, str]],
        locations_by_name: Dict[str, Dict[str, str]],
        locations_by_id: Dict[str, str],
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for row_number, raw in enumerate(raw_rows, start=2):
            display_name = raw.get("displayName", "").strip()
            location_name = raw.get("locatioName", "").strip()
            mac = self.csv_importer.normalize_mac(raw.get("mac", ""))
            location_id = raw.get("locationId", "").strip()
            matched_location = locations_by_name.get(location_name.casefold()) if location_name else None
            location_error = ""
            if location_id:
                target_location_name = locations_by_id.get(location_id)
                if target_location_name:
                    if location_name and target_location_name.casefold() != location_name.casefold():
                        location_error = (
                            f"Target locationName '{location_name}' does not match "
                            f"locationId '{location_id}'."
                        )
                    else:
                        matched_location = {"id": location_id, "name": target_location_name}
                else:
                    matched_location = None

            row: Dict[str, Any] = {
                "row_number": row_number,
                "display_name": display_name,
                "mac": mac,
                "model": raw.get("model", "").strip(),
                "extension": raw.get("extension", "").strip(),
                "phone_number": raw.get("phoneNumber", "").strip(),
                "location_name": location_name or (matched_location or {}).get("name", ""),
                "current_location": "",
                "target_location": (matched_location or {}).get("name", ""),
                "target_location_id": (matched_location or {}).get("id", ""),
                "workspace_id": "",
                "status": "READY",
                "error_message": "",
            }
            if not display_name:
                row["status"] = "FAILED"
                row["error_message"] = "Workspace displayName is required."
            elif location_error:
                row["status"] = "FAILED"
                row["error_message"] = location_error
            elif not location_name and not location_id:
                row["status"] = "FAILED"
                row["error_message"] = "Target locationName or locationId is required."
            elif not matched_location:
                row["status"] = "FAILED"
                target = location_id if location_id else location_name
                row["error_message"] = f"Target location '{target}' was not found."
            elif not mac or len(mac) != 12 or any(ch not in "0123456789ABCDEF" for ch in mac):
                row["status"] = "FAILED"
                row["error_message"] = f"Invalid MAC address: '{raw.get('mac', '')}'."
            rows.append(row)
        return rows

    @staticmethod
    def _validate_mpp_devices(devices: List[Dict[str, Any]]) -> str:
        for device in devices:
            if not device.get("id") or not device.get("mac"):
                return "A workspace device is missing its ID or MAC address; migration was blocked."
            if str(device.get("type", "")).casefold() == "roomdesk":
                return (
                    f"Device '{device.get('displayName', device.get('id'))}' is a RoomOS device. "
                    "This workflow only re-associates MPP devices; no changes were made."
                )
            if not (device.get("product") or device.get("model")):
                return (
                    f"Device '{device.get('displayName', device.get('id'))}' has no model identifier; "
                    "migration was blocked."
                )
        return ""

    def _migrate_calling_workspace(
        self,
        row: Dict[str, Any],
        migration: Dict[str, Any],
    ) -> None:
        workspace = migration["workspace"]
        workspace_id = str(workspace["id"])
        target_location_id = row["target_location_id"]
        original_name = str(workspace["displayName"])
        replacement_name = f"{original_name[:80]} [move-{uuid4().hex[:8]}]"
        base_fields = {
            "displayName": replacement_name,
            "capacity": workspace.get("capacity", 1),
            "type": workspace.get("type", "notSet"),
            "notes": workspace.get("notes"),
            "hotdeskingStatus": workspace.get("hotdeskingStatus", "off"),
            "supportedDevices": workspace.get("supportedDevices", "phones"),
        }
        base_fields = {key: value for key, value in base_fields.items() if value is not None}
        original_calling = deepcopy(workspace["calling"])
        original_calling_config = original_calling["webexCalling"]
        original_calling_config["locationId"] = target_location_id
        original_calling_config["extension"] = migration["target_extension"]
        if migration.get("target_phone_number"):
            original_calling_config["phoneNumber"] = migration["target_phone_number"]
        replacement_calling = deepcopy(original_calling)
        replacement_calling["webexCalling"].pop("phoneNumber", None)

        licenses = original_calling_config.get("licenses") or []
        primary_license_id = licenses[0] if licenses else None
        replacement_id = ""
        removed_devices: List[Dict[str, Any]] = []
        replacement_device_candidates = list(migration["devices"])
        unassigned_device = migration.get("unassigned_device")
        if unassigned_device:
            replacement_device_candidates.append(unassigned_device)
        original_deleted = False

        try:
            row["migration_stage"] = "creating replacement workspace"
            replacement = self.webex.create_workspace_with_calling(
                display_name=replacement_name,
                location_id=target_location_id,
                workspace_type=str(workspace.get("type") or "desk"),
                capacity=int(workspace.get("capacity") or 1),
                calling_type="webexCalling",
                extension=str(migration["target_extension"]),
                phone_number=None,
                calling_license_id=primary_license_id,
                supported_devices=str(workspace.get("supportedDevices") or "phones"),
            )
            replacement_id = str(replacement.get("id", "")).strip()
            if not replacement_id:
                raise RuntimeError("Webex did not return an ID for the replacement workspace.")
            row["replacement_workspace_id"] = replacement_id

            row["migration_stage"] = "preserving workspace settings"
            self.webex.update_workspace(
                replacement_id,
                {
                    **base_fields,
                    "locationId": target_location_id,
                    "calling": replacement_calling,
                },
            )

            for device in migration["devices"]:
                row["migration_stage"] = f"moving device {device['mac']} to replacement"
                self.webex.delete_device(str(device["id"]))
                removed_devices.append(device)
                self.webex.add_device_to_workspace(
                    mac_address=str(device["mac"]),
                    model=str(device.get("product") or device.get("model")),
                    workspace_id=replacement_id,
                )
            if unassigned_device:
                row["migration_stage"] = (
                    f"registering previously unassigned device {unassigned_device['mac']} "
                    "to replacement"
                )
                self.webex.add_device_to_workspace(
                    mac_address=str(unassigned_device["mac"]),
                    model=str(unassigned_device["product"]),
                    workspace_id=replacement_id,
                )
            row["migration_stage"] = "verifying replacement workspace and devices"
            created_workspace = self.webex.get_workspace(replacement_id)
            replacement_calling_config = (
                created_workspace.get("calling", {}).get("webexCalling", {})
            )
            if (
                self._workspace_location_id(created_workspace) != target_location_id
                or created_workspace.get("calling", {}).get("type") != "webexCalling"
                or replacement_calling_config.get("locationId") != target_location_id
                or str(replacement_calling_config.get("extension") or "")
                != str(migration["target_extension"])
            ):
                raise RuntimeError(
                    "Replacement workspace location, Calling extension, or Calling configuration did not verify."
                )
            replacement_devices = self.webex.get_devices_by_workspace(replacement_id)
            replacement_macs = {
                self.csv_importer.normalize_mac(
                    str(device.get("mac") or device.get("macAddress") or "")
                )
                for device in replacement_devices
            }
            missing_macs = [
                str(device["mac"])
                for device in replacement_device_candidates
                if self.csv_importer.normalize_mac(str(device["mac"])) not in replacement_macs
            ]
            if missing_macs:
                raise RuntimeError(
                    "Replacement workspace is missing device assignments for "
                    + ", ".join(missing_macs)
                    + "."
                )

            row["migration_stage"] = "deleting original workspace after replacement verification"
            self.webex.delete_workspace(workspace_id)
            original_deleted = True

            row["migration_stage"] = "restoring Calling number and original workspace name"
            final_fields = dict(base_fields)
            final_fields["displayName"] = original_name
            final_fields["locationId"] = target_location_id
            final_fields["calling"] = original_calling
            self.webex.update_workspace(replacement_id, final_fields)

            final_workspace = self.webex.get_workspace(replacement_id)
            final_calling_config = (
                final_workspace.get("calling", {}).get("webexCalling", {})
            )
            if (
                final_workspace.get("displayName") != original_name
                or self._workspace_location_id(final_workspace) != target_location_id
                or final_workspace.get("calling", {}).get("type") != "webexCalling"
                or str(final_calling_config.get("extension") or "")
                != str(migration["target_extension"])
                or (
                    migration.get("target_phone_number")
                    and str(final_calling_config.get("phoneNumber") or "")
                    != str(migration["target_phone_number"])
                )
            ):
                raise RuntimeError(
                    "Replacement exists, but its final name or location did not verify."
                )
            row["workspace_id"] = replacement_id
            row.pop("migration_stage", None)
        except Exception as migration_error:
            if original_deleted:
                raise RuntimeError(
                    f"{migration_error} The original workspace was deleted; replacement "
                    f"workspace {replacement_id or '(ID unavailable)'} may need manual completion."
                ) from migration_error

            recovery_errors = self._rollback_replacement(
                workspace_id=workspace_id,
                replacement_id=replacement_id,
                devices=removed_devices,
                replacement_device_candidates=replacement_device_candidates,
            )
            if recovery_errors:
                raise RuntimeError(
                    f"{migration_error}; rollback was incomplete: "
                    + "; ".join(recovery_errors)
                    + ". Reconcile both workspace records and device assignments in Control Hub."
                ) from migration_error
            raise RuntimeError(
                f"{migration_error}; original workspace and device assignments were preserved."
            ) from migration_error

    def _rollback_replacement(
        self,
        workspace_id: str,
        replacement_id: str,
        devices: List[Dict[str, Any]],
        replacement_device_candidates: Optional[List[Dict[str, Any]]] = None,
    ) -> List[str]:
        errors: List[str] = []
        replacement_device_candidates = replacement_device_candidates or devices
        if replacement_id:
            try:
                replacement_devices = self.webex.get_devices_by_workspace(replacement_id)
            except Exception as exc:
                replacement_devices = []
                errors.append(f"could not inspect replacement device assignments: {exc}")
            for attached in replacement_devices:
                attached_mac = self.csv_importer.normalize_mac(
                    str(attached.get("mac") or attached.get("macAddress") or "")
                )
                if any(
                    self.csv_importer.normalize_mac(str(device.get("mac", ""))) == attached_mac
                    for device in replacement_device_candidates
                ):
                    try:
                        self.webex.delete_device(str(attached["id"]))
                    except Exception as exc:
                        errors.append(f"could not detach {attached_mac} from replacement: {exc}")

        for device in devices:
            try:
                self.webex.add_device_to_workspace(
                    mac_address=str(device["mac"]),
                    model=str(device.get("product") or device.get("model")),
                    workspace_id=workspace_id,
                )
            except Exception as exc:
                errors.append(f"could not restore {device['mac']} to original workspace: {exc}")

        if replacement_id:
            try:
                self.webex.delete_workspace(replacement_id)
            except Exception as exc:
                errors.append(f"could not remove temporary replacement {replacement_id}: {exc}")
        return errors

    @staticmethod
    def _workspace_location_id(workspace: Dict[str, Any]) -> str:
        location_id = workspace.get("locationId")
        if not location_id:
            calling = workspace.get("calling", {})
            if isinstance(calling, dict):
                webex_calling = calling.get("webexCalling", {})
                if isinstance(webex_calling, dict):
                    location_id = webex_calling.get("locationId")
        return str(location_id or "").strip()

    @staticmethod
    def _mark_duplicates(rows: List[Dict[str, Any]]) -> None:
        names = Counter(
            row["display_name"].casefold()
            for row in rows
            if row["status"] != "FAILED" and row["display_name"]
        )
        macs = Counter(
            row["mac"]
            for row in rows
            if row["status"] != "FAILED" and row["mac"]
        )
        for row in rows:
            if row["status"] == "FAILED":
                continue
            if names[row["display_name"].casefold()] > 1:
                row["status"] = "FAILED"
                row["error_message"] = "Workspace name appears more than once in the CSV."
            elif macs[row["mac"]] > 1:
                row["status"] = "FAILED"
                row["error_message"] = "Device MAC appears more than once in the CSV."
