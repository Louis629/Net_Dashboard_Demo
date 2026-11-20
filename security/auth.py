# Save as: security/auth.py
import os
import ssl
import jwt
import base64
from datetime import datetime, timedelta, timezone
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional

import httpx
from fastapi import HTTPException, status, Depends, Request, Header
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from ldap3 import Server, Connection, SIMPLE, Tls, SUBTREE
from ldap3.utils.conv import escape_filter_chars
from pydantic_settings import BaseSettings, SettingsConfigDict

from security.roles import Role
from security.audit import security_logger, log_permission_check, log_action
from security.permissions import PERMISSIONS

# Non-blocking HTTP Basic scheme for Dev Guard
security_basic = HTTPBasic(auto_error=False)


def get_app_environment() -> str:
    environment = os.getenv("ENVIRONMENT", os.getenv("APP_ENV", "PROD")).strip().upper()
    if environment not in {"LOCAL", "DEV", "PROD"}:
        raise RuntimeError(f"Unsupported application environment: {environment}")
    return environment


def enforce_dev_environment_access(
    request: Request,
    credentials: Optional[HTTPBasicCredentials] = Depends(security_basic)
):
    """
    Allow local access, require LDAP credentials in DEV, and defer PROD to Okta.
    In PROD environment, passes through to delegated OIDC/Bearer authentication.
    """
    env = get_app_environment()

    # In Production mode, delegate to standard OIDC/Bearer token pipeline
    if env != "DEV":
        return True

    if not credentials:
        client_ip = request.client.host if request.client else "127.0.0.1"
        security_logger.info(f"DEV LDAPS Auth Challenge initiated for client_ip={client_ip} on {request.url.path}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sonic Healthcare Active Directory Authentication Required (DEV Server)",
            headers={"WWW-Authenticate": 'Basic realm="Sonic Healthcare AD"'},
        )

    auth_provider = LDAPSAuthProvider()
    is_authorized = auth_provider.authenticate(
        username=credentials.username,
        password=credentials.password,
        target_action="Dev Sandbox Login"
    )

    if not is_authorized:
        security_logger.warning(f"Dev Access Denied: User '{credentials.username}' failed Active Directory verification.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Dev Access Restricted: Invalid Active Directory credentials or insufficient role permissions.",
            headers={"WWW-Authenticate": 'Basic realm="Sonic Healthcare AD"'},
        )

    return True


def authenticate_step_up_credentials(username: str, password: str, target_action: str) -> None:
    if not username or not password or not LDAPSAuthProvider().authenticate(
        username=username,
        password=password,
        target_action=target_action
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="LDAP step-up authentication failed."
        )


class AuthSettings(BaseSettings):
    ENVIRONMENT: str = os.getenv("ENVIRONMENT", "DEV")
    DEV_DEFAULT_USER: str = os.getenv("DEV_DEFAULT_USER", "dev-engineer@corp.internal")
    AUTH_METHOD: str = os.getenv("AUTH_METHOD", "OKTA") 
    LDAP_SERVER_URI: str = os.getenv("LDAP_SERVER_URI", "ldaps://corp.internal:636")
    LDAP_DOMAIN: str = os.getenv("LDAP_DOMAIN", "corp.internal")
    LDAP_BIND_USER: str = os.getenv("LDAP_BIND_USER", "")
    LDAP_BIND_PASSWORD: str = os.getenv("LDAP_BIND_PASSWORD", "")
    LDAP_VERIFY_SSL: bool = True
    AD_GROUP_NETWORK_ADMIN: str = os.getenv("AD_GROUP_NETWORK_ADMIN", "")
    AD_GROUP_TECH_SERVICES: str = os.getenv("AD_GROUP_TECH_SERVICES", "")
    AD_GROUP_HELPDESK: str = os.getenv("AD_GROUP_HELPDESK", "")
    SCCM_REIMAGING_GROUP: str = os.getenv("SCCM_REIMAGING_GROUP", "")
    
    # Okta Configuration Properties
    OKTA_ISSUER: str = os.getenv("OKTA_ISSUER", "https://your-preview-domain.oktapreview.com")
    OKTA_CLIENT_ID: str = os.getenv("OKTA_CLIENT_ID", "")
    OKTA_AUDIENCE: str = os.getenv("OKTA_AUDIENCE", "")

    # Legacy/Local JWT Settings
    JWT_SECRET: str = os.getenv("JWT_SECRET", "fallback_dev_secret_key_change_me")
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRATION_MINUTES: int = int(os.getenv("JWT_EXPIRATION_MINUTES", "480"))

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = AuthSettings()
LDAP_BASE_DN = ",".join([f"DC={part}" for part in settings.LDAP_DOMAIN.split(".")])

# Initialize Okta JWKS Asynchronous Verification Cache Engine
JWKS_URL = f"{settings.OKTA_ISSUER}/v1/keys"
jwks_client = jwt.PyJWKClient(JWKS_URL) if "oktapreview.com" in settings.OKTA_ISSUER or "okta.com" in settings.OKTA_ISSUER else None


def create_access_token(data: dict) -> str:
    """Creates a cryptographically signed local JWT for standalone web sessions."""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.JWT_EXPIRATION_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


class BaseAuthProvider(ABC):
    @abstractmethod
    def authenticate(self, username: str, password: str, target_action: str = "Live Sync") -> bool:
        pass
    @abstractmethod
    def resolve_user_role(self, username: str, password: str) -> Role:
        pass
    @abstractmethod
    def verify_reimaging_authorization(self, username: str, password: str) -> tuple[bool, str]:
        pass


class LDAPSAuthProvider(BaseAuthProvider):
    def _get_user_connection(self, username: str, password: str) -> Optional[Connection]:
        if not username or not password: return None
        user_dn = f"{username}@{settings.LDAP_DOMAIN}" if "@" not in username else username
        try:
            verify_ssl = os.getenv("LDAP_VERIFY_SSL", "true").strip().lower() in {"1", "true", "yes"}
            tls_config = Tls(validate=ssl.CERT_REQUIRED if verify_ssl else ssl.CERT_NONE, version=ssl.PROTOCOL_TLSv1_2)
            server = Server(settings.LDAP_SERVER_URI, use_ssl=True, tls=tls_config, connect_timeout=5)
            conn = Connection(server, user=user_dn, password=password, authentication=SIMPLE)
            if conn.bind(): return conn
            return None
        except Exception: return None

    def _get_service_connection(self) -> Optional[Connection]:
        if not settings.LDAP_BIND_USER or not settings.LDAP_BIND_PASSWORD: return None
        try:
            verify_ssl = os.getenv("LDAP_VERIFY_SSL", "true").strip().lower() in {"1", "true", "yes"}
            tls_config = Tls(validate=ssl.CERT_REQUIRED if verify_ssl else ssl.CERT_NONE, version=ssl.PROTOCOL_TLSv1_2)
            server = Server(settings.LDAP_SERVER_URI, use_ssl=True, tls=tls_config, connect_timeout=5)
            bind_user = settings.LDAP_BIND_USER
            if "\\" not in bind_user and "@" not in bind_user:
                bind_user = f"{bind_user}@{settings.LDAP_DOMAIN}"
            conn = Connection(server, user=bind_user, password=settings.LDAP_BIND_PASSWORD, authentication=SIMPLE)
            if conn.bind(): return conn
            return None
        except Exception as e:
            security_logger.error(f"Service Account Bind Failed: {e}")
            return None

    def authenticate(self, username: str, password: str, target_action: str = "Live Sync") -> bool:
        user_conn = self._get_user_connection(username, password)
        if not user_conn:
            log_action(user=username, role="N/A", action=f"Step-Up Auth ({target_action})", target="LDAPS", result="DENIED - Invalid Credentials")
            return False
        user_conn.unbind()
        
        role = self._extract_role_from_ad(username)
        if role == Role.NETWORK_ADMIN:
            log_action(user=username, role=role.value, action=f"Step-Up Auth ({target_action})", target="LDAPS", result="SUCCESS")
            return True
        log_action(user=username, role=role.value if role else "N/A", action=f"Step-Up Auth ({target_action})", target="LDAPS", result="DENIED - Insufficient Clearance")
        return False

    def resolve_user_role(self, username: str, password: str) -> Role:
        user_conn = self._get_user_connection(username, password)
        if not user_conn:
            raise ValueError("Invalid LDAP credentials")
        user_conn.unbind()
        return self._extract_role_from_ad(username)

    def is_user_in_group(self, username: str, group_identifier: str) -> bool:
        """
        Queries Active Directory using the service connection to check whether the user is a
        direct or nested member of group_identifier. Supports full DNs or group names (sAMAccountName/CN).
        """
        if not group_identifier:
            return False
        conn = self._get_service_connection()
        if not conn:
            security_logger.error("No Service Account connection available for LDAP queries.")
            return False

        sam_account = username.split("@")[0] if "@" in username else username
        safe_sam = escape_filter_chars(sam_account)
        try:
            # If group_identifier is a full DN (contains "DC=")
            if "DC=" in group_identifier.upper():
                group_dn = group_identifier
            else:
                # Query group object to discover its full distinguishedName
                clean_name = escape_filter_chars(group_identifier.strip())
                group_filter = f"(&(objectCategory=group)(|(sAMAccountName={clean_name})(cn={clean_name})))"
                conn.search(search_base=LDAP_BASE_DN, search_filter=group_filter, search_scope=SUBTREE, attributes=['distinguishedName'])
                if conn.entries:
                    group_dn = str(conn.entries[0].distinguishedName.value)
                else:
                    group_dn = clean_name

            # Nested group membership check in Active Directory (LDAP_MATCHING_RULE_IN_CHAIN)
            user_filter = f"(&(objectClass=user)(sAMAccountName={safe_sam})(memberOf:1.2.840.113556.1.4.1941:={group_dn}))"
            conn.search(search_base=LDAP_BASE_DN, search_filter=user_filter, search_scope=SUBTREE, attributes=['sAMAccountName'])
            if conn.entries:
                conn.unbind()
                return True

            # Direct memberOf fallback check
            conn.search(
                search_base=LDAP_BASE_DN,
                search_filter=f"(&(objectClass=user)(sAMAccountName={safe_sam}))",
                search_scope=SUBTREE,
                attributes=['memberOf']
            )
            if conn.entries and hasattr(conn.entries[0], 'memberOf'):
                groups = [str(g).lower() for g in conn.entries[0].memberOf.values]
                target_lower = group_dn.lower()
                clean_lower = group_identifier.strip().lower()
                if any(target_lower == g or clean_lower in g for g in groups):
                    conn.unbind()
                    return True

            conn.unbind()
            return False
        except Exception as e:
            try:
                conn.unbind()
            except Exception:
                pass
            security_logger.error(f"Failed to check group membership for {username} in {group_identifier}: {e}")
            return False

    def verify_reimaging_authorization(self, username: str, password: str) -> tuple[bool, str]:
        """
        Mandatory secondary check for re-imaging. Validates user directory credentials
        and strictly requires membership in SCCM_REIMAGING_GROUP (no admin bypass).
        """
        user_conn = self._get_user_connection(username, password)
        if not user_conn:
            log_action(user=username, role="N/A", action="Step-Up Auth (Re-imaging)", target="LDAPS", result="DENIED - Invalid Credentials")
            return False, "Invalid corporate directory credentials."
        user_conn.unbind()

        reimaging_group = getattr(settings, "SCCM_REIMAGING_GROUP", "") or os.getenv("SCCM_REIMAGING_GROUP", "")
        if reimaging_group:
            in_reimaging_group = self.is_user_in_group(username, reimaging_group)
            if in_reimaging_group:
                log_action(user=username, role="ReimagingAuthorized", action="Step-Up Auth (Re-imaging)", target="LDAPS", result="SUCCESS")
                return True, "Authorized"

            # Per Windows manager requirements: No admin override. User must be in SCCM_REIMAGING_GROUP.
            log_action(user=username, role="N/A", action="Step-Up Auth (Re-imaging)", target="LDAPS", result="DENIED - Not in SCCM_REIMAGING_GROUP")
            return False, f"User '{username}' is not a member of the required re-imaging authorization group ({reimaging_group})."

        # Fallback if no specific SCCM_REIMAGING_GROUP is defined in environment
        role = self._extract_role_from_ad(username)
        if role in [Role.NETWORK_ADMIN, Role.LOCAL_TECH_SERVICES]:
            log_action(user=username, role=role.value, action="Step-Up Auth (Re-imaging)", target="LDAPS", result="SUCCESS")
            return True, "Authorized"

        log_action(user=username, role=role.value if role else "N/A", action="Step-Up Auth (Re-imaging)", target="LDAPS", result="DENIED - Insufficient Clearance")
        return False, f"User '{username}' does not have sufficient role clearance for workstation re-imaging."

    def _extract_role_from_ad(self, username: str) -> Role:
        conn = self._get_service_connection()
        if not conn:
            security_logger.error("No Service Account connection available for LDAP queries.")
            return Role.HELPDESK

        sam_account = username.split("@")[0] if "@" in username else username
        safe_sam = escape_filter_chars(sam_account)
        admin_dn = settings.AD_GROUP_NETWORK_ADMIN
        tech_dn = settings.AD_GROUP_TECH_SERVICES
        helpdesk_dn = settings.AD_GROUP_HELPDESK
        
        try:
            if admin_dn:
                admin_filter = f"(&(objectClass=user)(sAMAccountName={safe_sam})(memberOf:1.2.840.113556.1.4.1941:={admin_dn}))"
                conn.search(search_base=LDAP_BASE_DN, search_filter=admin_filter, search_scope=SUBTREE, attributes=['sAMAccountName'])
                if conn.entries:
                    conn.unbind()
                    return Role.NETWORK_ADMIN
                    
            if tech_dn:
                tech_filter = f"(&(objectClass=user)(sAMAccountName={safe_sam})(memberOf:1.2.840.113556.1.4.1941:={tech_dn}))"
                conn.search(search_base=LDAP_BASE_DN, search_filter=tech_filter, search_scope=SUBTREE, attributes=['sAMAccountName'])
                if conn.entries:
                    conn.unbind()
                    return Role.LOCAL_TECH_SERVICES

            if helpdesk_dn:
                helpdesk_filter = f"(&(objectClass=user)(sAMAccountName={safe_sam})(memberOf:1.2.840.113556.1.4.1941:={helpdesk_dn}))"
                conn.search(search_base=LDAP_BASE_DN, search_filter=helpdesk_filter, search_scope=SUBTREE, attributes=['sAMAccountName'])
                if conn.entries:
                    conn.unbind()
                    return Role.HELPDESK
                    
            conn.unbind()
            return Role.HELPDESK
            
        except Exception as e:
            conn.unbind()
            security_logger.error(f"Failed to extract nested AD groups for {username}: {e}")
            return Role.HELPDESK


class OktaAuthProvider(BaseAuthProvider):
    def authenticate(self, username: str, password: str, target_action: str = "Live Sync") -> bool:
        base_domain = settings.OKTA_ISSUER.split("/oauth2")[0]
        url = f"{base_domain}/api/v1/authn" 
        
        payload = {"username": username, "password": password}
        try:
            with httpx.Client(timeout=5) as client:
                resp = client.post(url, json=payload)
                if resp.status_code == 200 and resp.json().get("status") == "SUCCESS":
                    log_action(user=username, role="Validated via Okta", action=f"Step-Up Auth ({target_action})", target="Okta", result="SUCCESS")
                    return True
                else:
                    security_logger.warning(f"Okta API rejected credentials. Status: {resp.status_code}")
        except Exception as e:
            security_logger.error(f"Okta runtime step-up verification error: {e}")
        
        log_action(user=username, role="N/A", action=f"Step-Up Auth ({target_action})", target="Okta", result="DENIED")
        return False

    def resolve_user_role(self, username: str, password: str) -> Role:
        return Role.HELPDESK

    def verify_reimaging_authorization(self, username: str, password: str) -> tuple[bool, str]:
        is_authenticated = self.authenticate(username, password, target_action="Re-imaging")
        if not is_authenticated:
            return False, "Invalid Okta credentials."
        return True, "Authorized via Okta"


class AuthFactory:
    @staticmethod
    def get_provider() -> BaseAuthProvider:
        if get_app_environment() == "PROD":
            return OktaAuthProvider()
        return LDAPSAuthProvider()

    @classmethod
    def get_step_up_provider(cls) -> BaseAuthProvider:
        """
        All step-up authentications (re-imaging, ISE bypass, critical mutations)
        are strictly executed over LDAPS against Active Directory.
        If get_provider has been mocked (e.g. in test suites), return the mock.
        """
        prov = cls.get_provider()
        if not isinstance(prov, (OktaAuthProvider, LDAPSAuthProvider)):
            return prov
        return LDAPSAuthProvider()


@dataclass
class CurrentUser:
    username: str
    role: Role
    permissions: List[str]


def get_token_from_header(request: Request) -> Optional[str]:
    auth_header = request.headers.get("Authorization") or request.headers.get("X-API-Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header.split(" ")[1]
    return None


# security/auth.py

def get_current_user(
    request: Request,
    token: Optional[str] = Depends(get_token_from_header),
    credentials: Optional[HTTPBasicCredentials] = Depends(security_basic),
) -> CurrentUser:
    """
    Validate Okta identity in PROD and use the local admin identity in LOCAL/DEV.
    """
    environment = get_app_environment()
    if environment == "LOCAL":
        return CurrentUser(
            username=settings.DEV_DEFAULT_USER,
            role=Role.NETWORK_ADMIN,
            permissions=PERMISSIONS.get(Role.NETWORK_ADMIN, [])
        )

    if environment == "DEV":
        if token:
            try:
                payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
                username = payload.get("sub")
                role_str = payload.get("role")
                if not username or not role_str:
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Invalid token payload layout.",
                    )
                role = Role(role_str)
                return CurrentUser(username=username, role=role, permissions=PERMISSIONS.get(role, []))
            except (jwt.ExpiredSignatureError, jwt.InvalidTokenError, ValueError) as exc:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid or expired authentication token.",
                ) from exc

        if not isinstance(credentials, HTTPBasicCredentials):
            authorization = request.headers.get("Authorization", "")
            if authorization.startswith("Basic "):
                try:
                    decoded_credentials = base64.b64decode(
                        authorization.removeprefix("Basic ").strip(),
                        validate=True,
                    ).decode("utf-8")
                    username, password = decoded_credentials.split(":", 1)
                    credentials = HTTPBasicCredentials(username=username, password=password)
                except (ValueError, UnicodeDecodeError):
                    credentials = None

        if not isinstance(credentials, HTTPBasicCredentials):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Active Directory authentication required.",
                headers={"WWW-Authenticate": 'Basic realm="Sonic Healthcare AD"'},
            )

        try:
            role = LDAPSAuthProvider().resolve_user_role(
                credentials.username,
                credentials.password,
            )
            return CurrentUser(
                username=credentials.username,
                role=role,
                permissions=PERMISSIONS.get(role, []),
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Active Directory authentication failed.",
            ) from exc

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Okta identity token is required.",
        )

    if not jwks_client:
        raise HTTPException(status_code=500, detail="Okta JWKS client not properly initialized.")

    try:
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        expected_audience = settings.OKTA_AUDIENCE or settings.OKTA_CLIENT_ID or settings.OKTA_ISSUER
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=expected_audience,
        )

        username = payload.get("sub", "Unknown_User")
        okta_groups = payload.get("groups") or payload.get("oktaGroups") or []
        okta_groups_clean = [str(group).strip().lower() for group in okta_groups]

        admin_rule = os.getenv("OKTA_GROUP_NETWORK_ADMIN", "Network-Engineers").strip().lower()
        tech_rule = os.getenv("OKTA_GROUP_TECH_SERVICES", "SIT_Users").strip().lower()

        if admin_rule in okta_groups_clean or "ise_net_admin" in okta_groups_clean or "domain-admins" in okta_groups_clean:
            role = Role.NETWORK_ADMIN
        elif tech_rule in okta_groups_clean:
            role = Role.LOCAL_TECH_SERVICES
        else:
            role = Role.HELPDESK

        return CurrentUser(username=username, role=role, permissions=PERMISSIONS.get(role, []))
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Okta identity session has expired.") from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Okta validation rejected token payload: {str(exc)}",
        ) from exc


async def enforce_routing_auth(
    request: Request,
    x_forwarded_for: Optional[str] = Header(None, alias="X-Forwarded-For"),
    current_user: CurrentUser = Depends(get_current_user),
):
    client_ip = x_forwarded_for or (request.client.host if request.client else "127.0.0.1")
    environment = get_app_environment()

    if environment == "LOCAL":
        return {"status": "LOCAL_WORKSPACE", "client_ip": client_ip}

    if environment == "DEV":
        return {"status": "LDAP_AUTHENTICATED", "client_ip": client_ip}

    return {"status": "DELEGATED_TO_OIDC", "client_ip": client_ip}


class RoleChecker:
    def __init__(self, *allowed_roles: Role): self.allowed_roles = allowed_roles
    def __call__(self, current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if current_user.role not in self.allowed_roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient privileges: Required role missing.")
        return current_user


class PermissionChecker:
    def __init__(self, permission: str): self.permission = permission
    def __call__(self, current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if self.permission not in current_user.permissions:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient privileges: Required permission missing.")
        return current_user