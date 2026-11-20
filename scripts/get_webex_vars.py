# get_webex_vars.py
import os
import requests
from dotenv import load_dotenv
from core.config import get_settings

load_dotenv()
settings = get_settings()

token = os.getenv("WEBEX_ACCESS_TOKEN") or settings.WEBEX_ACCESS_TOKEN
if not token:
    print("❌ Please set WEBEX_ACCESS_TOKEN in your .env file first!")
    exit(1)

headers = {
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/json"
}

base_url = "https://webexapis.com/v1"

print("=== Fetching Webex Control Hub Environment Variables ===\n")

# 1. Fetch Organization ID
try:
    res = requests.get(f"{base_url}/organizations", headers=headers, timeout=10)
    res.raise_for_status()
    orgs = res.json().get("items", [])
    if orgs:
        print(f"WEBEX_ORG_ID={orgs[0]['id']}")
        print(f"Organization Name: {orgs[0]['displayName']}\n")
except Exception as e:
    print(f"❌ Failed to fetch Organization ID: {e}")

# 2. Fetch Locations
try:
    res = requests.get(f"{base_url}/locations", headers=headers, timeout=10)
    res.raise_for_status()
    locations = res.json().get("items", [])
    print("--- Discovered Locations ---")
    for loc in locations:
        print(f"Location Name: {loc['name']}")
        print(f"WEBEX_DEFAULT_LOCATION_ID={loc['id']}\n")
except Exception as e:
    print(f"❌ Failed to fetch Locations: {e}")

# 3. Fetch Calling Licenses
try:
    res = requests.get(f"{base_url}/licenses", headers=headers, timeout=10)
    res.raise_for_status()
    licenses = res.json().get("items", [])
    print("--- Discovered Licenses ---")
    for lic in licenses:
        name = lic.get("name", "")
        if "calling" in name.lower() or "professional" in name.lower() or "workstation" in name.lower():
            print(f"License Name: {name}")
            print(f"WEBEX_CALLING_LICENSE_ID={lic['id']}\n")
except Exception as e:
    print(f"❌ Failed to fetch Licenses: {e}")