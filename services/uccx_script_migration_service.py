"""Convert exported UCCX script descriptions into reviewable WxCC flow JSON."""

import json
import logging
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from services.wxcc_flow_generator_service import (
    WxCCEdge,
    WxCCFlowDefinition,
    WxCCFlowNode,
    WxCCNodePosition,
)

logger = logging.getLogger("UCCX_Script_Migration_Service")


class NormalizedScriptNode(BaseModel):
    id: str
    type: str
    name: str
    properties: Dict[str, Any] = Field(default_factory=dict)


class NormalizedScriptEdge(BaseModel):
    source: str
    output: str = "success"
    target: str


class NormalizedUCCXScript(BaseModel):
    script_name: str
    nodes: List[NormalizedScriptNode] = Field(default_factory=list)
    edges: List[NormalizedScriptEdge] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class UCCXScriptConversionResult(BaseModel):
    script: NormalizedUCCXScript
    flow_schema: Dict[str, Any]
    warnings: List[str] = Field(default_factory=list)


class UCCXScriptMigrationService:
    """Parser and converter for exported, text-readable UCCX script formats."""

    TYPE_MAP = {
        "start": "NewPhoneContact",
        "accept": "NewPhoneContact",
        "playprompt": "PlayMedia",
        "prompt": "PlayMedia",
        "menu": "Menu",
        "getdigitstring": "Menu",
        "selectresource": "QueueContact",
        "selectresource step": "QueueContact",
        "queue": "QueueContact",
        "transfer": "BlindTransfer",
        "callredirect": "BlindTransfer",
        "disconnect": "Disconnect",
        "hangup": "Disconnect",
    }

    def __init__(self, uccx_provider: Optional[Any] = None):
        self.uccx = uccx_provider

    def acquire_script(self, script_path: str) -> str:
        if not self.uccx:
            raise ValueError("A UCCX provider is required for remote script acquisition.")
        return self.uccx.download_script(script_path)

    def parse_script(self, script_name: str, content: str) -> NormalizedUCCXScript:
        """Parse canonical JSON exports or best-effort XML script descriptions."""
        try:
            payload = json.loads(content)
            return self._from_mapping(script_name, payload)
        except json.JSONDecodeError:
            pass

        try:
            root = ET.fromstring(content)
        except ET.ParseError as exc:
            raise ValueError("Script content is neither valid JSON nor XML.") from exc

        nodes: List[NormalizedScriptNode] = []
        warnings: List[str] = []
        for index, element in enumerate(root.iter()):
            if element is root:
                continue
            raw_tag = element.tag.rsplit("}", 1)[-1].strip()
            raw_type = (element.attrib.get("type") or raw_tag).strip()
            if "type" not in element.attrib and raw_tag.lower() not in self.TYPE_MAP:
                continue
            name = (element.attrib.get("name") or element.findtext("name") or raw_type).strip()
            node_id = element.attrib.get("id") or element.attrib.get("guid") or f"node_{index}"
            node_type = self._map_type(raw_type)
            properties = {
                key: value for key, value in element.attrib.items()
                if key not in {"id", "guid", "name", "type"}
            }
            nodes.append(NormalizedScriptNode(id=node_id, type=node_type, name=name, properties=properties))
            if node_type == "Unsupported":
                warnings.append(f"Unsupported UCCX step '{raw_type}' at {node_id}.")

        edges = [
            NormalizedScriptEdge(source=node.attrib["id"], target=node.attrib[target_key])
            for node in root.iter()
            if "id" in node.attrib
            for target_key in ("target", "next")
            if target_key in node.attrib
        ]

        return NormalizedUCCXScript(script_name=script_name, nodes=nodes, edges=edges, warnings=warnings)

    def convert(self, script_name: str, content: str, trigger_dn: Optional[str] = None) -> UCCXScriptConversionResult:
        script = self.parse_script(script_name, content)
        flow_nodes: List[WxCCFlowNode] = []
        flow_edges: List[WxCCEdge] = []
        node_ids = {node.id for node in script.nodes}

        for index, node in enumerate(script.nodes):
            properties = dict(node.properties)
            if node.type == "NewPhoneContact" and trigger_dn:
                properties["triggerDN"] = trigger_dn
            flow_nodes.append(WxCCFlowNode(
                id=node.id,
                name=node.name,
                type=node.type,
                position=WxCCNodePosition(x=100 + (index % 4) * 300, y=100 + (index // 4) * 160),
                properties=properties,
            ))

        for edge in script.edges:
            if edge.source in node_ids and edge.target in node_ids:
                flow_edges.append(WxCCEdge(
                    source_node_id=edge.source,
                    source_output_key=edge.output,
                    target_node_id=edge.target,
                ))
            else:
                script.warnings.append(f"Unresolved script edge {edge.source} -> {edge.target}.")

        flow = WxCCFlowDefinition(
            flow_name=script_name.rsplit(".", 1)[0],
            description=f"Converted from UCCX script '{script_name}'. Review warnings before import.",
            entry_point_id=trigger_dn or "DEFAULT_ENTRY_POINT",
            nodes=flow_nodes,
            edges=flow_edges,
        )
        return UCCXScriptConversionResult(
            script=script,
            flow_schema=flow.model_dump(),
            warnings=script.warnings,
        )

    def _from_mapping(self, script_name: str, payload: Dict[str, Any]) -> NormalizedUCCXScript:
        raw_nodes = payload.get("nodes", payload.get("steps", []))
        nodes: List[NormalizedScriptNode] = []
        warnings: List[str] = list(payload.get("warnings", []))
        for index, raw_node in enumerate(raw_nodes):
            raw_type = str(raw_node.get("type", raw_node.get("step_type", "Unsupported")))
            node = NormalizedScriptNode(
                id=str(raw_node.get("id", f"node_{index}")),
                type=self._map_type(raw_type),
                name=str(raw_node.get("name", raw_type)),
                properties=dict(raw_node.get("properties", {})),
            )
            nodes.append(node)
            if node.type == "Unsupported":
                warnings.append(f"Unsupported UCCX step '{raw_type}' at {node.id}.")

        edges = [NormalizedScriptEdge(**edge) for edge in payload.get("edges", [])]
        return NormalizedUCCXScript(script_name=script_name, nodes=nodes, edges=edges, warnings=warnings)

    def _map_type(self, raw_type: str) -> str:
        normalized = "".join(raw_type.lower().split())
        return self.TYPE_MAP.get(normalized, "Unsupported")