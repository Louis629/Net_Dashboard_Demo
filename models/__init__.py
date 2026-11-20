"""
models package: Domain Pydantic validation schemas.
"""

from models.common import MAC_PATTERN, HOSTNAME_PATTERN, DESTINATION_PATTERN, normalize_mac
from models.bypass import BypassRequest
from models.diagnostics import (
    WindowsDiagRequest,
    IPerfRequest,
    NetTestRequest,
    IdentityTrackerRequest,
    CacheWarmRequest,
)
from models.ndg import (
    TargetedAuditRequest,
    TargetedSyncRequest,
    ZtpProvisionRequest,
    ApplianceSwapRequest,
)

__all__ = [
    "MAC_PATTERN",
    "HOSTNAME_PATTERN",
    "DESTINATION_PATTERN",
    "normalize_mac",
    "BypassRequest",
    "WindowsDiagRequest",
    "IPerfRequest",
    "NetTestRequest",
    "IdentityTrackerRequest",
    "CacheWarmRequest",
    "TargetedAuditRequest",
    "TargetedSyncRequest",
    "ZtpProvisionRequest",
    "ApplianceSwapRequest",
]
