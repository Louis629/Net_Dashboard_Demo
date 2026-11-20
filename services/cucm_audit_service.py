# services/cucm_audit_service.py
"""
Audit and Extraction Service for CUCM 12.5(1) SU1 Users, Lines, and Devices.
Classifies phone models into Webex Calling Migration Readiness Tiers.
Filters physical endpoints (SEP*) vs. softphones (CSF/TCT/BOT) and resolves E.164 DIDs.
"""

import logging
import re
from collections import Counter
from typing import Dict, List, Optional, Any, Tuple
from pydantic import BaseModel

from providers.cucm_axl_provider import CUCMAXLProvider

logger = logging.getLogger("CUCM_Audit_Service")

# Supported MPP Conversion Families (Cisco Multiplatform Firmware)
TIER_1_MPP_MODELS = {
    "Cisco 8811", "Cisco 8841", "Cisco 8845", "Cisco 8851", "Cisco 8861", "Cisco 8865",
    "Cisco 7811", "Cisco 7821", "Cisco 7841", "Cisco 7861", "Cisco 7832", "Cisco 8832",
    "Cisco 6821", "Cisco 6841", "Cisco 6851", "Cisco 6861", "Cisco 6871",
    "Cisco Desk Phone 9841", "Cisco Desk Phone 9851", "Cisco Desk Phone 9861", "Cisco 9871"
}

TIER_2_GATEWAY_MODELS = {
    "Cisco ATA 190", "Cisco ATA 191", "Cisco ATA 192", "Cisco ATA 187",
    "Cisco VG202", "Cisco VG204", "Cisco VG310", "Cisco VG320", 
    "Cisco VG350", "Cisco VG400", "Cisco VG410", "Cisco VG420", "Cisco VG450"
}

SOFTPHONE_NAME_RE = re.compile(r"^(?:CSF|TCT|BOT|TAB)[A-Z0-9._-]+$", re.IGNORECASE)
SOFTPHONE_MODEL_RE = re.compile(
    r"(?:Unified Client Services Framework|Client Services Framework|Dual Mode for|Jabber|Cisco Webex|Webex Calling|Webex App|Softphone)",
    re.IGNORECASE,
)
HARDWARE_COLLAB_RE = re.compile(r"\b(?:Desk|Room|Board)\b", re.IGNORECASE)


def is_softphone_device(device_name: str = "", model_name: str = "", description: str = "") -> bool:
    """Returns True when a device name or model represents a CUCM/Webex softphone client."""
    dev_name = (device_name or "").strip().upper()
    model = (model_name or "").strip()
    desc = (description or "").strip().upper()

    if dev_name and SOFTPHONE_NAME_RE.match(dev_name):
        return True
    if "JABBER" in dev_name or "JABBER" in desc:
        return True
    if model and SOFTPHONE_MODEL_RE.search(model) and not HARDWARE_COLLAB_RE.search(model):
        return True
    return False


# -----------------------------------------------------------------------------
# Pydantic Output Models
# -----------------------------------------------------------------------------

class LineAuditRecord(BaseModel):
    dn: str
    partition: str
    external_mask: Optional[str] = "N/A"
    display_name: Optional[str] = "N/A"
    line_label: Optional[str] = "N/A"
    is_e164_compliant: bool
    e164_suggested: str


class DeviceAuditRecord(BaseModel):
    device_name: str
    device_model: str
    description: str
    device_pool: Optional[str] = "Default"
    owner_userid: str
    owner_email: str
    migration_tier: str  # TIER_1_MPP_DIRECT, TIER_2_GATEWAY_SOFTPHONE, TIER_3_INCOMPATIBLE_REFRESH
    lines: List[LineAuditRecord] = []


class MigrationAuditSummary(BaseModel):
    total_devices: int
    tier_1_mpp_direct_count: int
    tier_2_gateway_count: int
    tier_2_gateway_softphone_count: Optional[int] = 0
    tier_3_incompatible_count: int
    total_unique_users: int
    total_lines: int
    e164_compliant_lines_count: int
    e164_non_compliant_lines_count: int
    device_scope: Optional[str] = "PHYSICAL_ONLY"
    device_pool_filter: Optional[str] = "ALL"
    model_breakdown: Dict[str, int]
    non_compliant_lines_sample: List[LineAuditRecord]
    devices: List[DeviceAuditRecord]


# -----------------------------------------------------------------------------
# Audit Service Implementation
# -----------------------------------------------------------------------------

class CUCMAuditService:
    def __init__(self, axl_provider: CUCMAXLProvider):
        self.axl = axl_provider

    def build_audit_sql(
        self,
        device_scope: str = "PHYSICAL_ONLY",
        device_pool_filter: Optional[str] = None
    ) -> str:
        """
        Constructs Informix SQL query tailored for CUCM 12.5(1) SU1 schema.
        Uses typemodel tm ON d.tkmodel = tm.enum.
        """
        sql = """
        SELECT 
            d.name AS device_name,
            d.description AS device_description,
            tm.name AS device_model,
            dp.name AS device_pool,
            eu.userid AS owner_userid,
            eu.mailid AS owner_email,
            np.dnorpattern AS line_dn,
            rp.name AS partition_name,
            dnp.display AS line_display,
            dnp.label AS line_label,
            dnp.e164mask AS external_mask
        FROM device d
        INNER JOIN typemodel tm ON d.tkmodel = tm.enum
        INNER JOIN devicepool dp ON d.fkdevicepool = dp.pkid
        LEFT JOIN devicenumplanmap dnp ON dnp.fkdevice = d.pkid AND dnp.numplanindex = 1
        LEFT JOIN numplan np ON dnp.fknumplan = np.pkid
        LEFT JOIN routepartition rp ON np.fkroutepartition = rp.pkid
        LEFT JOIN enduser eu ON d.fkenduser = eu.pkid
        WHERE d.tkclass = 1
        """

        if device_scope == "PHYSICAL_ONLY":
            sql += """
            AND d.name LIKE 'SEP%'
            AND tm.name NOT LIKE '%Client Services Framework%'
            AND tm.name NOT LIKE '%Dual Mode%'
            AND tm.name NOT LIKE '%Jabber%'
            AND tm.name NOT LIKE '%Webex%'
            AND tm.name NOT LIKE '%CTI%'
            """
        elif device_scope == "SOFTPHONES_ONLY":
            sql += """
            AND (
                d.name LIKE 'CSF%' OR 
                d.name LIKE 'TCT%' OR 
                d.name LIKE 'BOT%' OR 
                d.name LIKE 'TAB%' OR
                tm.name LIKE '%Client Services Framework%' OR 
                tm.name LIKE '%Dual Mode%' OR
                tm.name LIKE '%Jabber%' OR
                tm.name LIKE '%Webex%'
            )
            """
        elif device_scope == "ANALOG_GATEWAYS":
            sql += """
            AND (
                d.name LIKE 'ATA%' OR 
                d.name LIKE 'VG%' OR 
                tm.name LIKE '%ATA%' OR 
                tm.name LIKE '%VG%'
            )
            """

        if device_pool_filter and device_pool_filter.strip():
            cleaned_pool = device_pool_filter.strip().replace("'", "''")
            sql += f" AND dp.name = '{cleaned_pool}'"

        sql += " ORDER BY dp.name, d.name"
        return sql

    def run_full_migration_audit(
        self,
        default_country_code: str = "+1",
        device_scope: str = "PHYSICAL_ONLY",
        device_pool_filter: Optional[str] = None
    ) -> MigrationAuditSummary:
        """
        Executes a correlated database audit across CUCM Users, Devices, and Lines.
        """
        query = self.build_audit_sql(device_scope=device_scope, device_pool_filter=device_pool_filter)
        logger.info(f"Starting CUCM AXL Migration Audit [Scope: {device_scope}, Pool: {device_pool_filter or 'ALL'}]...")

        raw_rows = self.axl.fetch_paginated_sql(query, chunk_size=1000)
        
        device_map: Dict[str, DeviceAuditRecord] = {}
        unique_users = set()
        model_counter = Counter()
        non_compliant_lines: List[LineAuditRecord] = []
        total_lines_count = 0
        e164_compliant_count = 0

        for row in raw_rows:
            dev_name = row.get("device_name", "").strip().upper()
            dev_model = row.get("device_model", "Unknown").strip()
            dev_desc = row.get("device_description", "").strip()
            dev_pool = row.get("device_pool", "Default").strip()
            owner_id = row.get("owner_userid", "Unassigned").strip()
            owner_mail = row.get("owner_email", "").strip()

            if owner_id and owner_id != "Unassigned":
                unique_users.add(owner_id)

            if dev_name not in device_map:
                model_counter[dev_model] += 1
                tier = self._classify_device_tier(
                    model_name=dev_model,
                    device_name=dev_name,
                    description=dev_desc
                )
                device_map[dev_name] = DeviceAuditRecord(
                    device_name=dev_name,
                    device_model=dev_model,
                    description=dev_desc,
                    device_pool=dev_pool or "Default",
                    owner_userid=owner_id or "Unassigned",
                    owner_email=owner_mail or "N/A",
                    migration_tier=tier,
                    lines=[]
                )

            dn = row.get("line_dn", "").strip()
            if dn:
                total_lines_count += 1
                partition = row.get("partition_name", "Global").strip()
                display = row.get("line_display", "").strip()
                label = row.get("line_label", "").strip()
                e164_mask = row.get("external_mask", "").strip()

                is_e164, e164_formatted = self._audit_e164_compliance(
                    dn=dn, 
                    mask=e164_mask, 
                    country_code=default_country_code
                )
                
                line_record = LineAuditRecord(
                    dn=dn,
                    partition=partition,
                    external_mask=e164_mask or "N/A",
                    display_name=display or "N/A",
                    line_label=label or "N/A",
                    is_e164_compliant=is_e164,
                    e164_suggested=e164_formatted
                )

                if is_e164:
                    e164_compliant_count += 1
                else:
                    non_compliant_lines.append(line_record)

                device_map[dev_name].lines.append(line_record)

        devices_list = list(device_map.values())
        
        tier_1 = sum(1 for d in devices_list if d.migration_tier == "TIER_1_MPP_DIRECT")
        tier_2 = sum(1 for d in devices_list if d.migration_tier == "TIER_2_GATEWAY_SOFTPHONE")
        tier_3 = sum(1 for d in devices_list if d.migration_tier == "TIER_3_INCOMPATIBLE_REFRESH")

        return MigrationAuditSummary(
            total_devices=len(devices_list),
            tier_1_mpp_direct_count=tier_1,
            tier_2_gateway_count=tier_2,
            tier_2_gateway_softphone_count=tier_2,
            tier_3_incompatible_count=tier_3,
            total_unique_users=len(unique_users),
            total_lines=total_lines_count,
            e164_compliant_lines_count=e164_compliant_count,
            e164_non_compliant_lines_count=total_lines_count - e164_compliant_count,
            device_scope=device_scope,
            device_pool_filter=device_pool_filter or "ALL",
            model_breakdown=dict(model_counter.most_common()),
            non_compliant_lines_sample=non_compliant_lines[:20],
            devices=devices_list
        )

    @staticmethod
    def _classify_device_tier(model_name: str, device_name: str = "", description: str = "") -> str:
        """
        Classifies endpoints into Webex Calling migration readiness tiers.
        """
        model = model_name.strip()
        dev_name = device_name.strip().upper()
        desc = description.strip().upper()

        if is_softphone_device(device_name=dev_name, model_name=model, description=desc) or "WEBEX" in desc:
            return "TIER_2_GATEWAY_SOFTPHONE"

        if model in TIER_1_MPP_MODELS or re.search(r"\b(Cisco|IP Phone)?\s*(7811|7821|7841|7861|7832|8811|8841|8845|8851|8861|8865|8832|8875|Desk 9841|Desk 9851|Desk 9861|9871)\b", model, re.I):
            return "TIER_1_MPP_DIRECT"

        if model in TIER_2_GATEWAY_MODELS or \
           re.search(r"\b(VG202|VG204|VG310|VG320|VG350|VG400|VG410|VG420|VG450|ATA 190|ATA 191|ATA 192|ATA 187)\b", model, re.I):
            return "TIER_2_GATEWAY_SOFTPHONE"

        return "TIER_3_INCOMPATIBLE_REFRESH"

    @staticmethod
    def _audit_e164_compliance(dn: str, mask: str = "", country_code: str = "+1") -> Tuple[bool, str]:
        """
        Audits directory pattern against E.164 requirements.
        """
        digits_dn = re.sub(r"\D", "", dn)

        if dn.startswith("+") and len(digits_dn) >= 11:
            return True, dn

        if mask:
            clean_mask = mask.strip().upper()
            mask_digits = re.sub(r"\D", "", clean_mask)

            if "X" not in clean_mask and len(mask_digits) >= 10:
                if clean_mask.startswith("+"):
                    return True, clean_mask
                elif len(mask_digits) == 10:
                    return True, f"{country_code}{mask_digits}"
                elif len(mask_digits) == 11 and mask_digits.startswith("1"):
                    return True, f"+{mask_digits}"

            if "X" in clean_mask:
                station_digits = digits_dn[2:] if len(digits_dn) == 6 else digits_dn
                x_count = clean_mask.count("X")

                if len(station_digits) >= x_count:
                    tail_digits = station_digits[-x_count:]
                    formatted = ""
                    digit_idx = 0
                    for char in clean_mask:
                        if char == "X":
                            formatted += tail_digits[digit_idx]
                            digit_idx += 1
                        else:
                            formatted += char

                    if not formatted.startswith("+"):
                        fmt_digits = re.sub(r"\D", "", formatted)
                        if len(fmt_digits) == 10:
                            formatted = f"{country_code}{fmt_digits}"
                        elif len(fmt_digits) == 11 and fmt_digits.startswith("1"):
                            formatted = f"+{fmt_digits}"

                    if formatted.startswith("+") and len(re.sub(r"\D", "", formatted)) >= 11:
                        return True, formatted

        if len(digits_dn) == 10:
            return True, f"{country_code}{digits_dn}"

        if len(digits_dn) == 11 and digits_dn.startswith("1"):
            return True, f"+{digits_dn}"

        return False, f"EXT_{dn}"