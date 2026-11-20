"""
routers/views.py

Web portal HTML view rendering and current user profile inspection.
"""

import os
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

import jwt
from core.config import get_settings
from security.audit import security_logger
from security.auth import (
    CurrentUser,
    enforce_dev_environment_access,
    enforce_routing_auth,
    get_current_user,
)

router = APIRouter(tags=["Frontend Views"])
templates = Jinja2Templates(directory="templates")


@router.get("/", response_class=HTMLResponse, dependencies=[Depends(enforce_dev_environment_access)])
async def serve_home_page(request: Request):
    """Renders the main portal dashboard."""
    settings = get_settings()
    auth_method = os.getenv("AUTH_METHOD", settings.AUTH_METHOD).strip().upper()
    primary_host = os.getenv("PRIMARY_CUCM_AXL_HOST", settings.PRIMARY_CUCM_AXL_HOST or settings.CUCM_AXL_HOST or settings.CUCM_PAN_IP)
    hawaii_host = os.getenv("HAWAII_CUCM_AXL_HOST", settings.HAWAII_CUCM_AXL_HOST)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "environment": os.getenv("ENVIRONMENT", settings.ENVIRONMENT).upper(),
            "auth_method": auth_method,
            "okta_issuer": settings.OKTA_ISSUER,
            "okta_client_id": settings.OKTA_CLIENT_ID,
            "okta_group_network_admin": os.getenv("OKTA_GROUP_NETWORK_ADMIN", settings.OKTA_GROUP_NETWORK_ADMIN),
            "okta_group_tech_services": os.getenv("OKTA_GROUP_TECH_SERVICES", settings.OKTA_GROUP_TECH_SERVICES),
            "okta_group_helpdesk": os.getenv("OKTA_GROUP_HELPDESK", settings.OKTA_GROUP_HELPDESK),
            "ad_group_network_admin": os.getenv("AD_GROUP_NETWORK_ADMIN", settings.AD_GROUP_NETWORK_ADMIN),
            "ad_group_tech_services": os.getenv("AD_GROUP_TECH_SERVICES", settings.AD_GROUP_TECH_SERVICES),
            "ad_group_helpdesk": os.getenv("AD_GROUP_HELPDESK", settings.AD_GROUP_HELPDESK),
            "primary_cucm_host": primary_host,
            "hawaii_cucm_host": hawaii_host,
        },
    )


@router.get("/telecom-migration", response_class=HTMLResponse, dependencies=[Depends(enforce_dev_environment_access)])
async def serve_telecom_migration_page(request: Request, view: Optional[str] = None):
    """Renders the Cisco CUCM/UCCX to Webex Calling migration view."""
    settings = get_settings()
    auth_method = os.getenv("AUTH_METHOD", settings.AUTH_METHOD).strip().upper()
    primary_host = os.getenv("PRIMARY_CUCM_AXL_HOST", settings.PRIMARY_CUCM_AXL_HOST or settings.CUCM_AXL_HOST or settings.CUCM_PAN_IP)
    hawaii_host = os.getenv("HAWAII_CUCM_AXL_HOST", settings.HAWAII_CUCM_AXL_HOST)

    if request.headers.get("HX-Request") == "true" or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return templates.TemplateResponse(
            request=request,
            name="telecom_migration.html",
            context={
                "primary_cucm_host": primary_host,
                "hawaii_cucm_host": hawaii_host,
            },
        )

    tab = "telecom_control"
    if view:
        v = view.lower().strip()
        if "workspace" in v or "device" in v:
            tab = "telecom_workspaces"
        elif "uccx" in v or "flow" in v:
            tab = "telecom_uccx"

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "active_tab": tab,
            "environment": os.getenv("ENVIRONMENT", settings.ENVIRONMENT).upper(),
            "auth_method": auth_method,
            "okta_issuer": settings.OKTA_ISSUER,
            "okta_client_id": settings.OKTA_CLIENT_ID,
            "primary_cucm_host": primary_host,
            "hawaii_cucm_host": hawaii_host,
        },
    )


from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from fastapi import status, HTTPException
from security.auth import LDAPSAuthProvider, create_access_token
from security.permissions import PERMISSIONS


class LDAPSLoginRequest(BaseModel):
    username: str
    password: str


@router.post("/api/v1/auth/login")
async def login_with_ldaps(credentials: LDAPSLoginRequest):
    """
    DEV environment LDAPS authentication endpoint.
    Verifies user credentials against Active Directory and returns a signed local session JWT.
    """
    settings = get_settings()
    env = os.getenv("ENVIRONMENT", settings.ENVIRONMENT).upper()
    if env == "PROD":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Direct LDAPS login is disabled in PRODUCTION. Please authenticate via Okta SSO."
        )

    auth_provider = LDAPSAuthProvider()
    try:
        role = await run_in_threadpool(auth_provider.resolve_user_role, credentials.username, credentials.password)
        token = create_access_token({"sub": credentials.username, "role": role.value})
        return {
            "status": "SUCCESS",
            "access_token": token,
            "token_type": "bearer",
            "username": credentials.username,
            "role": role.value,
            "permissions": PERMISSIONS.get(role, [])
        }
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Active Directory authentication failed: Invalid username or password ({exc})."
        )


@router.get("/api/v1/me", dependencies=[Depends(enforce_routing_auth)])
async def get_my_user(user: CurrentUser = Depends(get_current_user)):
    """Returns currently authenticated operator credentials, role, and permissions."""
    return {"username": user.username, "role": user.role.value, "permissions": user.permissions}


@router.post("/api/v1/auth/logout")
async def logout_user(request: Request):
    """
    Terminates the active operator session, logs the security audit event,
    and returns cleanup confirmation.
    """
    settings = get_settings()
    auth_header = request.headers.get("Authorization") or request.headers.get("X-API-Authorization")
    user_identity = "Unknown_User"
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
        try:
            payload = jwt.decode(
                token,
                settings.JWT_SECRET,
                algorithms=[settings.JWT_ALGORITHM],
                options={"verify_exp": False}
            )
            user_identity = payload.get("sub", user_identity)
        except Exception:
            pass

    security_logger.info(f"Operator '{user_identity}' logged out successfully.")
    return {"status": "SUCCESS", "message": "Session terminated successfully."}

