# tests/test_uccx_audit.py
import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
from providers.uccx_provider import UCCXProvider
from services.uccx_migration_service import UCCXMigrationService

load_dotenv(dotenv_path=PROJECT_ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s"
)
logger = logging.getLogger("Test_UCCX_Audit")


def main():
    print("=== Testing UCCX 12.5 SU3 Extraction & WxCC Mapping Engine ===")
    try:
        uccx_provider = UCCXProvider()
        print(f"Connecting to UCCX Engine: https://{uccx_provider.host}:8443/adminapi/")

        migration_service = UCCXMigrationService(uccx_provider=uccx_provider)
        summary = migration_service.run_full_contact_center_audit(default_country_code="+1")

        print("\n=== UCCX CONTACT CENTER AUDIT RESULTS ===")
        print(f"Total CSQs: {summary.total_csqs}")
        print(f"Total Skills: {summary.total_skills}")
        print(f"Total Agents: {summary.total_agents}")
        print(f"Total Applications/Scripts: {summary.total_applications}")

        if summary.queues:
            print("\nSample WxCC Queue Payload:")
            print(json.dumps(summary.queues[0].model_dump(), indent=2))

        if summary.entry_points:
            print("\nSample WxCC Entry Point Payload:")
            print(json.dumps(summary.entry_points[0].model_dump(), indent=2))

    except Exception as e:
        logger.exception("UCCX Audit Test Execution Failed")
        print(f"\n❌ UCCX Audit Test Failed: {e}")


if __name__ == "__main__":
    main()
