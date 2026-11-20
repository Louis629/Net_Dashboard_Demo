# NetDashboard Workspace Guardrails

## Core Execution Invariants
1. **Zero Unrelated Cross-Contamination**: Never modify, refactor, or import from modules outside the active domain unless covered by an explicit exception.
2. **Domain Isolation**:
   - When working on **Telecom Migration**:
     - **DO NOT MODIFY** ISE modules (`providers/ise*`, `services/ndg*`, `routers/ndg*`, `routers/bypass*`).
     - **DO NOT MODIFY** Meraki business logic (`services/appliance_swap*`, `services/ndg*`, `routers/meraki*`).
     - **DO NOT MODIFY** SCCM, Active Directory tools, or PowerShell scripts (`providers/sccm*`, `scripts/*.ps1`).
     - **DO NOT MODIFY** `security/**` (`auth.py`, `roles.py`, `permissions.py`) without explicit instruction.
     - **ALLOWED EXCEPTION (Meraki Read-Only)**: Telecom services MAY consume read-only telemetry from `providers/meraki_provider.py` (specifically `get_appliance_location_inventory()`, `get_organization_networks()`, and `get_organization_devices()`) to discover site addresses. Calling Meraki write methods or modifying `meraki_provider.py` is strictly forbidden.
   - When working on **Network Infrastructure / Diagnostics**:
     - **DO NOT MODIFY** any CUCM, UCCX, CUC, or Webex modules.
3. **Protected Assets (Strictly Read-Only)**:
   - Never edit, convert, overwrite, or delete binary files in `media/uccx_prompts/`.
   - Never edit vendor templates in `imports/migration_csv/templates/`.
   - Never alter diagnostic rules in `playbooks/`.
4. **Dependency Management (`requirements.txt`)**:
   - If a new feature, integration, or bug fix requires an external third-party package, **immediately append it to `requirements.txt`**.
   - Always pin new dependencies to an exact version (`package==x.y.z`).
   - Preserve all existing library pins and avoid unnecessary version bumps.
5. **Preserve Celery Task Signatures**: In `celery_app.py`, NEVER change existing task names, argument lists, or return schemas. Only append new tasks or update task internal bodies when explicitly requested.
6. **No File Reconstruction Hazards**: Always output complete, production-ready files or unified git diffs. Never use placeholders like `// ... rest of implementation unchanged ...`.