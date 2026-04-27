"""
Logging utility for the ECRM Chatbot.
Provides structured logging with timestamps for debugging the pipeline.
"""
import logging
import os
import sys
from datetime import datetime

_loggers = {}

def get_logger(name: str, log_dir: str = None) -> logging.Logger:
    """Get or create a named logger with file + console handlers."""
    if name in _loggers:
        return _loggers[name]

    logger = logging.Logger(name)
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)-7s | %(name)-18s | %(message)s",
        datefmt="%H:%M:%S"
    )

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # File handler
    if log_dir is None:
        log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)
    fh = logging.FileHandler(
        os.path.join(log_dir, f"chatbot_{datetime.now().strftime('%Y%m%d')}.log")
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    _loggers[name] = logger
    return logger


class PipelineTimer:
    """Context manager to time pipeline steps and log them."""
    def __init__(self, step_name: str, logger: logging.Logger):
        self.step = step_name
        self.logger = logger
        self.start = None

    def __enter__(self):
        self.start = datetime.now()
        self.logger.info(f">> START: {self.step}")
        return self

    def __exit__(self, *args):
        elapsed = (datetime.now() - self.start).total_seconds()
        self.logger.info(f"<< DONE:  {self.step} ({elapsed:.2f}s)")
