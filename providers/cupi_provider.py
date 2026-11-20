
"""
providers/cupi_provider.py

High-Performance Cisco Unity Connection (CUC 14 SU4) CUPI REST Provider.
Handles pagination, JSON structure normalization, and binary WAV audio stream downloads.
"""

import logging
import os
import re
from typing import Any, Dict, List, Optional
import requests
from requests.auth import HTTPBasicAuth
from requests.exceptions import RequestException

logger = logging.getLogger("CUC_CUPI_Provider")


class CUPIProviderError(Exception):
    """Base exception for CUC CUPI provider failures."""


class CUPIRequestError(CUPIProviderError):
    """Raised when CUPI returns an HTTP error or malformed payload."""


class CUPIProvider:
    """
    REST Client for Cisco Unity Connection (CUC) CUPI API (/vmrest).
    """

    def __init__(
        self,
        host: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        verify_ssl: Optional[bool] = None,
        timeout: Optional[int] = None
    ):
        self.host = host or os.getenv("CUC_HOST", "")
        self.username = username or os.getenv("CUC_USER", "")
        self.password = password or os.getenv("CUC_PASS", "")

        if verify_ssl is not None:
            self.verify_ssl = verify_ssl
        else:
            env_verify = os.getenv("CUC_VERIFY_SSL", "false").strip().lower()
            self.verify_ssl = env_verify in {"1", "true", "yes", "y"}

        self.timeout = timeout or int(os.getenv("CUC_TIMEOUT_SECONDS", "30"))
        self.base_url = f"https://{self.host}:8443/vmrest"

        if not self.host or not self.username or not self.password:
            logger.warning("CUC CUPI Provider initialized with missing host or credentials.")

        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(self.username, self.password)
        self.session.verify = self.verify_ssl

        if not self.verify_ssl:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json"
        })

    def _request(self, method: str, endpoint: str, **kwargs: Any) -> Dict[str, Any]:
        """Executes REST calls against CUPI API endpoints."""
        url = f"{self.base_url}{endpoint}" if endpoint.startswith("/") else f"{self.base_url}/{endpoint}"

        try:
            response = self.session.request(method=method, url=url, timeout=self.timeout, **kwargs)

            if response.status_code >= 400:
                logger.error("CUPI Request failed HTTP %s [%s]: %s", response.status_code, url, response.text[:200])
                raise CUPIRequestError(f"HTTP {response.status_code}: {response.text}")

            if response.status_code == 204 or not response.text:
                return {}

            return response.json()

        except RequestException as exc:
            logger.exception("Transport error calling CUC CUPI [%s]: %s", url, exc)
            raise CUPIRequestError(f"CUPI Transport Failure: {str(exc)}") from exc

    @staticmethod
    def _ensure_list(data: Any, key: str) -> List[Dict[str, Any]]:
        """
        Normalizes CUPI JSON objects.
        CUPI returns a dict for single-item results and a list for multi-item results.
        """
        if not data or not isinstance(data, dict):
            return []

        container = data.get(key)
        if container is None:
            return []

        if isinstance(container, list):
            return [item for item in container if isinstance(item, dict)]
        elif isinstance(container, dict):
            return [container]

        return []

    def get_users_paginated(self, page_size: int = 100) -> List[Dict[str, Any]]:
        """Retrieves all user voicemail profiles with pagination."""
        all_users: List[Dict[str, Any]] = []
        page = 1

        while True:
            endpoint = f"/users?rowsPerPage={page_size}&pageNumber={page}"
            logger.info("Fetching CUPI Users page %d (limit %d)...", page, page_size)
            
            res = self._request("GET", endpoint)
            page_users = self._ensure_list(res, "User")

            if not page_users:
                break

            all_users.extend(page_users)
            if len(page_users) < page_size:
                break

            page += 1

        return all_users

    def get_call_handlers_paginated(self, page_size: int = 100) -> List[Dict[str, Any]]:
        """Retrieves all System Call Handlers with pagination."""
        all_handlers: List[Dict[str, Any]] = []
        page = 1

        while True:
            endpoint = f"/handlers/callhandlers?rowsPerPage={page_size}&pageNumber={page}"
            logger.info("Fetching CUPI Call Handlers page %d (limit %d)...", page, page_size)

            res = self._request("GET", endpoint)
            page_handlers = self._ensure_list(res, "Callhandler")

            if not page_handlers:
                break

            all_handlers.extend(page_handlers)
            if len(page_handlers) < page_size:
                break

            page += 1

        return all_handlers

    def get_call_handler_menu_entries(self, handler_object_id: str) -> List[Dict[str, Any]]:
        """Retrieves DTMF Key Action mappings for a specific Call Handler."""
        endpoint = f"/handlers/callhandlers/{handler_object_id}/menuentries"
        res = self._request("GET", endpoint)
        return self._ensure_list(res, "MenuEntry")

    def get_call_handler_greetings(self, handler_object_id: str) -> List[Dict[str, Any]]:
        """Retrieves greeting configurations (Standard, Closed, Holiday) for a Call Handler."""
        endpoint = f"/handlers/callhandlers/{handler_object_id}/greetings"
        res = self._request("GET", endpoint)
        return self._ensure_list(res, "Greeting")

    def download_greeting_audio_file(
        self,
        handler_object_id: str,
        greeting_type: str,
        destination_path: str
    ) -> bool:
        """
        Downloads the raw WAV audio stream for a Call Handler greeting.
        Endpoint: /vmrest/handlers/callhandlers/{objectId}/greetings/{greetingType}/greetingstreamfiles/1/audio
        """
        endpoint = f"/handlers/callhandlers/{handler_object_id}/greetings/{greeting_type}/greetingstreamfiles/1/audio"
        url = f"{self.base_url}{endpoint}"

        headers = {"Accept": "audio/wav"}

        try:
            response = self.session.get(url, headers=headers, timeout=self.timeout, stream=True)
            if response.status_code != 200:
                logger.warning("No custom audio stream available for Handler %s [%s] (HTTP %s)", handler_object_id, greeting_type, response.status_code)
                return False

            os.makedirs(os.path.dirname(destination_path), exist_ok=True)
            with open(destination_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

            logger.info("Downloaded prompt WAV: %s", destination_path)
            return True

        except Exception as exc:
            logger.error("Failed downloading audio stream for Handler %s [%s]: %s", handler_object_id, greeting_type, exc)
            return False