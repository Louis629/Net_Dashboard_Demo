"""
providers/webex_calling_provider.py

High-Performance Cisco Webex Calling REST API Provider.
Handles Control Hub user licensing, location assignment, extension provisioning,
rate-limit mitigation (HTTP 429 Retry-After), and RFC5988 Link Header pagination.
"""

import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from dotenv import load_dotenv
import requests
from requests.exceptions import RequestException
import urllib3

logger = logging.getLogger("Webex_Calling_Provider")


class WebexAPIError(Exception):
    """Base exception for Webex API failures."""
    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class WebexRateLimitError(WebexAPIError):
    """Raised when Webex API returns HTTP 429 and retries fail."""


def normalize_webex_device_model(model: str) -> str:
    """
    Normalizes human or CUCM model strings into Webex Calling API-accepted model identifiers.
    Webex /telephony/config/devices and /devices/activationCode require canonical names
    like 'Cisco 8851', rejecting decorative suffixes like 'MPP', '(Enterprise)', '3PCC', or 'CP-'.
    """
    if not model:
        return model

    raw = str(model).strip()

    # Cisco 4-digit models (e.g. CP-8851, Cisco 8851 MPP, 8851, CP-7841-3PCC, Cisco 6851 MPP)
    m = re.search(r"(?:CP-)?(?:Cisco\s+)?([6789]\d{3})(?:-[3K]PCC)?(?:\s+(?:MPP|\(Enterprise\)))?", raw, re.IGNORECASE)
    if m:
        return f"Cisco {m.group(1)}"

    # ATA models
    m_ata = re.search(r"(?:Cisco\s+)?ATA\s*(\d{3})", raw, re.IGNORECASE)
    if m_ata:
        return f"Cisco ATA {m_ata.group(1)}"

    # Strip (Enterprise) or MPP suffixes if present
    cleaned = re.sub(r"\s*\((?:Enterprise|MPP)\)", "", raw, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+MPP\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^CP-", "Cisco ", cleaned, flags=re.IGNORECASE)

    return cleaned.strip()


WEBEX_TIMEZONE_MAP = {
    # US Central
    "US/CENTRAL": "America/Chicago",
    "CST": "America/Chicago",
    "CST6CDT": "America/Chicago",
    "CDT": "America/Chicago",
    "CENTRAL": "America/Chicago",
    "CENTRAL TIME": "America/Chicago",
    "CENTRAL TIME (US & CANADA)": "America/Chicago",
    # US Eastern
    "US/EASTERN": "America/New_York",
    "EST": "America/New_York",
    "EST5EDT": "America/New_York",
    "EDT": "America/New_York",
    "EASTERN": "America/New_York",
    "EASTERN TIME": "America/New_York",
    "EASTERN TIME (US & CANADA)": "America/New_York",
    "US/EAST-INDIANA": "America/Indiana/Indianapolis",
    "US/INDIANA-STARKE": "America/Indiana/Knox",
    "US/MICHIGAN": "America/Detroit",
    # US Mountain
    "US/MOUNTAIN": "America/Denver",
    "MST": "America/Denver",
    "MST7MDT": "America/Denver",
    "MDT": "America/Denver",
    "MOUNTAIN": "America/Denver",
    "MOUNTAIN TIME": "America/Denver",
    "MOUNTAIN TIME (US & CANADA)": "America/Denver",
    # US Arizona
    "US/ARIZONA": "America/Phoenix",
    "ARIZONA": "America/Phoenix",
    # US Pacific
    "US/PACIFIC": "America/Los_Angeles",
    "PST": "America/Los_Angeles",
    "PST8PDT": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "PACIFIC": "America/Los_Angeles",
    "PACIFIC TIME": "America/Los_Angeles",
    "PACIFIC TIME (US & CANADA)": "America/Los_Angeles",
    # US Alaska
    "US/ALASKA": "America/Anchorage",
    "AKST": "America/Anchorage",
    "AKDT": "America/Anchorage",
    "ALASKA": "America/Anchorage",
    # US Hawaii
    "US/HAWAII": "Pacific/Honolulu",
    "HST": "Pacific/Honolulu",
    "HAWAII": "Pacific/Honolulu",
    # US Territories
    "US/SAMOA": "Pacific/Pago_Pago",
    "SAMOA": "Pacific/Pago_Pago",
    "AST": "America/Puerto_Rico",
}

US_STATE_PRIMARY_TZ = {
    "HI": "Pacific/Honolulu",
    "AK": "America/Anchorage",
    "CA": "America/Los_Angeles",
    "WA": "America/Los_Angeles",
    "OR": "America/Los_Angeles",
    "NV": "America/Los_Angeles",
    "AZ": "America/Phoenix",
    "CO": "America/Denver",
    "UT": "America/Denver",
    "WY": "America/Denver",
    "MT": "America/Denver",
    "NM": "America/Denver",
    "ID": "America/Denver",
    "TX": "America/Chicago",
    "IL": "America/Chicago",
    "MO": "America/Chicago",
    "OK": "America/Chicago",
    "KS": "America/Chicago",
    "NE": "America/Chicago",
    "SD": "America/Chicago",
    "ND": "America/Chicago",
    "MN": "America/Chicago",
    "IA": "America/Chicago",
    "WI": "America/Chicago",
    "AR": "America/Chicago",
    "LA": "America/Chicago",
    "MS": "America/Chicago",
    "AL": "America/Chicago",
    "TN": "America/Chicago",
    "FL": "America/New_York",
    "GA": "America/New_York",
    "NC": "America/New_York",
    "SC": "America/New_York",
    "VA": "America/New_York",
    "WV": "America/New_York",
    "MD": "America/New_York",
    "DE": "America/New_York",
    "PA": "America/New_York",
    "NJ": "America/New_York",
    "NY": "America/New_York",
    "CT": "America/New_York",
    "RI": "America/New_York",
    "MA": "America/New_York",
    "VT": "America/New_York",
    "NH": "America/New_York",
    "ME": "America/New_York",
    "DC": "America/New_York",
    "OH": "America/New_York",
    "IN": "America/Indiana/Indianapolis",
    "MI": "America/Detroit",
    "KY": "America/New_York",
    "PR": "America/Puerto_Rico",
    "VI": "America/Puerto_Rico",
    "GU": "Pacific/Guam",
    "MP": "Pacific/Guam",
    "AS": "Pacific/Pago_Pago",
}


def normalize_webex_timezone(tz: Optional[str], state: Optional[str] = None, country: str = "US") -> str:
    """
    Normalizes Meraki or human-entered timezone strings into canonical IANA time zone identifiers
    accepted by Cisco Webex Calling API for the given country.
    Maps legacy/POSIX names (e.g. 'US/Central', 'EST', 'US/Mountain') to canonical IANA IDs.
    """
    clean_country = (country or "US").strip().upper()
    raw = str(tz or "").strip()
    key = raw.upper()

    # 1. Direct match in legacy/Meraki timezone map
    if key in WEBEX_TIMEZONE_MAP:
        return WEBEX_TIMEZONE_MAP[key]

    # 2. Canonical IANA path
    if raw.startswith(("America/", "Pacific/", "Atlantic/", "Europe/", "Asia/", "Australia/")):
        return raw

    # 3. Fallback based on US state code if country is US
    if clean_country in ("US", "USA", "UNITED STATES") and state:
        st_clean = state.strip().upper()
        if st_clean in US_STATE_PRIMARY_TZ:
            return US_STATE_PRIMARY_TZ[st_clean]

    # 4. Fallback defaults
    if clean_country in ("US", "USA", "UNITED STATES"):
        return "America/Chicago"

    return raw or "America/Chicago"



class WebexCallingProvider:
    """
    REST Client for Webex Control Hub and Webex Calling API v1.
    """

    def __init__(
        self,
        access_token: Optional[str] = None,
        org_id: Optional[str] = None,
        base_url: str = "https://webexapis.com/v1",
        timeout: int = 30,
        max_retries: int = 3
    ):
        # Reload .env dynamically so runtime token updates are immediately picked up
        load_dotenv(override=True)

        raw_token = access_token or os.getenv("WEBEX_ACCESS_TOKEN", "")
        if not raw_token:
            raw_token = os.getenv("WEBEX_TOKEN", "") or os.getenv("WEBEX_BEARER_TOKEN", "")
        clean_token = raw_token.strip().strip('"').strip("'")
        self.access_token = re.sub(r"^Bearer\s+", "", clean_token, flags=re.IGNORECASE)

        raw_org = org_id or os.getenv("WEBEX_ORG_ID", "")
        if not raw_org:
            raw_org = os.getenv("WEBEX_ORGANIZATION_ID", "")
        self.org_id = raw_org.strip().strip('"').strip("'")

        raw_loc = os.getenv("WEBEX_DEFAULT_LOCATION_ID", "") or os.getenv("WEBEX_LOCATION_ID", "")
        self.default_location_id = raw_loc.strip().strip('"').strip("'")

        raw_lic = os.getenv("WEBEX_CALLING_LICENSE_ID", "") or os.getenv("WEBEX_LICENSE_ID", "")
        self.calling_license_id = raw_lic.strip().strip('"').strip("'")

        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._ucm_calling_license_ids_cache: Optional[set[str]] = None

        if not self.access_token:
            logger.warning("Webex Calling Provider initialized without WEBEX_ACCESS_TOKEN.")
        else:
            logger.info("Webex Calling Provider initialized with token (length %d).", len(self.access_token))

        self.session = requests.Session()
        
        # Corporate SSL Decryption Proxy Handling
        # Define WEBEX_VERIFY_SSL=false in .env if you lack the corporate root CA bundle
        env_verify = os.getenv("WEBEX_VERIFY_SSL", "true").strip().lower()
        if env_verify in {"0", "false", "no", "n"}:
            self.session.verify = False
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            logger.warning("Webex SSL Verification Disabled (Corporate Proxy Bypass mode).")
        else:
            ca_bundle = os.getenv("REQUESTS_CA_BUNDLE") or os.getenv("SSL_CERT_FILE")
            if ca_bundle and os.path.exists(ca_bundle):
                self.session.verify = ca_bundle

        self.session.headers.update({
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        })

    def _request_page(self, method: str, url_or_endpoint: str, **kwargs: Any) -> Tuple[Dict[str, Any], Optional[str]]:
        """
        Executes HTTP requests to Webex APIs and parses RFC5988 Link headers for pagination.
        """
        if url_or_endpoint.startswith("http://") or url_or_endpoint.startswith("https://"):
            url = url_or_endpoint
        else:
            endpoint = url_or_endpoint if url_or_endpoint.startswith("/") else f"/{url_or_endpoint}"
            url = f"{self.base_url}{endpoint}"

        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.request(method=method, url=url, timeout=self.timeout, **kwargs)

                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 5))
                    logger.warning("Webex API Rate Limit hit (HTTP 429). Retrying in %d seconds...", retry_after)
                    time.sleep(retry_after)
                    continue

                if response.status_code >= 400:
                    logger.error("Webex API Error HTTP %s [%s]: %s", response.status_code, url, response.text)
                    raise WebexAPIError(f"HTTP {response.status_code}: {response.text}", status_code=response.status_code)

                if response.status_code == 204 or not response.text:
                    return {}, None

                next_url = response.links.get("next", {}).get("url")
                return response.json(), next_url

            except RequestException as exc:
                logger.exception("Network transport error calling Webex API [%s]: %s", url, exc)
                if attempt == self.max_retries:
                    raise WebexAPIError(f"Webex API Transport Failure: {str(exc)}") from exc
                time.sleep(2)

        raise WebexRateLimitError("Exceeded maximum retries for Webex API requests.")

    def _request(self, method: str, endpoint: str, **kwargs: Any) -> Dict[str, Any]:
        data, _ = self._request_page(method=method, url_or_endpoint=endpoint, **kwargs)
        return data

    def get_locations(self, max_per_page: int = 100, max_pages: int = 20) -> List[Dict[str, Any]]:
        """Retrieves all Control Hub locations configured for Webex Calling."""
        all_locations: List[Dict[str, Any]] = []
        next_url: Optional[str] = f"/locations?max={max_per_page}"
        page_num = 1
        seen_urls = set()

        while next_url and page_num <= max_pages:
            if next_url in seen_urls:
                logger.warning("Detected cycle in Webex locations pagination: %s", next_url)
                break
            seen_urls.add(next_url)
            logger.info("Fetching Webex Locations page %d...", page_num)
            data, next_url = self._request_page("GET", next_url)
            items = data.get("items", [])

            if not items:
                break

            all_locations.extend(items)
            page_num += 1

        return all_locations

    def get_calling_locations(self, max_per_page: int = 100, max_pages: int = 20) -> List[Dict[str, Any]]:
        """Alias to support both Dashboard router signatures."""
        return self.get_locations(max_per_page=max_per_page, max_pages=max_pages)

    def get_person_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        clean_email = email.strip()
        try:
            res = self._request("GET", "/people", params={"email": clean_email, "callingData": "true"})
        except WebexAPIError as exc:
            if exc.status_code in (400, 403):
                res = self._request("GET", "/people", params={"email": clean_email})
            else:
                raise
        items = res.get("items", [])
        return items[0] if items else None

    def get_person_by_id(self, person_id: str, calling_data: bool = True) -> Dict[str, Any]:
        """Retrieves a Webex Person profile by ID, optionally including callingData."""
        endpoint = f"/people/{person_id}?callingData=true" if calling_data else f"/people/{person_id}"
        return self._request("GET", endpoint)

    def move_person_location(
        self,
        person_id: str,
        location_id: str,
        phone_number: Optional[str] = None,
        extension: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Transitions a Webex Calling user to a new location.
        Attempts:
        1. POST /people/{personId}/actions/moveLocation
        2. POST /telephony/config/jobs/person/moveLocation
        3. Direct PUT /people/{personId} with locationId
        """
        clean_phone = None
        if phone_number and str(phone_number).strip():
            clean_phone = str(phone_number).strip()
            if not clean_phone.startswith("+") and clean_phone.isdigit():
                clean_phone = f"+{clean_phone}"

        # Attempt 1: Individual action endpoint
        try:
            move_body = {"locationId": location_id}
            res = self._request("POST", f"/people/{person_id}/actions/moveLocation", json=move_body)
            logger.info("Successfully moved user %s to location %s via /actions/moveLocation", person_id, location_id)
            return res
        except Exception as act_err:
            logger.warning("Move via /actions/moveLocation failed for %s (%s). Attempting moveLocation job...", person_id, act_err)

        # Attempt 2: Telephony config moveLocation job
        try:
            job_user_item: Dict[str, Any] = {"userId": person_id}
            if clean_phone:
                job_user_item["phoneNumber"] = clean_phone
            if extension:
                job_user_item["extension"] = str(extension).strip()

            job_payload = {
                "usersList": [
                    {
                        "locationId": location_id,
                        "validate": False,
                        "users": [job_user_item],
                    }
                ]
            }
            res = self._request("POST", "/telephony/config/jobs/person/moveLocation", json=job_payload)
            logger.info("Successfully initiated moveLocation job for %s to %s: %s", person_id, location_id, res)
            return res
        except Exception as job_err:
            logger.warning("Move via telephony moveLocation job failed for %s (%s). Attempting standard People update...", person_id, job_err)

        # Attempt 3: Direct PUT /people/{personId}?callingData=true
        user = self.get_person_by_id(person_id, calling_data=True)
        payload: Dict[str, Any] = {
            "firstName": user.get("firstName", ""),
            "lastName": user.get("lastName", ""),
            "emails": user.get("emails", []),
            "licenses": user.get("licenses", []),
            "locationId": location_id,
        }
        if user.get("displayName"):
            payload["displayName"] = user["displayName"]
        if self.extract_existing_did(user) and isinstance(user.get("phoneNumbers"), list):
            payload["phoneNumbers"] = [
                dict(entry) for entry in user["phoneNumbers"]
                if isinstance(entry, dict) and str(entry.get("value", "")).strip()
            ]
        elif clean_phone:
            payload["phoneNumbers"] = [{"type": "work", "value": clean_phone}]
        elif user.get("phoneNumbers"):
            payload["phoneNumbers"] = user["phoneNumbers"]

        existing_ext = str(user.get("extension") or "").strip()
        if existing_ext:
            payload["extension"] = existing_ext
        elif extension:
            payload["extension"] = str(extension).strip()
        self._preserve_person_attributes(user, payload)
        return self._put_person_with_fallbacks(person_id, payload)

    @staticmethod
    def extract_existing_did(person: Optional[Dict[str, Any]]) -> Optional[str]:
        """Returns an existing non-extension phone number (e.g., AD-synced work DID) from a Webex person payload."""
        if not isinstance(person, dict):
            return None
        phone_numbers = person.get("phoneNumbers")
        if not isinstance(phone_numbers, list):
            return None
        for preferred_type in ("work", None):
            for entry in phone_numbers:
                if not isinstance(entry, dict):
                    continue
                entry_type = str(entry.get("type", "")).strip().lower()
                val = str(entry.get("value", "")).strip()
                if not val or entry_type == "work_extension":
                    continue
                if preferred_type is None or entry_type == preferred_type:
                    return val
        return None

    @staticmethod
    def _preserve_person_attributes(user: Dict[str, Any], payload: Dict[str, Any]) -> None:
        """
        Ensures critical Webex Person attributes—especially administrative roles and org context—
        are preserved when performing full object PUT /people/{personId} updates.
        Webex REST API resets omitted attributes (e.g., omitting roles wipes admin privileges).
        """
        if user.get("roles"):
            payload["roles"] = user["roles"]
        if user.get("orgId"):
            payload["orgId"] = user["orgId"]
        if "siteAdmin" in user and user["siteAdmin"] is not None:
            payload["siteAdmin"] = user["siteAdmin"]
        if "loginEnabled" in user and user["loginEnabled"] is not None:
            payload["loginEnabled"] = user["loginEnabled"]
        for field in ("nickName", "title", "department", "manager", "managerId", "userType"):
            if user.get(field):
                payload[field] = user[field]

    _DIR_SYNC_METADATA_RULES: Tuple[Tuple[Tuple[str, ...], Tuple[str, ...]], ...] = (
        (("manager",), ("manager", "managerId")),
        (("department",), ("department",)),
        (("title",), ("title",)),
        (("nickname", "nick name"), ("nickName",)),
        (("usertype", "user type"), ("userType",)),
        (("displayname", "display name"), ("displayName",)),
        (("firstname", "first name"), ("firstName",)),
        (("lastname", "last name"), ("lastName",)),
    )
    _OPTIONAL_DIR_SYNC_METADATA: Tuple[str, ...] = (
        "manager",
        "managerId",
        "department",
        "title",
        "nickName",
        "userType",
        "displayName",
    )

    def _put_person_with_fallbacks(self, person_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Executes PUT /people/{personId}?callingData=true, automatically recovering from:
        - locationId conflicts (retried without locationId)
        - Directory Sync read-only attribute rejections (stripping only the named Dir Sync fields
          such as manager/managerId/department/title first, before touching phoneNumbers or extension)
        """
        put_endpoint = f"/people/{person_id}?callingData=true"
        current_body = dict(payload)
        attempted_keys: set[Tuple[str, ...]] = set()
        last_dir_sync_err: Optional[Exception] = None
        last_stripped_label = ""

        def _put_with_location_fallback(body: Dict[str, Any]) -> Dict[str, Any]:
            try:
                return self._request("PUT", put_endpoint, json=body)
            except Exception as err:
                if "location" in str(err).lower() and "locationId" in body:
                    logger.warning("Retrying Person PUT without locationId due to conflict: %s", err)
                    fallback_body = dict(body)
                    fallback_body.pop("locationId", None)
                    return self._request("PUT", put_endpoint, json=fallback_body)
                raise

        for _ in range(12):
            sig = tuple(sorted(current_body.keys()))
            if sig in attempted_keys:
                break
            attempted_keys.add(sig)
            try:
                return _put_with_location_fallback(current_body)
            except Exception as err:
                err_msg = str(err).lower()
                if "either phone number or extension is required" in err_msg and last_dir_sync_err is not None:
                    raise WebexAPIError(
                        f"Directory Sync blocked modifying {last_stripped_label or 'attributes'} via Webex /people API ({last_dir_sync_err}), "
                        f"and Webex rejected creating a new Calling line without a number or extension ({err}). "
                        f"Sync the user's work phone/extension from AD or assign their initial Webex Calling number/extension in Control Hub, then re-run to apply Caller ID, Voicemail, and RedSky MyE911.",
                        status_code=getattr(err, "status_code", 400),
                    ) from err

                if "dir sync" not in err_msg and "dirsync" not in err_msg:
                    raise

                last_dir_sync_err = err
                stripped: List[str] = []

                # 1. Strip explicitly named Dir Sync profile metadata fields first (e.g., Manager, Department, Title)
                for keywords, fields in self._DIR_SYNC_METADATA_RULES:
                    if any(kw in err_msg for kw in keywords):
                        for field in fields:
                            if field in current_body:
                                current_body.pop(field, None)
                                stripped.append(field)

                if not stripped:
                    # 2. Strip Calling fields ONLY when explicitly named (one field at a time)
                    if "extension" in err_msg and "phone" not in err_msg and "number" not in err_msg and "extension" in current_body:
                        current_body.pop("extension", None)
                        stripped.append("extension")
                    elif ("phone" in err_msg or "number" in err_msg) and "phoneNumbers" in current_body:
                        current_body.pop("phoneNumbers", None)
                        stripped.append("phoneNumbers")
                    else:
                        # 3. Generic Dir Sync error: drop remaining optional metadata before phoneNumbers/extension
                        meta_dropped = [f for f in self._OPTIONAL_DIR_SYNC_METADATA if f in current_body]
                        if meta_dropped:
                            for f in meta_dropped:
                                current_body.pop(f, None)
                            stripped.extend(meta_dropped)
                        elif "phoneNumbers" in current_body:
                            current_body.pop("phoneNumbers", None)
                            stripped.append("phoneNumbers")
                        elif "extension" in current_body:
                            swap_sig = tuple(sorted([*(k for k in current_body.keys() if k != "extension"), "phoneNumbers"]))
                            if "phoneNumbers" in payload and swap_sig not in attempted_keys:
                                current_body.pop("extension", None)
                                current_body["phoneNumbers"] = payload["phoneNumbers"]
                                stripped.append("extension (kept phoneNumbers)")
                            else:
                                current_body.pop("extension", None)
                                stripped.append("extension")

                if not stripped:
                    raise

                last_stripped_label = ", ".join(stripped)
                logger.info(
                    "Directory Sync rejected attribute (%s). Retrying Person PUT without %s...",
                    err,
                    last_stripped_label,
                )

        if last_dir_sync_err is not None:
            raise last_dir_sync_err
        raise WebexAPIError(f"Failed PUT {put_endpoint} after Dir Sync retries.")

    @staticmethod
    def _is_ucm_calling_license(lic: Dict[str, Any]) -> bool:
        """Returns True if a Control Hub license represents UCM / Call Manager calling (not Webex Calling Cloud)."""
        name = str(lic.get("name", "")).lower()
        raw_lic_id = str(lic.get("licenseId", "")).lower()
        return (
            "unified cm" in name
            or "unified communication" in name
            or "call manager" in name
            or "cucm" in name
            or bool(re.search(r"\bucm\b", name))
            or "ucm" in raw_lic_id
            or ("calling in webex" in name and "webex calling" not in name)
        )

    def _get_ucm_calling_license_ids(self) -> set[str]:
        """
        Identifies Control Hub license IDs representing Unified CM / Register to UCM calling
        so they are automatically disabled when migrating users to Webex Calling Cloud.
        """
        if self._ucm_calling_license_ids_cache is not None:
            return self._ucm_calling_license_ids_cache

        ucm_ids: set[str] = set()
        try:
            licenses = self.get_licenses()
            for lic in licenses:
                lic_id = str(lic.get("id", "")).strip()
                if not lic_id:
                    continue
                if self._is_ucm_calling_license(lic):
                    ucm_ids.add(lic_id)
            self._ucm_calling_license_ids_cache = ucm_ids
        except Exception as exc:
            logger.debug("Could not inspect org licenses for Unified CM Calling cleanup: %s", exc)
        return ucm_ids

    def enable_webex_calling_for_person(
        self,
        person_id: str,
        location_id: str,
        calling_license_id: str = "",
        extension: Optional[str] = None,
        phone_number: Optional[str] = None,
        replace_ucm_calling_license: bool = True,
    ) -> Dict[str, Any]:
        """Assigns Webex Calling License, Location, Extension, and Direct Phone Number."""
        user = self._request("GET", f"/people/{person_id}?callingData=true")

        current_loc = user.get("locationId")
        if location_id and current_loc and current_loc != location_id:
            try:
                effective_move_phone = self.extract_existing_did(user) or phone_number
                effective_move_ext = str(user.get("extension") or "").strip() or extension
                self.move_person_location(
                    person_id,
                    location_id,
                    phone_number=effective_move_phone,
                    extension=effective_move_ext,
                )
                user = self._request("GET", f"/people/{person_id}?callingData=true")
            except Exception as move_err:
                logger.warning("Location move attempt for %s failed: %s", person_id, move_err)

        existing_licenses = list(user.get("licenses", []))
        target_license = calling_license_id.strip() if calling_license_id else ""
        if replace_ucm_calling_license and existing_licenses:
            ucm_ids = self._get_ucm_calling_license_ids()
            if ucm_ids:
                should_strip_ucm = bool(target_license and target_license not in ucm_ids)
                if not should_strip_ucm and not target_license:
                    # Even if calling_license_id wasn't passed (e.g. profile/device sync for an already-licensed user),
                    # still strip UCM registration if the user already holds a Webex Calling Cloud license.
                    try:
                        wxc_ids = {
                            str(lic.get("id", "")).strip()
                            for lic in self.get_user_calling_licenses()
                            if lic.get("id") and not self._is_ucm_calling_license(lic)
                        }
                        should_strip_ucm = bool(wxc_ids & set(existing_licenses))
                    except Exception:
                        should_strip_ucm = False
                if should_strip_ucm:
                    filtered = [lic for lic in existing_licenses if lic == target_license or lic not in ucm_ids]
                    if len(filtered) != len(existing_licenses):
                        logger.info(
                            "Disabled %d Unified CM (Register to UCM) license(s) for Person ID %s for Webex Calling Cloud cutover.",
                            len(existing_licenses) - len(filtered),
                            person_id,
                        )
                    existing_licenses = filtered
        if target_license and target_license not in existing_licenses:
            existing_licenses.append(target_license)

        payload: Dict[str, Any] = {
            "firstName": user.get("firstName", ""),
            "lastName": user.get("lastName", ""),
            "emails": user.get("emails", []),
            "licenses": existing_licenses,
        }
        if user.get("displayName"):
            payload["displayName"] = user["displayName"]
        if location_id:
            payload["locationId"] = location_id

        existing_ext = str(user.get("extension") or "").strip()
        if existing_ext:
            payload["extension"] = existing_ext
        elif extension and str(extension).strip():
            payload["extension"] = str(extension).strip()

        existing_did = self.extract_existing_did(user)
        existing_phone_numbers = user.get("phoneNumbers")
        if existing_did and isinstance(existing_phone_numbers, list):
            payload["phoneNumbers"] = [
                dict(entry)
                for entry in existing_phone_numbers
                if isinstance(entry, dict) and str(entry.get("value", "")).strip()
            ]
            if phone_number and str(phone_number).strip() and existing_did != str(phone_number).strip():
                logger.info(
                    "Preserving existing AD-synced DID '%s' for Person ID %s (priority over Step 2 input '%s').",
                    existing_did,
                    person_id,
                    phone_number,
                )
        elif phone_number and str(phone_number).strip():
            clean_phone = str(phone_number).strip()
            if not clean_phone.startswith("+") and clean_phone.isdigit():
                clean_phone = f"+{clean_phone}"
            payload["phoneNumbers"] = [{"type": "work", "value": clean_phone}]

        # Preserve administrative roles, siteAdmin, and organization attributes
        self._preserve_person_attributes(user, payload)

        logger.info("Provisioning Webex Calling for Person ID: %s (Roles preserved: %s)", person_id, len(payload.get("roles", [])))
        return self._put_person_with_fallbacks(person_id, payload)

    def configure_caller_id(
        self,
        person_id: str,
        caller_id_type: str = "DIRECT_LINE",
        external_caller_id_name: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Configures Caller ID behavior for a Webex Calling user.
        caller_id_type: 'DIRECT_LINE' (shows assigned DID) or 'LOCATION_NUMBER' (shows branch main pilot).
        """
        clean_type = "DIRECT_LINE" if str(caller_id_type).upper() in {"DIRECT_LINE", "DIRECT", "DIRECT LINE"} else "LOCATION_NUMBER"
        payload: Dict[str, Any] = {
            "selected": clean_type,
        }
        if first_name:
            payload["firstName"] = first_name
        if last_name:
            payload["lastName"] = last_name
        if external_caller_id_name:
            payload["customExternalCallerIdName"] = external_caller_id_name
            payload["externalCallerIdNamePolicy"] = "OTHER"

        logger.info(
            "Configuring Caller ID for Person ID %s: %s (Name: %s)",
            person_id,
            clean_type,
            external_caller_id_name or "User Default",
        )
        return self._request("PUT", f"/people/{person_id}/features/callerId", json=payload)

    def get_caller_id(self, person_id: str) -> Dict[str, Any]:
        """Retrieves current Caller ID configuration for a user."""
        return self._request("GET", f"/people/{person_id}/features/callerId")

    def configure_voicemail(
        self,
        person_id: str,
        enabled: bool = True,
        send_email_notification: bool = True,
        email_copy_of_message: bool = True,
        notification_email: Optional[str] = None,
        default_pin: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Configures Webex Cloud Voicemail, MWI, email notifications, and audio (.wav) attachments.
        Optionally sets a default PIN passcode.
        """
        payload: Dict[str, Any] = {
            "enabled": enabled,
            "sendEmailNotification": send_email_notification,
            "emailCopyOfMessage": email_copy_of_message,
            "messageStorage": {
                "mwi": {"enabled": True},
                "storageType": "INTERNAL",
            },
        }
        if notification_email and send_email_notification:
            payload["notifications"] = {
                "type": "EMAIL",
                "destination": notification_email.strip(),
            }

        logger.info(
            "Configuring Voicemail for Person ID %s: enabled=%s, emailNotify=%s, wavAttach=%s",
            person_id,
            enabled,
            send_email_notification,
            email_copy_of_message,
        )
        res = self._request("PUT", f"/people/{person_id}/features/voicemail", json=payload)

        # Set default PIN if specified
        if default_pin and str(default_pin).strip():
            clean_pin = str(default_pin).strip()
            try:
                self._request("PUT", f"/people/{person_id}/features/voicemail/passcode", json={"passcode": clean_pin})
                logger.info("Set default voicemail PIN passcode for Person ID %s.", person_id)
            except Exception as pin_err:
                logger.warning("Could not set voicemail PIN for %s: %s", person_id, pin_err)

        return res

    def get_voicemail_settings(self, person_id: str) -> Dict[str, Any]:
        """Retrieves voicemail configuration for a user."""
        return self._request("GET", f"/people/{person_id}/features/voicemail")

    def provision_user_calling_profile(
        self,
        person_id: str,
        location_id: str,
        calling_license_id: str,
        extension: Optional[str] = None,
        phone_number: Optional[str] = None,
        caller_id_type: str = "DIRECT_LINE",
        caller_id_name: Optional[str] = None,
        voicemail_enabled: bool = True,
        voicemail_email_notification: bool = True,
        voicemail_attach_audio: bool = True,
        voicemail_pin: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Phase 2: Provisions complete User Calling Profile:
        1. Enables Webex Calling license, location, extension, direct DID
        2. Configures Caller ID policy (Direct Line vs Location Main Number)
        3. Configures Webex Cloud Voicemail & notifications
        """
        # Step 1: Base Calling Enablement
        calling_res = self.enable_webex_calling_for_person(
            person_id=person_id,
            location_id=location_id,
            calling_license_id=calling_license_id,
            extension=extension,
            phone_number=phone_number,
        )

        # Step 2: Caller ID Configuration
        caller_id_res: Dict[str, Any] = {}
        try:
            caller_id_res = self.configure_caller_id(
                person_id=person_id,
                caller_id_type=caller_id_type,
                external_caller_id_name=caller_id_name,
            )
        except Exception as cid_err:
            logger.warning("Could not set Caller ID for Person ID %s: %s", person_id, cid_err)

        # Step 3: Voicemail Configuration
        vm_res: Dict[str, Any] = {}
        try:
            user_email = calling_res.get("emails", [None])[0] if isinstance(calling_res, dict) else None
            vm_res = self.configure_voicemail(
                person_id=person_id,
                enabled=voicemail_enabled,
                send_email_notification=voicemail_email_notification,
                email_copy_of_message=voicemail_attach_audio,
                notification_email=user_email,
                default_pin=voicemail_pin,
            )
        except Exception as vm_err:
            logger.warning("Could not configure Voicemail for Person ID %s: %s", person_id, vm_err)

        return {
            "calling": calling_res,
            "caller_id": caller_id_res,
            "voicemail": vm_res,
        }

    def provision_mpp_device(self, mac_address: str, model: str, location_id: str, person_id: Optional[str] = None) -> Dict[str, Any]:
        clean_mac = mac_address.replace(":", "").replace("-", "").upper()
        norm_model = normalize_webex_device_model(model)
        # Webex 'Create a Device by MAC Address': POST /v1/devices (locationId is not accepted).
        payload: Dict[str, Any] = {"mac": clean_mac, "model": norm_model}
        if person_id: payload["personId"] = person_id
        logger.info("Registering Tier 1 MPP Device MAC %s (%s -> %s) in Control Hub...", clean_mac, model, norm_model)
        return self._request("POST", "/devices", json=payload)

    def get_licenses(self) -> List[Dict[str, Any]]:
        """Retrieves all active organization licenses from Webex Control Hub."""
        all_licenses: List[Dict[str, Any]] = []
        next_url: Optional[str] = "/licenses"
        page_num = 1

        while next_url:
            logger.info("Fetching Webex Licenses page %d...", page_num)
            data, next_url = self._request_page("GET", next_url)
            items = data.get("items", [])

            if not items:
                break

            all_licenses.extend(items)
            page_num += 1

        return all_licenses

    def get_workspace_calling_licenses(self) -> List[Dict[str, Any]]:
        """Finds Calling licenses eligible for workspace provisioning."""
        licenses = self.get_licenses()
        workspace_licenses = []
        for lic in licenses:
            lic_id = lic.get("id", "")
            name = lic.get("name", "").lower()
            if self.calling_license_id and lic_id == self.calling_license_id:
                workspace_licenses.append(lic)
            elif "calling" in name and any(term in name for term in ["workspace", "workstation", "common area", "standard", "professional"]):
                workspace_licenses.append(lic)
            elif "wxc_workspace" in lic.get("licenseId", "").lower():
                workspace_licenses.append(lic)

        # Sort so configured license is first, followed by "workspace" named licenses, then general calling licenses
        def _sort_key(lic: Dict[str, Any]) -> int:
            lid = lic.get("id", "")
            lname = lic.get("name", "").lower()
            if self.calling_license_id and lid == self.calling_license_id:
                return 0
            if "workspace" in lname:
                return 1
            return 2

        workspace_licenses.sort(key=_sort_key)
        return workspace_licenses or licenses

    def get_user_calling_licenses(self) -> List[Dict[str, Any]]:
        """Finds Calling licenses eligible for user provisioning (e.g. Webex Calling Professional)."""
        licenses = self.get_licenses()
        user_licenses = []
        for lic in licenses:
            lic_id = lic.get("id", "")
            name = lic.get("name", "").lower()
            if self._is_ucm_calling_license(lic):
                continue
            if self.calling_license_id and lic_id == self.calling_license_id:
                user_licenses.append(lic)
            elif "calling" in name and any(term in name for term in ["professional", "standard", "basic"]):
                user_licenses.append(lic)
            elif "wxc_user" in lic.get("licenseId", "").lower():
                user_licenses.append(lic)
        return user_licenses or licenses

    def get_workspace_by_name(self, display_name: str) -> Optional[Dict[str, Any]]:
        """Finds an existing Webex workspace by display name."""
        data = self._request(
            "GET",
            "/workspaces",
            params={"displayName": display_name.strip()},
        )
        items = data.get("items", [])
        return items[0] if items else None

    def get_workspaces_by_name(self, display_name: str) -> List[Dict[str, Any]]:
        """Finds all existing Webex workspaces matching a display name."""
        all_workspaces: List[Dict[str, Any]] = []
        next_url: Optional[str] = "/workspaces"
        params: Optional[Dict[str, str]] = {"displayName": display_name.strip()}

        while next_url:
            data, next_url = self._request_page("GET", next_url, params=params)
            params = None
            items = data.get("items", [])
            if not items:
                break
            all_workspaces.extend(items)

        return all_workspaces

    def get_devices_by_workspace(self, workspace_id: str) -> List[Dict[str, Any]]:
        """Retrieves devices currently assigned to a workspace."""
        all_devices: List[Dict[str, Any]] = []
        next_url: Optional[str] = "/devices"
        params: Optional[Dict[str, str]] = {"workspaceId": workspace_id}

        while next_url:
            data, next_url = self._request_page("GET", next_url, params=params)
            params = None
            items = data.get("items", [])
            if not items:
                break
            all_devices.extend(items)

        return all_devices

    def get_all_devices(self) -> List[Dict[str, Any]]:
        """Retrieves all devices visible to the organization for safe reassignment checks."""
        all_devices: List[Dict[str, Any]] = []
        next_url: Optional[str] = "/devices"

        while next_url:
            data, next_url = self._request_page("GET", next_url)
            items = data.get("items", [])
            if not items:
                break
            all_devices.extend(items)

        return all_devices

    def get_workspace(self, workspace_id: str) -> Dict[str, Any]:
        """Retrieves a workspace with its full calling configuration."""
        return self._request("GET", f"/workspaces/{workspace_id}")

    def delete_device(self, device_id: str) -> None:
        """Deletes an MPP device assignment before changing its workspace location."""
        self._request("DELETE", f"/devices/{device_id}")

    def update_workspace(
        self,
        workspace_id: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Updates a workspace using the supplied complete API payload."""
        return self._request("PUT", f"/workspaces/{workspace_id}", json=payload)

    def delete_workspace(self, workspace_id: str) -> None:
        """Deletes a workspace after its replacement has been verified."""
        self._request("DELETE", f"/workspaces/{workspace_id}")

    def update_workspace_location(
        self,
        workspace: Dict[str, Any],
        location_id: str,
    ) -> Dict[str, Any]:
        """Moves a non-calling workspace to a new location while preserving its setup."""
        workspace_id = str(workspace.get("id", "")).strip()
        if not workspace_id:
            raise WebexAPIError("Cannot move a workspace without a workspace ID.")
        if workspace.get("calling"):
            raise WebexAPIError(
                "Webex does not allow changing the location of a calling-enabled workspace."
            )

        payload = {
            key: workspace[key]
            for key in ("displayName", "type", "capacity", "supportedDevices", "calendar", "hotdesking")
            if key in workspace
        }
        payload["locationId"] = location_id

        return self._request("PUT", f"/workspaces/{workspace_id}", json=payload)

    def create_workspace(
        self,
        display_name: str,
        location_id: str,
        workspace_type: str = "desk",
        capacity: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Creates a Webex workspace for a reviewed migration row."""
        payload: Dict[str, Any] = {
            "displayName": display_name,
            "locationId": location_id,
            "type": workspace_type,
        }
        if capacity is not None:
            payload["capacity"] = capacity
        return self._request("POST", "/workspaces", json=payload)

    def create_workspace_with_calling(
        self,
        display_name: str,
        location_id: str,
        workspace_type: str = "desk",
        capacity: int = 1,
        calling_type: str = "webexCalling",
        extension: Optional[str] = None,
        phone_number: Optional[str] = None,
        calling_license_id: Optional[str] = None,
        supported_devices: str = "phones"
    ) -> Dict[str, Any]:
        """
        Creates a Webex Calling Workspace with extension and licensing attached.
        Matches the Postman 'Create Workspace Ext Standard' API specification.
        """
        payload: Dict[str, Any] = {
            "displayName": display_name,
            "locationId": location_id,
            "capacity": capacity,
            "type": workspace_type,
            "supportedDevices": supported_devices,
        }

        if calling_type:
            calling_obj: Dict[str, Any] = {"type": calling_type}
            if calling_type == "webexCalling":
                wxc_config: Dict[str, Any] = {
                    "locationId": location_id
                }
                if extension:
                    wxc_config["extension"] = extension
                if phone_number:
                    wxc_config["phoneNumber"] = phone_number
                if calling_license_id:
                    wxc_config["licenses"] = [calling_license_id]
                calling_obj["webexCalling"] = wxc_config

            payload["calling"] = calling_obj

        logger.info("Creating Webex Calling Workspace: '%s' in Location %s (Ext: %s)", display_name, location_id, extension)
        return self._request("POST", "/workspaces", json=payload)

    def add_device_to_workspace(self, mac_address: str, model: str, workspace_id: str) -> Dict[str, Any]:
        """
        Associates an MPP phone device with a workspace using POST /v1/devices.
        Matches the Postman 'Add Device to New Workspace' API specification.
        """
        clean_mac = mac_address.replace(":", "").replace("-", "").replace(".", "").upper()
        norm_model = normalize_webex_device_model(model)
        payload = {
            "mac": clean_mac,
            "model": norm_model,
            "workspaceId": workspace_id,
        }
        logger.info("Adding device MAC %s (%s -> %s) to Workspace ID %s via /v1/devices...", clean_mac, model, norm_model, workspace_id)
        return self._request("POST", "/devices", json=payload)

    def provision_workspace_device(self, mac_address: str, model: str, location_id: str, workspace_id: str) -> Dict[str, Any]:
        """Registers an MPP device and associates it with a Webex workspace via telephony config."""
        clean_mac = mac_address.replace(":", "").replace("-", "").replace(".", "").upper()
        norm_model = normalize_webex_device_model(model)
        payload = {
            "mac": clean_mac,
            "model": norm_model,
            "workspaceId": workspace_id,
        }
        logger.info("Registering MPP Device MAC %s (%s -> %s) to Workspace %s in Control Hub...", clean_mac, model, norm_model, workspace_id)
        return self._request("POST", "/devices", json=payload)

    def get_location_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Finds an existing Webex location by name (case-insensitive)."""
        target = str(name or "").strip().casefold()
        if not target:
            return None
        # Try direct query parameter if supported
        try:
            res = self._request("GET", "/locations", params={"name": name.strip()})
            for item in res.get("items", []):
                if str(item.get("name", "")).strip().casefold() == target:
                    return item
        except Exception:
            pass

        # Search cached or full locations list
        for loc in self.get_locations():
            if str(loc.get("name", "")).strip().casefold() == target:
                return loc
        return None

    def create_location(
        self,
        name: str,
        address1: str,
        city: str,
        state: str,
        postal_code: str,
        country: str = "US",
        time_zone: str = "America/Chicago",
        address2: Optional[str] = None,
        preferred_language: str = "en_US",
        announcement_language: str = "en_US",
    ) -> Dict[str, Any]:
        """Creates a new organizational location in Webex Control Hub."""
        clean_country = country.strip().upper() if country else "US"
        if clean_country in {"UNITED STATES", "USA", "U.S.", "U.S.A.", "US"}:
            clean_country = "US"
        elif clean_country in {"CANADA", "CA"}:
            clean_country = "CA"
        elif len(clean_country) > 2:
            clean_country = clean_country[:2]

        norm_tz = normalize_webex_timezone(time_zone, state=state, country=clean_country)

        # Ensure single-line clean strings without newlines
        clean_addr1 = " ".join(str(address1 or "").split())
        clean_city = " ".join(str(city or "").split())
        clean_state = str(state or "").strip().upper()
        clean_zip = re.sub(r"[^\d\-]", "", str(postal_code or "")).strip()

        address_obj: Dict[str, Any] = {
            "address1": clean_addr1,
            "city": clean_city,
            "state": clean_state,
            "postalCode": clean_zip,
            "country": clean_country or "US",
        }
        if address2 and str(address2).strip():
            address_obj["address2"] = " ".join(str(address2).split())

        norm_pref_lang = "en_US" if preferred_language.lower() in ("en_us", "en-us", "english", "en") else preferred_language.strip()
        norm_announ_lang = "en_US" if announcement_language.lower() in ("en_us", "en-us", "english", "en") else announcement_language.strip()

        payload: Dict[str, Any] = {
            "name": name.strip(),
            "timeZone": norm_tz,
            "preferredLanguage": norm_pref_lang,
            "announcementLanguage": norm_announ_lang,
            "address": address_obj,
        }
        logger.info("Creating Webex Location '%s' (%s, %s; TZ: %s)...", name, clean_city, clean_state, norm_tz)

        try:
            return self._request("POST", "/locations", json=payload)
        except WebexAPIError as exc:
            # Defensive recovery: If location name already exists or validation fails, check if location exists
            if "400" in str(exc) or "409" in str(exc):
                existing = self.get_location_by_name(name.strip())
                if existing:
                    logger.info("Location '%s' already exists in Control Hub (ID: %s). Reusing location.", name, existing.get("id"))
                    return existing
            raise

    def get_or_create_location(
        self,
        name: str,
        address1: str,
        city: str,
        state: str,
        postal_code: str,
        country: str = "US",
        time_zone: str = "America/Chicago",
        address2: Optional[str] = None,
        preferred_language: str = "en_US",
        announcement_language: str = "en_US",
    ) -> Tuple[Dict[str, Any], bool]:
        """Finds an existing location by name (case-insensitive) or creates a new one."""
        existing = self.get_location_by_name(name)
        if existing:
            return existing, False

        created = self.create_location(
            name=name,
            address1=address1,
            city=city,
            state=state,
            postal_code=postal_code,
            country=country,
            time_zone=time_zone,
            address2=address2,
            preferred_language=preferred_language,
            announcement_language=announcement_language,
        )
        return created, True

    def get_location_details(self, location_id: str) -> Dict[str, Any]:
        """Retrieves details of a specific Webex location."""
        return self._request("GET", f"/locations/{location_id}")

    def enable_calling_for_location(
        self,
        location_id: str,
        connection_type: str = "LOCAL_GATEWAY",
        routing_group_id: Optional[str] = None,
        internal_extension_length: int = 4,
        routing_prefix: Optional[str] = None,
        outside_dial_digit: Optional[str] = None,
        name: Optional[str] = None,
        announcement_language: str = "English",
    ) -> Dict[str, Any]:
        """
        Enables Webex Calling on an existing Control Hub location.
        POST /v1/telephony/config/locations accepts: id, name, announcementLanguage
        (required) plus optional routingPrefix / outsideDialDigit. Extension length and
        PSTN connection type are NOT part of this call and are configured separately.
        """
        details: Dict[str, Any] = {}
        try:
            details = self.get_location_details(location_id) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not fetch details for location %s: %s", location_id, exc)
        if not name:
            name = str(details.get("name", "")).strip()

        norm_lang = str(announcement_language or "en_us").strip().lower()
        if norm_lang in ("english", "en", "en-us", "en_us"):
            norm_lang = "en_us"
        elif "-" in norm_lang:
            norm_lang = norm_lang.replace("-", "_")

        payload: Dict[str, Any] = {
            "id": location_id,
            "name": name[:80],
            "announcementLanguage": norm_lang,
        }
        if details.get("timeZone"):
            payload["timeZone"] = details["timeZone"]
        pref = str(details.get("preferredLanguage") or "en_US").strip().replace("-", "_")
        payload["preferredLanguage"] = pref
        if isinstance(details.get("address"), dict) and details["address"]:
            payload["address"] = details["address"]
        if routing_prefix:
            payload["routingPrefix"] = routing_prefix
        if outside_dial_digit:
            payload["outsideDialDigit"] = outside_dial_digit

        logger.info(
            "Enabling Webex Calling for Location ID %s ('%s'; requested connection: %s, ExtLen: %s)",
            location_id,
            name,
            connection_type,
            internal_extension_length,
        )
        return self._request("POST", "/telephony/config/locations", json=payload)

    def is_calling_enabled_for_location(self, location_id: str) -> bool:
        """Returns True if the location already has Webex Calling configured."""
        try:
            self._request("GET", f"/telephony/config/locations/{location_id}")
            return True
        except WebexAPIError as exc:
            if str(exc).startswith("HTTP 404"):
                return False
            raise

    def update_location(
        self,
        location_id: str,
        name: Optional[str] = None,
        address1: Optional[str] = None,
        city: Optional[str] = None,
        state: Optional[str] = None,
        postal_code: Optional[str] = None,
        country: str = "US",
        time_zone: Optional[str] = None,
        address2: Optional[str] = None,
        preferred_language: str = "en_us",
    ) -> Dict[str, Any]:
        """Updates location details and address (acting as Emergency Services Address)."""
        existing = self.get_location_details(location_id)
        current_addr = existing.get("address", {}) or {}

        clean_country = country.strip().upper() if country else "US"
        if clean_country in {"UNITED STATES", "USA", "U.S.", "U.S.A.", "US"}:
            clean_country = "US"
        elif clean_country in {"CANADA", "CA"}:
            clean_country = "CA"
        elif len(clean_country) > 2:
            clean_country = clean_country[:2]

        address_obj = {
            "address1": (address1 or current_addr.get("address1", "")).strip(),
            "city": (city or current_addr.get("city", "")).strip(),
            "state": (state or current_addr.get("state", "")).strip(),
            "postalCode": (postal_code or current_addr.get("postalCode", "")).strip(),
            "country": clean_country or "US",
        }
        addr2 = address2 or current_addr.get("address2")
        if addr2 and addr2.strip():
            address_obj["address2"] = addr2.strip()

        payload = {
            "name": (name or existing.get("name", "")).strip(),
            "timeZone": (time_zone or existing.get("timeZone", "America/Chicago")).strip(),
            "preferredLanguage": (preferred_language or existing.get("preferredLanguage", "en_us")).strip(),
            "address": address_obj,
        }
        logger.info("Updating Webex Location ID %s address/details...", location_id)
        return self._request("PUT", f"/locations/{location_id}", json=payload)

    def get_route_groups(self) -> List[Dict[str, Any]]:
        """Lists all SIP Route Groups configured in Webex Control Hub."""
        all_rgs: List[Dict[str, Any]] = []
        next_url: Optional[str] = "/telephony/config/premisePstn/routeGroups"
        while next_url:
            data, next_url = self._request_page("GET", next_url)
            items = data.get("routeGroups", []) or data.get("items", [])
            if not items:
                break
            all_rgs.extend(items)
        return all_rgs

    def get_trunks(self) -> List[Dict[str, Any]]:
        """Lists all SIP Trunks configured in Webex Control Hub."""
        all_trunks: List[Dict[str, Any]] = []
        next_url: Optional[str] = "/telephony/config/premisePstn/trunks"
        while next_url:
            data, next_url = self._request_page("GET", next_url)
            items = data.get("trunks", []) or data.get("items", [])
            if not items:
                break
            all_trunks.extend(items)
        return all_trunks

    def get_pstn_routing_options(self) -> List[Dict[str, Any]]:
        """
        Retrieves combined Route Groups and Trunks available for premises-based PSTN routing.
        Each entry has 'id', 'name', 'type' ('ROUTE_GROUP' or 'TRUNK'), and 'display'.
        """
        options: List[Dict[str, Any]] = []
        try:
            for rg in self.get_route_groups():
                rg_id = rg.get("id") or rg.get("rgId")
                rg_name = rg.get("name", "")
                if rg_id and rg_name:
                    options.append({
                        "id": rg_id,
                        "name": rg_name,
                        "type": "ROUTE_GROUP",
                        "display": f"{rg_name} (Route Group)",
                    })
        except Exception as exc:
            logger.warning("Could not list route groups: %s", exc)

        try:
            for trk in self.get_trunks():
                trk_id = trk.get("id") or trk.get("trunkId")
                trk_name = trk.get("name", "")
                if trk_id and trk_name:
                    options.append({
                        "id": trk_id,
                        "name": trk_name,
                        "type": "TRUNK",
                        "display": f"{trk_name} (Trunk)",
                    })
        except Exception as exc:
            logger.warning("Could not list trunks: %s", exc)

        return options

    def get_location_pstn_connection(self, location_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves the current configured PSTN connection details for a location."""
        try:
            return self._request("GET", f"/telephony/pstn/locations/{location_id}/connection")
        except WebexAPIError as exc:
            if "HTTP 404" in str(exc) or "HTTP 400" in str(exc):
                return None
            raise

    def set_location_pstn_connection(
        self,
        location_id: str,
        route_type: str = "ROUTE_GROUP",
        route_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Sets or updates the premises-based PSTN connection for a location.
        PUT /v1/telephony/pstn/locations/{locationId}/connection
        Automatically resolves human-readable names (e.g. 'CUCM') against available
        Route Groups or Trunks.
        """
        target_id = route_id
        target_type = route_type or "ROUTE_GROUP"

        lookup_name = (route_id or "CUCM").strip()

        # Check route groups first
        try:
            route_groups = self.get_route_groups()
            for rg in route_groups:
                rg_name = str(rg.get("name", "")).strip()
                rg_id = rg.get("id") or rg.get("rgId")
                if rg_name.casefold() == lookup_name.casefold():
                    target_id = rg_id
                    target_type = "ROUTE_GROUP"
                    logger.info("Resolved PSTN route name '%s' to Route Group ID %s", lookup_name, target_id)
                    break
        except Exception as exc:
            logger.warning("Could not query route groups for PSTN resolution: %s", exc)

        # If still not resolved to a UUID/different ID, check trunks
        if target_id == lookup_name and len(lookup_name) < 32:
            try:
                trunks = self.get_trunks()
                for trk in trunks:
                    trk_name = str(trk.get("name", "")).strip()
                    trk_id = trk.get("id") or trk.get("trunkId")
                    if trk_name.casefold() == lookup_name.casefold():
                        target_id = trk_id
                        target_type = "TRUNK"
                        logger.info("Resolved PSTN route name '%s' to Trunk ID %s", lookup_name, target_id)
                        break
            except Exception as exc:
                logger.warning("Could not query trunks for PSTN resolution: %s", exc)

        if not target_id:
            raise ValueError(f"Could not resolve Route Group or Trunk '{lookup_name}' in Webex Control Hub.")

        payload = {
            "premiseRouteType": target_type,
            "premiseRouteId": target_id,
        }
        logger.info(
            "Configuring PSTN connection for Location ID %s (premiseRouteType=%s, premiseRouteId=%s)...",
            location_id,
            target_type,
            target_id,
        )
        return self._request("PUT", f"/telephony/pstn/locations/{location_id}/connection", json=payload)

    def get_location_numbers(self, location_id: str) -> List[Dict[str, Any]]:
        """Retrieves external phone numbers assigned to a Webex location."""
        all_numbers: List[Dict[str, Any]] = []
        next_url: Optional[str] = f"/telephony/config/locations/{location_id}/numbers"
        while next_url:
            data, next_url = self._request_page("GET", next_url)
            items = data.get("phoneNumbers", []) or data.get("items", [])
            if not items:
                break
            all_numbers.extend(items)
        return all_numbers

    def add_location_numbers(
        self,
        location_id: str,
        phone_numbers: List[str],
        state: str = "ACTIVE",
    ) -> Dict[str, Any]:
        """Adds external PSTN DIDs in E.164 format to the specified Webex location."""
        clean_numbers: List[str] = []
        for num in phone_numbers:
            clean = str(num).strip()
            if clean and not clean.startswith("+"):
                clean = f"+{clean}" if clean.isdigit() else clean
            if clean:
                clean_numbers.append(clean)

        payload = {
            "phoneNumbers": clean_numbers,
            "state": state,
        }
        logger.info("Adding %d phone number(s) to Webex Location ID %s...", len(clean_numbers), location_id)
        return self._request("POST", f"/telephony/config/locations/{location_id}/numbers", json=payload)

    def set_location_main_number(self, location_id: str, phone_number: str) -> Dict[str, Any]:
        """Designates the primary pilot DID as the Main Number for the location."""
        clean = str(phone_number).strip()
        if clean and not clean.startswith("+"):
            clean = f"+{clean}" if clean.isdigit() else clean
        payload = {"callingLineId": {"phoneNumber": clean}}
        logger.info("Setting Main Number for Location ID %s to %s", location_id, clean)
        return self._request("PUT", f"/telephony/config/locations/{location_id}", json=payload)

    def generate_device_activation_code(
        self,
        person_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Generates a 16-digit activation code for registering a phone to a user or workspace.
        Calls POST /v1/devices/activationCode.
        """
        payload: Dict[str, Any] = {}
        if person_id:
            payload["personId"] = person_id
        elif workspace_id:
            payload["workspaceId"] = workspace_id
        else:
            raise ValueError("Either person_id or workspace_id must be provided to generate an activation code.")

        if model:
            payload["model"] = normalize_webex_device_model(model)

        logger.info("Generating Webex device activation code for %s...", f"person {person_id}" if person_id else f"workspace {workspace_id}")
        return self._request("POST", "/devices/activationCode", json=payload)

    def get_device_details(self, device_id: str) -> Dict[str, Any]:
        """Retrieves detailed information for a specific registered device."""
        return self._request("GET", f"/devices/{device_id}")

    def get_device_by_mac(self, mac_address: str) -> Optional[Dict[str, Any]]:
        """Finds a device registered in Control Hub matching the specified MAC address."""
        clean_mac = re.sub(r"[^0-9A-Fa-f]", "", mac_address).upper()
        devices = self.get_all_devices()
        for dev in devices:
            dev_mac = re.sub(r"[^0-9A-Fa-f]", "", dev.get("mac", "")).upper()
            if dev_mac == clean_mac:
                return dev
        return None