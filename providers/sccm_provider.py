import json
import logging
import os
import re
import subprocess
from typing import Dict, Any, Optional, List

from core.config import get_settings

logger = logging.getLogger("SCCM_Provider")


class SCCMProvider:
    """
    Integrates with Microsoft SCCM (ConfigMgr) and Active Directory for automated device removal
    using the approved Windows automation script patterns.
    """

    def __init__(
        self,
        site_server: Optional[str] = None,
        site_code: Optional[str] = None,
        log_path: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
    ):
        settings = get_settings()
        self.site_server = (
            site_server
            or os.getenv("SCCM_SITE_SERVER")
            or settings.SCCM_SITE_SERVER
        ).strip()
        self.site_code = (
            site_code
            or os.getenv("SCCM_SITE_CODE")
            or settings.SCCM_SITE_CODE
        ).strip()
        self.log_path = (
            log_path
            or os.getenv("SCCM_LOG_PATH")
            or settings.SCCM_LOG_PATH
        ).strip()
        raw_timeout = os.getenv("SCCM_TIMEOUT_SECONDS")
        self.timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else (int(raw_timeout) if raw_timeout is not None else settings.SCCM_TIMEOUT_SECONDS)
        )
        self.script_path = os.path.abspath("scripts/remove_device_from_sccm.ps1")

    def remove_device(
        self,
        device_name: Optional[str] = None,
        mac_address: Optional[str] = None,
        operator_id: str = "",
        remove_sccm: bool = True,
        remove_ad: bool = True,
        credential_user: Optional[str] = None,
        credential_pass: Optional[str] = None,
        device_names: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Removes a workstation from SCCM SMS Provider WMI and/or Active Directory.
        Supports single device removal and bulk device list processing in a single PowerShell invocation.
        Optionally accepts step-up credentials to authenticate WMI and AD with elevated rights.
        """
        clean_device = (device_name or "").strip()
        clean_mac = (mac_address or "").strip()
        clean_operator = (operator_id or "").strip()

        # Resolve target devices list
        target_devices: List[str] = []
        if device_names:
            target_devices = [d.strip() for d in device_names if d and d.strip()]
        elif clean_device and any(sep in clean_device for sep in (",", "\n", "\r")):
            target_devices = [d.strip() for d in re.split(r"[\r\n,]+", clean_device) if d.strip()]

        if not target_devices and clean_device:
            target_devices = [clean_device]

        if not target_devices and not clean_mac:
            return {
                "success": False,
                "error": "Either DeviceName or MacAddress must be supplied for removal.",
            }

        if not os.path.exists(self.script_path):
            logger.error("SCCM removal script missing: %s", self.script_path)
            raise FileNotFoundError(
                f"Required SCCM removal script not found at {self.script_path}"
            )

        first_dev = target_devices[0] if target_devices else ""
        command = [
            "powershell.exe",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            self.script_path,
            "-DeviceName",
            first_dev,
            "-MacAddress",
            clean_mac,
            "-OperatorId",
            clean_operator,
            "-RemoveSccm",
            "true" if remove_sccm else "false",
            "-RemoveAd",
            "true" if remove_ad else "false",
            "-SiteServer",
            self.site_server,
            "-SiteCode",
            self.site_code,
            "-LogPath",
            self.log_path,
        ]

        payload_dict = {}
        if credential_user and credential_pass:
            payload_dict["username"] = credential_user.strip()
            payload_dict["password"] = credential_pass
        if len(target_devices) > 1:
            payload_dict["devices"] = target_devices

        input_payload = None
        if payload_dict:
            command.append("-UseStdinCredentials")
            input_payload = json.dumps(payload_dict)

        timeout = self.timeout_seconds
        if len(target_devices) > 1:
            timeout = max(self.timeout_seconds, len(target_devices) * 8)

        logger.info(
            "Executing SCCM removal script for Devices=%s (Count=%d), MAC='%s', Operator='%s', HasStepUpCreds=%s, Timeout=%ds",
            target_devices[:5] if len(target_devices) > 5 else target_devices,
            len(target_devices),
            clean_mac,
            clean_operator,
            bool(credential_user and credential_pass),
            timeout,
        )

        try:
            settings = get_settings()
            sub_env = os.environ.copy()
            if settings.LDAP_DOMAIN and "LDAP_DOMAIN" not in sub_env:
                sub_env["LDAP_DOMAIN"] = settings.LDAP_DOMAIN
            if settings.AD_NETBIOS_DOMAIN and "AD_NETBIOS_DOMAIN" not in sub_env:
                sub_env["AD_NETBIOS_DOMAIN"] = settings.AD_NETBIOS_DOMAIN

            process = subprocess.run(
                command,
                input=input_payload,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=sub_env,
            )

            stdout = process.stdout.strip()
            stderr = process.stderr.strip()

            if stderr:
                logger.warning("PowerShell stderr from SCCM script: %s", stderr)

            if stdout:
                # Find the JSON block in stdout (in case PowerShell printed any warnings or headers)
                json_start = stdout.find("{")
                json_end = stdout.rfind("}")
                if json_start != -1 and json_end != -1 and json_end > json_start:
                    parsed = json.loads(stdout[json_start : json_end + 1])
                    return parsed

            if process.returncode != 0:
                return {
                    "success": False,
                    "error": f"Script failed with code {process.returncode}: {stderr or stdout}",
                }

            return {
                "success": False,
                "error": "No structured JSON output received from removal script.",
            }

        except subprocess.TimeoutExpired:
            logger.error("SCCM removal script timed out after %s seconds", timeout)
            return {
                "success": False,
                "error": f"SCCM script execution timed out after {timeout} seconds.",
            }
        except Exception as exc:
            logger.exception("Failed to execute SCCM removal script: %s", exc)
            return {
                "success": False,
                "error": str(exc),
            }
