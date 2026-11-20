"""
providers/uccx_provider.py

Cisco Unified Contact Center Express (UCCX 12.0/12.5) REST API Provider.
Queries the UCCX Admin REST API (/adminapi/) to extract CSQs, Skills,
Agents (Resources), Teams, Applications, CTI Triggers, and Prompt Repository Files.
Includes backward-compatible timeout tuple mapping and fast Layer 4 reachability probes.
"""

import logging
import os
import socket
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import quote
import requests
from requests.auth import HTTPBasicAuth
from requests.exceptions import RequestException, ConnectTimeout, ReadTimeout
import urllib3

logger = logging.getLogger("UCCX_Provider")


class UCCXAPIError(Exception):
    """Base exception for UCCX REST API failures."""


class UCCXConnectionError(UCCXAPIError):
    """Raised when the remote UCCX node is offline or unreachable."""


class UCCXProvider:
    """
    REST API Provider for Cisco UCCX 12.0 / 12.5 Administration Engine.
    """

    def __init__(
        self,
        host: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        verify_ssl: Optional[bool] = None,
        timeout: Optional[Union[int, float, Tuple[float, float]]] = None,
        connect_timeout: float = 3.0,
        read_timeout: Optional[float] = None
    ):
        self.host = (host or os.getenv("UCCX_HOST", "")).strip()
        self.username = username or os.getenv("UCCX_USER", "")
        self.password = password or os.getenv("UCCX_PASS", "")

        if verify_ssl is not None:
            self.verify_ssl = verify_ssl
        else:
            env_verify = os.getenv("UCCX_VERIFY_SSL", "false").strip().lower()
            self.verify_ssl = env_verify in {"1", "true", "yes", "y"}

        # Normalize timeout parameter into requests-compatible (connect_timeout, read_timeout) tuple
        if timeout is not None:
            if isinstance(timeout, tuple):
                self.timeout: Tuple[float, float] = timeout
            else:
                self.timeout = (connect_timeout, float(timeout))
        else:
            env_timeout = float(os.getenv("UCCX_TIMEOUT_SECONDS", "30"))
            self.timeout = (connect_timeout, read_timeout or env_timeout)

        self.base_url = f"https://{self.host}:8443/adminapi"

        if not self.host or not self.username or not self.password:
            logger.warning("UCCX Provider initialized with missing host or credentials.")

        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(self.username, self.password)
        self.session.verify = self.verify_ssl

        if not self.verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        self.session.headers.update({
            "Accept": "application/xml",
            "Content-Type": "application/xml"
        })

    def is_reachable(self, port: int = 8443, timeout_sec: float = 2.0) -> bool:
        """Lightweight Layer 4 TCP probe to avoid blocking HTTP timeouts."""
        if not self.host:
            return False
        try:
            with socket.create_connection((self.host, port), timeout=timeout_sec):
                return True
        except (socket.timeout, OSError):
            return False

    def _request(self, endpoint: str) -> str:
        """Executes HTTP GET requests against UCCX Admin REST API with fast-fail handling."""
        if not self.host:
            raise UCCXConnectionError("UCCX Host IP is not configured.")

        url = f"{self.base_url}{endpoint}" if endpoint.startswith("/") else f"{self.base_url}/{endpoint}"

        try:
            response = self.session.get(url, timeout=self.timeout)
            if response.status_code != 200:
                logger.error("UCCX API Error HTTP %s [%s]: %s", response.status_code, url, response.text[:200])
                raise UCCXAPIError(f"HTTP {response.status_code}: {response.text}")

            return response.text

        except ConnectTimeout as exc:
            logger.error("Connect timeout to UCCX node at %s:8443 (%s)", self.host, exc)
            raise UCCXConnectionError(
                f"UCCX node at {self.host}:8443 is unreachable (Connection timed out after {self.timeout[0]}s). "
                f"Verify network routing, VPN status, or firewall rules."
            ) from exc

        except ReadTimeout as exc:
            logger.error("Read timeout from UCCX node at %s:8443 (%s)", self.host, exc)
            raise UCCXAPIError(f"UCCX node at {self.host} accepted the connection but timed out responding.") from exc

        except RequestException as exc:
            logger.exception("Network transport failure calling UCCX API [%s]: %s", url, exc)
            raise UCCXAPIError(f"UCCX Transport Failure: {str(exc)}") from exc

    def get_csqs(self) -> List[Dict[str, Any]]:
        """Extracts all Contact Service Queues (CSQs)."""
        xml_data = self._request("/csq")
        csq_list: List[Dict[str, Any]] = []

        try:
            root = ET.fromstring(xml_data)
            self._strip_namespaces(root)

            for csq in root.findall(".//csq"):
                csq_list.append({
                    "id": csq.findtext("id") or csq.findtext("csqId") or "",
                    "name": csq.findtext("name") or csq.findtext("csqName") or "Unknown",
                    "type": csq.findtext("csqType") or csq.findtext("type") or "VOICE",
                    "algorithm": csq.findtext("queueAlgorithm") or "FIFO",
                    "ref_url": csq.attrib.get("self", "")
                })

            return csq_list

        except ET.ParseError as exc:
            logger.error("Failed parsing UCCX CSQ XML response: %s", exc)
            raise UCCXAPIError("Invalid XML response from UCCX CSQ API") from exc

    def get_csq_detail(self, csq_id_or_name: str) -> Dict[str, Any]:
        """Extracts detailed configuration for a single CSQ by numerical ID or name."""
        encoded_identifier = quote(str(csq_id_or_name), safe="")
        xml_data = self._request(f"/csq/{encoded_identifier}")
        try:
            root = ET.fromstring(xml_data)
            self._strip_namespaces(root)

            skills = []
            for skill in root.findall(".//skillCsq/skillCompetenceWeight"):
                skills.append({
                    "skill_name": skill.findtext("skillName", ""),
                    "weight": skill.findtext("competenceWeight", "1")
                })

            return {
                "id": root.findtext("id") or root.findtext("csqId") or csq_id_or_name,
                "name": root.findtext("name") or root.findtext("csqName") or str(csq_id_or_name),
                "type": root.findtext("csqType") or root.findtext("type") or "VOICE",
                "algorithm": root.findtext("queueAlgorithm", "FIFO"),
                "csq_selection_criteria": root.findtext("csqSelectionCriteria", "LONGEST_AVAILABLE"),
                "skills": skills
            }

        except ET.ParseError as exc:
            logger.error("Failed parsing UCCX CSQ Detail XML for ID %s: %s", csq_id_or_name, exc)
            return {"id": csq_id_or_name, "name": str(csq_id_or_name), "skills": []}

    def get_skills(self) -> List[Dict[str, str]]:
        """Extracts all Skill definitions."""
        xml_data = self._request("/skill")
        skills: List[Dict[str, str]] = []

        try:
            root = ET.fromstring(xml_data)
            self._strip_namespaces(root)

            for skill in root.findall(".//skill"):
                skills.append({
                    "skill_name": skill.findtext("skillName") or skill.findtext("name") or "Unknown",
                    "skill_id": skill.findtext("skillId") or skill.findtext("id") or ""
                })

            return skills

        except ET.ParseError as exc:
            logger.error("Failed parsing UCCX Skill XML response: %s", exc)
            raise UCCXAPIError("Invalid XML response from UCCX Skill API") from exc

    def get_resources(self) -> List[Dict[str, Any]]:
        """Extracts all Agent (Resource) profiles."""
        xml_data = self._request("/resource")
        resources: List[Dict[str, Any]] = []

        try:
            root = ET.fromstring(xml_data)
            self._strip_namespaces(root)

            for res in root.findall(".//resource"):
                resources.append({
                    "user_id": res.findtext("userID") or res.findtext("id") or "Unknown",
                    "first_name": res.findtext("firstName", ""),
                    "last_name": res.findtext("lastName", ""),
                    "extension": res.findtext("extension", "N/A"),
                    "type": res.findtext("type", "AGENT")
                })

            return resources

        except ET.ParseError as exc:
            logger.error("Failed parsing UCCX Resource XML response: %s", exc)
            raise UCCXAPIError("Invalid XML response from UCCX Resource API") from exc

    def get_applications(self) -> List[Dict[str, Any]]:
        """Extracts Application scripts and CTI Trigger Route Points."""
        xml_data = self._request("/application")
        apps: List[Dict[str, Any]] = []

        try:
            root = ET.fromstring(xml_data)
            self._strip_namespaces(root)

            for app in root.findall(".//application"):
                triggers = []
                for trig in app.findall(".//trigger"):
                    triggers.append({
                        "directory_number": trig.findtext("directoryNumber", ""),
                        "locale": trig.findtext("locale", "en_US"),
                        "enabled": trig.findtext("enabled", "true")
                    })

                apps.append({
                    "name": app.findtext("applicationName") or app.findtext("name") or "Unknown",
                    "type": app.findtext("type", "SCRIPT"),
                    "script_name": app.findtext("script", "N/A"),
                    "max_sessions": app.findtext("maxSessions", "0"),
                    "triggers": triggers
                })

            return apps

        except ET.ParseError as exc:
            logger.error("Failed parsing UCCX Application XML response: %s", exc)
            raise UCCXAPIError("Invalid XML response from UCCX Application API") from exc

    def get_scripts(self) -> List[Dict[str, Any]]:
        """Lists scripts from a configured UCCX script endpoint.

        UCCX releases and deployments expose script repositories differently, so
        the endpoint is deliberately configurable instead of assuming a single
        undocumented Admin API resource.
        """
        endpoint = os.getenv("UCCX_SCRIPT_LIST_ENDPOINT", "").strip()
        if not endpoint:
            raise UCCXAPIError(
                "UCCX_SCRIPT_LIST_ENDPOINT is not configured. Export scripts from UCCX "
                "or configure the supported script inventory endpoint."
            )

        xml_data = self._request(endpoint)
        try:
            root = ET.fromstring(xml_data)
            self._strip_namespaces(root)
            scripts: List[Dict[str, Any]] = []
            for item in root.iter():
                name = (
                    item.findtext("name")
                    or item.findtext("scriptName")
                    or item.findtext("fileName")
                    or ""
                ).strip()
                if name and name.lower().endswith((".aef", ".xml", ".json")):
                    scripts.append({"name": name, "path": item.attrib.get("path", name)})
            return scripts
        except ET.ParseError as exc:
            logger.error("Failed parsing UCCX script inventory XML: %s", exc)
            raise UCCXAPIError("Invalid XML response from UCCX script inventory API") from exc

    def download_script(self, script_path: str) -> str:
        """Downloads a text script export from a configured UCCX endpoint."""
        endpoint_template = os.getenv("UCCX_SCRIPT_DOWNLOAD_ENDPOINT", "").strip()
        if not endpoint_template:
            raise UCCXAPIError(
                "UCCX_SCRIPT_DOWNLOAD_ENDPOINT is not configured. Provide a script export "
                "or configure the supported download endpoint."
            )

        endpoint = endpoint_template.format(path=quote(script_path.strip("/"), safe=""))
        return self._request(endpoint)

    def list_folder_contents(self, folder_path: str) -> Dict[str, List[str]]:
        """
        Queries UCCX REST API for a folder path and returns both .wav files and child subfolders.
        Enforces trailing slash and strips XML namespaces to guarantee node resolution.
        """
        clean_path = folder_path.strip("/")
        if clean_path.startswith("user/"):
            clean_path = clean_path[5:]

        request_path = f"/prompt/{clean_path}/" if clean_path else "/prompt/"
        xml_data = self._request(request_path)

        files: List[str] = []
        subfolders: List[str] = []

        try:
            root = ET.fromstring(xml_data)
            self._strip_namespaces(root)

            for elem in root.iter():
                text_val = (elem.text or "").strip()
                tag_name = elem.tag.lower()

                file_name = (
                    elem.findtext("promptName")
                    or elem.findtext("fileName")
                    or elem.findtext("name")
                    or (text_val if text_val.endswith(".wav") else "")
                ).strip()

                if file_name.endswith(".wav"):
                    files.append(file_name)

                if "folder" in tag_name or "dir" in tag_name:
                    folder_name = (
                        elem.findtext("folderName")
                        or elem.findtext("dirName")
                        or elem.findtext("name")
                        or (text_val if text_val and not text_val.endswith(".wav") else "")
                    ).strip()

                    if folder_name and folder_name.lower() not in ["prompts", "prompt", clean_path.lower()]:
                        if not folder_name.endswith(".wav"):
                            subfolders.append(folder_name)

            if not files and not subfolders:
                for elem in root.iter():
                    val = (elem.text or "").strip()
                    if val.endswith(".wav"):
                        files.append(val)
                    elif val and "/" not in val and "." not in val and val.lower() not in ["prompts", "prompt", clean_path.lower()]:
                        subfolders.append(val)

            return {
                "files": sorted(list(set(f for f in files if f and f.endswith(".wav")))),
                "subfolders": sorted(list(set(s for s in subfolders if s and not s.endswith(".wav"))))
            }

        except ET.ParseError as exc:
            logger.error("Failed parsing UCCX folder XML for path '%s': %s", folder_path, exc)
            return {"files": [], "subfolders": []}

    def discover_prompts_recursively(self, base_folder: str) -> List[Dict[str, str]]:
        """Recursively traverses a UCCX prompt repository tree starting from base_folder."""
        all_discovered_files: List[Dict[str, str]] = []

        clean_base = base_folder.strip("/")
        if clean_base.startswith("user/"):
            clean_base = clean_base[5:]

        folders_to_crawl = [clean_base]

        while folders_to_crawl:
            current_folder = folders_to_crawl.pop(0)
            logger.info("Scanning UCCX Repository Path: /adminapi/prompt/%s/", current_folder)

            contents = self.list_folder_contents(current_folder)

            for file_name in contents["files"]:
                all_discovered_files.append({
                    "file_name": file_name,
                    "folder_path": current_folder,
                    "relative_file_path": f"{current_folder}/{file_name}"
                })

            for subfolder in contents["subfolders"]:
                child_path = f"{current_folder}/{subfolder}"
                folders_to_crawl.append(child_path)

        return all_discovered_files

    def download_prompt_bytes(self, relative_path: str) -> bytes:
        """Downloads the raw binary audio stream (.wav) for a specific prompt file path."""
        clean_path = relative_path.strip("/")
        if clean_path.startswith("user/"):
            clean_path = clean_path[5:]

        url = f"{self.base_url}/prompt/{clean_path}"
        headers = {"Accept": "audio/x-wav, audio/wav, application/octet-stream, */*"}

        try:
            response = self.session.get(url, headers=headers, timeout=self.timeout, stream=True)

            if response.status_code == 404:
                url_fallback = f"{self.base_url}/prompt/download/{clean_path}"
                logger.info("Retrying prompt download via download path: %s", url_fallback)
                response = self.session.get(url_fallback, headers=headers, timeout=self.timeout, stream=True)

            if response.status_code != 200:
                logger.error("Failed downloading prompt HTTP %s [%s]", response.status_code, url)
                raise UCCXAPIError(f"HTTP {response.status_code} downloading prompt: {response.text[:200]}")

            return response.content

        except RequestException as exc:
            logger.exception("Transport failure downloading prompt audio [%s]: %s", url, exc)
            raise UCCXAPIError(f"UCCX Audio Transport Error: {str(exc)}") from exc

    @staticmethod
    def _strip_namespaces(root: ET.Element) -> None:
        """Strips XML namespaces in-place across the ElementTree DOM."""
        for elem in root.iter():
            if '}' in elem.tag:
                elem.tag = elem.tag.split('}', 1)[1]