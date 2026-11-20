# verify_environment.py
import os
import redis
import requests
import urllib3
from dotenv import load_dotenv

load_dotenv(override=True)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def check_redis():
    try:
        r = redis.from_url(os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"))
        r.ping()
        print("[+] Redis Broker: ONLINE")
    except Exception as e:
        print(f"[-] Redis Broker: OFFLINE ({e})")

def check_cucm():
    host = os.getenv("CUCM_AXL_HOST", os.getenv("CUCM_PAN_IP", ""))
    user = os.getenv("CUCM_AXL_USER", "")
    pwd = os.getenv("CUCM_AXL_PASS", "")
    if not host:
        print("[-] CUCM: Host not defined in .env")
        return
    try:
        res = requests.get(f"https://{host}:8443/axl/", auth=(user, pwd), verify=False, timeout=5)
        print(f"[+] CUCM AXL (12.5 SU1): REACHABLE (HTTP {res.status_code})")
    except Exception as e:
        print(f"[-] CUCM AXL: UNREACHABLE ({e})")

def check_webex():
    token = os.getenv("WEBEX_ACCESS_TOKEN", "")
    if not token:
        print("[-] Webex: Access token missing in .env")
        return
    try:
        headers = {"Authorization": f"Bearer {token}"}
        res = requests.get("https://webexapis.com/v1/locations?max=1", headers=headers, timeout=5)
        print(f"[+] Webex Calling API: AUTHENTICATED (HTTP {res.status_code})")
    except Exception as e:
        print(f"[-] Webex Calling API: FAILED ({e})")

if __name__ == "__main__":
    print("--- NetDashboard Toolchain Readiness Check ---")
    check_redis()
    check_cucm()
    check_webex()