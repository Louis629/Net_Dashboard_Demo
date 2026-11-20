"""
services/uccx_migration_service.py

Migration Service for UCCX 12.5 SU3 to Webex Contact Center (WxCC).
Parses CSQs, Skills, Agents, Applications, and CTI Route Points and
generates standardized WxCC Bulk Import JSON payloads.
"""

import logging
from typing import Dict, List, Optional, Any
from pydantic import BaseModel

from providers.uccx_provider import UCCXProvider

logger = logging.getLogger("UCCX_Migration_Service")


# -----------------------------------------------------------------------------
# Pydantic Output Models
# -----------------------------------------------------------------------------

class WxCCSkillMapping(BaseModel):
    skill_name: str
    wxcc_skill_type: str = "TEXT"  # Enum, Text, Boolean, Proficiency
    competence_weight: str


class WxCCQueueMapping(BaseModel):
    csq_name: str
    queue_type: str
    routing_type: str  # Skilled-Based, Longest Available
    skills_required: List[WxCCSkillMapping] = []


class WxCCEntryPointMapping(BaseModel):
    application_name: str
    script_name: str
    cti_trigger_dn: str
    e164_dial_number: str


class UCCXMigrationSummary(BaseModel):
    total_csqs: int
    total_skills: int
    total_agents: int
    total_applications: int
    queues: List[WxCCQueueMapping]
    entry_points: List[WxCCEntryPointMapping]


# -----------------------------------------------------------------------------
# Service Implementation
# -----------------------------------------------------------------------------

class UCCXMigrationService:
    def __init__(self, uccx_provider: UCCXProvider):
        self.uccx = uccx_provider

    def run_full_contact_center_audit(self, default_country_code: str = "+1") -> UCCXMigrationSummary:
        """
        Extracts UCCX configuration objects and maps them to WxCC structures.
        """
        logger.info("Starting UCCX Contact Center Audit & WxCC Payload Generation...")

        # 1. Fetch CSQs & Deep Detail
        raw_csqs = self.uccx.get_csqs()
        queue_mappings: List[WxCCQueueMapping] = []

        for csq in raw_csqs:
            csq_id = csq.get("id") or csq.get("name")
            csq_name = csq.get("name", "Unknown")
            
            # Request detail by CSQ numerical ID if available, otherwise fallback to name
            detail = self.uccx.get_csq_detail(csq_id)

            skills = [
                WxCCSkillMapping(
                    skill_name=s["skill_name"],
                    wxcc_skill_type="Proficiency",
                    competence_weight=s["weight"]
                )
                for s in detail.get("skills", [])
            ]

            queue_mappings.append(WxCCQueueMapping(
                csq_name=detail.get("name") or csq_name,
                queue_type=detail.get("type", "VOICE"),
                routing_type=detail.get("csq_selection_criteria", "LONGEST_AVAILABLE"),
                skills_required=skills
            ))

        # 2. Fetch Skills & Agents
        raw_skills = self.uccx.get_skills()
        raw_resources = self.uccx.get_resources()

        # 3. Fetch Applications & CTI Triggers
        raw_apps = self.uccx.get_applications()
        entry_point_mappings: List[WxCCEntryPointMapping] = []

        for app in raw_apps:
            app_name = app["name"]
            script_name = app["script_name"]
            
            for trig in app.get("triggers", []):
                dn = trig.get("directory_number", "")
                
                # Format CTI Route Point to E.164
                e164_dn = f"{default_country_code}{dn}" if len(dn) == 10 else f"+{dn}" if dn.startswith("1") else dn

                entry_point_mappings.append(WxCCEntryPointMapping(
                    application_name=app_name,
                    script_name=script_name,
                    cti_trigger_dn=dn,
                    e164_dial_number=e164_dn
                ))

        return UCCXMigrationSummary(
            total_csqs=len(queue_mappings),
            total_skills=len(raw_skills),
            total_agents=len(raw_resources),
            total_applications=len(raw_apps),
            queues=queue_mappings,
            entry_points=entry_point_mappings
        )