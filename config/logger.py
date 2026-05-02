"""
config/logger.py
Structured logging with loguru + rich console output
"""

import sys
from loguru import logger
from pathlib import Path


def setup_logger(log_level: str = "INFO", log_file: str = "logs/bot.log") -> None:
    """Initialize loguru logger with console + file sinks."""
    
    # Remove default handler
    logger.remove()

    # Console handler — colored, readable
    logger.add(
        sys.stdout,
        level=log_level,
        format=(
            "<green>{time:HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan> | "
            "<level>{message}</level>"
        ),
        colorize=True,
    )

    # File handler — full detail, rotation
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    logger.add(
        log_file,
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}",
        rotation="10 MB",
        retention="30 days",
        compression="gz",
        enqueue=True,  # thread-safe async logging
    )

    logger.info(f"Logger initialized | mode={log_level} | file={log_file}")
