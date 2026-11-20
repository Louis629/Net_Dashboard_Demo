"""
services/webex_location_provision_service.py

End-to-End Orchestrator for Phase 1 Webex Calling Location & DID Provisioning.
Extracts site physical addresses from Cisco Meraki network appliances (MX/Z4),
validates E911-mandated physical address fields, previews Webex parity,
and provisions Control Hub Locations, Calling connections, and PSTN DIDs.
"""

import ipaddress
import logging
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from core.config import get_settings
from models.telecom import (
    LocationDidProvisionResponse,
    MerakiLocationPreviewItem,
    MerakiLocationPreviewResponse,
    MerakiLocationSyncItem,
    MerakiLocationSyncResponse,
)
from providers.meraki_provider import MerakiProvider
from providers.redsky_provider import RedskyAPIError, RedskyProvider
from providers.webex_calling_provider import (
    WebexAPIError,
    WebexCallingProvider,
    normalize_webex_timezone,
)
from routers.meraki_admin import _parse_webex_location_address

logger = logging.getLogger("Net_Dashboard.WebexLocationProvisionService")

E164_PATTERN = re.compile(r"^\+[1-9]\d{6,14}$")
REQUIRED_ADDRESS_FIELDS = ("Address Line 1", "City", "State", "ZIP", "Country", "Time Zone")

VOICE_NAME_RE = re.compile(r"voice|voip|phone|telephon", re.IGNORECASE)
NON_VOICE_NAME_RE = re.compile(r"guest|visitor|iot|camera|cctv|quarantine|dmz|isolated", re.IGNORECASE)


def _get_voice_supernets() -> List[ipaddress.IPv4Network]:
    """Resolves configured Voice supernet CIDRs from VOICE_SUPERNET_CIDR."""
    settings = get_settings()
    raw_cidrs = os.getenv("VOICE_SUPERNET_CIDR")
    cidr_spec = raw_cidrs if raw_cidrs is not None else settings.VOICE_SUPERNET_CIDR
    networks: List[ipaddress.IPv4Network] = []
    for raw_cidr in cidr_spec.split(","):
        cleaned = raw_cidr.strip()
        if not cleaned:
            continue
        try:
            net = ipaddress.ip_network(cleaned, strict=False)
            if isinstance(net, ipaddress.IPv4Network):
                networks.append(net)
        except ValueError:
            logger.warning("Ignoring invalid VOICE_SUPERNET_CIDR entry: %s", cleaned)
    return networks


class WebexLocationProvisionService:
    """Orchestrates Phase 1: Meraki address extraction -> Webex Location & DID Setup & RedSky E911."""

    def __init__(
        self,
        webex_provider: Optional[WebexCallingProvider] = None,
        meraki_provider: Optional[MerakiProvider] = None,
        redsky_provider: Optional[RedskyProvider] = None,
    ):
        self.webex = webex_provider
        self.meraki = meraki_provider
        self.redsky = redsky_provider if redsky_provider is not None else RedskyProvider()
        self._subnet_cache: Dict[str, Tuple[List[Tuple[str, str]], str]] = {}

    def classify_phone_subnets(
        self, network_id: str
    ) -> Tuple[List[Tuple[str, str]], str]:
        """
        Classifies Meraki appliance subnets for E911 dispatch wiremapping (Ray Baum's Act / Kari's Law):
        1. Larger Sites (Dedicated Voice VLANs):
           - Matches configured Voice supernet CIDR(s) in VOICE_SUPERNET_CIDR
           - OR VLAN name matches 'Voice', 'VoIP', 'Phone', etc.
        2. Teleworkers & Smaller Sites:
           - Single LAN / Routed Mode (no VLANs): uses the appliance single LAN subnet.
           - Single Data VLAN: if only one corporate data VLAN exists (and no voice VLANs),
             selects that single data VLAN (ignoring guest/IoT).
        Returns:
            (subnets_to_map, classification_strategy)
            subnets_to_map: List of (cidr_string, description_suffix)
            classification_strategy: "DEDICATED_VOICE", "SINGLE_DATA_VLAN", "SINGLE_LAN", "CORPORATE_DATA_VLAN", or "NO_SUBNETS_FOUND"
        """
        if not self.meraki:
            return [], "NO_MERAKI_PROVIDER"

        if hasattr(self, "_subnet_cache") and network_id in self._subnet_cache:
            return self._subnet_cache[network_id]

        def _cache_and_return(subs: List[Tuple[str, str]], strat: str) -> Tuple[List[Tuple[str, str]], str]:
            if hasattr(self, "_subnet_cache"):
                self._subnet_cache[network_id] = (subs, strat)
            return subs, strat

        vlans: Any = []
        try:
            if hasattr(self.meraki, "dashboard") and hasattr(self.meraki.dashboard, "appliance"):
                vlans = self.meraki.dashboard.appliance.getNetworkApplianceVlans(network_id)
            elif hasattr(self.meraki, "get_network_appliance_vlans"):
                vlans = self.meraki.get_network_appliance_vlans(network_id)
        except Exception as exc:
            logger.debug("VLAN query for '%s' exception (may be in single LAN mode): %s", network_id, exc)
            vlans = []

        if vlans and isinstance(vlans, list) and len(vlans) > 0:
            voice_subnets: List[Tuple[str, str]] = []
            candidate_data_subnets: List[Tuple[str, str]] = []
            voice_supernets = _get_voice_supernets()

            for vlan in vlans:
                s_cidr = vlan.get("subnet")
                if not s_cidr or not isinstance(s_cidr, str):
                    continue
                s_clean = s_cidr.strip()
                vlan_name = str(vlan.get("name") or f"VLAN {vlan.get('id', '')}").strip()

                try:
                    vlan_net = ipaddress.ip_network(s_clean, strict=False)
                except ValueError:
                    continue

                is_supernet_voice = any(
                    vlan_net.subnet_of(supernet) or vlan_net.overlaps(supernet)
                    for supernet in voice_supernets
                )
                is_named_voice = bool(VOICE_NAME_RE.search(vlan_name))

                if is_supernet_voice or is_named_voice:
                    label = f"{vlan_name} (Voice)" if "voice" not in vlan_name.lower() else vlan_name
                    voice_subnets.append((s_clean, label))
                else:
                    if not NON_VOICE_NAME_RE.search(vlan_name):
                        candidate_data_subnets.append((s_clean, vlan_name))

            if voice_subnets:
                return _cache_and_return(voice_subnets, "DEDICATED_VOICE")

            if len(candidate_data_subnets) == 1:
                s_cidr, v_name = candidate_data_subnets[0]
                return _cache_and_return([(s_cidr, f"{v_name} (Single Data VLAN)")], "SINGLE_DATA_VLAN")

            if candidate_data_subnets:
                return _cache_and_return([(s, f"{n} (Corporate Data)") for s, n in candidate_data_subnets], "CORPORATE_DATA_VLAN")

        # Check for Single LAN mode (routed appliance mode, standard on teleworker Z-series/Z4)
        try:
            if hasattr(self.meraki, "dashboard") and hasattr(self.meraki.dashboard, "appliance"):
                single_lan = self.meraki.dashboard.appliance.getNetworkApplianceSingleLan(network_id)
                if isinstance(single_lan, dict) and single_lan.get("subnet"):
                    s_cidr = str(single_lan.get("subnet", "")).strip()
                    if s_cidr:
                        return _cache_and_return([(s_cidr, "Teleworker Single LAN")], "SINGLE_LAN")
        except Exception as exc:
            logger.debug("Single LAN query for '%s' exception: %s", network_id, exc)

        return _cache_and_return([], "NO_SUBNETS_FOUND")

    def _wiremap_subnets(
        self,
        network_id: str,
        building_id: str,
        location_name: str,
        custom_subnets: Optional[List[str]] = None,
    ) -> Tuple[int, List[str], str]:
        """
        Discovers Meraki appliance subnets/VLANs and registers them with RedSky E911
        Anywhere for Ray Baum's Act dispatchable location mapping.
        Returns:
            (mapped_count, list_of_wiremapped_cidrs, classification_strategy)
        """
        if not self.redsky or not building_id:
            return 0, [], "UNCONFIGURED"

        if custom_subnets:
            subnets_to_map = [(s.strip(), "Custom Subnet") for s in custom_subnets if s and s.strip()]
            strategy = "MANUAL"
        else:
            subnets_to_map, strategy = self.classify_phone_subnets(network_id)

        mapped_count = 0
        mapped_labels: List[str] = []

        for s_cidr, desc in subnets_to_map:
            try:
                res = self.redsky.map_subnet_to_building(building_id, s_cidr, f"{location_name} - {desc}")
                mapped_count += 1
                status_suffix = " (already active)" if isinstance(res, dict) and res.get("status") == "ALREADY_ASSIGNED" else ""
                mapped_labels.append(f"{s_cidr} ({desc}){status_suffix}")
                logger.info("Wiremapped subnet %s [%s] to RedSky Building %s (%s)%s", s_cidr, desc, building_id, location_name, status_suffix)
            except Exception as map_err:
                logger.warning("RedSky subnet map note for %s (%s): %s", s_cidr, location_name, map_err)

        return mapped_count, mapped_labels, strategy

    def wiremap_location_subnets(
        self,
        network_id: Optional[str] = None,
        location_name: Optional[str] = None,
        building_id: Optional[str] = None,
        custom_subnets: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Standalone on-demand wiremapping of Meraki phone subnets to a RedSky building.
        """
        if not self.redsky or not self.redsky.is_configured():
            raise RedskyAPIError("RedSky Provider is uninitialized or not configured.")

        target_name = (location_name or "").strip()
        target_bldg_id = (building_id or "").strip()

        if not target_bldg_id and target_name:
            bldg = self.redsky.get_building_by_name(target_name)
            if bldg and bldg.get("id"):
                target_bldg_id = bldg["id"]

        if not target_bldg_id:
            raise ValueError(f"Could not find registered RedSky building for '{target_name or 'unspecified location'}'.")

        mapped_count, mapped_labels, strategy = self._wiremap_subnets(
            network_id=network_id or "",
            building_id=target_bldg_id,
            location_name=target_name,
            custom_subnets=custom_subnets,
        )

        return {
            "status": "SUCCESS",
            "location_name": target_name,
            "building_id": target_bldg_id,
            "strategy": strategy,
            "wiremapped_subnets": mapped_labels,
            "details": f"Wiremapped {mapped_count} subnet(s) to RedSky Building {target_bldg_id} [{strategy}].",
        }

    def preview_meraki_locations(self) -> MerakiLocationPreviewResponse:
        """
        Inspects Meraki MX/Z4 appliance addresses and cross-references against live
        Webex Control Hub locations and RedSky E911 buildings to evaluate creation readiness.
        """
        if not self.meraki:
            raise ValueError("Meraki Provider is uninitialized or not configured.")

        # 1. Fetch live Meraki appliance locations
        meraki_inventory = self.meraki.get_appliance_location_inventory()

        # 2. Fetch existing Webex locations to check for existing sites (if token/provider available)
        webex_locations: List[Dict[str, Any]] = []
        if self.webex:
            try:
                webex_locations = self.webex.get_locations(max_per_page=100)
            except Exception as loc_err:
                logger.warning("Could not pre-fetch Webex locations: %s", loc_err)
                webex_locations = []

        webex_by_name: Dict[str, Dict[str, Any]] = {
            str(loc.get("name", "")).strip().casefold(): loc
            for loc in webex_locations
            if loc.get("name")
        }

        # 3. Fetch existing RedSky buildings if configured
        redsky_by_name: Dict[str, Dict[str, Any]] = {}
        if self.redsky and self.redsky.is_configured():
            try:
                redsky_buildings = self.redsky.get_buildings()
                redsky_by_name = {
                    str(b.get("name", "")).strip().casefold(): b
                    for b in redsky_buildings
                    if b.get("name")
                }
            except Exception as e:
                logger.warning("Could not pre-fetch RedSky buildings: %s", e)

        preview_items: List[MerakiLocationPreviewItem] = []
        existing_count = 0
        new_count = 0
        invalid_count = 0

        for row in meraki_inventory:
            network_id = row.get("network_id", "")
            network_name = row.get("network_name", "").strip()
            raw_address = row.get("address", "")
            parsed = _parse_webex_location_address(raw_address)
            time_zone = normalize_webex_timezone(
                row.get("time_zone"),
                state=parsed.get("State"),
                country=parsed.get("Country", "US"),
            )
            values = {
                **parsed,
                "Time Zone": time_zone,
            }

            missing = [f for f in REQUIRED_ADDRESS_FIELDS if not values.get(f, "").strip()]
            is_valid = len(missing) == 0 and not row.get("lookup_error")

            existing_match = webex_by_name.get(network_name.casefold())
            exists_in_webex = existing_match is not None
            webex_location_id = existing_match.get("id") if existing_match else None

            # RedSky E911 status
            redsky_match = redsky_by_name.get(network_name.casefold())
            redsky_building_id = redsky_match.get("id") if redsky_match else None
            if not self.redsky or not self.redsky.is_configured():
                redsky_status = "UNCONFIGURED"
            elif redsky_match:
                msag_val = str(redsky_match.get("msagValid") or (redsky_match.get("address", {}).get("msagValid") if isinstance(redsky_match.get("address"), dict) else "")).upper()
                is_msag = msag_val in ("VALID", "CIVIC", "TRUE", "1") or not redsky_match.get("ambiguous", False)
                redsky_status = "VALID" if is_msag else "AMBIGUOUS"
            else:
                redsky_status = "READY" if is_valid else "INCOMPLETE"

            if not is_valid:
                invalid_count += 1
            elif exists_in_webex:
                existing_count += 1
            else:
                new_count += 1

            models_raw = row.get("appliance_models", [])
            if isinstance(models_raw, str):
                appliance_models = [m.strip() for m in models_raw.split(",") if m.strip()]
            elif isinstance(models_raw, (list, tuple, set)):
                appliance_models = [str(m).strip() for m in models_raw if str(m).strip()]
            else:
                appliance_models = []

            serials_raw = row.get("serial_numbers", [])
            if isinstance(serials_raw, str):
                serial_numbers = [s.strip() for s in serials_raw.split(",") if s.strip()]
            elif isinstance(serials_raw, (list, tuple, set)):
                serial_numbers = [str(s).strip() for s in serials_raw if str(s).strip()]
            else:
                serial_numbers = []

            # Phone subnets are evaluated during sync or on-demand inspection to ensure instantaneous preview loading
            phone_subnets_display: List[str] = []
            wiremap_strategy: Optional[str] = None

            preview_items.append(
                MerakiLocationPreviewItem(
                    network_id=network_id,
                    location_name=network_name,
                    source_address=raw_address,
                    address_line_1=parsed.get("Address Line 1", ""),
                    city=parsed.get("City", ""),
                    state=parsed.get("State", ""),
                    zip_code=parsed.get("ZIP", ""),
                    country=parsed.get("Country", "United States"),
                    time_zone=time_zone,
                    appliance_models=appliance_models,
                    serial_numbers=serial_numbers,
                    device_count=row.get("device_count", 1),
                    exists_in_webex=exists_in_webex,
                    webex_location_id=webex_location_id,
                    is_valid=is_valid,
                    missing_fields=missing,
                    redsky_building_id=redsky_building_id,
                    redsky_status=redsky_status,
                    phone_subnets=phone_subnets_display,
                    wiremap_strategy=wiremap_strategy,
                )
            )

        return MerakiLocationPreviewResponse(
            status="SUCCESS",
            total_meraki_networks=len(preview_items),
            existing_in_webex_count=existing_count,
            new_to_create_count=new_count,
            invalid_address_count=invalid_count,
            locations=preview_items,
        )

    def sync_locations(
        self,
        network_ids: Optional[List[str]] = None,
        dry_run: bool = True,
        connection_type: str = "LOCAL_GATEWAY",
        routing_group_id: Optional[str] = None,
        internal_extension_length: int = 4,
        routing_prefix: Optional[str] = None,
        sync_redsky: bool = True,
    ) -> MerakiLocationSyncResponse:
        """
        Creates Webex Calling locations for specified Meraki networks, configures
        their telephony routing parameters, and registers E911 dispatch buildings in RedSky.
        """
        preview = self.preview_meraki_locations()
        target_ids: Optional[Set[str]] = set(network_ids) if network_ids else None

        results: List[MerakiLocationSyncItem] = []
        created_count = 0
        existing_count = 0
        failed_count = 0
        skipped_count = 0

        for item in preview.locations:
            if target_ids is not None and item.network_id not in target_ids:
                continue

            if not item.is_valid:
                results.append(
                    MerakiLocationSyncItem(
                        network_id=item.network_id,
                        location_name=item.location_name,
                        status="SKIPPED",
                        details=f"Incomplete address data; missing: {', '.join(item.missing_fields)}",
                    )
                )
                skipped_count += 1
                continue

            if item.exists_in_webex:
                if dry_run:
                    cand_subs, strat = self.classify_phone_subnets(item.network_id)
                    redsky_sim = ""
                    if sync_redsky and self.redsky and self.redsky.is_configured():
                        cand_desc = f" ({len(cand_subs)} subnet(s) ready [{strat}]: {', '.join(s[0] for s in cand_subs)})" if cand_subs else ""
                        redsky_sim = f" RedSky E911 dispatch registration simulated{cand_desc}."
                    elif cand_subs:
                        redsky_sim = f" ({len(cand_subs)} subnet(s) ready [{strat}]: {', '.join(s[0] for s in cand_subs)})."
                    results.append(
                        MerakiLocationSyncItem(
                            network_id=item.network_id,
                            location_name=item.location_name,
                            status="DRY_RUN",
                            webex_location_id=item.webex_location_id,
                            details=f"Location exists in Webex Control Hub.{redsky_sim}",
                            wiremapped_subnets=[s[0] for s in cand_subs],
                            wiremap_strategy=strat,
                        )
                    )
                    existing_count += 1
                    continue

                if item.webex_location_id:
                    # Location may exist (e.g. from an earlier partial run) without Calling or PSTN enabled
                    try:
                        actions_taken: List[str] = []
                        if self.webex and not self.webex.is_calling_enabled_for_location(item.webex_location_id):
                            self.webex.enable_calling_for_location(
                                location_id=item.webex_location_id,
                                connection_type=connection_type,
                                routing_group_id=routing_group_id,
                                internal_extension_length=internal_extension_length,
                                routing_prefix=routing_prefix,
                                name=item.location_name,
                            )
                            actions_taken.append("Calling enabled")

                        # Configure Premises-based PSTN connection if LOCAL_GATEWAY
                        if connection_type == "LOCAL_GATEWAY" and self.webex:
                            pstn_target = routing_group_id or "CUCM"
                            try:
                                self.webex.set_location_pstn_connection(
                                    location_id=item.webex_location_id,
                                    route_type="ROUTE_GROUP",
                                    route_id=pstn_target,
                                )
                                actions_taken.append(f"Premises-based PSTN routed to {pstn_target}")
                            except Exception as pstn_err:
                                logger.warning("PSTN routing setup warning for existing '%s': %s", item.location_name, pstn_err)
                                actions_taken.append(f"PSTN setup note: {pstn_err}")

                        cand_subs, strat = self.classify_phone_subnets(item.network_id)
                        wiremapped_subnets_list: List[str] = [s[0] for s in cand_subs]
                        strat_name: Optional[str] = strat

                        # RedSky E911 registration if enabled
                        if sync_redsky:
                            if not self.redsky or not self.redsky.is_configured():
                                actions_taken.append("RedSky E911 skipped (REDSKY_API is unconfigured in .env)")
                            else:
                                try:
                                    bldg, b_created = self.redsky.get_or_create_building(
                                        name=item.location_name,
                                        address1=item.address_line_1,
                                        city=item.city,
                                        state=item.state,
                                        postal_code=item.zip_code,
                                        country=item.country,
                                    )
                                    b_id = bldg.get("id") or ""
                                    msag_raw = bldg.get("msagValid") or (bldg.get("address", {}).get("msagValid") if isinstance(bldg.get("address"), dict) else None)
                                    msag_str = "VALID" if (str(msag_raw).upper() in ("VALID", "CIVIC", "TRUE", "1") or not bldg.get("ambiguous")) else "AMBIGUOUS"
                                    wiremapped, wiremapped_labels, wiremapped_strat = self._wiremap_subnets(item.network_id, b_id, item.location_name)
                                    wiremap_note = f", {wiremapped} subnet(s) wiremapped" if wiremapped > 0 else ""
                                    actions_taken.append(f"RedSky E911 registered [ID: {b_id[:8]}..., MSAG: {msag_str}{wiremap_note}]")
                                    if wiremapped_labels:
                                        wiremapped_subnets_list = wiremapped_labels
                                        strat_name = wiremapped_strat
                                except Exception as redsky_err:
                                    logger.warning("RedSky registration warning for existing '%s': %s", item.location_name, redsky_err)
                                    actions_taken.append(f"RedSky note: {redsky_err}")

                        if actions_taken:
                            results.append(
                                MerakiLocationSyncItem(
                                    network_id=item.network_id,
                                    location_name=item.location_name,
                                    status="CREATED",
                                    webex_location_id=item.webex_location_id,
                                    details="; ".join(actions_taken) + ".",
                                    wiremapped_subnets=wiremapped_subnets_list,
                                    wiremap_strategy=strat_name,
                                )
                            )
                            created_count += 1
                            continue
                    except (WebexAPIError, ValueError, KeyError) as exc:
                        logger.exception("Failed enabling Calling/PSTN on existing location '%s': %s", item.location_name, exc)
                        results.append(
                            MerakiLocationSyncItem(
                                network_id=item.network_id,
                                location_name=item.location_name,
                                status="FAILED",
                                webex_location_id=item.webex_location_id,
                                details=f"Location exists but enabling Webex Calling failed: {exc}",
                            )
                        )
                        failed_count += 1
                        continue
                cand_subs, strat = self.classify_phone_subnets(item.network_id)
                results.append(
                    MerakiLocationSyncItem(
                        network_id=item.network_id,
                        location_name=item.location_name,
                        status="EXISTING",
                        webex_location_id=item.webex_location_id,
                        details="Location already exists in Webex Control Hub.",
                        wiremapped_subnets=[s[0] for s in cand_subs],
                        wiremap_strategy=strat,
                    )
                )
                existing_count += 1
                continue

            if dry_run:
                target_route = routing_group_id or "CUCM"
                redsky_sim = ""
                cand_subs, strat = self.classify_phone_subnets(item.network_id)
                if sync_redsky and self.redsky and self.redsky.is_configured():
                    cand_desc = f" ({len(cand_subs)} subnet(s) ready [{strat}]: {', '.join(s[0] for s in cand_subs)})" if cand_subs else ""
                    redsky_sim = f" RedSky E911 dispatch registration simulated{cand_desc}."
                results.append(
                    MerakiLocationSyncItem(
                        network_id=item.network_id,
                        location_name=item.location_name,
                        status="DRY_RUN",
                        details=(
                            f"Validated for provisioning. Target connection: {connection_type} ({target_route}), "
                            f"Extension length: {internal_extension_length}.{redsky_sim}"
                        ),
                        wiremapped_subnets=[s[0] for s in cand_subs],
                        wiremap_strategy=strat,
                    )
                )
                created_count += 1
                continue

            # Live Mutation
            location_id = None
            try:
                created = self.webex.create_location(
                    name=item.location_name,
                    address1=item.address_line_1,
                    city=item.city,
                    state=item.state,
                    postal_code=item.zip_code,
                    country=item.country,
                    time_zone=item.time_zone,
                )
                location_id = created.get("id", "")

                # Enable Calling telephony parameters
                calling_msg = "Webex Calling enabled"
                try:
                    self.webex.enable_calling_for_location(
                        location_id=location_id,
                        connection_type=connection_type,
                        routing_group_id=routing_group_id,
                        internal_extension_length=internal_extension_length,
                        routing_prefix=routing_prefix,
                        name=item.location_name,
                    )
                except WebexAPIError as calling_err:
                    if "already" in str(calling_err).lower() or "409" in str(calling_err):
                        calling_msg = "Webex Calling configured"
                    else:
                        raise

                pstn_msg = ""
                if connection_type == "LOCAL_GATEWAY":
                    pstn_target = routing_group_id or "CUCM"
                    try:
                        self.webex.set_location_pstn_connection(
                            location_id=location_id,
                            route_type="ROUTE_GROUP",
                            route_id=pstn_target,
                        )
                        pstn_msg = f", and Premises-based PSTN routed to {pstn_target}"
                    except Exception as pstn_err:
                        logger.warning("PSTN routing setup warning for new '%s': %s", item.location_name, pstn_err)
                        pstn_msg = f" (PSTN setup note: {pstn_err})"

                cand_subs, strat = self.classify_phone_subnets(item.network_id)
                new_wiremapped_subnets: List[str] = [s[0] for s in cand_subs]
                new_strat_name: Optional[str] = strat
                redsky_msg = ""
                if sync_redsky:
                    if not self.redsky or not self.redsky.is_configured():
                        redsky_msg = ", RedSky E911 skipped (REDSKY_API is unconfigured in .env)"
                    else:
                        try:
                            bldg, b_created = self.redsky.get_or_create_building(
                                name=item.location_name,
                                address1=item.address_line_1,
                                city=item.city,
                                state=item.state,
                                postal_code=item.zip_code,
                                country=item.country,
                            )
                            b_id = bldg.get("id") or ""
                            msag_raw = bldg.get("msagValid") or (bldg.get("address", {}).get("msagValid") if isinstance(bldg.get("address"), dict) else None)
                            msag_str = "VALID" if (str(msag_raw).upper() in ("VALID", "CIVIC", "TRUE", "1") or not bldg.get("ambiguous")) else "AMBIGUOUS"
                            wiremapped, wiremapped_labels, wiremapped_strat = self._wiremap_subnets(item.network_id, b_id, item.location_name)
                            wiremap_note = f", {wiremapped} subnet(s) wiremapped" if wiremapped > 0 else ""
                            redsky_msg = f", RedSky E911 registered [MSAG: {msag_str}{wiremap_note}]"
                            if wiremapped_labels:
                                new_wiremapped_subnets = wiremapped_labels
                                new_strat_name = wiremapped_strat
                        except Exception as redsky_err:
                            logger.warning("RedSky registration warning for new '%s': %s", item.location_name, redsky_err)
                            redsky_msg = f" (RedSky note: {redsky_err})"

                results.append(
                    MerakiLocationSyncItem(
                        network_id=item.network_id,
                        location_name=item.location_name,
                        status="CREATED",
                        webex_location_id=location_id,
                        details=f"Location created, {calling_msg}{pstn_msg}{redsky_msg}.",
                        wiremapped_subnets=new_wiremapped_subnets,
                        wiremap_strategy=new_strat_name,
                    )
                )
                created_count += 1

            except (WebexAPIError, ValueError, KeyError) as exc:
                logger.exception("Failed creating Webex location for '%s': %s", item.location_name, exc)
                step_hint = "Location exists, enabling Calling failed: " if location_id else ""
                results.append(
                    MerakiLocationSyncItem(
                        network_id=item.network_id,
                        location_name=item.location_name,
                        status="FAILED",
                        webex_location_id=location_id,
                        details=f"{step_hint}{exc}",
                    )
                )
                failed_count += 1

        return MerakiLocationSyncResponse(
            status="SUCCESS",
            dry_run=dry_run,
            total_evaluated=len(results),
            created_count=created_count,
            existing_count=existing_count,
            failed_count=failed_count,
            skipped_count=skipped_count,
            results=results,
        )

    def provision_dids(
        self,
        location_id: str,
        phone_numbers: List[str],
        main_number: Optional[str] = None,
        dry_run: bool = True,
    ) -> LocationDidProvisionResponse:
        """
        Validates and activates external PSTN DIDs in E.164 format on a Webex Calling location.
        """
        if not location_id:
            raise ValueError("Webex location_id is required.")
        if not phone_numbers:
            raise ValueError("At least one phone number must be provided.")

        # 1. Normalize and validate E.164 formatting
        clean_numbers: List[str] = []
        invalid_numbers: List[str] = []

        for raw_num in phone_numbers:
            candidate = str(raw_num).strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
            if candidate and not candidate.startswith("+"):
                candidate = f"+{candidate}"
            if E164_PATTERN.match(candidate):
                clean_numbers.append(candidate)
            else:
                invalid_numbers.append(raw_num)

        if invalid_numbers:
            raise ValueError(
                f"Invalid E.164 format for numbers: {', '.join(invalid_numbers)}. "
                f"Numbers must follow E.164 standards (e.g. +15125550199)."
            )

        clean_main: Optional[str] = None
        if main_number:
            clean_main = str(main_number).strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
            if clean_main and not clean_main.startswith("+"):
                clean_main = f"+{clean_main}"
            if not E164_PATTERN.match(clean_main):
                raise ValueError(f"Main number '{main_number}' must be in E.164 format (e.g. +15125550199).")
            if clean_main not in clean_numbers:
                clean_numbers.append(clean_main)

        # 2. Verify target location exists
        loc_details = self.webex.get_location_details(location_id)
        loc_name = loc_details.get("name", location_id)

        if dry_run:
            return LocationDidProvisionResponse(
                status="SUCCESS",
                dry_run=True,
                location_id=location_id,
                location_name=loc_name,
                phone_numbers_added=clean_numbers,
                main_number_set=clean_main,
                details=f"Pre-flight validation passed for {len(clean_numbers)} DID(s). Ready for live activation.",
            )

        # 3. Live DID Addition
        self.webex.add_location_numbers(location_id, clean_numbers, state="ACTIVE")

        if clean_main:
            self.webex.set_location_main_number(location_id, clean_main)

        return LocationDidProvisionResponse(
            status="SUCCESS",
            dry_run=False,
            location_id=location_id,
            location_name=loc_name,
            phone_numbers_added=clean_numbers,
            main_number_set=clean_main,
            details=f"Successfully provisioned {len(clean_numbers)} DID(s) to location '{loc_name}'.",
        )
