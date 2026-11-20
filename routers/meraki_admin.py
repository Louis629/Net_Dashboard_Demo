"""
routers/meraki_admin.py

Cisco Meraki Dashboard Administration and Appliance Operations Router.
Handles network and configuration template discovery, cache warm-up, and hardware appliance swaps.
"""

import csv
import io
import logging
import re
from pathlib import Path
from typing import Optional

try:
    import usaddress
except ImportError:
    usaddress = None

from fastapi import APIRouter, Depends, HTTPException, Response, status
from starlette.concurrency import run_in_threadpool

from celery_app import execute_meraki_appliance_swap_task
from core.dependencies import get_meraki_provider
from models.diagnostics import CacheWarmRequest
from models.ndg import ApplianceSwapRequest
from providers.meraki_provider import MerakiProvider
from security.audit import log_action
from security.auth import (
    AuthFactory,
    CurrentUser,
    enforce_routing_auth,
    get_current_user,
)
from security.roles import Role

logger = logging.getLogger("Net_Dashboard.MerakiAdmin")
router = APIRouter(tags=["Meraki Administration"])
MERAKI_SWAP_ALLOWED_ROLES = {Role.LOCAL_TECH_SERVICES, Role.NETWORK_ADMIN}


def _require_meraki_swap_role(current_user: CurrentUser) -> None:
    if current_user.role not in MERAKI_SWAP_ALLOWED_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access Denied: Meraki Appliance Swap is restricted to LocalTechServices and NetworkAdmin groups.",
        )


US_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC", "PR", "VI", "GU", "MP", "AS",
}

US_STATE_NAMES = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE",
    "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID",
    "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS",
    "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD",
    "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN", "MISSISSIPPI": "MS",
    "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY",
    "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK",
    "OREGON": "OR", "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC",
    "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT",
    "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA", "WEST VIRGINIA": "WV",
    "WISCONSIN": "WI", "WYOMING": "WY", "DISTRICT OF COLUMBIA": "DC",
    "PUERTO RICO": "PR", "VIRGIN ISLANDS": "VI", "GUAM": "GU",
}

STREET_SUFFIXES = [
    "ALLEY", "ALY", "AVENUE", "AVE", "BOULEVARD", "BLVD", "BEND", "BLUFF",
    "CIRCLE", "CIR", "COURT", "CT", "CROSSING", "XING", "DRIVE", "DR",
    "EXPRESSWAY", "EXPY", "FREEWAY", "FWY", "HIGHWAY", "HWY", "LANE", "LN",
    "LOOP", "PARKWAY", "PKWY", "PLACE", "PL", "PLAZA", "PLZ", "ROAD", "RD",
    "ROW", "RUN", "STREET", "ST", "TERRACE", "TER", "TRAIL", "TRL",
    "TURNPIKE", "TPKE", "WAY", "WALK", "TRACE", "TRCE", "PATH",
]

UNIT_KEYWORDS = [
    "SUITE", "STE", "UNIT", "APT", "APARTMENT", "BLDG", "BUILDING",
    "ROOM", "RM", "DEPT", "DEPARTMENT", "FL", "FLOOR", "SPACE", "SPC", "LOT", "#",
]

HWY_PATTERN = re.compile(
    r"(?i)\b(?:HWY|HIGHWAY|FM|RM|RR|CR|STATE HIGHWAY|SH|ROUTE|RTE|US|US-|I-|IH-|INTERSTATE|LOOP)\s*([0-9]+[A-Z]?)\b"
)

KNOWN_CITIES = {
    "CORPUS CHRISTI", "SAN ANTONIO", "FORT WORTH", "EL PASO", "ROUND ROCK",
    "CEDAR PARK", "SUGAR LAND", "COLLEGE STATION", "WICHITA FALLS", "NEW BRAUNFELS",
    "THE WOODLANDS", "MISSOURI CITY", "GRAND PRAIRIE", "NORTH RICHLAND HILLS",
    "BAY TOWN", "LAKE JACKSON", "PORT ARTHUR", "SAN ANGELO", "SAN MARCOS",
    "SALT LAKE CITY", "NEW YORK", "LOS ANGELES", "SAN FRANCISCO", "SAN DIEGO",
    "KANSAS CITY", "OKLAHOMA CITY", "BATON ROUGE", "NEW ORLEANS", "LAS VEGAS",
    "DALLAS", "HOUSTON", "AUSTIN", "PLANO", "IRVING", "GARLAND", "FRISCO",
    "MCKINNEY", "CARROLLTON", "CARROLTON", "DENTON", "MIDLAND", "WACO",
    "ABILENE", "ODESSA", "BEAUMONT", "TYLER", "MESQUITE", "ROCKPORT",
    "PORTLAND", "CELINA", "KELLER", "TEMPLE", "KILLEEN", "LAREDO",
    "MCALLEN", "BROWNSVILLE", "HARLINGEN", "EDINBURG", "CONROE", "PEARLAND",
    "LEAGUE CITY", "PASADENA", "KATY", "SPRING", "VICTORIA", "HONOLULU",
    "MEMPHIS", "NASHVILLE", "KNOXVILLE", "CHATTANOOGA", "PROVIDENCE", "WARWICK",
    "ATLANTA", "CHICAGO", "SEATTLE", "DENVER", "PHOENIX", "TUCSON",
}


def _normalize_state(raw_state: str) -> str:
    cleaned = (raw_state or "").strip().rstrip(".").upper()
    if cleaned in US_STATE_CODES:
        return cleaned
    if cleaned in US_STATE_NAMES:
        return US_STATE_NAMES[cleaned]
    return cleaned


def _parse_webex_location_address(address: str) -> dict[str, str]:
    raw_address = (address or "").strip()
    empty_fields = {
        "Address Line 1": "",
        "City": "",
        "State": "",
        "ZIP": "",
        "Country": "",
    }
    if not raw_address:
        return empty_fields

    # Normalize multiline / whitespace-heavy addresses into single line
    raw_lines = [line.strip().strip(",") for line in str(raw_address).splitlines() if line.strip()]
    normalized_raw = ", ".join(raw_lines) if raw_lines else str(raw_address).strip()

    # Attempt usaddress if available
    if usaddress is not None:
        try:
            parsed, _ = usaddress.tag(normalized_raw)
        except Exception:
            parsed = {}

        if parsed:
            base_labels = (
                "AddressNumberPrefix",
                "AddressNumber",
                "AddressNumberSuffix",
                "StreetNamePreDirectional",
                "StreetNamePreModifier",
                "StreetNamePreType",
                "StreetName",
                "StreetNamePostType",
                "StreetNamePostDirectional",
                "StreetNamePostModifier",
                "USPSBoxType",
                "USPSBoxID",
            )
            unit_labels = (
                "OccupancyType",
                "OccupancyIdentifier",
                "SubaddressType",
                "SubaddressIdentifier",
            )
            base_line = " ".join(parsed[label].strip() for label in base_labels if parsed.get(label))
            unit_line = " ".join(parsed[label].strip() for label in unit_labels if parsed.get(label))
            if base_line and unit_line:
                if unit_line.startswith("#"):
                    address_line = f"{base_line} {unit_line}"
                else:
                    address_line = f"{base_line}, {unit_line}"
            else:
                address_line = base_line or unit_line

            address_line = " ".join(address_line.split())
            city = " ".join(parsed.get("PlaceName", "").split())
            state = _normalize_state(parsed.get("StateName", ""))
            postal_code = parsed.get("ZipCode", "").strip()
            if all((address_line, city, state, postal_code)):
                country = parsed.get("CountryName", "") or "United States"
                return {
                    "Address Line 1": address_line,
                    "City": city,
                    "State": state,
                    "ZIP": postal_code,
                    "Country": country,
                }

    # Resilient multi-pass heuristic parser
    cleaned = " ".join(normalized_raw.split())

    # 1. Strip Country if present
    country = "United States"
    country_match = re.search(
        r"(?i)(?:,\s*|\s+)(?:USA|U\.S\.A\.|United States(?: of America)?|US|U\.S\.)\s*$",
        cleaned,
    )
    if country_match:
        cleaned = cleaned[:country_match.start()].strip().rstrip(",").strip()

    # 2. Extract ZIP Code
    postal_code = ""
    zip_match = re.search(r"(?i)(?:,\s*|\s+)(\d{5}(?:-\d{4})?)\s*$", cleaned)
    if zip_match:
        postal_code = zip_match.group(1)
        cleaned = cleaned[:zip_match.start()].strip().rstrip(",").strip()

    # 3. Extract State
    state = ""
    state_match = re.search(r"(?i)(?:^|[\s,]+)([A-Za-z]{2})\.?\s*$", cleaned)
    if state_match and state_match.group(1).upper() in US_STATE_CODES:
        state = state_match.group(1).upper()
        cleaned = cleaned[:state_match.start(1)].strip().rstrip(",").strip()
    else:
        for s_name, s_code in sorted(US_STATE_NAMES.items(), key=lambda x: len(x[0]), reverse=True):
            pattern = rf"(?i)(?:^|[\s,]+){re.escape(s_name)}\.?\s*$"
            if re.search(pattern, cleaned):
                state = s_code
                cleaned = re.sub(pattern, "", cleaned).strip().rstrip(",").strip()
                break

    # If neither state nor zip could be extracted, preserve raw_address for review
    if not state and not postal_code:
        return {**empty_fields, "Address Line 1": raw_address}

    address_line_1 = ""
    city = ""

    # Case A: Comma-separated parts in remaining string
    if "," in cleaned:
        parts = [p.strip() for p in cleaned.split(",") if p.strip()]
        if len(parts) == 1:
            cleaned = parts[0]
        elif len(parts) >= 2:
            last_part_upper = parts[-1].upper()
            is_last_unit = any(last_part_upper.startswith(u) for u in UNIT_KEYWORDS)
            if is_last_unit and len(parts) >= 3:
                city = parts[-2]
                address_line_1 = f"{parts[0]}, {parts[-1]}"
            else:
                city = parts[-1]
                address_line_1 = ", ".join(parts[:-1])

    # Case B: No commas separating street and city
    if not city:
        # Check Subcase B1: Unit / Suite keyword
        unit_pattern = re.compile(
            r"(?i)(?:^|[\s,]+)(SUITE|STE|UNIT|APT|APARTMENT|BLDG|BUILDING|ROOM|RM|DEPT|FL|FLOOR|#)\.?\s*([A-Za-z0-9\-#]+)\s+(.+)$"
        )
        u_match = unit_pattern.search(cleaned)
        if u_match:
            unit_start = u_match.start()
            unit_keyword = u_match.group(1)
            unit_val = u_match.group(2)
            city_val = u_match.group(3).strip()
            street_val = cleaned[:unit_start].strip().rstrip(",").strip()
            if unit_keyword == "#":
                unit_str = f"#{unit_val}"
            else:
                unit_str = f"{unit_keyword} {unit_val}"
            address_line_1 = f"{street_val} {unit_str}".strip()
            city = city_val
        else:
            # Check Subcase B2: Known City matching from tail (1, 2, or 3 words)
            tokens = cleaned.split()
            matched_city = False
            for num_words in (3, 2, 1):
                if len(tokens) > num_words:
                    candidate_city = " ".join(tokens[-num_words:]).upper()
                    if candidate_city in KNOWN_CITIES:
                        city = " ".join(tokens[-num_words:])
                        address_line_1 = " ".join(tokens[:-num_words])
                        matched_city = True
                        break

            if not matched_city:
                # Check Subcase B3: Street suffix or Highway pattern
                suffix_regex = r"(?i)\b(" + "|".join(STREET_SUFFIXES) + r")\.?(?:\s+(N|S|E|W|NE|NW|SE|SW|NORTH|SOUTH|EAST|WEST))?\s+(.+)$"
                s_match = re.search(suffix_regex, cleaned)
                if s_match:
                    suffix_end = s_match.end(1)
                    if s_match.group(2):
                        suffix_end = s_match.end(2)
                    address_line_1 = cleaned[:suffix_end].strip()
                    city = cleaned[suffix_end:].strip()
                else:
                    h_match = HWY_PATTERN.search(cleaned)
                    if h_match:
                        h_end = h_match.end()
                        address_line_1 = cleaned[:h_end].strip()
                        city = cleaned[h_end:].strip()
                    else:
                        if len(tokens) >= 2:
                            city = tokens[-1]
                            address_line_1 = " ".join(tokens[:-1])
                        else:
                            address_line_1 = cleaned

    return {
        "Address Line 1": address_line_1.strip(),
        "City": city.strip(),
        "State": state.strip(),
        "ZIP": postal_code.strip(),
        "Country": country if (address_line_1 or city or state or postal_code) else "",
    }


def _webex_location_template_columns() -> list[str]:
    template_path = (
        Path(__file__).resolve().parents[1]
        / "imports"
        / "migration_csv"
        / "templates"
        / "locations_template.csv"
    )
    try:
        with template_path.open(newline="", encoding="utf-8-sig") as template_file:
            return next(csv.reader(template_file))
    except (OSError, StopIteration) as exc:
        logger.exception("Could not read Webex locations CSV template.")
        raise HTTPException(status_code=500, detail="Webex locations CSV template is unavailable.") from exc


def warm_meraki_cache() -> None:
    """Synchronously pre-warms organization networks and devices into the TTL cache."""
    provider = get_meraki_provider()
    if provider is None:
        return
    try:
        logger.info("Starting Meraki cache warm-up...")
        networks = provider.get_organization_networks()
        devices = provider.get_organization_devices()
        logger.info("Meraki cache warm-up completed. Networks: %s, Devices: %s", len(networks), len(devices))
    except Exception as exc:
        logger.exception("Meraki cache warm-up failed: %s", exc)


@router.get("/api/v1/meraki-health", status_code=status.HTTP_200_OK)
async def meraki_health_check(
    meraki_provider: Optional[MerakiProvider] = Depends(get_meraki_provider),
):
    """Health check for Meraki Dashboard API connectivity and credential validity."""
    return meraki_provider.health_check() if meraki_provider else {"status": "DISABLED", "detail": "Meraki uninitialized."}


@router.get("/api/v1/admin/meraki-networks", dependencies=[Depends(enforce_routing_auth)])
async def list_meraki_networks(
    meraki_provider: Optional[MerakiProvider] = Depends(get_meraki_provider),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Lists all organization networks sorted alphabetically."""
    _require_meraki_swap_role(current_user)
    if not meraki_provider:
        raise HTTPException(status_code=503, detail="Meraki Provider uninitialized.")
    networks = meraki_provider.get_organization_networks()
    return [{"id": n.get("id"), "name": n.get("name")} for n in sorted(networks, key=lambda k: k.get("name", "").lower())]


@router.get("/api/v1/admin/meraki-webex-location-export", dependencies=[Depends(enforce_routing_auth)])
async def export_meraki_webex_locations(
    meraki_provider: Optional[MerakiProvider] = Depends(get_meraki_provider),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Downloads MX/Z4 network addresses in the Webex Calling locations CSV format."""
    _require_meraki_swap_role(current_user)
    if not meraki_provider:
        raise HTTPException(status_code=503, detail="Meraki Provider uninitialized.")

    inventory = await run_in_threadpool(meraki_provider.get_appliance_location_inventory)
    failed_lookups = [row for row in inventory if row.get("lookup_error")]
    if failed_lookups:
        affected_networks = ", ".join(
            f"{row['network_name']} ({row['network_id']})" for row in failed_lookups
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "Could not retrieve Meraki address details for: "
                f"{affected_networks}. Retry the export after Meraki API connectivity recovers."
            ),
        )

    fieldnames = _webex_location_template_columns()
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for row in inventory:
        writer.writerow({
            "Location Name": row["network_name"],
            **_parse_webex_location_address(row["address"]),
            "Time Zone": row["time_zone"],
        })

    return Response(
        content="\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=meraki_webex_locations_import.csv"},
    )


@router.get("/api/v1/admin/meraki-webex-location-preview", dependencies=[Depends(enforce_routing_auth)])
async def preview_meraki_webex_locations(
    meraki_provider: Optional[MerakiProvider] = Depends(get_meraki_provider),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Returns Meraki address data and parsed Webex location fields for review."""
    _require_meraki_swap_role(current_user)
    if not meraki_provider:
        raise HTTPException(status_code=503, detail="Meraki Provider uninitialized.")

    inventory = await run_in_threadpool(meraki_provider.get_appliance_location_inventory)
    preview_rows = []
    required_fields = ("Address Line 1", "City", "State", "ZIP", "Country", "Time Zone")
    for row in inventory:
        values = {
            "Location Name": row["network_name"],
            **_parse_webex_location_address(row["address"]),
            "Time Zone": row["time_zone"],
        }
        missing_fields = [field for field in required_fields if not values.get(field, "").strip()]
        preview_rows.append({
            "network_id": row["network_id"],
            "source_address": row["address"],
            "address_source": row.get("address_source", ""),
            "address_conflict": row.get("address_conflict", False),
            "device_count": row.get("device_count", 1),
            "appliance_models": row["appliance_models"],
            "serial_numbers": row["serial_numbers"],
            "lookup_error": row["lookup_error"],
            "missing_fields": missing_fields,
            "values": values,
        })

    return {
        "columns": _webex_location_template_columns(),
        "appliance_count": sum(row["device_count"] for row in preview_rows),
        "rows": preview_rows,
    }


@router.get("/api/v1/admin/meraki-templates", dependencies=[Depends(enforce_routing_auth)])
async def list_meraki_templates(
    meraki_provider: Optional[MerakiProvider] = Depends(get_meraki_provider),
):
    """Lists all organization configuration templates."""
    if not meraki_provider:
        raise HTTPException(status_code=503, detail="Meraki Provider uninitialized.")
    return [{"id": t.get("id"), "name": t.get("name")} for t in meraki_provider.get_organization_templates()]


@router.post("/api/v1/admin/warm-meraki-cache")
async def warm_meraki_cache_endpoint(
    payload: CacheWarmRequest,
    meraki_provider: Optional[MerakiProvider] = Depends(get_meraki_provider),
):
    """Explicitly triggers cache pre-warming for Meraki networks and devices."""
    if not meraki_provider:
        raise HTTPException(status_code=503, detail="Meraki provider uninitialized.")
    results = {}
    if payload.include_networks:
        results["networks"] = {"loaded": True, "count": len(meraki_provider.get_organization_networks())}
    if payload.include_devices:
        results["devices"] = {"loaded": True, "count": len(meraki_provider.get_organization_devices())}
    return {"status": "SUCCESS", "results": results}


@router.post("/api/v1/admin/meraki-swap-appliance", dependencies=[Depends(enforce_routing_auth)])
async def trigger_appliance_swap(
    payload: ApplianceSwapRequest,
    meraki_provider: Optional[MerakiProvider] = Depends(get_meraki_provider),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Enqueues an asynchronous hardware appliance swap task into Celery/Valkey."""
    _require_meraki_swap_role(current_user)

    if not payload.step_up_user or not payload.step_up_pass:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Step-up authentication required: enter your corporate directory credentials to execute an appliance swap.",
        )

    auth_provider = AuthFactory.get_step_up_provider()
    try:
        step_up_role = await run_in_threadpool(
            auth_provider.resolve_user_role,
            payload.step_up_user,
            payload.step_up_pass,
        )
    except ValueError as exc:
        log_action(
            user=payload.step_up_user,
            role="N/A",
            action="Step-Up Auth (Meraki Appliance Swap)",
            target="LDAPS",
            result="DENIED - Invalid Credentials",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Step-up authentication failed: invalid corporate directory credentials.",
        ) from exc

    if step_up_role not in MERAKI_SWAP_ALLOWED_ROLES:
        log_action(
            user=payload.step_up_user,
            role=step_up_role.value,
            action="Step-Up Auth (Meraki Appliance Swap)",
            target="LDAPS",
            result="DENIED - Insufficient Clearance",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Step-up authorization failed: credentials must belong to the LocalTechServices or NetworkAdmin group.",
        )

    log_action(
        user=payload.step_up_user,
        role=step_up_role.value,
        action="Step-Up Auth (Meraki Appliance Swap)",
        target="LDAPS",
        result="SUCCESS",
    )

    if not meraki_provider:
        raise HTTPException(status_code=503, detail="Meraki Provider uninitialized.")

    task = execute_meraki_appliance_swap_task.delay(
        network_id=payload.network_id,
        old_serial=payload.old_serial,
        new_serial=payload.new_serial,
    )
    return {"status": "QUEUED", "task_id": task.id, "message": "Appliance swap task queued."}
