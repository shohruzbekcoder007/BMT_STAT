"""
Process entrypoint for the Hermes host service.

Usage:
  python -m app.main
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path


def _bootstrap_paths() -> None:
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


def main() -> None:
    _bootstrap_paths()

    try:
        from dotenv import load_dotenv

        env_file = Path(__file__).resolve().parent.parent / ".env"
        if env_file.is_file():
            load_dotenv(env_file)
    except Exception:
        pass

    from app.logging_setup import setup_logging

    setup_logging()
    logger = logging.getLogger("app")

    bind_host = (os.getenv("APP_HOST") or "0.0.0.0").strip() or "0.0.0.0"
    port = int(os.getenv("APP_PORT") or "8080")
    workers = int(os.getenv("API_WORKERS") or "1")
    reload = os.getenv("API_RELOAD", "false").strip().lower() in {
        "1",
        "true",
        "yes",
    }

    logger.info("=" * 60)
    logger.info("Hermes host — server starting")
    logger.info(
        "Bind=%s Port=%s Workers=%s Reload=%s",
        bind_host,
        port,
        workers,
        reload,
    )
    logger.info("LLM_MODEL=%s", os.getenv("LLM_MODEL", "gpt-4.1"))
    logger.info("=" * 60)

    try:
        from agents.hermes_host import get_hermes_host

        hermes = get_hermes_host()
        logger.info("Hermes host readiness: %s", hermes.readiness())
    except Exception:
        logger.exception(
            "Hermes host init failed at startup — API starts; /ready may return 503"
        )

    import uvicorn

    uvicorn.run(
        "app.api:app",
        host=bind_host,
        port=port,
        workers=1 if reload else max(1, workers),
        reload=reload,
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
        access_log=True,
    )


if __name__ == "__main__":
    main()
