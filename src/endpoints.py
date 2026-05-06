import time
import warnings
from asyncio import wait_for
from http import HTTPStatus
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from playwright_captcha import CaptchaType

from src.consts import CHALLENGE_TITLES
from src.models import (
    HealthcheckResponse,
    InputCookie,
    LinkRequest,
    LinkResponse,
    Solution,
)
from src.utils import CamoufoxDepClass, TimeoutTimer, get_camoufox, logger

warnings.filterwarnings("ignore", category=SyntaxWarning)


router = APIRouter()

CamoufoxDep = Annotated[CamoufoxDepClass, Depends(get_camoufox)]

_COOKIE_SAME_SITE_MAP = {
    "none": "None",
    "lax": "Lax",
    "strict": "Strict",
    "no_restriction": "None",
}

_PROTECTED_COOKIE_NAMES = {
    "cf_clearance",
    "__cf_bm",
}

_PROTECTED_COOKIE_PREFIXES = (
    "cf_",
    "__cf",
)


def _is_protected_cookie_name(cookie_name: str) -> bool:
    lowered_name = cookie_name.strip().lower()
    if lowered_name in _PROTECTED_COOKIE_NAMES:
        return True
    return lowered_name.startswith(_PROTECTED_COOKIE_PREFIXES)


def _to_playwright_cookies(
    cookies: list[InputCookie], request_url: str
) -> list[dict[str, str | float | bool]]:
    request_domain = urlsplit(request_url).hostname
    parsed_cookies: list[dict[str, str | float | bool]] = []

    for cookie in cookies:
        if _is_protected_cookie_name(cookie.name):
            logger.debug("Skipping protected Cloudflare cookie from input: %s", cookie.name)
            continue

        parsed_cookie: dict[str, str | float | bool] = {
            "name": cookie.name,
            "value": cookie.value,
        }

        if cookie.domain:
            parsed_cookie["domain"] = cookie.domain
            parsed_cookie["path"] = cookie.path or "/"
        elif request_domain:
            parsed_cookie["domain"] = request_domain
            parsed_cookie["path"] = cookie.path or "/"
        else:
            parsed_cookie["url"] = request_url

        if cookie.expires is not None:
            parsed_cookie["expires"] = float(cookie.expires)
        if cookie.http_only is not None:
            parsed_cookie["httpOnly"] = cookie.http_only
        if cookie.secure is not None:
            parsed_cookie["secure"] = cookie.secure
        if cookie.same_site is not None:
            same_site_key = cookie.same_site.strip().lower()
            if same_site_key not in _COOKIE_SAME_SITE_MAP:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Invalid cookie sameSite value. Supported values: "
                        "None, Lax, Strict, no_restriction"
                    ),
                )
            parsed_cookie["sameSite"] = _COOKIE_SAME_SITE_MAP[same_site_key]

        parsed_cookies.append(parsed_cookie)

    return parsed_cookies


@router.get("/", include_in_schema=False)
def read_root():
    """Redirect to /docs."""
    logger.debug("Redirecting to /docs")
    return RedirectResponse(url="/docs", status_code=301)


@router.get("/health")
async def health_check(sb: CamoufoxDep):
    """Health check endpoint."""
    health_check_request = await read_item(
        LinkRequest.model_construct(url="https://google.com"),
        sb,
    )

    if health_check_request.solution.status != HTTPStatus.OK:
        raise HTTPException(
            status_code=500,
            detail="Health check failed",
        )

    return HealthcheckResponse(user_agent=health_check_request.solution.user_agent)


@router.post("/v1")
async def read_item(request: LinkRequest, dep: CamoufoxDep) -> LinkResponse:
    """Handle POST requests."""
    start_time = int(time.time() * 1000)

    timer = TimeoutTimer(duration=request.max_timeout)

    request.url = request.url.replace('"', "").strip()

    if request.cookies:
        try:
            await dep.context.add_cookies(
                _to_playwright_cookies(request.cookies, request.url)
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Invalid request cookies format")
            raise HTTPException(status_code=422, detail="Invalid cookies format") from e

    try:
        page_request = await dep.page.goto(
            request.url, timeout=timer.remaining() * 1000
        )
        status = page_request.status if page_request else HTTPStatus.OK
        await dep.page.wait_for_load_state(
            state="domcontentloaded", timeout=timer.remaining() * 1000
        )
        await dep.page.wait_for_load_state(
            "networkidle", timeout=timer.remaining() * 1000
        )

        if await dep.page.title() in CHALLENGE_TITLES:
            logger.info("Challenge detected, attempting to solve...")
            # Solve the captcha
            await wait_for(
                dep.solver.solve_captcha(  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
                    captcha_container=dep.page,
                    captcha_type=CaptchaType.CLOUDFLARE_INTERSTITIAL,
                    wait_checkbox_attempts=1,
                    wait_checkbox_delay=0.5,
                ),
                timeout=timer.remaining(),
            )
            status = HTTPStatus.OK
            logger.debug("Challenge solved successfully.")
    except TimeoutError as e:
        logger.error("Timed out while solving the challenge")
        raise HTTPException(
            status_code=408,
            detail="Timed out while solving the challenge",
        ) from e

    cookies = await dep.context.cookies()

    return LinkResponse(
        message="Success",
        solution=Solution(
            user_agent=await dep.page.evaluate("navigator.userAgent"),
            url=dep.page.url,
            status=status,
            cookies=cookies,
            headers=page_request.headers if page_request else {},
            response=await dep.page.content(),
        ),
        start_timestamp=start_time,
    )
