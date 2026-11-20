import logging
from pathlib import Path

Path("logs").mkdir(exist_ok=True)

security_logger = logging.getLogger("security")

if not security_logger.handlers:

    handler = logging.FileHandler(
        "logs/security.log",
        encoding="utf-8"
    )

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s"
    )

    handler.setFormatter(formatter)

    security_logger.addHandler(handler)

security_logger.setLevel(logging.INFO)

def log_permission_check(
    user: str,
    role: str,
    permission: str,
    result: str,
):
    security_logger.info(
        "PERMISSION_CHECK "
        "user=%s role=%s "
        "permission=%s result=%s",
        user,
        role,
        permission,
        result,
    )
    
def log_action(
    user: str,
    role: str,
    action: str,
    target: str,
    result: str,
):
    security_logger.info(
        "ACTION "
        "user=%s role=%s "
        "action=%s target=%s result=%s",
        user,
        role,
        action,
        target,
        result,
    )