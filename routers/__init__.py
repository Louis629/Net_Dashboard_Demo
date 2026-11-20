"""
routers package: Modular API and view route handlers for Net_Dashboard.
"""

from routers.telecom import router as telecom_router
from routers.views import router as views_router
from routers.bypass import router as bypass_router
from routers.diagnostics import router as diagnostics_router
from routers.meraki_admin import router as meraki_router
from routers.ndg_admin import router as ndg_router

__all__ = [
    "telecom_router",
    "views_router",
    "bypass_router",
    "diagnostics_router",
    "meraki_router",
    "ndg_router",
]
