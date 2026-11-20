# Telecom & Webex Migration Architecture Rules

## Subsystem Architecture & Responsibilities
- `providers/`: Raw API/SOAP/REST communications only.
  - `cucm_axl_provider.py`: CUCM AXL SQL queries (`executeSQLQuery`), pagination (`fetch_paginated_sql`), SOAP envelope construction.
  - `uccx_provider.py`: UCCX REST Admin API (`/adminapi/`). Layer 4 socket reachability checks, CSQ/Skill/Application XML extraction.
  - `cupi_provider.py`: Cisco Unity Connection REST (`/vmrest`). Call handlers, greetings, audio stream downloads.
  - `webex_calling_provider.py`: Webex Control Hub v1 REST APIs. Rate-limiting (429 Retry-After), Link header pagination, token override.
  - `telecom_factory.py`: Multi-cluster resolution (`PRIMARY` vs `HAWAII`). Do not hardcode cluster endpoints.
  - `meraki_provider.py` *(External Read-Only Dependency)*: Injected strictly for reading MX/Z4 appliance location inventory (`get_appliance_location_inventory()`). Treat as an immutable external interface.

## Domain Invariants & Boundaries
- **Anti-Corruption Layer (ACL)**: When ingesting site data from `meraki_provider`, immediately map raw dictionaries into Telecom-owned Pydantic models (e.g., `StagedLocationRecord`) inside the Telecom service layer (`services/webex_workspace_location_service.py`).
- **Hardware Tiering**:
  - **Tier 1 (MPP Direct)**: Cisco 78xx, 88xx, 98xx desk phones.
  - **Tier 2 (Gateways/Softphones)**: Cisco VG gateways, ATA 19x, CSF/Jabber.
  - **Tier 3 (Incompatible / Refresh)**: Legacy Cisco 79xx, 69xx hardware.
- **E.164 Normalization**: Always enforce country code prefix (`+1`) and validate directory numbers against external phone number masks.
- **UCCX Script Parsing**: Native `.aef` execution graphs are not decompiled directly. Parse structured JSON/XML exports into WxCC Flow schemas (`services/wxcc_flow_generator_service.py`).
- **Audio Prompt Compliance**: Must strictly enforce WxCC limits: CCITT u-law (Format 7) or PCM (Format 1), 8000 Hz, Mono, <= 5 MB.

## Pre-Flight Code Generation Protocol
Before generating or modifying code in the Telecom stack:
1. **Target File Declaration**: State the exact file being modified.
2. **Dependency & Import Check**: Verify that imports stay within `telecom` domains (with the sole exception of read-only `MerakiProvider` injection).
3. **Dependency Check**: If new third-party libraries are required, declare them and update `requirements.txt` with exact version pins (`==x.y.z`).
4. **Cluster Compatibility**: Ensure logic supports both `PRIMARY` (CUCM 12.5 / UCCX 12.5 / CUC 14) and `HAWAII` (CUCM 11.5 / UCCX 12.0 / CUC 11.5).