"""Simple logging configuration"""
import logging
import os
import sys


def setup_logger(name: str = "newscaster") -> logging.Logger:
    """Configure and return a logger instance."""
    logger = logging.getLogger(name)

    # Avoid duplicate handlers when module is imported multiple times
    if logger.handlers:
        return logger

    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    logger.setLevel(level)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)

    formatter = logging.Formatter(
        '%(asctime)s.%(msecs)03d - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger