"""
providers/redsky_provider.py

RedSky E911 Anywhere REST API Client.
Automates emergency location dispatch provisioning, civic address validation (MSAG/PSAP),
and network wiremapping (IP subnets) for compliance with Kari's Law and Ray Baum's Act.

API documentation: https://resources.e911cloud.com/apis/index.html
"""

import ipaddress
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple
from dotenv import load_dotenv
import requests
from requests.exceptions import RequestException

logger = logging.getLogger("Redsky_Provider")


class RedskyAPIError(Exception):
    """Base exception for RedSky E911 API errors."""


class RedskyAuthError(RedskyAPIError):
    """Raised when RedSky API key authentication fails."""


class RedskyProvider:
    """
    REST client for RedSky E911 Anywhere platform.
    Handles token exchange, building registration, MSAG address validation,
    and subnet wiremapping.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = "https://api.wxc.e911cloud.com",
        timeout: int = 30,
    ):
        settings = None
        try:
            from core.config import get_settings
            settings = get_settings()
        except Exception:
            pass

        # Resolve base URL (supports REDSKY_URL and REDSKY_BASE_URL)
        resolved_url = ""
        if settings and (settings.REDSKY_URL or settings.REDSKY_BASE_URL):
            resolved_url = settings.REDSKY_URL or settings.REDSKY_BASE_URL
        if not resolved_url:
            resolved_url = os.getenv("REDSKY_URL", "") or os.getenv("REDSKY_BASE_URL", "")
        if resolved_url and resolved_url.strip():
            base_url = resolved_url.strip().strip('"').strip("'")

        if api_key is not None:
            raw_key = api_key
        else:
            raw_key = ""
            if settings and (settings.REDSKY_API or settings.REDSKY_API_KEY):
                raw_key = settings.REDSKY_API or settings.REDSKY_API_KEY

            if not raw_key:
                from pathlib import Path
                env_candidate = Path(__file__).resolve().parent.parent / ".env"
                if env_candidate.exists():
                    load_dotenv(dotenv_path=env_candidate, override=False)
                else:
                    load_dotenv()

                for k in ("REDSKY_API", "REDSKY_API_KEY", "REDSKY_KEY", "RED_SKY_API", "RED_SKY_API_KEY", "REDSKY_TOKEN"):
                    val = os.getenv(k, "").strip().strip('"').strip("'")
                    if val:
                        raw_key = val
                        break

        self.api_key = raw_key.strip().strip('"').strip("'")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

        self._access_token: Optional[str] = None
        self._token_expiry: float = 0.0
        self._company_id: Optional[str] = None

        if not self.api_key:
            logger.info("RedSky Provider initialized without REDSKY_API key.")
        else:
            logger.info("RedSky Provider initialized with API key (length %d).", len(self.api_key))

    def is_configured(self) -> bool:
        """Returns True if a RedSky API key is available."""
        return bool(self.api_key)

    def _authenticate(self) -> str:
        """
        Exchanges the API key for an E911 Anywhere JWT access token via POST /auth-service/login/apiKey.
        """
        if not self.api_key:
            raise RedskyAuthError("RedSky API key is not configured. Set REDSKY_API in .env.")

        auth_url = f"{self.base_url}/auth-service/login/apiKey"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        try:
            logger.info("Authenticating with RedSky E911 Anywhere (%s)...", auth_url)
            # RedSky expects the API key as a JSON string literal
            resp = requests.post(auth_url, json=self.api_key, headers=headers, timeout=self.timeout)

            if not resp.ok:
                err_detail = resp.text
                try:
                    err_json = resp.json()
                    err_detail = err_json.get("detail") or err_json.get("title") or resp.text
                except Exception:
                    pass
                raise RedskyAuthError(
                    f"RedSky authentication failed (HTTP {resp.status_code}): {err_detail}"
                )

            data = resp.json()
            token = data.get("accessToken", "").strip()
            if not token:
                raise RedskyAuthError("RedSky auth response did not contain an 'accessToken'.")

            self._access_token = token
            # Typical JWT expiry is 1 hour; cache for 50 minutes (3000 seconds)
            self._token_expiry = time.time() + 3000

            # Extract tenant companyId from JWT claims (rsOrg)
            try:
                import base64
                import json
                parts = token.split(".")
                if len(parts) >= 2:
                    payload_b64 = parts[1]
                    payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)
                    claims = json.loads(base64.urlsafe_b64decode(payload_b64))
                    if claims.get("rsOrg"):
                        self._company_id = str(claims["rsOrg"]).strip()
                        logger.info("Extracted RedSky tenant companyId from JWT: %s (%s)", self._company_id, claims.get("rsOrgName"))
            except Exception as token_err:
                logger.debug("Could not decode JWT claims for companyId: %s", token_err)

            logger.info("Successfully obtained RedSky access token.")
            return self._access_token

        except RequestException as exc:
            logger.exception("RedSky authentication connection failure: %s", exc)
            raise RedskyAuthError(f"RedSky connection error during auth: {exc}") from exc

    def _get_valid_token(self) -> str:
        """Returns active cached token or fetches a new one if expired."""
        if self._access_token and time.time() < self._token_expiry:
            return self._access_token
        return self._authenticate()

    def _request(
        self,
        method: str,
        endpoint: str,
        service: str = "geography-service",
        **kwargs
    ) -> Any:
        """
        Executes an authenticated request against a RedSky microservice.
        Auto-refreshes token on HTTP 401.
        """
        url = f"{self.base_url}/{service.strip('/')}/{endpoint.lstrip('/')}"
        token = self._get_valid_token()

        headers = kwargs.pop("headers", {}) or {}
        headers.setdefault("Authorization", f"Bearer {token}")
        headers.setdefault("Accept", "application/json")
        kwargs["headers"] = headers
        kwargs.setdefault("timeout", self.timeout)

        try:
            resp = requests.request(method, url, **kwargs)

            # Retry once on 401 unauthorized in case token expired early
            if resp.status_code == 401:
                logger.warning("RedSky returned HTTP 401. Re-authenticating...")
                token = self._authenticate()
                headers["Authorization"] = f"Bearer {token}"
                resp = requests.request(method, url, **kwargs)

            if not resp.ok:
                err_msg = resp.text
                try:
                    err_json = resp.json()
                    err_msg = err_json.get("detail") or err_json.get("title") or err_json.get("message") or resp.text
                except Exception:
                    pass
                raise RedskyAPIError(f"RedSky API HTTP {resp.status_code} [{url}]: {err_msg}")

            if resp.status_code == 204 or not resp.content:
                return {}

            return resp.json()

        except RequestException as exc:
            logger.exception("RedSky request exception [%s %s]: %s", method, url, exc)
            raise RedskyAPIError(f"RedSky network error [{url}]: {exc}") from exc

    # =========================================================================
    # Geography Service (Buildings & Dispatchable Locations)
    # =========================================================================

    def get_buildings(self) -> List[Dict[str, Any]]:
        """
        Retrieves all physical buildings/locations registered in RedSky.
        Supports both Cisco Webex Calling RedSky tenant (/locations/search?pattern=*)
        and RedSky Anywhere (/buildings).
        """
        # 1. Try standard /buildings endpoint (supported on RedSky Anywhere)
        try:
            res = self._request("GET", "/buildings", service="geography-service")
            if isinstance(res, list):
                return res
            if isinstance(res, dict):
                items = res.get("items", []) or res.get("buildings", [])
                if items:
                    return items
        except RedskyAPIError as exc:
            # HTTP 405 Method Not Allowed indicates Webex Calling RedSky tenant
            logger.debug("GET /buildings not supported (%s), falling back to location search...", exc)

        # 2. Webex Calling RedSky tenant: search locations by wildcard pattern '*'
        comp_id = self.get_company_id()
        params: Dict[str, Any] = {"pattern": "*"}
        if comp_id:
            params["orgId"] = comp_id

        try:
            res = self._request("GET", "/locations/search", service="geography-service", params=params)
            items = res if isinstance(res, list) else (res.get("items", []) if isinstance(res, dict) else [])
            normalized = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                addr = item.get("address") or {}
                msag = addr.get("msagValid") if isinstance(addr, dict) else item.get("msagValid")
                normalized.append({
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "parentId": item.get("parentId"),
                    "msagValid": msag or "VALID",
                    "address": addr,
                    "normalizedAddress": addr.get("normalizedAddress") if isinstance(addr, dict) else None,
                    "raw": item,
                })
            return normalized
        except Exception as search_err:
            logger.warning("RedSky location search failed: %s", search_err)
            return []

    def get_building_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Finds a registered RedSky building/location matching the specified name (case-insensitive)."""
        target = name.strip().casefold()
        if not target:
            return None

        # 1. Try direct location search by pattern (Webex Calling RedSky tenant)
        comp_id = self.get_company_id()
        params: Dict[str, Any] = {"pattern": name.strip()}
        if comp_id:
            params["orgId"] = comp_id

        try:
            res = self._request("GET", "/locations/search", service="geography-service", params=params)
            items = res if isinstance(res, list) else (res.get("items", []) if isinstance(res, dict) else [])
            for item in items:
                if not isinstance(item, dict):
                    continue
                loc_name = str(item.get("name", "")).strip().casefold()
                if loc_name == target:
                    addr = item.get("address") or {}
                    msag = addr.get("msagValid") if isinstance(addr, dict) else item.get("msagValid")
                    return {
                        "id": item.get("id"),
                        "name": item.get("name"),
                        "parentId": item.get("parentId"),
                        "msagValid": msag or "VALID",
                        "address": addr,
                        "normalizedAddress": addr.get("normalizedAddress") if isinstance(addr, dict) else None,
                        "raw": item,
                    }
        except Exception as e:
            logger.debug("Direct RedSky location search failed (%s), falling back to list scan...", e)

        # 2. Fallback to scanning all buildings/locations
        buildings = self.get_buildings()
        for b in buildings:
            if str(b.get("name", "")).strip().casefold() == target:
                return b
        return None

    def create_building(
        self,
        name: str,
        address1: str,
        city: str,
        state: str,
        postal_code: str,
        country: str = "US",
        address2: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Registers a new physical building in RedSky using single-line civic address.
        Calls POST /geography-service/buildings.
        RedSky automatically executes MSAG and PSAP address validation.
        """
        addr_parts = [address1.strip()]
        if address2 and address2.strip():
            addr_parts.append(address2.strip())
        addr_parts.append(f"{city.strip()}, {state.strip()} {postal_code.strip()}")
        full_address = ", ".join(addr_parts)

        payload = {
            "name": name.strip()[:255],
            "fullAddress": full_address,
        }

        logger.info("Registering building '%s' in RedSky E911 Anywhere (%s)...", name, full_address)
        return self._request("POST", "/buildings", service="geography-service", json=payload)

    def get_or_create_building(
        self,
        name: str,
        address1: str,
        city: str,
        state: str,
        postal_code: str,
        country: str = "US",
        address2: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], bool]:
        """
        Checks whether the building already exists in RedSky.
        If not found, creates it and returns (building_data, was_created).
        """
        existing = self.get_building_by_name(name)
        if existing:
            logger.info("Building '%s' already exists in RedSky (ID: %s).", name, existing.get("id"))
            return existing, False

        created = self.create_building(
            name=name,
            address1=address1,
            city=city,
            state=state,
            postal_code=postal_code,
            country=country,
            address2=address2,
        )
        return created, True

    # =========================================================================
    # Networking Service (Wiremapping IP Ranges)
    # =========================================================================

    def get_ip_ranges(self, company_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Retrieves all wiremapped IP ranges in RedSky."""
        comp_id = company_id or self.get_company_id()
        if comp_id:
            try:
                res = self._request("GET", f"/ipRange/company/{comp_id}", service="networking-service")
                if isinstance(res, list):
                    return res
                if isinstance(res, dict):
                    return res.get("items", []) or []
            except Exception as e:
                logger.debug("Failed fetching ipRanges via company path: %s", e)

        try:
            res = self._request("GET", "/ipRange", service="networking-service")
            if isinstance(res, list):
                return res
            if isinstance(res, dict):
                return res.get("items", []) or []
        except Exception:
            pass
        return []

    def map_subnet_to_building(
        self,
        building_id: str,
        subnet_cidr: str,
        description: Optional[str] = None,
        company_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Wiremaps a branch IPv4 subnet to a RedSky Building location for Ray Baum's Act compliance.
        Calls POST /networking-service/ipRange?companyId={companyId}.
        Handles already-assigned / duplicate subnets idempotently.
        """
        try:
            net = ipaddress.ip_network(subnet_cidr.strip(), strict=False)
            low_ip = str(net.network_address + 1 if net.num_addresses > 2 else net.network_address)
            high_ip = str(net.broadcast_address - 1 if net.num_addresses > 2 else net.broadcast_address)
        except ValueError as exc:
            raise ValueError(f"Invalid CIDR subnet '{subnet_cidr}': {exc}") from exc

        payload = {
            "locationId": building_id,
            "ipAddressLow": low_ip,
            "ipAddressHigh": high_ip,
            "description": description or f"Branch subnet {subnet_cidr}",
        }

        target_comp_id = company_id or getattr(self, "_company_id", None)
        if not target_comp_id and self.api_key:
            try:
                self._get_valid_token()
                target_comp_id = getattr(self, "_company_id", None)
            except Exception:
                pass

        params: Dict[str, Any] = {}
        if target_comp_id:
            params["companyId"] = target_comp_id

        logger.info(
            "Wiremapping subnet %s (%s - %s) to RedSky Building %s (companyId=%s)...",
            subnet_cidr, low_ip, high_ip, building_id, target_comp_id
        )
        try:
            return self._request(
                "POST",
                "/ipRange",
                service="networking-service",
                params=params,
                json=payload,
            )
        except RedskyAPIError as exc:
            err_str = str(exc).lower()
            if "already been assigned" in err_str or "duplicate" in err_str:
                logger.info("Subnet %s is already wiremapped in RedSky (idempotent).", subnet_cidr)
                return {
                    "status": "ALREADY_ASSIGNED",
                    "locationId": building_id,
                    "ipAddressLow": low_ip,
                    "ipAddressHigh": high_ip,
                    "description": payload["description"],
                    "detail": str(exc),
                }
            raise

    # =========================================================================
    # Admin Service (Device Users & MyE911 Provisioning)
    # =========================================================================

    def get_companies(self) -> List[Dict[str, Any]]:
        """
        Retrieves all organizations/companies for the authenticated tenant.
        Calls GET /admin-service/company.
        """
        res = self._request("GET", "/company", service="admin-service")
        if isinstance(res, list):
            return res
        if isinstance(res, dict):
            return res.get("items", []) or res.get("companies", []) or []
        return []

    def get_company_id(self) -> Optional[str]:
        """Returns the primary tenant Company ID in RedSky."""
        if getattr(self, "_company_id", None):
            return self._company_id
        if self.api_key:
            try:
                self._get_valid_token()
                if getattr(self, "_company_id", None):
                    return self._company_id
            except Exception:
                pass
        try:
            companies = self.get_companies()
            if companies and isinstance(companies, list) and len(companies) > 0:
                first_comp = companies[0]
                if isinstance(first_comp, dict) and first_comp.get("id"):
                    self._company_id = first_comp.get("id")
                    return self._company_id
        except Exception:
            pass
        return None

    def get_device_users(
        self,
        company_id: Optional[str] = None,
        search_term: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Retrieves device users registered under a company in RedSky Anywhere.
        Calls GET /admin-service/deviceUser/company/{id}.
        """
        comp_id = company_id or self.get_company_id()
        if not comp_id:
            logger.warning("No RedSky company ID available to fetch device users.")
            return []

        params: Dict[str, Any] = {}
        if search_term:
            params["searchTerm"] = search_term.strip()

        res = self._request("GET", f"/deviceUser/company/{comp_id}", service="admin-service", params=params)
        if isinstance(res, list):
            return res
        if isinstance(res, dict):
            return res.get("items", []) or res.get("deviceUsers", []) or []
        return []

    def get_device_user_by_email(
        self,
        email: str,
        company_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Finds a RedSky device user by matching email address or heldUserId (case-insensitive).
        """
        target = email.strip().casefold()
        users = self.get_device_users(company_id=company_id, search_term=target)
        for u in users:
            u_email = str(u.get("email", "")).strip().casefold()
            u_held = str(u.get("heldUserId", "")).strip().casefold()
            if u_email == target or u_held == target:
                return u
        return None

    @staticmethod
    def _phone_format_fallbacks(number: str) -> List[str]:
        """Generates alternate phone number formats (10-digit NANP, digits-only) when E.164 '+' is rejected."""
        candidates: List[str] = []
        digits = "".join(ch for ch in str(number or "") if ch.isdigit())
        if len(digits) == 11 and digits.startswith("1") and digits[1:] != number:
            candidates.append(digits[1:])
        if digits and digits != number and digits not in candidates:
            candidates.append(digits)
        return candidates

    def _device_user_request_with_phone_fallback(
        self,
        method: str,
        payload: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], str, str]:
        """Sends PUT/POST /deviceUser, retrying with 10-digit NANP or digits-only if RedSky rejects '+' E.164."""
        orig_phone = str(payload.get("phoneNumber", ""))
        orig_cb = str(payload.get("callbackNumber", ""))
        try:
            res = self._request(method, "/deviceUser", service="admin-service", json=payload)
            return res, orig_phone, orig_cb
        except RedskyAPIError as err:
            err_msg = str(err).lower()
            if "phone" not in err_msg and "400" not in err_msg:
                raise
            phone_alts = self._phone_format_fallbacks(orig_phone)
            cb_alts = self._phone_format_fallbacks(orig_cb)
            if not phone_alts and not cb_alts:
                raise

            last_err: Exception = err
            max_alts = max(len(phone_alts), len(cb_alts))
            for idx in range(max_alts):
                alt_phone = phone_alts[idx] if idx < len(phone_alts) else (phone_alts[-1] if phone_alts else orig_phone)
                alt_cb = cb_alts[idx] if idx < len(cb_alts) else (cb_alts[-1] if cb_alts else orig_cb)
                retry_payload = dict(payload)
                retry_payload["phoneNumber"] = alt_phone
                retry_payload["callbackNumber"] = alt_cb
                logger.info(
                    "RedSky rejected phone format '%s'. Retrying %s /deviceUser with '%s'...",
                    orig_phone,
                    method,
                    alt_phone,
                )
                try:
                    res = self._request(method, "/deviceUser", service="admin-service", json=retry_payload)
                    return res, alt_phone, alt_cb
                except RedskyAPIError as retry_err:
                    last_err = retry_err
                    if "phone" not in str(retry_err).lower():
                        raise
            raise last_err

    def sync_device_user_mye911(
        self,
        email: str,
        phone_number: str,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        callback_number: Optional[str] = None,
        enable_mye911: bool = True,
        held_user_id: Optional[str] = None,
        alternate_ids: Optional[List[str]] = None,
        company_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Provisions or updates a user in RedSky Anywhere with MyE911 functionality enabled,
        binding their Webex Calling direct DID / callback number for Kari's Law and Ray Baum's Act compliance.
        Enables 933 location confirmation and nomadic emergency call routing.
        Calls PUT or POST /admin-service/deviceUser.
        """
        clean_email = email.strip().lower()
        # Clean phone digits while preserving leading '+' if present
        raw_phone = phone_number.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
        clean_phone = raw_phone
        clean_cb = callback_number.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "") if callback_number else clean_phone
        target_held = (held_user_id or clean_email).strip().lower()

        existing = self.get_device_user_by_email(clean_email, company_id=company_id)

        user_opts = "MYE911_ENABLED" if enable_mye911 else "NONE"

        if existing and existing.get("id"):
            user_id = existing["id"]
            payload = {
                "id": user_id,
                "email": clean_email,
                "firstName": first_name or existing.get("firstName") or "",
                "lastName": last_name or existing.get("lastName") or "",
                "phoneNumber": clean_phone,
                "callbackNumber": clean_cb,
                "heldUserId": target_held,
                "userOptions": user_opts,
            }
            if alternate_ids is not None:
                payload["alternateIdList"] = alternate_ids

            logger.info("Updating existing RedSky device user '%s' [ID: %s] with MyE911 and phone %s...", clean_email, user_id, clean_phone)
            res, accepted_phone, accepted_cb = self._device_user_request_with_phone_fallback("PUT", payload)
            return {
                "status": "SUCCESS",
                "action": "UPDATED",
                "user_id": user_id,
                "email": clean_email,
                "phone_number": accepted_phone,
                "callback_number": accepted_cb,
                "enable_mye911": enable_mye911,
                "device_user": res,
            }

        # User does not exist, create new deviceUser
        comp_id = company_id or self.get_company_id()
        if not comp_id:
            raise RedskyAPIError("No RedSky Company ID available to create new device user.")

        payload = {
            "companyId": comp_id,
            "email": clean_email,
            "firstName": first_name or "",
            "lastName": last_name or "",
            "phoneNumber": clean_phone,
            "callbackNumber": clean_cb,
            "heldUserId": target_held,
            "userOptions": user_opts,
        }
        if alternate_ids is not None:
            payload["alternateIdList"] = alternate_ids

        logger.info("Creating new RedSky device user '%s' in company %s with MyE911 and phone %s...", clean_email, comp_id, clean_phone)
        res, accepted_phone, accepted_cb = self._device_user_request_with_phone_fallback("POST", payload)
        new_id = res.get("id") if isinstance(res, dict) else None
        return {
            "status": "SUCCESS",
            "action": "CREATED",
            "user_id": new_id,
            "email": clean_email,
            "phone_number": accepted_phone,
            "callback_number": accepted_cb,
            "enable_mye911": enable_mye911,
            "device_user": res,
        }
