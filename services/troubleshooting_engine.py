# services/troubleshooting_engine.py

import json
import logging
import re
from pathlib import Path
from typing import List, Dict, Any, Optional, Literal
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger("Troubleshooting_Engine")


class PlaybookCondition(BaseModel):
    field: str = Field(..., description="Key in telemetry payload (e.g., 'ad_account_match_count', 'ise_failure_code')")
    operator: Literal["equals", "not_equals", "contains", "not_contains", "less_than", "greater_than", "in", "regex"]
    value: Any = Field(..., description="Target value to evaluate against")


class Playbook(BaseModel):
    id: str
    category: Literal["8021X", "Wireless", "Wired", "VPN", "Voice"]
    name: str
    severity: Literal["Critical", "High", "Medium", "Low", "Info"]
    match_mode: Literal["ALL", "ANY"] = "ALL"
    conditions: List[PlaybookCondition]
    diagnosis: str
    confidence: int = Field(ge=0, le=100)
    recommended_actions: List[str]
    auto_actions: List[str] = Field(default_factory=list, description="UI action buttons to unlock dynamically")
    kb_id: str
    escalation_team: str = "Network Engineering"
    recommended_role: str = "HelpdeskTech"


class TroubleshootingEngine:
    def __init__(self, playbooks_dir: str = "playbooks"):
        self.playbooks_dir = Path(playbooks_dir)
        self.playbooks: List[Playbook] = []
        self.load_playbooks()

    def load_playbooks(self) -> int:
        loaded = []
        if not self.playbooks_dir.exists():
            logger.warning("Playbooks directory '%s' does not exist. Creating it.", self.playbooks_dir)
            self.playbooks_dir.mkdir(parents=True, exist_ok=True)
            return 0

        for filepath in self.playbooks_dir.rglob("*.json"):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    raw_data = json.load(f)
                    playbook = Playbook(**raw_data)
                    loaded.append(playbook)
                    logger.debug("Successfully loaded playbook '%s' from %s", playbook.id, filepath.name)
            except ValidationError as ve:
                logger.error("Schema validation error in playbook '%s': %s", filepath, ve)
            except Exception as e:
                logger.error("Failed to parse playbook file '%s': %s", filepath, e)

        self.playbooks = loaded
        logger.info("Troubleshooting Engine initialized with %d valid playbooks.", len(self.playbooks))
        return len(self.playbooks)

    def evaluate(self, telemetry_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        matched_results = []

        for playbook in self.playbooks:
            condition_evaluations = [
                self._eval_condition(cond, telemetry_data) 
                for cond in playbook.conditions
            ]

            is_match = False
            if playbook.match_mode == "ALL":
                is_match = all(condition_evaluations) if condition_evaluations else False
            elif playbook.match_mode == "ANY":
                is_match = any(condition_evaluations) if condition_evaluations else False

            if is_match:
                matched_results.append(playbook.model_dump())

        matched_results.sort(key=lambda x: x["confidence"], reverse=True)
        return matched_results

    def _eval_condition(self, cond: PlaybookCondition, data: Dict[str, Any]) -> bool:
        current = data.get(cond.field)
        if current is None:
            return False

        op = cond.operator
        val = cond.value

        try:
            if op == "equals":
                return str(current).lower() == str(val).lower()
            elif op == "not_equals":
                return str(current).lower() != str(val).lower()
            elif op == "contains":
                return str(val).lower() in str(current).lower()
            elif op == "not_contains":
                return str(val).lower() not in str(current).lower()
            elif op == "less_than":
                return float(current) < float(val)
            elif op == "greater_than":
                return float(current) > float(val)
            elif op == "in":
                return current in val if isinstance(val, list) else str(current) in str(val)
            elif op == "regex":
                return bool(re.search(str(val), str(current), re.IGNORECASE))
        except (ValueError, TypeError) as err:
            logger.debug("Comparison failure for field '%s' (%s vs %s): %s", cond.field, current, val, err)
            return False

        return False