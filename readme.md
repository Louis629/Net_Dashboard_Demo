# NetDashboard — Enterprise Network Automation & Telecom Migration Portal

**NetDashboard** is a centralized, high-performance network automation engine and collaboration migration portal. Built with **FastAPI**, **Celery (Valkey/Redis)**, **Tailwind CSS**, and **Okta OIDC / LDAPS**, it unifies complex infrastructure operations across enterprise networking and telecommunications domains:

* **Cisco Identity Services Engine (ISE)** — ERS REST API, MnT RADIUS transaction logs, and Network Device Group (NDG) compliance
* **Cisco Catalyst Center (DNA Center)** — Client 360 assurance telemetry, device health scoring, and wireless RF metrics
* **Cisco Meraki Dashboard APIs** — Organizations, networks, templates, MX/Z4 hardware inventory, and Zero-Touch Provisioning (ZTP)
* **Cisco Unified Communications Manager (CUCM 11.5 / 12.5)** — Direct AXL SOAP database extraction, phone inventory, dial-plan audits, hybrid Route Pattern injection, and device decommissioning
* **Cisco Unified Contact Center Express (UCCX 12.0 / 12.5)** — Admin REST API, CRS application script (`.aef`) analysis, and WxCC Flow Studio conversion
* **Cisco Unity Connection (CUC 11.5 / 14)** — CUPI REST API mailboxes, Call Handlers, and audio prompt compliance auditing
* **Cisco Webex Calling (REST API)** — Control Hub calling locations, users, workspaces, tenant calling licenses, extensions, and MPP device provisioning
* **Cisco Webex Contact Center (WxCC)** — Flow Builder JSON schema generation and media repository compliance
* **RedSky Technologies E911** — Emergency location dispatch validation and building/floor routing parity
* **Microsoft Active Directory & DHCP** — Step-up authentication, LDAPS directory binding, dynamic Windows Server DHCP scope generation, and WinRM 802.1X/EAP-TEAP client diagnostics
* **Offline Demo & Evaluation Engine (`DEMO_MODE`)** — Built-in in-memory mock providers (`providers/demo_providers.py`) enabling full-featured local walkthroughs and automated testing without live network appliances, Active Directory, or external brokers

---

## 🎯 Value to Support & Engineering Teams

### 🟢 Tier 1 / Tier 2 Helpdesk Technicians
* **Single-Pane-of-Glass Troubleshooting:** Eliminates swivel-chair navigation across multiple vendor portals. Technicians query a single **Hostname**, **IP Address**, or **MAC Address** to instantly retrieve switch port connection, VLAN, wireless RF health (RSSI, SNR, Channel), and 24-hour Cisco ISE RADIUS pass/fail history.
* **Windows & Network Diagnostic Workflows:** Supports PowerShell and WinRM-assisted collection for 802.1X/EAP-TEAP client diagnostics, network ping/traceroute sweeps, and multi-stream iPerf throughput testing.
* **Zero Infrastructure Exposure:** Tier 1 technicians resolve end-user connectivity issues rapidly without requiring CLI credentials or elevated console access to core network switches or controllers.

### 🔵 Network Administrators & Systems Engineers
* **Meraki Zero-Touch Provisioning (ZTP):** Automates branch site onboarding through a 5-step pipeline: Meraki network creation, corporate template enforcement, dynamic subnet allocation, Windows DHCP scope generation, and Cisco ISE RADIUS client registration.
* **Automated Appliance Swap:** Validates existing appliance assignments, triggers the Meraki appliance replacement workflow, and tracks execution through a background Celery task.
* **ISE - Meraki Compliance Engine:** Continuously audits Meraki networks against Cisco ISE Network Device Groups (NDG) with automated drift detection and one-click live remediation.
* **Imaging 802.1X Bypass & Re-imaging Suite:** Stages fresh workstation deployments in ISE MAB groups with instantaneous RADIUS CoA, or purges existing devices from SCCM and Active Directory while clearing stale ISE identities in preparation for re-imaging.

### 🟣 Telecom & Collaboration Engineers
* **End-to-End Webex Calling Migration Pipeline:** Unified 5-phase migration suite taking sites from on-premise CUCM and Meraki appliances into Webex Calling:
  * **Phase 1: Location Setup & DIDs (Meraki-Powered)** — Automated extraction of physical site addresses from Cisco Meraki MX/Z4 appliances, resilient multi-pass address normalization (handling suites, units, unformatted lines, and E911 dispatch requirements), and PSTN E.164 DID provisioning.
  * **Phase 2 & 3: User Profiles & Device Onboarding** — Staged bulk CSV processing, Webex Calling license allocation (Professional vs. Workspaces), user profile activation, and Cisco Multiplatform Phone (MPP) hardware registration with role preservation safeguards.
  * **Phase 4: CUCM Dial-Plan & Decommissioning** — Hybrid Translation / Route Pattern injection pointing migrated extensions to Local Gateways (CUBE/LGW) to eliminate split-brain blackholes, followed by line disassociation and `SEP<MAC>` device deletion to reclaim Cisco Smart Licenses (UCL/CUWL).
  * **Phase 5: Post-Cutover Verification & Licensing Audit** — Automated 8-point post-migration verification verifying active user calling status, MPP phone SIP registration, RedSky E911 address dispatch compliance, and Control Hub license reconciliation.
* **Webex Calling Bulk CSV Migration Studio:** High-performance vendor CSV importer supporting phased bulk onboarding with auto-MAC sanitization, model tiering (Tier 1 MPP Direct vs. Tier 3 Legacy Refresh), pre-flight dry runs, live batch execution, and direct hand-off exports for CUCM decommissioning.
* **Multi-Cluster CUCM Migration Control:** Multi-environment support (Primary / Mainland CUCM 12.5 and Hawaii CUCM 11.5) extracting live inventory, DID assignments, and classifying phone models into Webex Calling hardware tiers.
* **UCCX to WxCC Flow Studio:** Converts supported JSON/XML UCCX script representations and CUC Call Handlers into reviewable WxCC flow JSON schemas.
* **Prompt Compliance & Audio Auditing:** Recursively crawls on-premise prompt repositories, downloads binary `.wav` files, and validates them against WxCC Media Repository requirements (8 kHz, 16-bit/8-bit Mono, &le; 5 MB).

---

## 🚀 Toolkit Modules & Navigation

```text
+----------------------------------------------------------------------------------------------------+
|  NET DASHBOARD | Tier 1/2 Automation & Telecom Migration Portal [ User: netadmin | Role: Admin ]   |
+----------------------------------------------------------------------------------------------------+
| NAVIGATION SIDEBAR           | MAIN WORKSPACE VIEW                                                 |
|                              |                                                                     |
| 🔎 TROUBLESHOOTING           | [ Identity Location Tracker ]                                       |
|  > Identity Tracker          | Search: [ US38307 / 10.144.2.50 / 00:1A:2B:3C:4D:5E ] [ Query ]    |
|  > Windows Deep Dive         | +-----------------------------------------------------------------+ |
|  > Throughput Test (iPerf)   | | 📡 Catalyst Center Assurance Telemetry (Health Score: 9/10)     | |
|  > Network Utilities         | | Type: 🔌 Wired  | Switch: 10.10.10.1 | Port: Gi1/0/12 | VLAN: 120   | |
|                              | | 🛡️ RADIUS Transactions (Last 24h): Passed (EAP-TEAP)            | |
| ⚡ ADMIN TOOLS 🔒             | +-----------------------------------------------------------------+ |
|  > Meraki - ISE Audit        |                                                                     |
|  > Imaging 802.1X Bypass     | [ Meraki Zero-Touch Provisioning ]                                  |
|  > Meraki ZTP Pipeline       | Serial: [ Q2XX-XXXX-XXXX ]  Physical Address: [ 1301 Medical Pkwy ] |
|  > Meraki Appliance Swap     | [ (x) Claim Net  (x) Enforce Template  (•) Subnets  ( ) DHCP ]    |
|                              |                                                                     |
| 📞 TELECOM MIGRATION 🔒      | [ 5-Phase Webex Calling Migration Pipeline ]                        |
|  > Migration Pipeline (All)  | Token: [ Active (12h) ]  License: [ Webex Calling - Professional ▼ ]|
|  > Phase 1: Locations & DIDs | Sites: 1,121 | In Webex: 329 | Ready: 792 | Incomplete: 0           |
|  > Phase 2-3: Users & Devices| Staged: [ 145 Rows ] | Tier 1 MPP: 138 | Legacy Refresh: 7          |
|  > Phase 4: Decom & Licenses | Routing: [ Hybrid CUBE Injection ] | Release: [ Enhanced UCL ]     |
|  > Phase 5: Cutover Sign-Off | Audit: [ 8-Point Suite ] | E911: [ Valid ] | SIP Reg: [ 100% ]       |
|  > Workspaces & MPP CSV      | 5-Stage Studio: [ Staging -> Dry Run -> Live -> CUCM Hand-off ]     |
|  > UCCX to WxCC Flow Studio  | Script: [ LaniHuliMainMenu_v1.aef ] [ Generate WxCC Flow JSON ]     |
+----------------------------------------------------------------------------------------------------+
```

### Module Breakdown:

| Category | Module | Primary Capability |
| :--- | :--- | :--- |
| **Troubleshooting** | **Identity Location Tracker** | Real-time switchport discovery, Catalyst Center RF assurance telemetry, and 24h ISE RADIUS logs. |
| **Troubleshooting** | **Windows Deep Dive** | WinRM-based collection of client 802.1X and EAP-TEAP authentication logs packaged into downloadable ZIPs. |
| **Troubleshooting** | **Throughput Test** | Multi-stream bidirectional iPerf3 tests dispatched to remote endpoints against regional datacenters. |
| **Troubleshooting** | **Network Utilities** | On-demand path analysis, ping sweeps, and traceroute routines executed directly from target machines. |
| **Admin Tools** | **Imaging 802.1X Bypass** | Authorizes new workstations in Cisco ISE MAB staging, or purges existing devices from SCCM & Active Directory for re-imaging. |
| **Admin Tools** | **Meraki - ISE Audit** | Audits Meraki networks against ISE Network Device Groups (NDG) with dry-run diffs and live sync. |
| **Admin Tools** | **Meraki ZTP Pipeline** | Automated 5-stage branch onboarding: network creation, template binding, DHCP, and RADIUS. |
| **Admin Tools** | **Meraki Appliance Swap** | Asynchronous Celery task that validates the target network and invokes the Meraki appliance swap workflow. |
| **Telecom Migration** | **Migration Pipeline (All)** | Orchestrates the complete 5-phase migration lifecycle from on-premise CUCM to Webex Calling Control Hub. |
| **Telecom Migration** | **Phase 1: Locations & DIDs** | Meraki MX/Z4 appliance address harvesting, multi-pass resilient parsing, and PSTN E.164 DID provisioning. |
| **Telecom Migration** | **Phase 2 & 3: Users & Devices**| Bulk CSV staging for Webex Calling user profiles, calling license assignment, and Cisco MPP phone onboarding. |
| **Telecom Migration** | **Phase 4: CUCM Decommission** | Pre-decom Webex parity checks, hybrid Route Pattern injection into CUBE/LGW, DN unassignment, and Smart License release. |
| **Telecom Migration** | **Phase 5: Post-Cutover Audit** | Automated 8-point health audit covering E911 dispatch compliance, phone registration, and license reconciliation. |
| **Telecom Migration** | **CUCM Migration Control** | Multi-cluster AXL audits classifying devices into MPP Direct (Tier 1), Gateway (Tier 2), and Incompatible (Tier 3). |
| **Telecom Migration** | **Workspaces & MPP CSV Studio** | Phased 5-step bulk deployment studio for vendor CSVs with auto-MAC sanitization, tiering, dry run, live provisioning, and CUCM hand-off export. |
| **Telecom Migration** | **UCCX to WxCC Flow Studio** | Converts supported JSON/XML UCCX script representations and CUC Call Handlers into reviewable WxCC flow JSON. |

---

## 📚 Operator Guides & How-Tos

Detailed, concise step-by-step documentation is available for all telecom migration workflows:

* 📘 [**Webex Calling Migration Pipeline Operator Guide**](./MIGRATION_PIPELINE_HOWTO.md) — Comprehensive guide covering Phase 1 through Phase 5, multi-cluster CUCM discovery, hybrid dial-plan routing, and post-cutover sign-off.
* 📙 [**Webex Calling Workspace & MPP CSV Studio Guide**](./WEBEX_WORKSPACE_DEVICE_IMPORTER_HOWTO.md) — Complete walkthrough of the 5-step phased bulk CSV deployment studio, validation rules, live execution, CUCM batch export, and Day-2 location corrections.

---

## 🔐 Security & Role-Based Access Control (RBAC)

Access permissions are enforced centrally via `security/permissions.py` and router-level FastAPI dependencies:

| Capability | Helpdesk | LocalTechServices | NetworkEngineer | NetworkAdmin |
| :--- | :---: | :---: | :---: | :---: |
| Identity Location Tracker | Yes | Yes | Yes | Yes |
| Session Triage & History | Yes | Yes | Yes | Yes |
| Windows Diagnostics (WinRM) | Yes | Yes | Yes | Yes |
| Network Utilities & iPerf3 | Yes | Yes | Yes | Yes |
| Imaging 802.1X Bypass | No | Yes | Yes | Yes |
| SCCM Device Removal | No | Yes | No | Yes |
| Meraki - ISE NDG Audit | No | No | Yes | Yes |
| Meraki - ISE NDG Live Sync | No | No | No | Yes |
| Meraki Appliance Swap | No | Yes | No | Yes |
| Telecom Migration Pipeline | No | No | No | Yes |
| Workspaces & MPP CSV Studio | No | No | No | Yes |
| CUCM AXL Decommissioning | No | No | No | Yes |

* **Step-Up Authentication:** High-impact operations (such as live Webex provisioning, CUCM decommissioning, and Meraki appliance swaps) require active administrative credentials verification at execution time.
* **Role Preservation Safeguard:** All Webex Calling person update routines preserve existing tenant administrator privileges (`roles`, `siteAdmin`, `orgId`, `loginEnabled`, `title`, and `department`) to prevent privilege stripping during migration.
* **Authentication Engines:** NetDashboard supports Okta OIDC JWT bearer tokens in production (`AUTH_METHOD=OKTA`), direct Active Directory LDAPS authentication (`AUTH_METHOD=LDAP`), and local developer simulation (`AUTH_METHOD=LOCAL`).

---

## 📡 Supported Collaboration Platform Versions

The telecom migration engine targets these release families:

| Platform | Primary Profile | Hawaii Profile | API Protocol |
| :--- | :---: | :---: | :--- |
| **Cisco Unified Communications Manager** | 12.5(1) | 11.5(1) | AXL SOAP & Database Query API |
| **Cisco Unified Contact Center Express** | 12.5(1) | 12.0(1) | Admin REST API & CRS Script Parser |
| **Cisco Unity Connection** | 14.0 | 11.5 | CUPI REST API (Users, Handlers, Prompts) |
| **Cisco Webex Calling** | Current v1 API | Current v1 API | Webex REST API (People, Workspaces, Devices, Licenses) |
| **RedSky Technologies E911** | Horizon / BGW | Horizon / BGW | Emergency Routing & Location REST APIs |

---

## 🛠️ Architecture & Tech Stack

```text
                        +----------------------------+
                        |   NetDashboard Frontend    |
                        | (Jinja2, Tailwind, JS API) |
                        +--------------+-------------+
                                       |
                                       v
                        +----------------------------+
                        |     FastAPI Web Engine     |
                        |      (Uvicorn / ASGI)      |
                        +-------+------------+-------+
                                |            |
                  HTTP / REST   |            | Asynchronous Tasks
                                v            v
         +------------------------+        +--------------------------+
         | On-Prem & Cloud APIs   |        |   Valkey / Redis Broker  |
         | - Cisco ISE (ERS/MnT)  |        +-------------+------------+
         | - Cisco Catalyst (DNA) |                      |
         | - Cisco Meraki API     |                      v
         | - Cisco CUCM (AXL)     |        +--------------------------+
         | - Cisco UCCX (AdminAPI)|        |   Celery Task Workers    |
         | - Cisco CUC (CUPI)     |        |   (Migrations, Swaps,    |
         | - Cisco Webex Calling  |        |    Prompts, Long Scans)  |
         | - Active Directory     |        +--------------------------+
         +------------------------+
```

* **Backend Engine:** FastAPI 0.138+, Pydantic v2 validation models, Starlette ASGI concurrency.
* **Task Queue:** Celery 5.4+ with dynamic Valkey/Redis broker auto-detection and SQLite fallback.
* **Front-End:** Vanilla JavaScript & Tailwind CSS with curated dark slate aesthetics, micro-animations, and real-time polling telemetry.
* **Demo & Mock Layer:** `providers/demo_providers.py` delivers in-memory mock implementations for ISE, Catalyst Center, Meraki, Active Directory/LDAP, CUCM (`PRIMARY` and `HAWAII` clusters), UCCX, and CUC, supporting dry-run and simulated live execution.
* **Testing:** Pytest with comprehensive unit and mock integration coverage (329 passing tests, including dedicated `DEMO_MODE` verification).

---

## 💻 Deployment & Operation

### 🧪 Standalone Demo Mode (Offline / No Live Infrastructure Required)

NetDashboard includes a self-contained `DEMO_MODE` so you can run, evaluate, and demonstrate the entire portal locally without live Cisco appliances, Active Directory, or Valkey/Redis servers:

```powershell
# 1. Clone repository and activate virtual environment
git clone <repo_url>
cd Net_Dashboard
.\.venv\Scripts\Activate.ps1

# 2. Install dependencies
pip install -r requirements.txt

# 3. Create .env from .env.example and enable standalone demo mode
Copy-Item .env.example .env -Force
(Get-Content .env -Raw) `
    -replace "(?m)^DEMO_MODE=.*", "DEMO_MODE=true" `
    -replace "(?m)^ENVIRONMENT=.*", "ENVIRONMENT=LOCAL" `
    -replace "(?m)^AUTH_METHOD=.*", "AUTH_METHOD=LOCAL" |
    Set-Content .env -NoNewline

# 4. Start the FastAPI server
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000` for the web UI or `http://127.0.0.1:8000/docs` for the interactive OpenAPI documentation. When `DEMO_MODE=true` is active:
* Dependency factories (`core/dependencies.py` and `TelecomClusterRegistry`) automatically substitute in-memory demo providers (`DemoISEProvider`, `DemoDNACProvider`, `DemoMerakiProvider`, `DemoLDAPProvider`, `DemoCUCMProvider`, `DemoUCCXProvider`, and `DemoCUPIProvider`).
* Both `PRIMARY` and `HAWAII` telecom clusters return realistic mock inventory, dial-plan, and migration telemetry.
* Startup background Meraki cache warm-up is skipped automatically.

### Quick Start (Live Enterprise Development)

```powershell
# 1. Clone repository and activate virtual environment
git clone <repo_url>
cd Net_Dashboard
.\.venv\Scripts\Activate.ps1

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment settings
Copy-Item .env.example .env
# Edit .env with DEMO_MODE=false and your Cisco ISE, Meraki, CUCM, and Webex credentials

# 4. Start the FastAPI development server
python main.py
```

### Starting the Background Task Worker (CLI)

```powershell
# Windows execution requires the solo pool
celery -A celery_app worker --loglevel=info --pool=solo
```

### Production Windows Service Management

In production, NetDashboard runs as two Windows Services:
* **`Net Dashboard Web Engine`** — FastAPI ASGI Web Application
* **`Net Dashboard Celery Worker`** — Asynchronous task processing engine

#### Pulling Code Updates & Service Restart Routine:
Always stop the Web Engine before the Celery Worker, and start the Celery Worker before the Web Engine:

```powershell
$ProjectPath = "C:\Apps\Net_Dashboard"
Set-Location $ProjectPath

# 1. Stop Services (Web Engine first, then Celery Worker)
Stop-Service -DisplayName "Net Dashboard Web Engine" -Force
Stop-Service -DisplayName "Net Dashboard Celery Worker" -Force

# 2. Pull latest code from repository
git pull origin main

# 3. Apply updated dependencies if requirements.txt changed
.\.venv\Scripts\pip.exe install -r requirements.txt

# 4. Start Services (Celery Worker first, then Web Engine)
Start-Service -DisplayName "Net Dashboard Celery Worker"
Start-Service -DisplayName "Net Dashboard Web Engine"
```

### Running Unit & Integration Tests

```powershell
# Execute complete pytest suite inside virtual environment
.\.venv\Scripts\pytest.exe -v
```