"""Minimal authenticated API surface for a future Chushpan Core Server."""

from __future__ import annotations

import hmac
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, status

from chushpan_core import __version__
from chushpan_core.secrets import load_api_token

logger = logging.getLogger("chushpan_core")


def create_app(token_loader: Callable[[], str] = load_api_token) -> FastAPI:
    """Build the API with injectable token loading for isolated tests."""
    api = FastAPI(
        title="Chushpan Core API",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @api.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "chushpan-core", "version": __version__}

    @api.get("/v1/status")
    async def protected_status(
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, str]:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Bearer token required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        supplied = authorization[7:]
        if not supplied or len(supplied) > 256:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            expected = token_loader()
        except (OSError, RuntimeError, ValueError):
            logger.exception("Protected API token is unavailable")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Core API credentials are unavailable",
            ) from None
        if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return {"status": "ok", "service": "chushpan-core", "version": __version__}

    return api


def configure_logging(filename: str = "core.log") -> None:
    """Write bounded, content-free service logs under ProgramData."""
    from logging.handlers import RotatingFileHandler

    root = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
    log_dir = root / "Chushpan" / "CoreServer" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / filename, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


app = create_app()
