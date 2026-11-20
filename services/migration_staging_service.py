"""Validate vendor Webex CSV templates against the source inventory manifest."""

import csv
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from pydantic import BaseModel, Field

from services.cucm_audit_service import SOFTPHONE_NAME_RE, is_softphone_device

DEFAULT_MIGRATION_CSV_DIR = Path("imports/migration_csv")
MAC_PATTERN = re.compile(r"^[0-9A-F]{12}$")


class MigrationException(BaseModel):
    source_file: str
    row_number: int
    code: str
    message: str
    severity: str = "ERROR"
    values: Dict[str, str] = Field(default_factory=dict)


class StagedMigrationRow(BaseModel):
    source_file: str
    row_number: int
    status: str
    values: Dict[str, str]
    source_match: Optional[Dict[str, Any]] = None


class MigrationStagingResult(BaseModel):
    csv_directory: str
    locations: List[StagedMigrationRow] = Field(default_factory=list)
    users: List[StagedMigrationRow] = Field(default_factory=list)
    workspaces: List[StagedMigrationRow] = Field(default_factory=list)
    exceptions: List[MigrationException] = Field(default_factory=list)

    @property
    def ready_rows(self) -> int:
        return sum(
            row.status == "READY"
            for rows in (self.locations, self.users, self.workspaces)
            for row in rows
        )


class MigrationStagingService:
    """Build a reviewable Webex target package from vendor CSV templates."""

    FILES = {
        "locations": "webex_locations_import.csv",
        "users": "webex_calling_user_import.csv",
        "workspaces": "webex_workspaces_import.csv",
    }

    def stage(
        self,
        source_manifest: Dict[str, Any],
        csv_directory: Path = DEFAULT_MIGRATION_CSV_DIR,
    ) -> MigrationStagingResult:
        csv_directory = Path(csv_directory)
        result = MigrationStagingResult(csv_directory=str(csv_directory))
        csv_data = {
            key: self._read_csv(csv_directory / filename)
            for key, filename in self.FILES.items()
        }
        locations = {row.get("Location Name", "").strip() for row in csv_data["locations"]}
        source_devices = self._source_devices(source_manifest)
        source_softphones = self._source_softphones(source_manifest)

        result.locations = self._stage_rows(
            "webex_locations_import.csv", csv_data["locations"], result.exceptions,
            required=["Location Name", "Address Line 1", "City", "State", "ZIP", "Country", "Time Zone"],
        )
        result.users = self._stage_rows(
            "webex_calling_user_import.csv", csv_data["users"], result.exceptions,
            required=["Display Name", "Email", "Extension", "Location"],
            locations=locations,
            source_devices=source_devices,
            source_softphones=source_softphones,
            allow_softphone_only=True,
        )
        result.workspaces = self._stage_rows(
            "webex_workspaces_import.csv", csv_data["workspaces"], result.exceptions,
            required=["Workspace Name", "Location Name", "Type", "Calling Behavior", "Extension", "Device Model", "MAC Address"],
            locations=locations,
            source_devices=source_devices,
        )
        self._check_duplicates(result.workspaces, result.exceptions, "MAC Address", "DUPLICATE_WORKSPACE_MAC")
        self._check_duplicates(result.workspaces, result.exceptions, "Extension", "DUPLICATE_WORKSPACE_EXTENSION")
        self._check_duplicates(result.users, result.exceptions, "MAC Address", "DUPLICATE_USER_MAC")
        return result

    def write_ready_csvs(self, result: MigrationStagingResult, output_directory: Path) -> List[str]:
        """Write only READY rows, preserving the vendor column names."""
        output_directory.mkdir(parents=True, exist_ok=True)
        written: List[str] = []
        for key, filename in self.FILES.items():
            rows = [row for row in getattr(result, key) if row.status == "READY"]
            if not rows:
                continue
            path = output_directory / filename
            with path.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0].values.keys()))
                writer.writeheader()
                writer.writerows(row.values for row in rows)
            written.append(str(path))
        return written

    @staticmethod
    def _read_csv(path: Path) -> List[Dict[str, str]]:
        if not path.exists():
            raise FileNotFoundError(f"Migration CSV not found: {path}")
        with path.open(newline="", encoding="utf-8-sig") as handle:
            return [
                {str(key).strip(): (value or "").strip() for key, value in row.items()}
                for row in csv.DictReader(handle)
            ]

    def _stage_rows(
        self,
        source_file: str,
        rows: Iterable[Dict[str, str]],
        exceptions: List[MigrationException],
        required: List[str],
        locations: Optional[set[str]] = None,
        source_devices: Optional[Dict[str, Dict[str, Any]]] = None,
        source_softphones: Optional[Dict[str, Dict[str, Any]]] = None,
        allow_softphone_only: bool = False,
    ) -> List[StagedMigrationRow]:
        staged: List[StagedMigrationRow] = []
        for row_number, values in enumerate(rows, start=2):
            row_exceptions: List[MigrationException] = []
            raw_mac = values.get("MAC Address", "").strip()
            raw_model = values.get("Device Model", "").strip()
            softphone_only = allow_softphone_only and self._is_softphone_user_row(values)

            effective_required = list(required)
            if allow_softphone_only and not softphone_only:
                effective_required.extend(["Device Model", "MAC Address"])

            for field in effective_required:
                if not values.get(field, "").strip():
                    row_exceptions.append(MigrationException(
                        source_file=source_file,
                        row_number=row_number,
                        code="MISSING_REQUIRED_FIELD",
                        message=f"Required field '{field}' is empty.",
                        values=values,
                    ))

            mac = ""
            if raw_mac and not (softphone_only and SOFTPHONE_NAME_RE.match(raw_mac)):
                mac = self._normalize_mac(raw_mac)
                if not MAC_PATTERN.fullmatch(mac):
                    row_exceptions.append(MigrationException(
                        source_file=source_file, row_number=row_number, code="INVALID_MAC",
                        message="MAC Address must contain exactly 12 hexadecimal characters.", values=values,
                    ))

            if locations is not None:
                location = values.get("Location", values.get("Location Name", ""))
                if location and location not in locations:
                    row_exceptions.append(MigrationException(
                        source_file=source_file, row_number=row_number, code="UNKNOWN_LOCATION",
                        message=f"Location '{location}' is not defined in the locations CSV.", values=values,
                    ))

            if softphone_only:
                email_key = values.get("Email", "").strip().lower()
                source_match = source_softphones.get(email_key) if source_softphones and email_key else None
            else:
                source_match = source_devices.get(mac) if source_devices and mac else None
                if source_devices and mac and not source_match:
                    row_exceptions.append(MigrationException(
                        source_file=source_file, row_number=row_number, code="SOURCE_DEVICE_NOT_FOUND",
                        message=f"No CUCM device with MAC '{mac}' was found in the source manifest.",
                        severity="WARNING", values=values,
                    ))

            if row_exceptions:
                exceptions.extend(row_exceptions)
            updated_values = dict(values)
            if mac:
                updated_values["MAC Address"] = mac
            elif softphone_only and not raw_model:
                updated_values["Device Model"] = "Webex App (Softphone)"
            staged.append(StagedMigrationRow(
                source_file=source_file,
                row_number=row_number,
                status="EXCEPTION" if any(error.severity == "ERROR" for error in row_exceptions) else "READY",
                values=updated_values,
                source_match=source_match,
            ))
        return staged

    @staticmethod
    def _is_softphone_user_row(values: Dict[str, str]) -> bool:
        raw_mac = values.get("MAC Address", "").strip()
        raw_model = values.get("Device Model", "").strip()
        endpoint_type = values.get("Endpoint Type", values.get("Device Type", "")).strip().upper()
        if endpoint_type in {"SOFTPHONE", "WEBEX_APP", "SOFTPHONE_ONLY"} and not raw_mac:
            return True
        if raw_mac and SOFTPHONE_NAME_RE.match(raw_mac):
            return True
        if not raw_mac:
            if not raw_model or raw_model.upper() in {"SOFTPHONE", "WEBEX APP", "WEBEX", "NONE", "N/A"}:
                return True
            if is_softphone_device(model_name=raw_model):
                return True
        return False

    @staticmethod
    def _normalize_mac(value: str) -> str:
        return re.sub(r"[^0-9A-Fa-f]", "", value).upper()

    @staticmethod
    def _source_devices(manifest: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        devices = manifest.get("cucm", {}).get("devices", [])
        normalized: Dict[str, Dict[str, Any]] = {}
        for device in devices:
            device_name = str(device.get("device_name", ""))
            mac = device_name[3:] if device_name.upper().startswith("SEP") else ""
            mac = MigrationStagingService._normalize_mac(mac)
            if MAC_PATTERN.fullmatch(mac):
                normalized[mac] = device
        return normalized

    @staticmethod
    def _source_softphones(manifest: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        devices = manifest.get("cucm", {}).get("devices", [])
        indexed: Dict[str, Dict[str, Any]] = {}
        for device in devices:
            device_name = str(device.get("device_name", ""))
            model_name = str(device.get("device_model", ""))
            email = str(device.get("owner_email", "")).strip().lower()
            if not email or email == "n/a":
                continue
            if is_softphone_device(device_name=device_name, model_name=model_name):
                indexed.setdefault(email, device)
        return indexed

    @staticmethod
    def _check_duplicates(
        rows: List[StagedMigrationRow],
        exceptions: List[MigrationException],
        field: str,
        code: str,
    ) -> None:
        grouped: Dict[str, List[StagedMigrationRow]] = {}
        for row in rows:
            value = row.values.get(field, "").strip()
            if value:
                grouped.setdefault(value.upper(), []).append(row)
        for value, matches in grouped.items():
            if len(matches) < 2:
                continue
            for row in matches:
                row.status = "EXCEPTION"
                exceptions.append(MigrationException(
                    source_file=row.source_file,
                    row_number=row.row_number,
                    code=code,
                    message=f"{field} '{value}' appears {len(matches)} times.",
                    severity="WARNING",
                    values=row.values,
                ))