from enum import Enum


class Role(str, Enum):
    HELPDESK = "Helpdesk"
    LOCAL_TECH_SERVICES = "LocalTechServices"
    NETWORK_ENGINEER = "NetworkEngineer"
    NETWORK_ADMIN = "NetworkAdmin"