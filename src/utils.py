import logging
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
        camoufox = AsyncCamoufox(
            main_world_eval=True,
            addons=[ADDON_PATH],
            geoip=True,
            proxy=proxy_config,
            locale="en-US",
            headless=True,
            humanize=True,
            i_know_what_im_doing=True,
            config={"forceScopeAccess": True},  # add this when creating Camoufox instance
            disable_coop=True,  # add this when creating Camoufox instance
        )
        try:
            browser_raw = await camoufox.__aenter__()
        except BaseException as enter_exc:
            # Camoufox.__aenter__ leaks the playwright node driver if launch fails
            # (e.g. invalid proxy raises before browser launch). Force cleanup.
            playwright_cm = getattr(camoufox, "_playwright_context_manager", None) or camoufox
            try:
                await playwright_cm.__aexit__(
                    type(enter_exc), enter_exc, enter_exc.__traceback__
                )
            except Exception as cleanup_exc:
                logger.warning("Playwright cleanup after launch failure failed: %s", cleanup_exc)
            raise
        try:
            # Cast to Browser since AsyncCamoufox always returns a Browser, not BrowserContext
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
        finally:
            # AsyncCamoufox.__aexit__ first calls browser.close() then stops the
            # playwright node driver. If browser.close() raises (e.g. browser already
            # crashed), the node process leaks. Force-stop both stages.
            try:
                if camoufox.browser is not None:
                    try:
                        await camoufox.browser.close()
                    except Exception as close_exc:
                        logger.warning("browser.close() failed: %s", close_exc)
            finally:
                playwright_cm = getattr(camoufox, "_playwright_context_manager", None) or camoufox
                try:
                    await playwright_cm.__aexit__(None, None, None)
                except Exception as stop_exc:
                    logger.warning("Playwright stop failed: %s", stop_exc)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to launch browser: %s", e)
        raise HTTPException(
            status_code=502,
            detail=f"Failed to launch browser: {e}",
        ) from e
