import glob
import logging
import os
import shutil
import tempfile
import time
from collections.abc import AsyncGenerator
from typing import Annotated, NamedTuple, cast

from camoufox import AsyncCamoufox
from fastapi import Header, HTTPException
from playwright.async_api import Browser, BrowserContext, Page
from playwright_captcha import (
    ClickSolver,
    FrameworkType,
)
from pydantic import BaseModel, Field

from src.consts import (
    ADDON_PATH,
    LOG_LEVEL,
    MAX_ATTEMPTS,
    PROXY_PASSWORD,
    PROXY_SERVER,
    PROXY_USERNAME,
)

solver_logger = logging.getLogger("playwright_captcha")
solver_logger.handlers.clear()
if LOG_LEVEL == logging.DEBUG:
    solver_logger.addHandler(logging.StreamHandler())
    solver_logger.setLevel(LOG_LEVEL)
else:
    solver_logger.handlers.append(logging.NullHandler())

logger = logging.getLogger("uvicorn.error")
logger.setLevel(LOG_LEVEL)
if len(logger.handlers) == 0:
    logger.addHandler(logging.StreamHandler())

PROFILE_DIR_PREFIX = "byparr_camoufox_profile-"
_TMP_DIR = tempfile.gettempdir()


def sweep_stale_tmp_dirs() -> None:
    """Remove leftover camoufox/playwright tmp dirs from previous runs.

    Killed/restarted containers can leave behind profile and playwright artifact
    directories that the per-request finally block never got to clean.
    """
    patterns = (
        f"{PROFILE_DIR_PREFIX}*",
        "playwright-artifacts-*",
        "playwright_firefoxdev_profile-*",
    )
    removed = 0
    for pattern in patterns:
        for path in glob.glob(os.path.join(_TMP_DIR, pattern)):
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    if removed:
        logger.info("Swept %d stale tmp dirs on startup", removed)


class TimeoutTimer(BaseModel):
    duration: int  # in seconds
    start_time: float = Field(default_factory=time.perf_counter)

    def remaining(self) -> float:
        """Get remaining time in seconds."""
        return max(0, self.duration - (time.perf_counter() - self.start_time))


class CamoufoxDepClass(NamedTuple):
    page: Page
    solver: ClickSolver
    context: BrowserContext


async def get_camoufox(
    x_proxy_server: Annotated[
        str | None,
        Header(
            alias="X-Proxy-Server",
            description="Override proxy server for this request in protocol://host:port format.",
        ),
    ] = None,
    x_proxy_username: Annotated[
        str | None,
        Header(
            alias="X-Proxy-Username",
        ),
    ] = None,
    x_proxy_password: Annotated[
        str | None,
        Header(
            alias="X-Proxy-Password",
        ),
    ] = None,
) -> AsyncGenerator[CamoufoxDepClass]:
    """Get Camoufox instance."""
    header_server = x_proxy_server
    header_username = x_proxy_username
    header_password = x_proxy_password

    proxy_config = None

    if header_server:
        proxy_config = {
            "server": header_server,
            "username": header_username,
            "password": header_password,
        }
    elif PROXY_SERVER:
        proxy_config = {
            "server": PROXY_SERVER,
            "username": PROXY_USERNAME,
            "password": PROXY_PASSWORD,
        }

    try:
        async with AsyncCamoufox(
            main_world_eval=True,
            addons=[ADDON_PATH],
            geoip=True,
            proxy=proxy_config,
            locale="en-US",
            headless=True,
            humanize=True,
            i_know_what_im_doing=True,
            config={"forceScopeAccess": True},
            disable_coop=True,
        ) as browser_raw:
            browser = cast("Browser", browser_raw)
            context = await browser.new_context()
            page = await context.new_page()
            async with ClickSolver(
                framework=FrameworkType.CAMOUFOX,
                page=page,
                max_attempts=MAX_ATTEMPTS,
                attempt_delay=1,
            ) as solver:
                yield CamoufoxDepClass(page, solver, context)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to launch browser: %s", e)
        raise HTTPException(
            status_code=502,
            detail=f"Failed to launch browser: {e}",
        ) from e
