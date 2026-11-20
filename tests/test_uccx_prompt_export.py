# tests/test_uccx_prompt_export.py
import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
from providers.uccx_provider import UCCXProvider
from services.uccx_prompt_export_service import UCCXPromptExportService

load_dotenv(dotenv_path=PROJECT_ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s"
)
logger = logging.getLogger("Test_UCCX_Prompt_Export")


def main():
    print("=== Testing UCCX Multi-Language Recursive Prompt Exporter ===")
    try:
        uccx_provider = UCCXProvider()
        print(f"Connecting to UCCX Repository: https://{uccx_provider.host}:8443/adminapi/")

        prompt_service = UCCXPromptExportService(uccx_provider=uccx_provider)
        
        # Recursively crawl and export 'en_US' and 'es_US' repository trees
        summary = prompt_service.export_prompts_recursively(
            target_language_folders=["en_US", "es_US"],
            output_dir="prompts/uccx_export"
        )

        print("\n=== PROMPT RECURSIVE EXPORT SUMMARY ===")
        print(f"Languages Crawled: {summary.languages_processed}")
        print(f"Total Discovered: {summary.total_discovered}")
        print(f"Total Downloaded: {summary.total_downloaded}")
        print(f"Total Failed: {summary.total_failed}")
        print(f"Total Size Downloaded: {summary.total_megabytes_downloaded} MB")
        print(f"Export Root Directory: {summary.export_directory}")
        print(f"WxCC Over-size Alerts (>5MB): {summary.wxcc_non_compliant_count}")

        if summary.prompts:
            print("\nSample Exported Prompt Record:")
            print(json.dumps(summary.prompts[0].model_dump(), indent=2))

    except Exception as e:
        logger.exception("UCCX Prompt Export Execution Failed")
        print(f"\n❌ UCCX Prompt Export Failed: {e}")


if __name__ == "__main__":
    main()