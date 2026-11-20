"""
models/common.py

Shared validation regexes, normalization routines, and base patterns.
"""

import re

MAC_PATTERN = re.compile(r"^(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$|^[0-9A-Fa-f]{12}$")
HOSTNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
DESTINATION_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+$")


def normalize_mac(mac_address: str) -> str:
    """Normalizes MAC addresses to standard uppercase colon-delimited format (AA:BB:CC:DD:EE:FF)."""
    if not mac_address or not isinstance(mac_address, str):
        raise ValueError("MAC address is required.")
    candidate = mac_address.strip().replace("-", ":").upper()
    if re.fullmatch(r"[0-9A-F]{12}", candidate):
        candidate = ":".join(candidate[i : i + 2] for i in range(0, 12, 2))
    if not MAC_PATTERN.match(candidate):
        raise ValueError("Invalid MAC address format. Use AA:BB:CC:DD:EE:FF format.")
    return candidate
