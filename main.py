from __future__ import annotations

import asyncio
import logging
import sys

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from src.consts import HOST, LOG_LEVEL, PORT, VERSION
from src.endpoints import health_check, router
from src.middlewares import LogRequest
from src.utils import logger, shutdown_shared_browser, sweep_stale_tmp_dirs

logger.info("Using version %s", VERSION)
logger.info("Log level set to %s", logging.getLevelName(LOG_LEVEL))

sweep_stale_tmp_dirs()

app = FastAPI(debug=LOG_LEVEL == logging.DEBUG, log_level=LOG_LEVEL)
app.add_middleware(GZipMiddleware)
app.add_middleware(LogRequest)

app.include_router(router=router)


def _error_body(message: str) -> dict[str, str]:
    """Build an error payload that always carries a descriptive message.

    Keeps both ``detail`` and ``message`` populated (FlareSolverr-style) so
    callers never fall back to a generic "unknown error".
    """
    message = message.strip() or "Unknown error"
    return {"status": "error", "message": message, "detail": message}


@app.exception_handler(HTTPException)
async def _http_exception_handler(_request: Request, exc: HTTPException):
    detail = exc.detail
    if isinstance(detail, str) and detail.strip():
        message = detail
    else:
        message = f"HTTP {exc.status_code} error"
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body(message),
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(_request: Request, exc: RequestValidationError):
    parts: list[str] = []
    for err in exc.errors():
        location = ".".join(
            str(loc) for loc in err.get("loc", ()) if loc != "body"
        )
        msg = err.get("msg", "invalid value")
        parts.append(f"{location}: {msg}" if location else msg)
    message = (
        "Request validation failed: " + "; ".join(parts)
        if parts
        else "Request validation failed"
    )
    return JSONResponse(status_code=422, content=_error_body(message))


@app.exception_handler(Exception)
async def _unhandled_exception_handler(_request: Request, exc: Exception):
    logger.exception("Unhandled error while processing request")
    message = f"Internal server error: {type(exc).__name__}: {exc}"
    return JSONResponse(status_code=500, content=_error_body(message))


@app.on_event("shutdown")
async def _shutdown_browser():
    await shutdown_shared_browser()


async def init():
    """Initialize the application."""
    try:
        await health_check()
    finally:
        await shutdown_shared_browser()


if __name__ == "__main__":
    # Check for --init flag to run the app in development mode
    if "--init" in sys.argv:
        logger.info("Running initialization script...")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(init())
        logger.info("Initialization complete.")
    else:
        uvicorn.run(app, host=HOST, port=PORT)
