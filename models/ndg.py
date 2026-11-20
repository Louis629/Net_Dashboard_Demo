"""
models/ndg.py

Pydantic validation models for Cisco ISE Network Device Group (NDG) compliance audits,
live synchronization, zero-touch provisioning (ZTP), and hardware appliance swaps.
"""

from typing import List, Optional
from pydantic import BaseModel


class TargetedAuditRequest(BaseModel):
    template_id: str
    hardware_filter: int
    gateway_type: str
    gateway_designation: str
    deployment_stage: str = "Limited Access"
    client_groups: Optional[List[str]] = []


class TargetedSyncRequest(TargetedAuditRequest):
    step_up_user: str
    step_up_pass: str


class ZtpProvisionRequest(BaseModel):
    serial_number: str
    network_name: str
    template_id: str
    gateway_type: str
    gateway_designation: str
    deployment_stage: str = "Limited Access"
    physical_address: str
    step_up_user: str
    step_up_pass: str


class ApplianceSwapRequest(BaseModel):
    network_id: str
    old_serial: str
    new_serial: str
    step_up_user: Optional[str] = None
    step_up_pass: Optional[str] = None
