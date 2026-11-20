# test_webex_provider.py
import logging
from dotenv import load_dotenv
from providers.webex_calling_provider import WebexCallingProvider, WebexAPIError

logging.basicConfig(level=logging.INFO)
load_dotenv()

def main():
    print("=== Testing Webex Calling Provider Connectivity ===")
    try:
        provider = WebexCallingProvider()
        print(f"Target Org ID: {provider.org_id}")

        # Step 1: Query Locations via Provider
        print("\nQuerying Control Hub Locations via Provider...")
        locations = provider.get_locations()
        print(f"✅ Successfully retrieved {len(locations)} locations from Control Hub.")

        # Step 2: Test Person Lookup (using a sample email query)
        test_email = "louis.gonzales@corp.internal"
        print(f"\nTesting User Lookup for '{test_email}'...")
        person = provider.get_person_by_email(test_email)
        
        if person:
            print(f"✅ Discovered Person: {person.get('displayName')} (ID: {person.get('id')})")
        else:
            print(f"ℹ️ User '{test_email}' not found in Control Hub (Verify Directory Sync/SCIM status).")

    except WebexAPIError as exc:
        print(f"❌ Webex Provider API Error: {exc}")
    except Exception as exc:
        print(f"❌ General Failure: {exc}")

if __name__ == "__main__":
    main()
