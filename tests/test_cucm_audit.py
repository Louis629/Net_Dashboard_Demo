# tests/test_cucm_audit.py
import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
from providers.cucm_axl_provider import CUCMAXLProvider
from services.cucm_audit_service import CUCMAuditService

load_dotenv(dotenv_path=PROJECT_ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s"
)
logger = logging.getLogger("Test_CUCM_Audit")


def main():
    print("=== Testing CUCM AXL Extraction & Audit Engine ===")
    try:
        axl_provider = CUCMAXLProvider()
        print(f"Connecting to CUCM Publisher: https://{axl_provider.host}:8443/axl/")

        print("\nTesting AXL SQL query (Process Node Check)...")
        test_query = "SELECT name, description, nodeid FROM processnode WHERE nodeid > 1"
        nodes = axl_provider.execute_sql_query(test_query)
        print(f"Discovered CUCM Nodes: {nodes}\n")

        print("Executing Full CUCM Migration Audit (Paginated AXL SQL)...")
        audit_service = CUCMAuditService(axl_provider=axl_provider)
        summary = audit_service.run_full_migration_audit(default_country_code="+1")

        print("\n=== AUDIT RESULTS ===")
        print(f"Total Devices: {summary.total_devices}")
        print(f" - Tier 1 (MPP Direct): {summary.tier_1_mpp_direct_count}")
        print(f" - Tier 2 (Gateway/Softphone): {summary.tier_2_gateway_softphone_count}")
        print(f" - Tier 3 (Incompatible Refresh): {summary.tier_3_incompatible_count}")
        print(f"Total Lines Analyzed: {summary.total_lines}")
        print(f" - E.164 Compliant: {summary.e164_compliant_lines_count}")
        print(f" - Non-Compliant: {summary.e164_non_compliant_lines_count}")
        print(f"Unique Users: {summary.total_unique_users}")

        print("\n=== TOP DEVICE MODELS IN CLUSTER ===")
        for model_name, count in list(summary.model_breakdown.items())[:10]:
            print(f" - {model_name}: {count}")

        if summary.non_compliant_lines_sample:
            print("\n=== NON-COMPLIANT LINES SAMPLE (Missing Masks in CUCM) ===")
            for line in summary.non_compliant_lines_sample[:5]:
                print(f" - DN: {line.dn} | Partition: {line.partition} | Display: {line.display_name}")

        if summary.devices:
            print("\nSample Device Record:")
            print(json.dumps(summary.devices[0].model_dump(), indent=2))

    except Exception as e:
        logger.exception("CUCM AXL Test Execution Failed")
        print(f"\n❌ CUCM AXL Test Failed: {e}")


if __name__ == "__main__":
    main()