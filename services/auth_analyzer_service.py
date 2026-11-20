"""
services/auth_analyzer_service.py
Parses Cisco ISE RADIUS failure reason codes, translates them into human-readable 
Helpdesk remediation checklists, and generates Markdown summaries for ServiceNow.
"""

import logging
import asyncio
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class RemediationStep(BaseModel):
    step_number: int
    title: str
    action: str
    automated_tool_link: Optional[str] = None


class AnalysisResult(BaseModel):
    mac_address: str
    ise_code: int
    severity: str
    raw_reason: str
    likely_cause: str
    helpdesk_steps: List[RemediationStep]
    kb_reference: str
    servicenow_worknote: str


class AuthAnalyzerService:
    """
    Engine mapping Cisco ISE MnT reason codes to actionable Helpdesk workflows.
    """

    # Knowledge Base Mapping Table for Cisco ISE Failure Codes
    ISE_CODE_MAP: Dict[int, Dict[str, Any]] = {
        12514: {
            "cause": "EAP-TLS/TEAP Handshake Failed — Client certificate is expired, missing, or untrusted.",
            "severity": "HIGH",
            "kb": "KB00108 - 802.1X Certificate & EAP-TLS Troubleshooting Guide",
            "steps": [
                "Verify if the client computer certificate is present in the Windows Certificate Store (certlm.msc).",
                "Check certificate expiration date under Personal -> Certificates.",
                "Run 'gpupdate /force' on the target machine via WinRM or CMD to trigger auto-enrollment.",
                "Restart the 'Wired AutoConfig' (dot3svc) service on the client host.",
                "Re-test 802.1X authentication."
            ]
        },
        22056: {
            "cause": "Subject Not Found in Identity Store — User or Machine account does not exist in Active Directory or ISE Allowed Groups.",
            "severity": "MEDIUM",
            "kb": "KB00108 - 802.1X Active Directory Identity Troubleshooting",
            "steps": [
                "Verify host/user account exists and is ENABLED in Active Directory.",
                "Check if the computer account was recently re-imaged or renamed without domain rejoin.",
                "Verify machine object resides within an OU evaluated by ISE Active Directory Identity Stores.",
                "Perform domain rejoin if trust relationship is broken."
            ]
        },
        5440: {
            "cause": "Endpoint Abandoned EAP Session — Supplicant timed out or stopped responding during 802.1X negotiation.",
            "severity": "MEDIUM",
            "kb": "KB00104 - Network Connectivity & Supplicant Self-Service Checklist",
            "steps": [
                "Check physical network cable or switch port patch lead.",
                "Verify wireless/wired network adapter drivers are up to date.",
                "Ensure Windows 802.1X Authentication properties have 'Enable IEEE 802.1X authentication' checked.",
                "Execute 'Windows Deep Dive' log extraction to check client-side EAP-TEAP event logs."
            ]
        },
        5400: {
            "cause": "Authentication Failed — Incorrect user credentials or inner EAP method mismatched.",
            "severity": "LOW",
            "kb": "KB00108 - 802.1X Credential & Supplicant Guide",
            "steps": [
                "Verify user password is not expired or locked out in Active Directory.",
                "Ensure user is entering valid corporate credentials.",
                "If using EAP-TEAP, ensure inner auth protocol is set to MS-CHAPv2."
            ]
        },
        15039: {
            "cause": "Selected Authorization Profile Rejected — Endpoint authenticated but matched a Deny/Quarantine Policy Rule.",
            "severity": "HIGH",
            "kb": "KB00108 - 802.1X Authorization Policy Rules",
            "steps": [
                "Check if device belongs to an isolated or restricted ISE Endpoint Identity Group.",
                "Verify posture assessment / antivirus compliance on client endpoint.",
                "If device is an IP Phone or Printer, ensure MAB bypass group is applied."
            ]
        }
    }

    def __init__(self, ise_provider=None):
        self.ise_provider = ise_provider

    async def analyze_endpoint_failure(self, mac_address: str, ise_code: int, raw_reason: str = "") -> AnalysisResult:
        """
        Processes a raw ISE code and produces a structured Helpdesk diagnosis.
        """
        logger.info(f"Analyzing ISE RADIUS Code [{ise_code}] for MAC [{mac_address}]")
        
        # Fallback for unknown ISE codes
        rule = self.ISE_CODE_MAP.get(ise_code, {
            "cause": f"Unclassified ISE RADIUS Code ({ise_code}). Requires deeper inspection.",
            "severity": "MEDIUM",
            "kb": "KB00108 - General 802.1X Infrastructure Guide",
            "steps": [
                "Review raw ISE Monitoring logs for detailed RADIUS attributes.",
                "Verify physical port link state and switch interface VLAN.",
                "Escalate to Network Engineering if authentication fails repeatedly."
            ]
        })

        # Build structured remediation objects
        remediation_steps = [
            RemediationStep(
                step_number=idx + 1,
                title=f"Action Item {idx + 1}",
                action=step_text,
                automated_tool_link="/api/v1/windows-diag" if "Deep Dive" in step_text else None
            )
            for idx, step_text in enumerate(rule["steps"])
        ]

        # Generate formatted ServiceNow Work Note
        work_note = self._build_servicenow_worknote(
            mac_address=mac_address,
            ise_code=ise_code,
            cause=rule["cause"],
            steps=rule["steps"],
            kb=rule["kb"]
        )

        return AnalysisResult(
            mac_address=mac_address,
            ise_code=ise_code,
            severity=rule["severity"],
            raw_reason=raw_reason or f"ISE Reason Code {ise_code}",
            likely_cause=rule["cause"],
            helpdesk_steps=remediation_steps,
            kb_reference=rule["kb"],
            servicenow_worknote=work_note
        )

    @staticmethod
    def _build_servicenow_worknote(mac_address: str, ise_code: int, cause: str, steps: List[str], kb: str) -> str:
        """
        Formats a clean, standardized Markdown snippet for ServiceNow incident work notes.
        """
        formatted_steps = "\n".join([f"{idx+1}. {step}" for idx, step in enumerate(steps)])
        
        return f"""
### 🛡️ NetDashboard Auto-Triage: 802.1X Failure Diagnosis
* **Target MAC:** `{mac_address}`
* **ISE Reason Code:** `{ise_code}`
* **Reference Document:** `{kb}`

#### 💡 Likely Root Cause
> {cause}

#### 🛠️ Recommended Helpdesk Actions
{formatted_steps}

---
*Generated automatically by NetDashboard Identity Engine.*
""".strip()