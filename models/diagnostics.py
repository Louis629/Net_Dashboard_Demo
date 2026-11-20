"""
models/diagnostics.py

Pydantic models for client endpoint diagnostics, telemetry, performance tests, and cache pre-warming.
"""

from pydantic import BaseModel, field_validator
from models.common import HOSTNAME_PATTERN


class WindowsDiagRequest(BaseModel):
    hostname: str

    @field_validator("hostname")
    @classmethod
    def validate_hostname(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or len(cleaned) > 255 or not HOSTNAME_PATTERN.match(cleaned):
            raise ValueError("Invalid hostname format")
        return cleaned


class IPerfRequest(BaseModel):
    hostname: str


class NetTestRequest(BaseModel):
    hostname: str
    destination: str


class IdentityTrackerRequest(BaseModel):
    hostname: str


class CacheWarmRequest(BaseModel):
    include_devices: bool = True
    include_networks: bool = True
