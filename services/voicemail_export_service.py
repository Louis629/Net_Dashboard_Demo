"""
services/voicemail_export_service.py

Extraction and Export Service for Cisco Unity Connection (CUC 14 SU4).
Exports voicemail mailboxes, System Call Handler trees, and prompt .wav audio files.
"""

import logging
import os
import re
from typing import Any, Dict, List, Optional
from pydantic import BaseModel

from providers.cupi_provider import CUPIProvider

logger = logging.getLogger("Voicemail_Export_Service")


# -----------------------------------------------------------------------------
# Pydantic Output Models
# -----------------------------------------------------------------------------

class DTMFKeyMap(BaseModel):
    touch_tone_key: str
    action: str  # e.g., Transfer to Extension, Skip Greeting, Hangup
    target_handler_or_ext: str


class CallHandlerNode(BaseModel):
    object_id: str
    display_name: str
    extension: str
    dtmf_key_maps: List[DTMFKeyMap] = []
    downloaded_prompts: List[str] = []


class VoicemailUserNode(BaseModel):
    object_id: str
    alias: str
    display_name: str
    dtmf_access_id: str  # Extension / Primary DN
    smtp_address: str
    partition: str


class CUCExportSummary(BaseModel):
    total_voicemail_users: int
    total_call_handlers: int
    total_prompts_downloaded: int
    users: List[VoicemailUserNode]
    call_handlers: List[CallHandlerNode]


# -----------------------------------------------------------------------------
# Service Implementation
# -----------------------------------------------------------------------------

class VoicemailExportService:
    def __init__(self, cupi_provider: CUPIProvider, export_dir: Optional[str] = None):
        self.cupi = cupi_provider
        self.export_dir = export_dir or os.getenv("CUC_EXPORT_DIR", "exports/voicemail")

    def run_full_voicemail_export(self) -> CUCExportSummary:
        """
        Executes a complete extraction of Voicemail Accounts, Call Handlers, DTMF Menus, and WAV prompts.
        """
        logger.info("Starting CUC 14 SU4 Voicemail & Call Handler Export Pipeline...")

        # Step 1: Export Voicemail Users
        raw_users = self.cupi.get_users_paginated(page_size=100)
        user_nodes: List[VoicemailUserNode] = []

        for u in raw_users:
            alias = u.get("Alias", "").strip()
            ext = u.get("DtmfAccessId", "").strip()
            disp = u.get("DisplayName", "").strip()
            smtp = u.get("SmtpAddress", "").strip()
            obj_id = u.get("ObjectId", "").strip()
            partition = u.get("PartitionString", "Global").strip()

            if ext:  # Filter system templates or non-mailbox accounts
                user_nodes.append(VoicemailUserNode(
                    object_id=obj_id,
                    alias=alias,
                    display_name=disp or alias,
                    dtmf_access_id=ext,
                    smtp_address=smtp or "N/A",
                    partition=partition or "Global"
                ))

        # Step 2: Export System Call Handlers & DTMF Menus
        raw_handlers = self.cupi.get_call_handlers_paginated(page_size=100)
        handler_nodes: List[CallHandlerNode] = []
        total_prompts = 0

        for h in raw_handlers:
            h_id = h.get("ObjectId", "").strip()
            h_name = h.get("DisplayName", "Unnamed_Handler").strip()
            h_ext = h.get("Extension", "").strip()

            # Clean name for filesystem safety
            clean_h_name = re.sub(r"[^A-Za-z0-9_\-]", "_", h_name)

            # Fetch Menu Entries (DTMF key maps)
            raw_menu = self.cupi.get_call_handler_menu_entries(h_id)
            key_maps: List[DTMFKeyMap] = []

            for entry in raw_menu:
                key = str(entry.get("Touchtone", "")).strip()
                action = str(entry.get("Action", "")).strip()
                target = str(entry.get("TargetHandlerDisplayName", entry.get("TransferExtension", "N/A"))).strip()

                if key:
                    key_maps.append(DTMFKeyMap(
                        touch_tone_key=key,
                        action=action or "Ignore Key",
                        target_handler_or_ext=target or "N/A"
                    ))

            # Fetch & Download Greeting Prompts (Standard, Closed, Alternate)
            greetings = ["Standard", "Closed", "Alternate"]
            downloaded_files: List[str] = []

            for g_type in greetings:
                file_name = f"{clean_h_name}_{g_type}.wav"
                target_path = os.path.join(self.export_dir, "prompts", file_name)

                success = self.cupi.download_greeting_audio_file(
                    handler_object_id=h_id,
                    greeting_type=g_type,
                    destination_path=target_path
                )
                if success:
                    downloaded_files.append(target_path)
                    total_prompts += 1

            handler_nodes.append(CallHandlerNode(
                object_id=h_id,
                display_name=h_name,
                extension=h_ext or "N/A",
                dtmf_key_maps=key_maps,
                downloaded_prompts=downloaded_files
            ))

        logger.info("Voicemail export completed. Processed %d users, %d call handlers, %d prompts.", len(user_nodes), len(handler_nodes), total_prompts)

        return CUCExportSummary(
            total_voicemail_users=len(user_nodes),
            total_call_handlers=len(handler_nodes),
            total_prompts_downloaded=total_prompts,
            users=user_nodes,
            call_handlers=handler_nodes
        )
