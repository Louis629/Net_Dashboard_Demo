import json
from unittest.mock import MagicMock

from providers.uccx_provider import UCCXProvider
from services.uccx_script_migration_service import UCCXScriptMigrationService


def test_converts_canonical_script_json_and_reports_unsupported_steps():
    content = json.dumps({
        "nodes": [
            {"id": "start", "type": "Start", "name": "Inbound"},
            {"id": "menu", "type": "Menu", "name": "Main menu", "properties": {"customKeys": ["1"]}},
            {"id": "queue", "type": "SelectResource", "name": "Billing", "properties": {"queueId": "BILLING"}},
            {"id": "integration", "type": "Database", "name": "Lookup customer"}
        ],
        "edges": [
            {"source": "start", "output": "success", "target": "menu"},
            {"source": "menu", "output": "key_1", "target": "queue"}
        ]
    })

    result = UCCXScriptMigrationService().convert("Main.aef", content, "+15125551000")

    assert result.flow_schema["flow_name"] == "Main"
    assert len(result.flow_schema["nodes"]) == 4
    assert len(result.flow_schema["edges"]) == 2
    assert result.flow_schema["nodes"][0]["properties"]["triggerDN"] == "+15125551000"
    assert any("Database" in warning for warning in result.warnings)


def test_provider_downloads_script_from_configured_endpoint(monkeypatch):
    monkeypatch.setenv("UCCX_SCRIPT_DOWNLOAD_ENDPOINT", "/script/{path}")
    provider = UCCXProvider(host="uccx.example", username="user", password="pass")
    provider._request = MagicMock(return_value='{"nodes": []}')

    assert provider.download_script("Main.aef") == '{"nodes": []}'
    provider._request.assert_called_once_with("/script/Main.aef")