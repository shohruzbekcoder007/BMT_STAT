"""Structured logging setup with rotation."""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging() -> None:
    """
    Configure root and named loggers.

    Environment:
      LOG_LEVEL, LOG_DIR, LOG_MAX_BYTES, LOG_BACKUP_COUNT, LOG_FORMAT
      (json | plain)
    """
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    log_dir = Path(os.getenv("LOG_DIR", "/app/logs"))
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Fall back to local logs/ when /app/logs is not writable (dev)
        log_dir = Path(__file__).resolve().parent.parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

    max_bytes = int(os.getenv("LOG_MAX_BYTES", str(10 * 1024 * 1024)))
    backup_count = int(os.getenv("LOG_BACKUP_COUNT", "10"))
    log_format = os.getenv("LOG_FORMAT", "json").strip().lower()

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    if log_format == "json":
        try:
            from pythonjsonlogger.json import JsonFormatter  # type: ignore

            formatter: logging.Formatter = JsonFormatter(
                "%(asctime)s %(levelname)s %(name)s %(message)s"
            )
        except Exception:
            try:
                from pythonjsonlogger import jsonlogger  # type: ignore

                formatter = jsonlogger.JsonFormatter(
                    "%(asctime)s %(levelname)s %(name)s %(message)s"
                )
            except Exception:
                formatter = logging.Formatter(
                    "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
                )
    else:
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
        )

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        log_dir / "app.log",
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    err_handler = RotatingFileHandler(
        log_dir / "app-errors.log",
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    err_handler.setLevel(logging.WARNING)
    err_handler.setFormatter(formatter)
    root.addHandler(err_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)

    logging.getLogger("app").info(
        "Logging initialized level=%s dir=%s format=%s",
        level_name,
        log_dir,
        log_format,
    )
