import os
from dotenv import load_dotenv
from core.config import get_settings
from providers.ldap_provider import LDAPProvider

# Load your .env file
load_dotenv()
settings = get_settings()

print("--- LDAP Sandbox Tester ---")
print(f"LDAP Server URI: {settings.LDAP_SERVER_URI}")
print(f"LDAP Domain: {settings.LDAP_DOMAIN}")
print(f"LDAP Search Base: {settings.LDAP_SEARCH_BASE}")
print(f"Service Account: {os.getenv('LDAP_BIND_USER') or settings.LDAP_BIND_USER}")

# Instantiate the provider
ldap = LDAPProvider()

# Replace with your actual username and password to test
test_user = input("Enter your username to test (e.g. jdoe): ")
test_pass = input("Enter your password: ")

print("\nAttempting authentication pipeline...")
success = ldap.authenticate(test_user, test_pass)

if success:
    print("\n✅ SUCCESS! LDAP bind and search completed perfectly.")
else:
    print("\n❌ FAILED! Check the terminal logs above for the exact error.")