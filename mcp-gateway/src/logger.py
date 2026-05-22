import logging
import sys
from datetime import datetime, timezone


def setup_logger(name: str = "mcp-gateway", level: int = logging.INFO) -> logging.Logger:
    """配置结构化日志"""
    logger = logging.getLogger(name)
    logger.setLevel(level)

    if logger.handlers:
        return logger

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger("mcp-gateway")
