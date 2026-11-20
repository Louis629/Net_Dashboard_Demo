# tests/test_celery_valkey.py
import pytest
from celery_app import celery_app, execute_meraki_appliance_swap_task


def test_valkey_broker_connection():
    """
    Verifies that Celery can successfully ping the Valkey/Redis broker on 127.0.0.1:6379.
    """
    try:
        conn = celery_app.connection_for_write()
        conn.connect()
        assert conn.connected is True
        conn.close()
    except Exception as exc:
        pytest.fail(f"Could not connect to Valkey broker on port 6379: {exc}")


def test_celery_task_state_tracking(mocker):
    """
    Verifies state payload serialization for task progress polling.
    """
    # 1. Patch MerakiProvider inside celery_app so constructor validation doesn't run
    mocker.patch("celery_app.MerakiProvider")

    # 2. Patch ApplianceSwapService.execute_appliance_swap
    mock_swap = mocker.patch("services.appliance_swap_service.ApplianceSwapService.execute_appliance_swap")
    mock_swap.return_value = {
        "status": "SUCCESS",
        "network_name": "Test_Branch",
        "old_serial": "Q2XX-1111-1111",
        "new_serial": "Q2YY-2222-2222"
    }

    # Execute task synchronously in test mode
    res = execute_meraki_appliance_swap_task.apply(args=["N_123", "Q2XX-1111-1111", "Q2YY-2222-2222"])

    assert res.state == "SUCCESS"
    assert res.result["status"] == "COMPLETED"
    assert "Test_Branch" in res.result["message"]