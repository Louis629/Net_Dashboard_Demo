from unittest.mock import MagicMock

from services.source_inventory_service import SourceInventoryService


def test_collects_cucm_uccx_and_cuc_inventory():
    cucm_provider = MagicMock()
    cucm_provider.fetch_paginated_sql.return_value = []

    uccx_provider = MagicMock()
    uccx_provider.get_csqs.return_value = []
    uccx_provider.get_skills.return_value = [{"skill_name": "Billing"}]
    uccx_provider.get_resources.return_value = [{"user_id": "agent1"}]
    uccx_provider.get_applications.return_value = [{"name": "Main", "script_name": "main.aef", "triggers": []}]

    cuc_provider = MagicMock()
    cuc_provider.get_users_paginated.return_value = [{"Alias": "agent1"}]
    cuc_provider.get_call_handlers_paginated.return_value = [{"ObjectId": "handler1", "DisplayName": "Main AA"}]
    cuc_provider.get_call_handler_menu_entries.return_value = [{"Touchtone": "1", "Action": "Transfer"}]
    cuc_provider.get_call_handler_greetings.return_value = [{"GreetingType": "Standard"}]

    manifest = SourceInventoryService(cucm_provider, uccx_provider, cuc_provider).collect("HAWAII")

    assert manifest.cluster == "HAWAII"
    assert manifest.errors == []
    assert manifest.cucm["total_devices"] == 0
    assert manifest.uccx["total_applications"] == 1
    assert manifest.cuc["counts"] == {"users": 1, "call_handlers": 1}
    assert manifest.cuc["call_handlers"][0]["menu_entries"][0]["Touchtone"] == "1"


def test_preserves_partial_inventory_when_one_source_fails():
    cucm_provider = MagicMock()
    cucm_provider.fetch_paginated_sql.side_effect = RuntimeError("CUCM unavailable")
    uccx_provider = MagicMock()
    uccx_provider.get_csqs.return_value = []
    uccx_provider.get_skills.return_value = []
    uccx_provider.get_resources.return_value = []
    uccx_provider.get_applications.return_value = []
    cuc_provider = MagicMock()
    cuc_provider.get_users_paginated.return_value = []
    cuc_provider.get_call_handlers_paginated.return_value = []

    manifest = SourceInventoryService(cucm_provider, uccx_provider, cuc_provider).collect("PRIMARY")

    assert manifest.uccx["total_applications"] == 0
    assert manifest.cuc["counts"]["users"] == 0
    assert manifest.errors[0].source == "cucm"