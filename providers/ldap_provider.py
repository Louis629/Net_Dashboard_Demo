# providers/ldap_provider.py
import os
import ssl
import logging
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field
from ldap3 import Server, Connection, ALL, Tls, SUBTREE
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars

from core.config import get_settings

logger = logging.getLogger("LDAP_Provider")



class LDAPAccountDetails(BaseModel):
    sam_account_name: str
    user_principal_name: Optional[str] = None
    mail: Optional[str] = None
    display_name: Optional[str] = None
    distinguished_name: str
    is_enabled: bool = True


class IdentityCollisionResult(BaseModel):
    search_term: str
    has_collision: bool
    match_count: int
    accounts: List[LDAPAccountDetails]
    summary_message: str


class LDAPProvider:
    def __init__(self):
        settings = get_settings()
        self.server_uri = (os.getenv("LDAP_SERVER_URI") or settings.LDAP_SERVER_URI).strip()
        self.domain = (os.getenv("LDAP_DOMAIN") or settings.LDAP_DOMAIN).strip()
        self.bind_user = os.getenv("LDAP_BIND_USER", settings.LDAP_BIND_USER)
        self.bind_password = os.getenv("LDAP_BIND_PASSWORD", settings.LDAP_BIND_PASSWORD)
        self.search_base = (os.getenv("LDAP_SEARCH_BASE") or settings.LDAP_SEARCH_BASE).strip()
        raw_verify = os.getenv("LDAP_VERIFY_SSL")
        if raw_verify is not None:
            verify_ssl = raw_verify.strip().lower() in {"1", "true", "yes"}
        else:
            verify_ssl = settings.LDAP_VERIFY_SSL
        self.tls_config = Tls(validate=ssl.CERT_REQUIRED if verify_ssl else ssl.CERT_NONE, version=ssl.PROTOCOL_TLSv1_2)

    def _get_server(self) -> Server:
        """Constructs ldap3 Server instance cleanly with instance context."""
        return Server(
            self.server_uri, 
            use_ssl=True, 
            tls=self.tls_config, 
            get_info=ALL,
            connect_timeout=5,
            mode='IP_V4_ONLY'
        )

    def _get_service_principal(self) -> str:
        """Formats the bind service account principal cleanly with instance context."""
        service_principal = self.bind_user
        if service_principal and "\\" not in service_principal and "@" not in service_principal:
            service_principal = f"{self.domain}\\{self.bind_user}"
        return service_principal

    def authenticate(self, username: str, password: str) -> bool:
        """
        Validates technician credentials against Active Directory for Step-Up operations.
        Used by main.py for ZTP Provisioning & Live Sync verification.
        """
        if not username or not password:
            logger.warning("LDAPS Auth rejected: Missing username or password.")
            return False

        try:
            server = self._get_server()
            service_principal = self._get_service_principal()
                
            logger.info(f"Establishing search connection via: {service_principal}")
            
            conn = Connection(
                server, 
                user=service_principal, 
                password=self.bind_password, 
                auto_bind=True,
                receive_timeout=10,
                auto_referrals=False
            )
            
            clean_username = escape_filter_chars(username.strip())
            search_filter = f"(sAMAccountName={clean_username})"
            logger.info(f"Querying Global Catalog matching ISE search_base='{self.search_base}'...")
            
            conn.search(
                search_base=self.search_base,
                search_filter=search_filter,
                attributes=['distinguishedName'],
                size_limit=1,
                get_operational_attributes=False
            )
            
            if not conn.entries:
                logger.warning(f"LDAPS lookup failed: User '{username}' not found under base tree.")
                conn.unbind()
                return False
                
            user_dn = conn.entries[0].distinguishedName.value
            logger.info(f"Discovered user DN: {user_dn}")
            conn.unbind()
            
            logger.info(f"Dispatching secondary authentication validation bind...")
            user_conn = Connection(
                server, 
                user=user_dn, 
                password=password, 
                auto_bind=True,
                receive_timeout=10,
                auto_referrals=False
            )
            
            logger.info(f"User '{username}' successfully verified via lookup pipeline.")
            user_conn.unbind()
            return True
            
        except LDAPException as e:
            logger.warning(f"LDAPS transaction rejected for '{username}': ({e})")
            return False
        except Exception as e:
            logger.error(f"Unexpected error running AD validation: {e}")
            return False

    def check_identity_collisions(self, identifier: str) -> IdentityCollisionResult:
        """
        Queries Active Directory for accounts sharing a sAMAccountName, UPN, or Email address.
        Detects ambiguous identities that cause Cisco ISE 802.1X authentication failures.
        """
        clean_id = escape_filter_chars(identifier.strip())
        search_filter = (
            f"(&(objectCategory=person)(objectClass=user)"
            f"(|(sAMAccountName={clean_id})(userPrincipalName={clean_id})(mail={clean_id})))"
        )
        attributes = ["sAMAccountName", "userPrincipalName", "mail", "displayName", "distinguishedName", "userAccountControl"]

        try:
            server = self._get_server()
            service_principal = self._get_service_principal()

            with Connection(
                server, 
                user=service_principal, 
                password=self.bind_password, 
                auto_bind=True, 
                receive_timeout=10, 
                auto_referrals=False
            ) as conn:
                conn.search(
                    search_base=self.search_base,
                    search_filter=search_filter,
                    search_scope=SUBTREE,
                    attributes=attributes
                )

                accounts: List[LDAPAccountDetails] = []
                for entry in conn.entries:
                    sam = getattr(entry, "sAMAccountName", None)
                    sam_val = str(sam.value) if sam and hasattr(sam, "value") else "UNKNOWN"

                    upn = getattr(entry, "userPrincipalName", None)
                    upn_val = str(upn.value) if upn and hasattr(upn, "value") else None

                    mail = getattr(entry, "mail", None)
                    mail_val = str(mail.value) if mail and hasattr(mail, "value") else None

                    disp = getattr(entry, "displayName", None)
                    disp_val = str(disp.value) if disp and hasattr(disp, "value") else None

                    uac_attr = getattr(entry, "userAccountControl", None)
                    uac_val = int(uac_attr.value) if uac_attr and hasattr(uac_attr, "value") and uac_attr.value else 512
                    is_enabled = not bool(uac_val & 2)

                    accounts.append(
                        LDAPAccountDetails(
                            sam_account_name=sam_val,
                            user_principal_name=upn_val,
                            mail=mail_val,
                            display_name=disp_val,
                            distinguished_name=str(entry.entry_dn),
                            is_enabled=is_enabled
                        )
                    )

                match_count = len(accounts)
                has_collision = match_count > 1

                if match_count == 0:
                    summary = f"No Active Directory account found matching '{clean_id}'."
                elif match_count == 1:
                    summary = f"Unique identity resolved: {accounts[0].sam_account_name}."
                else:
                    sams = ", ".join([a.sam_account_name for a in accounts])
                    summary = f"CRITICAL: {match_count} AD accounts share identity '{clean_id}' [{sams}]. This causes Cisco ISE 802.1X failures."

                return IdentityCollisionResult(
                    search_term=clean_id,
                    has_collision=has_collision,
                    match_count=match_count,
                    accounts=accounts,
                    summary_message=summary
                )

        except Exception as e:
            logger.error(f"Error checking identity collision for '{clean_id}': {e}")
            return IdentityCollisionResult(
                search_term=clean_id,
                has_collision=False,
                match_count=0,
                accounts=[],
                summary_message=f"LDAP Collision Check Error: {str(e)}"
            )