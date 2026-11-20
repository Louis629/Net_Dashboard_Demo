# Net Dashboard - Engineering & Migration Copilot Guidelines

## Repository Overview

`Net_Dashboard` is an enterprise network diagnostics and telecom migration automation portal built with FastAPI, Jinja2, Celery (Redis/Valkey with SQLite fallback), and Pydantic v2.

## 🛑 File Scoping & Active Domain Isolation (CRITICAL)

To prevent code degradation, cross-module regressions, and AI hallucinations, all Copilot interactions must adhere strictly to these scoping boundaries:

### 1. Active Domain Separation

Operate strictly within the domain indicated by the current task:

* **When working on Telecom Migration**:

  * **DO NOT MODIFY** ISE modules (`providers/ise*`, `services/ndg*`, `routers/ndg*`, `routers/bypass*`).

  * **DO NOT MODIFY** Meraki business logic or admin routers (`services/appliance_swap*`, `services/ndg*`, `routers/meraki*`).

  * **DO NOT MODIFY** SCCM providers, Active Directory tools, or PowerShell scripts (`providers/sccm*`, `scripts/*.ps1`).

  * **DO NOT IMPORT** peer domain services (e.g., Telecom services must never import `NDGAuditService` or `ApplianceSwapService`).

  * **PERMITTED CROSS-DOMAIN EXCEPTION (Meraki -> Telecom Read-Only)**:

    * Telecom services (e.g., `services/webex_workspace_location_service.py`) **MAY** consume read-only telemetry from `providers/meraki_provider.py` (specifically `get_appliance_location_inventory()`, `get_organization_networks()`, and `get_organization_devices()`).

    * Telecom code **MUST NOT** invoke Meraki mutation methods (`claimNetworkDevices`, `swapNetworkAppliance`, `removeNetworkDevices`, etc.).

    * Telecom code **MUST NOT** edit `providers/meraki_provider.py` during a Telecom task; treat its public interface as an immutable read-only SDK.

    * Telecom services **MUST** immediately map raw Meraki dictionary outputs into Telecom-specific Pydantic schemas (e.g., `StagedLocationRecord`) at the service boundary.

* **When working on Network Infrastructure & Diagnostics**:

  * **DO NOT MODIFY** any Telecom, CUCM, UCCX, CUC, or Webex services, providers, or templates.

### 2. Protected Paths & Dependency Management

* **Strictly Read-Only (Never Edit, Delete, Truncate, or Overwrite)**:

  * `imports/migration_csv/templates/*` (Standard vendor CSV templates)

  * `media/**` (Binary `.wav` audio prompt files)

  * `playbooks/**` (Diagnostic JSON rules)

* **Dependency Management (`requirements.txt`)**:

  * **Active Dependency Updates Required**: Whenever adding, updating, or troubleshooting features that introduce new third-party Python packages, **you must update `requirements.txt`**.

  * Always append new dependencies with exact, pinned versions (e.g., `package==x.y.z`) matching the project runtime environment.

  * **Do not** arbitrarily modify, upgrade, or remove unrelated existing libraries in `requirements.txt`.

* **High-Risk Shared Infrastructure (Modify ONLY when registering new routes or tasks)**:

  * `main.py`: Only register new routers. Do not refactor logging, lifespan events, CORS, or middleware.

  * `celery_app.py`: Only append new `@celery_app.task` definitions. Do not alter existing task signatures, argument orders, or broker fallback logic.

  * `security/**`: Never alter roles, permissions, or authentication flows (`security/auth.py`, `security/roles.py`, `security/permissions.py`) without explicit instruction.

### 3. Output Integrity Protocol

* **No Incomplete Diffs or Truncation**: Never emit placeholder comments such as `// ... rest of code unchanged ...` or `# existing methods remain the same`. Always output complete, self-contained functions or intact file blocks.

* **Pre-Flight Scope Declaration**: Before outputting code, state:

  1. The specific target file(s) being modified.

  2. Any newly introduced third-party dependencies added to `requirements.txt`.

  3. The upstream/downstream callers verified against signature changes.

  4. Confirmation that the change honors domain isolation.

## Dual Domain Architecture

The repository is partitioned into two distinct functional domains:

1. **Network Infrastructure & Automation**: Cisco ISE (ERS/MnT APIs), Cisco Meraki Dashboard API, Cisco Catalyst Center (DNAC), Windows Diagnostic WinRM scripts, and Zero-Touch Provisioning (ZTP).

2. **Telecom & Webex Migration**: Multi-cluster Cisco Collaboration migration to Webex Calling and Webex Contact Center (WxCC).

## Telecom & Migration Domain Rules

### Target Cluster Environments

Always differentiate between the two on-premises collaboration environments managed via `providers/telecom_factory.py`:

* **Hawaii Cluster (HIGH PRIORITY)**:

  * CUCM: Version 11.5(1)

  * UCCX: Version 12.0(1)

  * CUC: Version 11.5(1)

  * Environment Prefix: `HAWAII_` (e.g., `HAWAII_CUCM_AXL_HOST`, `HAWAII_UCCX_HOST`, `HAWAII_CUC_HOST`)

* **Primary / Mainland Cluster**:

  * CUCM: Version 12.5(1) SU1

  * UCCX: Version 12.5(1) SU3

  * CUC: Version 14 SU4

  * Environment Prefix: `PRIMARY_` (e.g., `PRIMARY_CUCM_AXL_HOST`, `PRIMARY_UCCX_HOST`, `PRIMARY_CUC_HOST`)

### Target Cloud Ecosystem

* **Webex Calling**: Multi-Tenant (MT) & Dedicated Instance (DI). MPP direct phone onboarding, location-based dial plans, E.164 normalization, and Control Hub CSV provisioning packages.

* **Webex Contact Center (WxCC)**: Flow Builder DAG schemas, Contact Queues, Entry Points, and Media Repository audio prompts.

### Telecom Code & Schema Conventions

* **Multi-Cluster Resolution**: Always resolve cluster providers dynamically via `TelecomClusterRegistry.get_cucm_provider(cluster)`, `TelecomClusterRegistry.get_uccx_provider(cluster)`, or `TelecomClusterRegistry.get_cupi_provider(cluster)`. Never hardcode cluster hostnames or credentials.

* **UCCX Script Parsing**: Native `.aef` execution graphs cannot be decompiled directly. Parse structured JSON/XML script representations into WxCC Flow schemas (`services/wxcc_flow_generator_service.py`).

* **UCCX XML Parsing**: UCCX 12.0 XML responses lack certain 12.5 namespace wrappers. Always use `UCCXProvider._strip_namespaces(root)` when parsing ElementTree responses.

* **WxCC Flow Generation**: `WxCCFlowGeneratorService.generate_uccx_menu_flow()` must produce valid DAG schemas with non-overlapping 2D coordinates (`x`, `y`) for nodes (`NewPhoneContact`, `BusinessHour`, `PlayMedia`, `Menu`, `QueueContact`, `BlindTransfer`, `Disconnect`).

* **Audio Prompt Compliance**: All prompt exports must be validated against WxCC Media Repository limits: CCITT u-law (Format 7) or linear PCM (Format 1), 8000 Hz sample rate, 1 channel (Mono), and <= 5 MB (5,242,880 bytes).

* **CUCM Tier Classification**:

  * **Tier 1 (MPP Direct)**: Cisco 78xx, 88xx, 98xx desk phones.

  * **Tier 2 (Gateway/Softphone)**: VG gateways, ATA 19x, CSF/Jabber.

  * **Tier 3 (Incompatible)**: Legacy hardware requiring refresh (e.g., 79xx, 69xx).

## Network Diagnostics & ISE/Meraki Domain Rules

* **Authentication**: Respect `AUTH_METHOD` (`LOCAL` developer override, `LDAP` in Dev/Sandbox mode, `OKTA` in Production). Dev mode auto-assigns `Role.NETWORK_ADMIN`.

* **ISE ERS API**: When updating Network Devices via PUT, always preserve or inject `radiusSharedSecret` and remove `previousSharedSecret*` metadata fields using `scrub_previous_secret_fields()`.

* **Meraki API**: Use `meraki.DashboardAPI` with `wait_on_rate_limit=True`. Cache heavy lookups (networks, devices, templates) using internal TTL caching.

* **ZTP Provisioning**: Network creation must dynamically match product types from the target Meraki template before binding. Device names must follow `{network_name}_{model}_{last4_sn}` with sanitized alphanumeric characters.

## Strict Python & FastAPI Coding Standards

1. **Thread Safety in Async Endpoints**: Never run blocking synchronous I/O (e.g., `requests.get()`, `UCCXProvider` methods, or `AXL` queries) directly inside `async def` routes. Offload them using `await run_in_threadpool(...)` from `starlette.concurrency`.

2. **Timeout Handling**: Pass dual-timeout tuples `(connect_timeout, read_timeout)` to `requests.Session` calls to prevent remote offline clusters from locking worker threads.

3. **Pydantic v2**: Use `model_dump()` instead of `.dict()`, and `@field_validator` instead of `@validator`.

4. **Celery Task Consistency**: Background tasks in `celery_app.py` must return JSON-serializable dictionaries. Do not break existing task names or argument signatures.

5. **No Hallucinated APIs**: Only use verified Cisco AXL, UCCX Admin REST (`/adminapi`), CUC CUPI (`/vmrest`), and Webex Calling v1 REST endpoints. If uncertain about a schema field, flag it for verification against official documentation.