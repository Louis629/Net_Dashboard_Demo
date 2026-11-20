"""
providers/telecom_factory.py

Dynamic Factory and Configuration Registry for multi-cluster Cisco Collaboration environments.
Supports toggling between Primary (CUCM 12.5 / UCCX 12.5 / CUC 14) and Hawaii (CUCM 11.5 / UCCX 12.0 / CUC 11.5).
"""

import os
from enum import Enum
from typing import Optional, Union
from dataclasses import dataclass

from core.config import get_settings
from providers.cucm_axl_provider import CUCMAXLProvider
from providers.cupi_provider import CUPIProvider
from providers.demo_providers import (
    DemoCUCMProvider,
    DemoCUPIProvider,
    DemoUCCXProvider,
)
from providers.uccx_provider import UCCXProvider


class ClusterEnvironment(str, Enum):
    PRIMARY = "PRIMARY"
    HAWAII = "HAWAII"


@dataclass
class ClusterConfig:
    cluster: ClusterEnvironment
    cucm_host: str
    cucm_user: str
    cucm_pass: str
    cucm_version: str
    cucm_verify_ssl: bool
    cucm_timeout: int
    uccx_host: str
    uccx_user: str
    uccx_pass: str
    uccx_verify_ssl: bool
    uccx_timeout: int
    cuc_host: str
    cuc_user: str
    cuc_pass: str
    cuc_verify_ssl: bool
    cuc_timeout: int


class TelecomClusterRegistry:
    """
    Manages multi-cluster profiles and initializes environment-specific providers.
    """

    @staticmethod
    def _resolve_cluster(cluster: Optional[ClusterEnvironment] = None) -> ClusterEnvironment:
        return cluster or ClusterEnvironment(
            os.getenv("ACTIVE_TELECOM_CLUSTER", "PRIMARY").upper()
        )

    @staticmethod
    def get_cluster_config(cluster: Optional[ClusterEnvironment] = None) -> ClusterConfig:
        target_cluster = TelecomClusterRegistry._resolve_cluster(cluster)
        prefix = f"{target_cluster.value}_"

        # Safe fallback: Check PREF_VAR -> GLOBAL_VAR -> Default
        return ClusterConfig(
            cluster=target_cluster,
            cucm_host=os.getenv(f"{prefix}CUCM_AXL_HOST", os.getenv("CUCM_AXL_HOST", os.getenv("CUCM_PAN_IP", ""))),
            cucm_user=os.getenv(f"{prefix}CUCM_AXL_USER", os.getenv("CUCM_AXL_USER", os.getenv("CUCM_AXL_USERNAME", ""))),
            cucm_pass=os.getenv(f"{prefix}CUCM_AXL_PASS", os.getenv("CUCM_AXL_PASS", os.getenv("CUCM_AXL_PASSWORD", ""))),
            cucm_version=os.getenv(f"{prefix}CUCM_AXL_VERSION", os.getenv("CUCM_AXL_VERSION", "12.5")),
            cucm_verify_ssl=os.getenv(f"{prefix}CUCM_AXL_VERIFY_SSL", os.getenv("CUCM_AXL_VERIFY_SSL", "false")).lower() in {"1", "true", "yes"},
            cucm_timeout=int(os.getenv(f"{prefix}CUCM_AXL_TIMEOUT_SECONDS", os.getenv("CUCM_AXL_TIMEOUT_SECONDS", "30"))),

            uccx_host=os.getenv(f"{prefix}UCCX_HOST", os.getenv("UCCX_HOST", "")),
            uccx_user=os.getenv(f"{prefix}UCCX_USER", os.getenv("UCCX_USER", "")),
            uccx_pass=os.getenv(f"{prefix}UCCX_PASS", os.getenv("UCCX_PASS", "")),
            uccx_verify_ssl=os.getenv(f"{prefix}UCCX_VERIFY_SSL", os.getenv("UCCX_VERIFY_SSL", "false")).lower() in {"1", "true", "yes"},
            uccx_timeout=int(os.getenv(f"{prefix}UCCX_TIMEOUT_SECONDS", os.getenv("UCCX_TIMEOUT_SECONDS", "30"))),

            cuc_host=os.getenv(f"{prefix}CUC_HOST", os.getenv("CUC_HOST", "")),
            cuc_user=os.getenv(f"{prefix}CUC_USER", os.getenv("CUC_USER", "")),
            cuc_pass=os.getenv(f"{prefix}CUC_PASS", os.getenv("CUC_PASS", "")),
            cuc_verify_ssl=os.getenv(f"{prefix}CUC_VERIFY_SSL", os.getenv("CUC_VERIFY_SSL", "false")).lower() in {"1", "true", "yes"},
            cuc_timeout=int(os.getenv(f"{prefix}CUC_TIMEOUT_SECONDS", os.getenv("CUC_TIMEOUT_SECONDS", "30"))),
        )

    @classmethod
    def get_cucm_provider(
        cls, cluster: Optional[ClusterEnvironment] = None
    ) -> Union[CUCMAXLProvider, DemoCUCMProvider]:
        target_cluster = cls._resolve_cluster(cluster)
        if get_settings().DEMO_MODE:
            return DemoCUCMProvider(cluster=target_cluster.value)
        cfg = cls.get_cluster_config(target_cluster)
        return CUCMAXLProvider(
            host=cfg.cucm_host,
            username=cfg.cucm_user,
            password=cfg.cucm_pass,
            version=cfg.cucm_version,
            verify_ssl=cfg.cucm_verify_ssl,
            timeout=cfg.cucm_timeout
        )

    @classmethod
    def get_uccx_provider(
        cls, cluster: Optional[ClusterEnvironment] = None
    ) -> Union[UCCXProvider, DemoUCCXProvider]:
        target_cluster = cls._resolve_cluster(cluster)
        if get_settings().DEMO_MODE:
            return DemoUCCXProvider(cluster=target_cluster.value)
        cfg = cls.get_cluster_config(target_cluster)
        return UCCXProvider(
            host=cfg.uccx_host,
            username=cfg.uccx_user,
            password=cfg.uccx_pass,
            verify_ssl=cfg.uccx_verify_ssl,
            timeout=cfg.uccx_timeout
        )

    @classmethod
    def get_cupi_provider(
        cls, cluster: Optional[ClusterEnvironment] = None
    ) -> Union[CUPIProvider, DemoCUPIProvider]:
        target_cluster = cls._resolve_cluster(cluster)
        if get_settings().DEMO_MODE:
            return DemoCUPIProvider(cluster=target_cluster.value)
        cfg = cls.get_cluster_config(target_cluster)
        return CUPIProvider(
            host=cfg.cuc_host,
            username=cfg.cuc_user,
            password=cfg.cuc_pass,
            verify_ssl=cfg.cuc_verify_ssl,
            timeout=cfg.cuc_timeout
        )