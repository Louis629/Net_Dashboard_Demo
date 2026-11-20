"""
ISE Helpdesk Automation & Telecom Migration Portal.
Modular FastAPI application entry point.
"""

try:
    import pip_system_certs.wrapt_requests
except Exception:
    pass

from contextlib import asynccontextmanager
import logging
from logging.handlers import TimedRotatingFileHandler
import os
import threading

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.config import get_settings
from core.dependencies import (
    get_dnac_provider,
    get_ise_provider,
    get_ldap_provider,
    get_meraki_provider,
    get_sccm_provider,
)
from models.bypass import BypassRequest
from routers import (
    bypass_router,
    diagnostics_router,
    meraki_router,
    ndg_router,
    telecom_router,
    views_router,
)
from routers.meraki_admin import warm_meraki_cache
from security.auth import (
    AuthFactory,
    CurrentUser,
    PermissionChecker,
    RoleChecker,
    authenticate_step_up_credentials,
    create_access_token,
    enforce_dev_environment_access,
    enforce_routing_auth,
    get_app_environment,
    get_current_user,
    security_basic,
)
from security.roles import Role

load_dotenv(override=True)
settings = get_settings()

LOG_DIR = os.path.join(os.path.dirname(__file__), settings.LOG_DIR)
os.makedirs(LOG_DIR, exist_ok=True)

log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s")
file_handler = TimedRotatingFileHandler(
    filename=os.path.join(LOG_DIR, "net_dashboard.log"),
    when="D",
    interval=1,
    backupCount=60,
    encoding="utf-8",
)
file_handler.setFormatter(log_formatter)
file_handler.setLevel(logging.INFO)

console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
console_handler.setLevel(logging.INFO)

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
if not root_logger.handlers:
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

logger = logging.getLogger("Net_Dashboard")

# Retain provider exports used by existing integrations while routers consume
# the same cached instances through core.dependencies.
ise_provider = get_ise_provider()
sccm_provider = get_sccm_provider()
meraki_provider = get_meraki_provider()
dnac_provider = get_dnac_provider()
ldap_provider = get_ldap_provider()


@asynccontextmanager
async def lifespan(app: FastAPI):
    effective_auth = os.getenv("AUTH_METHOD", settings.AUTH_METHOD).strip().upper()
    logger.info("Effective Portal Authentication Method: '%s'", effective_auth)
    logger.info("Application startup tasks beginning via Lifespan handler.")
    runtime_settings = get_settings()
    if runtime_settings.DEMO_MODE:
        logger.info("DEMO_MODE enabled: skipping background Meraki cache warm-up.")
    else:
        threading.Thread(target=warm_meraki_cache, daemon=True).start()
    yield
    logger.info("Application shutting down.")


app = FastAPI(
    title=settings.APP_NAME,
    description="Enterprise Network Diagnostics, ISE Helpdesk Automation, and Cisco Webex Migration Portal",
    version="2.0.0",
    lifespan=lifespan,
)

raw_cors_origins = os.getenv("CORS_ORIGINS")
cors_origins = (
    [origin.strip() for origin in raw_cors_origins.split(",") if origin.strip()]
    if raw_cors_origins is not None
    else settings.cors_origins_list
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(views_router)
app.include_router(bypass_router)
app.include_router(diagnostics_router)
app.include_router(meraki_router)
app.include_router(ndg_router)
app.include_router(telecom_router)


if __name__ == "__main__":
    import uvicorn

    env_reload = os.getenv("UVICORN_RELOAD")
    if env_reload is not None:
        enable_reload = env_reload.lower() in {"true", "1", "yes"}
    else:
        enable_reload = get_app_environment() != "production"

    if enable_reload:
        uvicorn.run(
            "main:app",
            host="0.0.0.0",
            port=8000,
            reload=True,
            reload_dirs=["routers", "services", "providers", "core", "models", "templates"],
            reload_excludes=["*.log", "logs/*", "*.sqlite*", "reports/*", "imports/*", "scratch/*"],
        )
    else:
        uvicorn.run(app, host="0.0.0.0", port=8000)
