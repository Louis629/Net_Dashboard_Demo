"""
services/wxcc_flow_generator_service.py

High-Performance WxCC Flow Generator Engine.
Translates Cisco UCCX (12.0/12.5) Application Scripts and CUC (11.5/14) Call Handlers
into production-ready Webex Contact Center (WxCC) Flow Builder JSON schemas.
Preserves existing CUC batch export methods while adding interactive UCCX generation.
"""

import json
import logging
import os
import uuid
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

logger = logging.getLogger("WxCC_Flow_Generator")


# -----------------------------------------------------------------------------
# WxCC Flow Builder Schema Models
# -----------------------------------------------------------------------------

class WxCCNodePosition(BaseModel):
    x: int
    y: int


class WxCCEdge(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source_node_id: str
    source_output_key: str  # e.g., "key_1", "key_2", "success", "timeout", "open", "closed"
    target_node_id: str


class WxCCFlowNode(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    type: str  # NewPhoneContact, BusinessHour, PlayMedia, Menu, QueueContact, BlindTransfer, Disconnect
    position: WxCCNodePosition
    properties: Dict[str, Any] = Field(default_factory=dict)


class WxCCFlowDefinition(BaseModel):
    flow_name: str
    description: str
    version: str = "1.0.0"
    entry_point_id: Optional[str] = "DEFAULT_ENTRY_POINT"
    nodes: List[WxCCFlowNode] = []
    edges: List[WxCCEdge] = []

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.model_dump(), indent=indent)


# -----------------------------------------------------------------------------
# Flow Generator Service Implementation
# -----------------------------------------------------------------------------

class WxCCFlowGeneratorService:
    def __init__(self, media_repository_url_prefix: Optional[str] = None):
        self.media_prefix = (
            media_repository_url_prefix
            or os.getenv("WXCC_MEDIA_BASE_URL", "https://media.wxcc-us1.cisco.com/prompts")
        ).rstrip("/")

    # -------------------------------------------------------------------------
    # Cisco Unity Connection (CUC) Call Handler Translation (Preserved Pipeline)
    # -------------------------------------------------------------------------

    def convert_call_handler_to_wxcc_flow(
        self,
        call_handler_data: Dict[str, Any],
        default_queue_id: str = "WXCC_DEFAULT_QUEUE_ID"
    ) -> WxCCFlowDefinition:
        """
        Converts a single CUC Call Handler JSON structure into a complete WxCC Flow.
        Preserved exactly for Celery task compatibility.
        """
        handler_name = call_handler_data.get("display_name", "Extracted_Call_Handler")
        handler_ext = call_handler_data.get("extension", "N/A")
        dtmf_maps = call_handler_data.get("dtmf_key_maps", [])
        prompts = call_handler_data.get("downloaded_prompts", [])

        logger.info("Generating WxCC Flow for Call Handler '%s' (Ext: %s)...", handler_name, handler_ext)

        nodes: List[WxCCFlowNode] = []
        edges: List[WxCCEdge] = []

        entry_node = WxCCFlowNode(
            name="Inbound Phone Call",
            type="NewPhoneContact",
            position=WxCCNodePosition(x=100, y=100),
            properties={"description": f"Triggered by inbound call to extension {handler_ext}"}
        )
        nodes.append(entry_node)

        greeting_file = prompts[0] if prompts else f"{handler_name}_Standard.wav"
        greeting_url = f"{self.media_prefix}/{greeting_file.replace('\\', '/').split('/')[-1]}"

        play_prompt_node = WxCCFlowNode(
            name="Play Main Greeting",
            type="PlayMedia",
            position=WxCCNodePosition(x=100, y=250),
            properties={
                "audioSource": "URL",
                "audioUrl": greeting_url,
                "allowInterrupt": True
            }
        )
        nodes.append(play_prompt_node)

        edges.append(WxCCEdge(
            source_node_id=entry_node.id,
            source_output_key="contact_assigned",
            target_node_id=play_prompt_node.id
        ))

        active_dtmf_keys = [str(m.get("touch_tone_key")) for m in dtmf_maps if m.get("touch_tone_key")]
        menu_node = WxCCFlowNode(
            name="DTMF Key Menu",
            type="Menu",
            position=WxCCNodePosition(x=100, y=400),
            properties={
                "numberOfReattempts": 3,
                "interDigitTimeoutSeconds": 5,
                "customKeys": active_dtmf_keys or ["1", "2", "0"]
            }
        )
        nodes.append(menu_node)

        edges.append(WxCCEdge(
            source_node_id=play_prompt_node.id,
            source_output_key="success",
            target_node_id=menu_node.id
        ))

        x_offset = 450
        y_cursor = 100

        for m in dtmf_maps:
            key = str(m.get("touch_tone_key", "")).strip()
            action = str(m.get("action", "")).strip()
            target = str(m.get("target_handler_or_ext", "")).strip()

            if not key:
                continue

            if "Transfer" in action or target.isdigit():
                target_node = WxCCFlowNode(
                    name=f"Transfer Key {key} to {target}",
                    type="BlindTransfer",
                    position=WxCCNodePosition(x=x_offset, y=y_cursor),
                    properties={"destinationType": "DN", "destination": target}
                )
            elif "Queue" in action or "CSQ" in action:
                target_node = WxCCFlowNode(
                    name=f"Queue Key {key} ({target})",
                    type="QueueContact",
                    position=WxCCNodePosition(x=x_offset, y=y_cursor),
                    properties={"queueId": default_queue_id, "priority": 1}
                )
            else:
                target_node = WxCCFlowNode(
                    name=f"Disconnect Key {key}",
                    type="Disconnect",
                    position=WxCCNodePosition(x=x_offset, y=y_cursor),
                    properties={}
                )

            nodes.append(target_node)
            edges.append(WxCCEdge(
                source_node_id=menu_node.id,
                source_output_key=f"key_{key}",
                target_node_id=target_node.id
            ))
            y_cursor += 150

        disconnect_node = WxCCFlowNode(
            name="Menu Timeout Disconnect",
            type="Disconnect",
            position=WxCCNodePosition(x=100, y=max(y_cursor, 550)),
            properties={"reason": "DTMF Timeout Exceeded"}
        )
        nodes.append(disconnect_node)

        edges.append(WxCCEdge(
            source_node_id=menu_node.id,
            source_output_key="timeout",
            target_node_id=disconnect_node.id
        ))

        return WxCCFlowDefinition(
            flow_name=f"WxCC_Flow_{handler_name}",
            description=f"Auto-generated WxCC Flow from CUC Call Handler '{handler_name}' (Ext: {handler_ext}).",
            nodes=nodes,
            edges=edges
        )

    def convert_cuc_export_batch(
        self,
        cuc_export_summary: Dict[str, Any],
        output_dir: str = "exports/wxcc_flows"
    ) -> List[str]:
        """
        Batch converts a CUC Export Summary payload into individual WxCC Flow JSON files.
        Preserved exactly for Celery task compatibility.
        """
        os.makedirs(output_dir, exist_ok=True)
        call_handlers = cuc_export_summary.get("call_handlers", [])
        generated_files: List[str] = []

        for ch in call_handlers:
            h_name = ch.get("display_name", "Unnamed")
            flow_def = self.convert_call_handler_to_wxcc_flow(ch)

            file_path = os.path.join(output_dir, f"{h_name}_WxCC_Flow.json")
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(flow_def.to_json())

            generated_files.append(file_path)
            logger.info("Exported WxCC Flow JSON: %s", file_path)

        return generated_files

    # -------------------------------------------------------------------------
    # Cisco UCCX Script & Multi-Tier Menu Translation (Interactive Engine)
    # -------------------------------------------------------------------------

    def generate_uccx_menu_flow(
        self,
        flow_name: str,
        trigger_dn: str,
        open_greeting_prompt: str,
        closed_greeting_prompt: Optional[str] = None,
        menu_dtmf_mappings: Optional[Dict[str, Dict[str, str]]] = None,
        business_hours_id: Optional[str] = None,
        default_queue_id: str = "WXCC_DEFAULT_QUEUE_ID"
    ) -> WxCCFlowDefinition:
        """
        Translates UCCX Contact Center scripts with Business Hours, Prompts, Menus, and CSQs.
        Uses structured 2D coordinate calculations to avoid visual node overlap.
        """
        nodes: List[WxCCFlowNode] = []
        edges: List[WxCCEdge] = []
        mappings = menu_dtmf_mappings or {}

        # 1. Entry Point Node
        entry_node = WxCCFlowNode(
            name="Inbound Contact Trigger",
            type="NewPhoneContact",
            position=WxCCNodePosition(x=100, y=100),
            properties={"triggerDN": trigger_dn}
        )
        nodes.append(entry_node)

        current_parent_id = entry_node.id
        current_output_key = "contact_assigned"
        y_cursor = 240

        # 2. Optional Business Hours Evaluation Node
        if business_hours_id:
            bh_node = WxCCFlowNode(
                name="Check Business Operating Hours",
                type="BusinessHour",
                position=WxCCNodePosition(x=100, y=y_cursor),
                properties={"businessHoursId": business_hours_id}
            )
            nodes.append(bh_node)
            edges.append(WxCCEdge(
                source_node_id=current_parent_id,
                source_output_key=current_output_key,
                target_node_id=bh_node.id
            ))

            if closed_greeting_prompt:
                clean_closed = closed_greeting_prompt.replace("\\", "/").split("/")[-1]
                closed_prompt_node = WxCCFlowNode(
                    name="Play Closed Greeting",
                    type="PlayMedia",
                    position=WxCCNodePosition(x=450, y=y_cursor),
                    properties={
                        "audioSource": "URL",
                        "audioUrl": f"{self.media_prefix}/{clean_closed}",
                        "allowInterrupt": False
                    }
                )
                closed_disc_node = WxCCFlowNode(
                    name="Closed Disconnect",
                    type="Disconnect",
                    position=WxCCNodePosition(x=750, y=y_cursor),
                    properties={"reason": "Closed Operating Hours Call Termination"}
                )
                nodes.extend([closed_prompt_node, closed_disc_node])
                edges.append(WxCCEdge(
                    source_node_id=bh_node.id,
                    source_output_key="closed",
                    target_node_id=closed_prompt_node.id
                ))
                edges.append(WxCCEdge(
                    source_node_id=closed_prompt_node.id,
                    source_output_key="success",
                    target_node_id=closed_disc_node.id
                ))

            current_parent_id = bh_node.id
            current_output_key = "open"
            y_cursor += 160

        # 3. Main Greeting Prompt Node
        clean_open = open_greeting_prompt.replace("\\", "/").split("/")[-1]
        open_prompt_node = WxCCFlowNode(
            name="Play Main Greeting",
            type="PlayMedia",
            position=WxCCNodePosition(x=100, y=y_cursor),
            properties={
                "audioSource": "URL",
                "audioUrl": f"{self.media_prefix}/{clean_open}",
                "allowInterrupt": True
            }
        )
        nodes.append(open_prompt_node)
        edges.append(WxCCEdge(
            source_node_id=current_parent_id,
            source_output_key=current_output_key,
            target_node_id=open_prompt_node.id
        ))
        y_cursor += 160

        # 4. DTMF Key Menu Node
        active_keys = sorted(list(mappings.keys())) or ["1", "2", "0"]
        menu_node = WxCCFlowNode(
            name="Main DTMF Menu",
            type="Menu",
            position=WxCCNodePosition(x=100, y=y_cursor),
            properties={
                "customKeys": active_keys,
                "numberOfReattempts": 3,
                "interDigitTimeoutSeconds": 5
            }
        )
        nodes.append(menu_node)
        edges.append(WxCCEdge(
            source_node_id=open_prompt_node.id,
            source_output_key="success",
            target_node_id=menu_node.id
        ))

        # 5. Routing Destination Nodes (Branches)
        x_branch_offset = 450
        y_branch_cursor = y_cursor - 60

        for key, target_info in mappings.items():
            action_type = str(target_info.get("type", "QUEUE")).upper()
            destination = str(target_info.get("destination", "")).strip()

            if action_type in {"QUEUE", "CSQ"}:
                target_node = WxCCFlowNode(
                    name=f"Queue Key {key} [{destination}]",
                    type="QueueContact",
                    position=WxCCNodePosition(x=x_branch_offset, y=y_branch_cursor),
                    properties={
                        "queueId": destination or default_queue_id,
                        "priority": int(target_info.get("priority", 1))
                    }
                )
            elif action_type in {"TRANSFER", "BLIND_TRANSFER", "DN"}:
                target_node = WxCCFlowNode(
                    name=f"Transfer Key {key} to {destination}",
                    type="BlindTransfer",
                    position=WxCCNodePosition(x=x_branch_offset, y=y_branch_cursor),
                    properties={
                        "destination": destination,
                        "destinationType": "DN"
                    }
                )
            else:
                target_node = WxCCFlowNode(
                    name=f"Disconnect Key {key}",
                    type="Disconnect",
                    position=WxCCNodePosition(x=x_branch_offset, y=y_branch_cursor),
                    properties={"reason": f"Menu Key {key} Termination"}
                )

            nodes.append(target_node)
            edges.append(WxCCEdge(
                source_node_id=menu_node.id,
                source_output_key=f"key_{key}",
                target_node_id=target_node.id
            ))
            y_branch_cursor += 140

        # 6. Fallback Timeout Node
        timeout_disc_node = WxCCFlowNode(
            name="Menu Timeout Disconnect",
            type="Disconnect",
            position=WxCCNodePosition(x=100, y=max(y_cursor + 160, y_branch_cursor)),
            properties={"reason": "DTMF Input Timeout Exceeded"}
        )
        nodes.append(timeout_disc_node)
        edges.append(WxCCEdge(
            source_node_id=menu_node.id,
            source_output_key="timeout",
            target_node_id=timeout_disc_node.id
        ))

        return WxCCFlowDefinition(
            flow_name=flow_name,
            description=f"Auto-generated WxCC Flow for trigger {trigger_dn}.",
            nodes=nodes,
            edges=edges
        )