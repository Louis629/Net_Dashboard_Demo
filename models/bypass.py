"""
models/bypass.py

Pydantic validation models for 802.1X imaging bypass and device reimaging.
"""

import re
from typing import Optional, List
from pydantic import BaseModel, field_validator, model_validator
from models.common import normalize_mac, HOSTNAME_PATTERN


class BypassRequest(BaseModel):
    mac_address: Optional[str] = None
    device_name: Optional[str] = None
    device_names: Optional[List[str]] = None
    operator_id: str
    device_type: str = "new"  # "new" or "reimage"
    remove_sccm: bool = True
    remove_ad: bool = True
    remove_ise: bool = True
    stage_in_ise: bool = False
    step_up_user: Optional[str] = None
    step_up_pass: Optional[str] = None

    @field_validator("mac_address")
    @classmethod
    def validate_mac(cls, value: Optional[str]) -> Optional[str]:
        if not value or not value.strip():
            return None
        return normalize_mac(value)

    @field_validator("device_name")
    @classmethod
    def validate_device_name(cls, value: Optional[str]) -> Optional[str]:
        if not value or not value.strip():
            return None
        cleaned = value.strip()
        tokens = [t.strip() for t in re.split(r"[\r\n,]+", cleaned) if t.strip()]
        if len(tokens) > 1:
            for token in tokens:
                if not HOSTNAME_PATTERN.match(token):
                    raise ValueError(f"Invalid computer name format: '{token}'. Alphanumeric, dash, dot, and underscore only.")
            return cleaned
        if not HOSTNAME_PATTERN.match(cleaned):
            raise ValueError("Invalid computer name format. Alphanumeric, dash, dot, and underscore only.")
        return cleaned

    @field_validator("device_names")
    @classmethod
    def validate_device_names(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        if not value:
            return None
        cleaned_list = []
        for item in value:
            if not item or not item.strip():
                continue
            cleaned = item.strip()
            if not HOSTNAME_PATTERN.match(cleaned):
                raise ValueError(f"Invalid computer name format: '{cleaned}'. Alphanumeric, dash, dot, and underscore only.")
            cleaned_list.append(cleaned)
        return cleaned_list or None

    @field_validator("operator_id")
    @classmethod
    def validate_operator_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or len(cleaned) > 255:
            raise ValueError("Invalid operator_id length")
        return cleaned

    @field_validator("device_type")
    @classmethod
    def validate_device_type(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if cleaned not in {"new", "reimage"}:
            raise ValueError("device_type must be either 'new' or 'reimage'")
        return cleaned

    @model_validator(mode="after")
    def validate_workflow_requirements(self):
        if self.device_type == "new":
            if not self.mac_address:
                raise ValueError("MAC address is required for new device 802.1X bypass staging.")
        elif self.device_type == "reimage":
            # Extract and normalize device list
            extracted = []
            if self.device_names:
                extracted.extend(self.device_names)
            elif self.device_name:
                tokens = [t.strip() for t in re.split(r"[\r\n,]+", self.device_name) if t.strip()]
                extracted.extend(tokens)

            if extracted:
                self.device_names = extracted
                if len(extracted) == 1 and not self.device_name:
                    self.device_name = extracted[0]

            if not self.device_name and not self.device_names and not self.mac_address:
                raise ValueError("At least one identifier (Computer Name or MAC Address) is required for device reimaging.")
        return self
