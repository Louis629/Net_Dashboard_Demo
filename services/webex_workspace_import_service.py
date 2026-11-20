"""
services/webex_workspace_import_service.py

High-performance Webex Calling Workspace & Device Migration Service.
Replaces third-party Postman CSV tools with an in-house automated pipeline:
- Flexible CSV parsing (handles vendor typos such as locatioName vs locationName)
- Live Control Hub location & license auto-discovery
- Pre-flight validation (MAC regex, duplicate extensions, hardware tier classification)
- Dual-stage execution:
    1. POST /v1/workspaces (with nested calling extension & workspace license)
    2. POST /v1/devices (associating phone MAC & model with the workspace ID)
- Generation of exportable Postman Collections (v2.1) and Cleaned CSV Packages
"""

import csv
import io
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from pydantic import BaseModel, Field

from providers.webex_calling_provider import WebexCallingProvider, WebexAPIError

logger = logging.getLogger("Webex_Workspace_Import_Service")

MAC_CLEAN_RE = re.compile(r"[^0-9A-Fa-f]")
MAC_VALID_RE = re.compile(r"^[0-9A-F]{12}$")

# Tier 1 Models natively supporting Webex Calling MPP
TIER_1_MPP_PATTERNS = [
    re.compile(r"(?:cisco\s*)?(?:cp-)?(?:78\d{2})", re.IGNORECASE),
    re.compile(r"(?:cisco\s*)?(?:cp-)?(?:88\d{2})", re.IGNORECASE),
    re.compile(r"(?:cisco\s*)?(?:cp-)?(?:98\d{2})", re.IGNORECASE),
]

# Legacy End-of-Life Models requiring hardware refresh or ATA gateway
LEGACY_79XX_PATTERNS = [
    re.compile(r"(?:cisco\s*)?(?:cp-)?(?:79\d{2})", re.IGNORECASE),
    re.compile(r"(?:cisco\s*)?(?:cp-)?(?:69\d{2})", re.IGNORECASE),
]


class WorkspaceImportRow(BaseModel):
    row_number: int
    display_name: str
    location_name: str
    location_id: str = ""
    owner_type: str = ""
    owner_email: str = ""
    person_id: str = ""
    workspace_id: str = ""
    type: str = "desk"
    capacity: int = 1
    calling_type: str = "webexCalling"
    extension: str = ""
    phone_number: str = ""
    mac: str = ""
    clean_mac: str = ""
    model: str = ""
    model_tier: str = "TIER_1_MPP_DIRECT"  # TIER_1_MPP_DIRECT, TIER_3_LEGACY, UNKNOWN
    status: str = "READY"  # READY, WARNING, ERROR
    issues: List[str] = Field(default_factory=list)
    selected: bool = True


class WorkspaceValidationSummary(BaseModel):
    total_rows: int
    ready_count: int
    warning_count: int
    error_count: int
    locations_detected: List[str] = Field(default_factory=list)
    locations_mapped: Dict[str, str] = Field(default_factory=dict)
    locations_unmapped: List[str] = Field(default_factory=list)
    workspace_licenses: List[Dict[str, Any]] = Field(default_factory=list)
    default_calling_license_id: Optional[str] = None
    rows: List[WorkspaceImportRow] = Field(default_factory=list)


class WorkspaceImportExecutionItem(BaseModel):
    row_number: int
    display_name: str
    mac: str
    extension: str
    location_name: str
    status: str  # SUCCESS, FAILED, SKIPPED, DRY_RUN
    workspace_id: str = ""
    device_id: str = ""
    error_message: str = ""


class WorkspaceImportExecutionSummary(BaseModel):
    dry_run: bool
    total_processed: int
    successful_count: int
    failed_count: int
    skipped_count: int
    items: List[WorkspaceImportExecutionItem] = Field(default_factory=list)


class WebexWorkspaceImportService:
    """Orchestrates CSV parsing, pre-flight validation, and Webex Calling batch provisioning."""

    def __init__(self, webex_provider: Optional[WebexCallingProvider] = None):
        self.webex = webex_provider or WebexCallingProvider()

    @staticmethod
    def normalize_mac(raw_mac: str) -> str:
        """Strips punctuation and spaces, returns 12-char uppercase hex MAC."""
        return MAC_CLEAN_RE.sub("", raw_mac or "").upper()

    @staticmethod
    def classify_model_tier(model_name: str) -> Tuple[str, Optional[str]]:
        """
        Determines if a phone model is Tier 1 MPP Direct or Legacy requiring refresh.
        Returns (tier, warning_message).
        """
        model = (model_name or "").strip()
        if not model:
            return "UNKNOWN", "Phone model is not specified."

        for pattern in TIER_1_MPP_PATTERNS:
            if pattern.search(model):
                return "TIER_1_MPP_DIRECT", None

        for pattern in LEGACY_79XX_PATTERNS:
            if pattern.search(model):
                return (
                    "TIER_3_LEGACY",
                    f"Model '{model}' is legacy Cisco hardware and does not support Webex MPP firmware. Requires hardware refresh or ATA gateway."
                )

        return "UNKNOWN", f"Model '{model}' is not in the standard MPP direct catalog; verify compatibility."

    def parse_csv(self, content: Any) -> List[Dict[str, str]]:
        """
        Parses CSV string or raw bytes with encoding tolerance.
        Normalizes column headers, gracefully handling vendor typos like 'locatioName'.
        """
        text = ""
        if isinstance(content, bytes):
            for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin1"):
                try:
                    text = content.decode(encoding)
                    break
                except UnicodeDecodeError:
                    continue
            if not text:
                text = content.decode("utf-8", errors="replace")
        else:
            text = str(content)

        # Strip BOM if present
        if text.startswith("\ufeff"):
            text = text[1:]

        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise ValueError("Provided CSV file is empty or missing headers.")

        # Map varied headers to canonical keys
        header_map: Dict[str, str] = {}
        for h in reader.fieldnames:
            clean_h = (h or "").strip()
            norm = clean_h.lower().replace(" ", "").replace("_", "")
            if norm in {"displayname", "workspacename", "name"}:
                header_map[clean_h] = "displayName"
            elif norm in {"locationame", "locationname", "location", "webexcallinglocation"}:
                header_map[clean_h] = "locatioName"
            elif norm in {"type", "workspacetype"}:
                header_map[clean_h] = "type"
            elif norm in {"capacity"}:
                header_map[clean_h] = "capacity"
            elif norm in {"callingtype", "callingbehavior"}:
                header_map[clean_h] = "callingType"
            elif norm in {"extension", "webexcallingextension", "ext", "dn"}:
                header_map[clean_h] = "extension"
            elif norm in {"phonenumber", "webexcallingphonenumber", "did", "e164"}:
                header_map[clean_h] = "phoneNumber"
            elif norm in {"ownertype"}:
                header_map[clean_h] = "ownerType"
            elif norm in {"owneremail"}:
                header_map[clean_h] = "ownerEmail"
            elif norm in {"personid"}:
                header_map[clean_h] = "personId"
            elif norm in {"workspaceid"}:
                header_map[clean_h] = "workspaceId"
            elif norm in {"locationid"}:
                header_map[clean_h] = "locationId"
            elif norm in {"mac", "macaddress", "device"}:
                header_map[clean_h] = "mac"
            elif norm in {"model", "devicemodel", "phonemodel"}:
                header_map[clean_h] = "model"
            else:
                header_map[clean_h] = clean_h

        parsed_rows: List[Dict[str, str]] = []
        for raw in reader:
            normalized_row: Dict[str, str] = {}
            for orig_key, val in raw.items():
                target_key = header_map.get(orig_key, orig_key)
                normalized_row[target_key] = (val or "").strip()

            # Normalize callingType values (e.g. 'Webex Calling' -> 'webexCalling')
            raw_calling = normalized_row.get("callingType", "").strip().lower()
            if "webex" in raw_calling or "calling" in raw_calling:
                normalized_row["callingType"] = "webexCalling"
            elif raw_calling in {"none", "free", "nocalling"}:
                normalized_row["callingType"] = "none"
            elif not raw_calling:
                normalized_row["callingType"] = (
                    "none" if normalized_row.get("ownerType") else "webexCalling"
                )

            # Skip empty rows
            if any(normalized_row.values()):
                parsed_rows.append(normalized_row)

        return parsed_rows

    def validate_batch(
        self,
        raw_rows: List[Dict[str, str]],
        webex_provider: Optional[WebexCallingProvider] = None
    ) -> WorkspaceValidationSummary:
        """
        Validates parsed CSV rows against Webex Control Hub locations and licenses,
        checks MAC formats, detects duplicates, and assigns status.
        """
        provider = webex_provider or self.webex

        # 1. Fetch live Webex Locations
        live_locations: Dict[str, str] = {}
        live_location_names: Dict[str, str] = {}
        try:
            locations_list = provider.get_locations()
            for loc in locations_list:
                loc_name = str(loc.get("name", "")).strip()
                loc_id = str(loc.get("id", "")).strip()
                if loc_name and loc_id:
                    live_locations[loc_name] = loc_id
                    live_locations[loc_name.lower()] = loc_id
                    live_location_names[loc_id] = loc_name
        except Exception:
            logger.exception("Could not fetch live Webex locations for validation.")
            raise

        # 2. Fetch live Webex Licenses
        workspace_licenses: List[Dict[str, Any]] = []
        default_license_id: Optional[str] = None
        if any(not row.get("ownerType") for row in raw_rows):
            try:
                workspace_licenses = provider.get_workspace_calling_licenses()
                if workspace_licenses:
                    default_license_id = workspace_licenses[0].get("id")
            except Exception:
                logger.exception("Could not fetch Webex licenses for validation.")
                raise

        if not default_license_id:
            prov_lic = getattr(provider, "calling_license_id", None)
            if isinstance(prov_lic, str) and prov_lic.strip():
                default_license_id = prov_lic.strip().strip('"').strip("'")
            else:
                env_lic = os.getenv("WEBEX_CALLING_LICENSE_ID", "") or os.getenv("WEBEX_LICENSE_ID", "")
                if env_lic.strip():
                    default_license_id = env_lic.strip().strip('"').strip("'")

        # 3. Analyze rows
        detected_locs: set[str] = set()
        mac_counts: Dict[str, List[int]] = {}
        ext_loc_counts: Dict[Tuple[str, str], List[int]] = {}

        staged_rows: List[WorkspaceImportRow] = []

        prov_loc = getattr(provider, "default_location_id", None)
        if isinstance(prov_loc, str) and prov_loc.strip():
            default_loc_id = prov_loc.strip().strip('"').strip("'")
        else:
            default_loc_id = (os.getenv("WEBEX_DEFAULT_LOCATION_ID", "") or os.getenv("WEBEX_LOCATION_ID", "")).strip().strip('"').strip("'")

        for idx, row in enumerate(raw_rows, start=1):
            display_name = row.get("displayName", f"Workspace-{idx}")
            loc_name = row.get("locatioName") or row.get("locationName") or row.get("Location", "")
            owner_type = row.get("ownerType", "").strip().upper()
            owner_email = row.get("ownerEmail", "").strip()
            person_id = row.get("personId", "").strip()
            workspace_id = row.get("workspaceId", "").strip()
            provided_loc_id = row.get("locationId", "").strip()
            ws_type = row.get("type") or "desk"
            raw_capacity = row.get("capacity", "1")
            try:
                capacity = int(raw_capacity) if raw_capacity else 1
            except ValueError:
                capacity = 1

            calling_type = row.get("callingType") or ("none" if owner_type else "webexCalling")
            extension = row.get("extension", "")
            phone_num = row.get("phoneNumber", "")
            raw_mac = row.get("mac", "")
            clean_mac = self.normalize_mac(raw_mac)
            model = row.get("model", "Cisco 8841")

            if clean_mac:
                mac_counts.setdefault(clean_mac, []).append(idx)
            if extension and loc_name:
                ext_loc_counts.setdefault((loc_name.lower(), extension), []).append(idx)

            tier, tier_warning = self.classify_model_tier(model)

            staged_row = WorkspaceImportRow(
                row_number=idx,
                display_name=display_name,
                location_name=loc_name,
                location_id=provided_loc_id,
                owner_type=owner_type,
                owner_email=owner_email,
                person_id=person_id,
                workspace_id=workspace_id,
                type=ws_type,
                capacity=capacity,
                calling_type=calling_type,
                extension=extension,
                phone_number=phone_num,
                mac=raw_mac,
                clean_mac=clean_mac,
                model=model,
                model_tier=tier,
                status="READY",
                issues=[],
                selected=True
            )

            # Check location mapping
            matched_loc_id = live_locations.get(loc_name) or live_locations.get(loc_name.lower(), "")
            if provided_loc_id:
                if provided_loc_id not in live_location_names:
                    staged_row.issues.append(
                        f"Location ID '{provided_loc_id}' was not found in Webex Control Hub."
                    )
                    staged_row.status = "ERROR"
                elif matched_loc_id and matched_loc_id != provided_loc_id:
                    staged_row.issues.append(
                        f"Location ID '{provided_loc_id}' does not match location '{loc_name}'."
                    )
                    staged_row.status = "ERROR"
                elif loc_name and live_location_names[provided_loc_id].casefold() != loc_name.casefold():
                    staged_row.issues.append(
                        f"Location name '{loc_name}' does not match location ID '{provided_loc_id}'."
                    )
                    staged_row.status = "ERROR"
                else:
                    matched_loc_id = provided_loc_id
                    if not loc_name:
                        loc_name = live_location_names[provided_loc_id]
                        staged_row.location_name = loc_name
            if not matched_loc_id and not loc_name and default_loc_id:
                matched_loc_id = default_loc_id

            if matched_loc_id:
                staged_row.location_id = matched_loc_id
            elif not provided_loc_id:
                staged_row.issues.append(f"Location '{loc_name}' was not found in Webex Control Hub.")
                staged_row.status = "ERROR"

            detected_locs.add(loc_name)

            # Resolve the existing owner for device-assignment CSVs.
            if owner_type == "USER":
                if not person_id and owner_email:
                    try:
                        person = provider.get_person_by_email(owner_email)
                        person_id = str(person.get("id", "")).strip() if person else ""
                    except Exception as exc:
                        logger.warning("Could not resolve Webex user %s: %s", owner_email, exc)
                        staged_row.issues.append(
                            f"Could not resolve Webex user '{owner_email}': {exc}"
                        )
                        staged_row.status = "ERROR"
                staged_row.person_id = person_id
                if not person_id and owner_email and not any(
                    "Could not resolve Webex user" in issue for issue in staged_row.issues
                ):
                    staged_row.issues.append(
                        f"Webex user '{owner_email}' was not found."
                    )
                    staged_row.status = "ERROR"
                elif not person_id and not owner_email:
                    staged_row.issues.append(
                        f"Provide a personId or ownerEmail for USER '{display_name}'."
                    )
                    staged_row.status = "ERROR"
            elif owner_type == "WORKSPACE":
                if not workspace_id and display_name:
                    try:
                        workspace = provider.get_workspace_by_name(display_name)
                        workspace_id = str(workspace.get("id", "")).strip() if workspace else ""
                    except Exception as exc:
                        logger.warning("Could not resolve Webex workspace %s: %s", display_name, exc)
                        staged_row.issues.append(
                            f"Could not resolve Webex workspace '{display_name}': {exc}"
                        )
                        staged_row.status = "ERROR"
                staged_row.workspace_id = workspace_id
                if not workspace_id and not any(
                    "Could not resolve Webex workspace" in issue for issue in staged_row.issues
                ):
                    staged_row.issues.append(
                        f"Existing Webex workspace '{display_name}' was not found; provide its workspaceId."
                    )
                    staged_row.status = "ERROR"
            elif owner_type:
                staged_row.issues.append(
                    f"Unsupported ownerType '{owner_type}'. Use USER or WORKSPACE."
                )
                staged_row.status = "ERROR"

            # Check MAC format
            if not clean_mac:
                staged_row.issues.append("MAC address is missing.")
                staged_row.status = "ERROR"
            elif not MAC_VALID_RE.fullmatch(clean_mac):
                staged_row.issues.append(f"MAC address '{clean_mac}' is invalid. Must be exactly 12 hexadecimal characters.")
                staged_row.status = "ERROR"

            # Check Extension format
            if not owner_type and calling_type == "webexCalling":
                if not extension:
                    staged_row.issues.append("Calling extension is missing for webexCalling type.")
                    staged_row.status = "ERROR"
                elif not extension.isdigit():
                    staged_row.issues.append(f"Extension '{extension}' must contain digits only.")
                    staged_row.status = "ERROR"

            # Check Model tier
            if tier_warning:
                staged_row.issues.append(tier_warning)
                if staged_row.status != "ERROR":
                    staged_row.status = "WARNING"

            staged_rows.append(staged_row)

        # 4. Check for duplicates across rows
        for mac, row_indices in mac_counts.items():
            if len(row_indices) > 1:
                for r_idx in row_indices:
                    row_obj = staged_rows[r_idx - 1]
                    row_obj.issues.append(f"Duplicate MAC address '{mac}' detected in multiple rows ({', '.join(map(str, row_indices))}).")
                    row_obj.status = "ERROR"

        for (loc, ext), row_indices in ext_loc_counts.items():
            if len(row_indices) > 1:
                for r_idx in row_indices:
                    row_obj = staged_rows[r_idx - 1]
                    row_obj.issues.append(f"Duplicate extension '{ext}' in location '{loc}' in rows ({', '.join(map(str, row_indices))}).")
                    row_obj.status = "ERROR"

        # Summary statistics
        ready_count = sum(1 for r in staged_rows if r.status == "READY")
        warning_count = sum(1 for r in staged_rows if r.status == "WARNING")
        error_count = sum(1 for r in staged_rows if r.status == "ERROR")

        mapped_locs = {name: live_locations[name] for name in detected_locs if name in live_locations}
        unmapped_locs = [name for name in detected_locs if name and name not in live_locations and name.lower() not in live_locations]

        return WorkspaceValidationSummary(
            total_rows=len(staged_rows),
            ready_count=ready_count,
            warning_count=warning_count,
            error_count=error_count,
            locations_detected=sorted(list(detected_locs)),
            locations_mapped=mapped_locs,
            locations_unmapped=sorted(unmapped_locs),
            workspace_licenses=workspace_licenses,
            default_calling_license_id=default_license_id,
            rows=staged_rows
        )

    def execute_batch(
        self,
        rows: List[Dict[str, Any]],
        webex_provider: Optional[WebexCallingProvider] = None,
        dry_run: bool = True,
        calling_license_id: Optional[str] = None
    ) -> WorkspaceImportExecutionSummary:
        """
        Registers MPP devices for existing USER/WORKSPACE owners or creates a new
        Calling workspace and associates its device for legacy workspace-import rows.
        """
        provider = webex_provider or self.webex
        prov_lic = getattr(provider, "calling_license_id", None)
        target_license_id = None
        if calling_license_id and isinstance(calling_license_id, str) and calling_license_id.strip():
            target_license_id = calling_license_id.strip().strip('"').strip("'")
        elif isinstance(prov_lic, str) and prov_lic.strip():
            target_license_id = prov_lic.strip().strip('"').strip("'")
        else:
            env_lic = os.getenv("WEBEX_CALLING_LICENSE_ID", "") or os.getenv("WEBEX_LICENSE_ID", "")
            if env_lic.strip():
                target_license_id = env_lic.strip().strip('"').strip("'")

        # If no license provided, discover default workspace license
        if not target_license_id and any(
            item.get("owner_type", "").upper() not in {"USER", "WORKSPACE"}
            for item in rows
        ):
            try:
                licenses = provider.get_workspace_calling_licenses()
                if licenses:
                    target_license_id = licenses[0].get("id")
            except Exception as e:
                logger.warning("Could not auto-resolve workspace calling license: %s", e)

        prov_loc = getattr(provider, "default_location_id", None)
        if isinstance(prov_loc, str) and prov_loc.strip():
            default_loc_id = prov_loc.strip().strip('"').strip("'")
        else:
            default_loc_id = (os.getenv("WEBEX_DEFAULT_LOCATION_ID", "") or os.getenv("WEBEX_LOCATION_ID", "")).strip().strip('"').strip("'")

        # Get location map
        location_map: Dict[str, str] = {}
        try:
            for loc in provider.get_locations():
                if loc.get("name") and loc.get("id"):
                    location_map[loc["name"]] = loc["id"]
                    location_map[loc["name"].lower()] = loc["id"]
        except Exception as e:
            logger.warning("Could not query locations during execution: %s", e)

        results: List[WorkspaceImportExecutionItem] = []
        success_c = 0
        failed_c = 0
        skipped_c = 0

        for item in rows:
            row_num = item.get("row_number", 0)
            display_name = item.get("display_name", "")
            loc_name = item.get("location_name", "")
            loc_id = (
                item.get("location_id")
                or location_map.get(loc_name)
                or location_map.get(loc_name.lower())
                or (default_loc_id if not loc_name else "")
            )
            clean_mac = self.normalize_mac(item.get("clean_mac") or item.get("mac", ""))
            model = item.get("model", "Cisco 8841")
            extension = item.get("extension", "")
            phone_number = item.get("phone_number", "")
            ws_type = item.get("type", "desk")
            capacity = int(item.get("capacity", 1))
            calling_type = item.get("calling_type", "webexCalling")
            owner_type = item.get("owner_type", "").upper()
            person_id = item.get("person_id", "")
            workspace_id = item.get("workspace_id", "")

            # Check if skipped or invalid
            if not item.get("selected", True):
                results.append(WorkspaceImportExecutionItem(
                    row_number=row_num,
                    display_name=display_name,
                    mac=clean_mac,
                    extension=extension,
                    location_name=loc_name,
                    status="SKIPPED",
                    error_message="Row was deselected by user."
                ))
                skipped_c += 1
                continue

            if not loc_id:
                results.append(WorkspaceImportExecutionItem(
                    row_number=row_num,
                    display_name=display_name,
                    mac=clean_mac,
                    extension=extension,
                    location_name=loc_name,
                    status="FAILED",
                    error_message=f"Location '{loc_name}' does not map to any known Webex Location ID."
                ))
                failed_c += 1
                continue

            if not clean_mac or not MAC_VALID_RE.fullmatch(clean_mac):
                results.append(WorkspaceImportExecutionItem(
                    row_number=row_num,
                    display_name=display_name,
                    mac=clean_mac,
                    extension=extension,
                    location_name=loc_name,
                    status="FAILED",
                    error_message=f"Invalid MAC address: '{clean_mac}'."
                ))
                failed_c += 1
                continue

            if owner_type not in {"", "USER", "WORKSPACE"}:
                results.append(WorkspaceImportExecutionItem(
                    row_number=row_num,
                    display_name=display_name,
                    mac=clean_mac,
                    extension=extension,
                    location_name=loc_name,
                    status="FAILED",
                    error_message=f"Unsupported owner type '{owner_type}'."
                ))
                failed_c += 1
                continue

            if owner_type == "USER" and not person_id:
                results.append(WorkspaceImportExecutionItem(
                    row_number=row_num,
                    display_name=display_name,
                    mac=clean_mac,
                    extension=extension,
                    location_name=loc_name,
                    status="FAILED",
                    error_message="No resolved personId was supplied for this USER device."
                ))
                failed_c += 1
                continue

            if owner_type == "WORKSPACE" and not workspace_id:
                results.append(WorkspaceImportExecutionItem(
                    row_number=row_num,
                    display_name=display_name,
                    mac=clean_mac,
                    extension=extension,
                    location_name=loc_name,
                    status="FAILED",
                    error_message="No resolved workspaceId was supplied for this WORKSPACE device."
                ))
                failed_c += 1
                continue

            if dry_run:
                results.append(WorkspaceImportExecutionItem(
                    row_number=row_num,
                    display_name=display_name,
                    mac=clean_mac,
                    extension=extension,
                    location_name=loc_name,
                    status="DRY_RUN",
                    workspace_id=workspace_id if owner_type == "WORKSPACE" else "",
                    error_message="Validation passed; pre-flight simulation completed."
                ))
                success_c += 1
                continue

            # Live Execution
            try:
                if owner_type == "USER":
                    logger.info(
                        "[%d/%d] Registering Device %s (%s) for Person ID %s...",
                        row_num, len(rows), clean_mac, model, person_id
                    )
                    dev_res = provider.provision_mpp_device(
                        mac_address=clean_mac,
                        model=model,
                        location_id=loc_id,
                        person_id=person_id
                    )
                elif owner_type == "WORKSPACE":
                    logger.info(
                        "[%d/%d] Adding Device %s (%s) to Workspace ID %s...",
                        row_num, len(rows), clean_mac, model, workspace_id
                    )
                    dev_res = provider.add_device_to_workspace(
                        mac_address=clean_mac,
                        model=model,
                        workspace_id=workspace_id
                    )
                else:
                    # Legacy workspace-import flow creates a new calling workspace first.
                    logger.info("[%d/%d] Creating Workspace '%s'...", row_num, len(rows), display_name)
                    ws_res = provider.create_workspace_with_calling(
                        display_name=display_name,
                        location_id=loc_id,
                        workspace_type=ws_type,
                        capacity=capacity,
                        calling_type=calling_type,
                        extension=extension or None,
                        phone_number=phone_number or None,
                        calling_license_id=target_license_id
                    )
                    workspace_id = ws_res.get("id", "")
                    if not workspace_id:
                        raise WebexAPIError(f"Webex did not return a valid workspace ID for '{display_name}'.")

                    logger.info(
                        "[%d/%d] Adding Device %s (%s) to Workspace ID %s...",
                        row_num, len(rows), clean_mac, model, workspace_id
                    )
                    dev_res = provider.add_device_to_workspace(
                        mac_address=clean_mac,
                        model=model,
                        workspace_id=workspace_id
                    )
                device_id = dev_res.get("id", "")

                results.append(WorkspaceImportExecutionItem(
                    row_number=row_num,
                    display_name=display_name,
                    mac=clean_mac,
                    extension=extension,
                    location_name=loc_name,
                    status="SUCCESS",
                    workspace_id=workspace_id,
                    device_id=device_id,
                    error_message="Workspace created and phone device successfully registered."
                ))
                success_c += 1

            except (WebexAPIError, Exception) as exc:
                logger.exception("Failed provisioning row %d (%s): %s", row_num, display_name, exc)
                results.append(WorkspaceImportExecutionItem(
                    row_number=row_num,
                    display_name=display_name,
                    mac=clean_mac,
                    extension=extension,
                    location_name=loc_name,
                    status="FAILED",
                    error_message=str(exc)
                ))
                failed_c += 1

        return WorkspaceImportExecutionSummary(
            dry_run=dry_run,
            total_processed=len(rows),
            successful_count=success_c,
            failed_count=failed_c,
            skipped_count=skipped_c,
            items=results
        )

    def generate_postman_collection(
        self,
        rows: List[Dict[str, Any]],
        collection_name: str = "WxC Workspaces Import (Generated)"
    ) -> Dict[str, Any]:
        """
        Generates a Postman Collection v2.1 JSON replicating the third-party vendor's collection.
        Enables engineers to run the exact requests in Postman if desired.
        """
        if any(str(row.get("owner_type", "")).strip() for row in rows):
            raise ValueError(
                "Postman Collection export supports new-workspace rows only; "
                "existing-owner devices must be assigned through the importer."
            )

        collection = {
            "info": {
                "_postman_id": "wxc-workspaces-migrator-generated",
                "name": collection_name,
                "description": "Generated by Net Dashboard - Cisco Call Manager to Webex Calling Migration Suite",
                "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
            },
            "item": [
                {
                    "name": "1. GET All Location Info",
                    "request": {
                        "method": "GET",
                        "header": [
                            {"key": "Authorization", "value": "Bearer {{webex_token}}", "type": "text"},
                            {"key": "Content-Type", "value": "application/json", "type": "text"}
                        ],
                        "url": {
                            "raw": "https://webexapis.com/v1/locations?max=500",
                            "protocol": "https",
                            "host": ["webexapis", "com"],
                            "path": ["v1", "locations"],
                            "query": [{"key": "max", "value": "500"}]
                        }
                    }
                },
                {
                    "name": "2. GET License Information for Workspaces",
                    "request": {
                        "method": "GET",
                        "header": [
                            {"key": "Authorization", "value": "Bearer {{webex_token}}", "type": "text"},
                            {"key": "Content-Type", "value": "application/json", "type": "text"}
                        ],
                        "url": {
                            "raw": "https://webexapis.com/v1/licenses",
                            "protocol": "https",
                            "host": ["webexapis", "com"],
                            "path": ["v1", "licenses"]
                        }
                    }
                },
                {
                    "name": "3. POST Create Workspace Ext Standard",
                    "request": {
                        "method": "POST",
                        "header": [
                            {"key": "Authorization", "value": "Bearer {{webex_token}}", "type": "text"},
                            {"key": "Content-Type", "value": "application/json", "type": "text"}
                        ],
                        "body": {
                            "mode": "raw",
                            "raw": json.dumps({
                                "displayName": "{{displayName}}",
                                "locationId": "{{locationId}}",
                                "capacity": "{{capacity}}",
                                "type": "{{type}}",
                                "calling": {
                                    "type": "{{callingType}}",
                                    "webexCalling": {
                                        "extension": "{{extension}}",
                                        "locationId": "{{locationId}}",
                                        "licenses": ["{{workspaceLicenseId}}"]
                                    }
                                },
                                "supportedDevices": "phones"
                            }, indent=2)
                        },
                        "url": {
                            "raw": "https://webexapis.com/v1/workspaces",
                            "protocol": "https",
                            "host": ["webexapis", "com"],
                            "path": ["v1", "workspaces"]
                        }
                    }
                },
                {
                    "name": "4. POST Add Device to New Workspace",
                    "request": {
                        "method": "POST",
                        "header": [
                            {"key": "Authorization", "value": "Bearer {{webex_token}}", "type": "text"},
                            {"key": "Content-Type", "value": "application/json", "type": "text"}
                        ],
                        "body": {
                            "mode": "raw",
                            "raw": json.dumps({
                                "mac": "{{mac}}",
                                "model": "{{model}}",
                                "workspaceId": "{{workspaceId}}"
                            }, indent=2)
                        },
                        "url": {
                            "raw": "https://webexapis.com/v1/devices",
                            "protocol": "https",
                            "host": ["webexapis", "com"],
                            "path": ["v1", "devices"]
                        }
                    }
                }
            ],
            "variable": [
                {"key": "webex_token", "value": "YOUR_WEBEX_BOT_OR_INTEGRATION_TOKEN_HERE"},
                {"key": "workspaceLicenseId", "value": "YOUR_WORKSPACE_LICENSE_ID_HERE"},
                {"key": "locationId", "value": "YOUR_LOCATION_ID_HERE"}
            ]
        }
        return collection
