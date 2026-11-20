# verify_clusters.py
import os
import sys
from dotenv import load_dotenv

load_dotenv(override=True)

from providers.telecom_factory import TelecomClusterRegistry, ClusterEnvironment


def verify_cluster(cluster_env: ClusterEnvironment):
    print(f"\n========================================================")
    print(f" TESTING {cluster_env.value} CLUSTER CONNECTIVITY")
    print(f"========================================================")

    # 1. Verify CUCM AXL
    try:
        print(f"[*] Testing CUCM AXL ({cluster_env.value})...", end="", flush=True)
        axl = TelecomClusterRegistry.get_cucm_provider(cluster_env)
        # Query 1 row from processnode table to verify DB authentication
        res = axl.execute_sql_query("SELECT FIRST 1 name, description FROM processnode")
        print(f" [PASS] (Connected to host: {axl.host}, ver: {axl.version})")
    except Exception as e:
        print(f" [FAIL]: {e}")

    # 2. Verify UCCX REST
    try:
        print(f"[*] Testing UCCX REST ({cluster_env.value})...", end="", flush=True)
        uccx = TelecomClusterRegistry.get_uccx_provider(cluster_env)
        csqs = uccx.get_csqs()
        print(f" [PASS] (Discovered {len(csqs)} CSQs on {uccx.host})")
    except Exception as e:
        print(f" [FAIL]: {e}")

    # 3. Verify CUC CUPI REST
    try:
        print(f"[*] Testing CUC CUPI ({cluster_env.value})...", end="", flush=True)
        cupi = TelecomClusterRegistry.get_cupi_provider(cluster_env)
        users = cupi.get_users_paginated(page_size=1)
        print(f" [PASS] (Connected to {cupi.host})")
    except Exception as e:
        print(f" [FAIL]: {e}")


if __name__ == "__main__":
    verify_cluster(ClusterEnvironment.PRIMARY)
    verify_cluster(ClusterEnvironment.HAWAII)# verify_clusters.py
import os
import sys
from dotenv import load_dotenv

load_dotenv(override=True)

from providers.telecom_factory import TelecomClusterRegistry, ClusterEnvironment


def verify_cluster(cluster_env: ClusterEnvironment):
    print(f"\n========================================================")
    print(f" TESTING {cluster_env.value} CLUSTER CONNECTIVITY")
    print(f"========================================================")

    # 1. Verify CUCM AXL
    try:
        print(f"[*] Testing CUCM AXL ({cluster_env.value})...", end="", flush=True)
        axl = TelecomClusterRegistry.get_cucm_provider(cluster_env)
        # Query 1 row from processnode table to verify DB authentication
        res = axl.execute_sql_query("SELECT FIRST 1 name, description FROM processnode")
        print(f" [PASS] (Connected to host: {axl.host}, ver: {axl.version})")
    except Exception as e:
        print(f" [FAIL]: {e}")

    # 2. Verify UCCX REST
    try:
        print(f"[*] Testing UCCX REST ({cluster_env.value})...", end="", flush=True)
        uccx = TelecomClusterRegistry.get_uccx_provider(cluster_env)
        csqs = uccx.get_csqs()
        print(f" [PASS] (Discovered {len(csqs)} CSQs on {uccx.host})")
    except Exception as e:
        print(f" [FAIL]: {e}")

    # 3. Verify CUC CUPI REST
    try:
        print(f"[*] Testing CUC CUPI ({cluster_env.value})...", end="", flush=True)
        cupi = TelecomClusterRegistry.get_cupi_provider(cluster_env)
        users = cupi.get_users_paginated(page_size=1)
        print(f" [PASS] (Connected to {cupi.host})")
    except Exception as e:
        print(f" [FAIL]: {e}")


if __name__ == "__main__":
    verify_cluster(ClusterEnvironment.PRIMARY)
    verify_cluster(ClusterEnvironment.HAWAII)