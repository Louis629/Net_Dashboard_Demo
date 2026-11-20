"""Collect normalized source inventory for CUCM, UCCX, and CUC migrations."""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from services.cucm_audit_service import CUCMAuditService
from services.uccx_migration_service import UCCXMigrationService

logger = logging.getLogger("Source_Inventory_Service")


class InventoryCollectionError(BaseModel):
    source: str
    error_type: str
    message: str


class SourceInventoryManifest(BaseModel):
    schema_version: str = "1.0.0"
    collected_at: str
    cluster: str
    cucm: Dict[str, Any] = Field(default_factory=dict)
    uccx: Dict[str, Any] = Field(default_factory=dict)
    cuc: Dict[str, Any] = Field(default_factory=dict)
    errors: List[InventoryCollectionError] = Field(default_factory=list)


class SourceInventoryService:
    """Orchestrates source extraction without coupling source systems together."""

    def __init__(self, cucm_provider: Any, uccx_provider: Any, cuc_provider: Any):
        self.cucm = CUCMAuditService(cucm_provider)
        self.uccx = UCCXMigrationService(uccx_provider)
        self.cuc = cuc_provider

    def collect(
        self,
        cluster: str,
        default_country_code: str = "+1",
        device_scope: str = "PHYSICAL_ONLY",
        device_pool_filter: Optional[str] = None,
        cuc_page_size: int = 100,
    ) -> SourceInventoryManifest:
        manifest = SourceInventoryManifest(
            collected_at=datetime.now(timezone.utc).isoformat(),
            cluster=cluster,
        )

        self._collect_section(
            manifest,
            "cucm",
            lambda: self.cucm.run_full_migration_audit(
                default_country_code=default_country_code,
                device_scope=device_scope,
                device_pool_filter=device_pool_filter,
            ).model_dump(),
        )
        self._collect_section(
            manifest,
            "uccx",
            lambda: self.uccx.run_full_contact_center_audit(
                default_country_code=default_country_code,
            ).model_dump(),
        )
        self._collect_section(
            manifest,
            "cuc",
            lambda: self._collect_cuc(cuc_page_size),
        )
        return manifest

    def _collect_cuc(self, page_size: int) -> Dict[str, Any]:
        users = self.cuc.get_users_paginated(page_size=page_size)
        handlers = self.cuc.get_call_handlers_paginated(page_size=page_size)
        handler_details = []
        for handler in handlers:
            object_id = handler.get("ObjectId") or handler.get("objectId")
            detail = dict(handler)
            if object_id:
                detail["menu_entries"] = self.cuc.get_call_handler_menu_entries(object_id)
                detail["greetings"] = self.cuc.get_call_handler_greetings(object_id)
            handler_details.append(detail)
        return {
            "users": users,
            "call_handlers": handler_details,
            "counts": {
                "users": len(users),
                "call_handlers": len(handler_details),
            },
        }

    @staticmethod
    def _collect_section(manifest: SourceInventoryManifest, section: str, collector: Any) -> None:
        try:
            setattr(manifest, section, collector())
        except Exception as exc:
            logger.exception("Failed collecting %s source inventory", section)
            manifest.errors.append(InventoryCollectionError(
                source=section,
                error_type=type(exc).__name__,
                message=str(exc),
            ))