import time
import warnings
from asyncio import wait_for
from http import HTTPStatus
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import RedirectResponse
from playwright_captcha import CaptchaType

from src.consts import CHALLENGE_TITLES, REQUEST_DEADLINE_MARGIN
from src.models import (
    HealthcheckResponse,
    InputCookie,
    LinkRequest,
    LinkResponse,
    Solution,
)
from src.utils import CamoufoxDepClass, TimeoutTimer, camoufox_session, logger

warnings.filterwarnings("ignore", category=SyntaxWarning)


router = APIRouter()

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
async def health_check():
    """Health check endpoint."""
    health_check_request = await read_item(
        LinkRequest.model_construct(url="https://google.com"),
    )

    if health_check_request.solution.status != HTTPStatus.OK:
        raise HTTPException(
            status_code=500,
            detail="Health check failed",
        )

    return HealthcheckResponse(user_agent=health_check_request.solution.user_agent)


@router.post("/v1")
async def read_item(
    request: LinkRequest,
    x_proxy_server: Annotated[
        str | None,
        Header(
            alias="X-Proxy-Server",
            description="Override proxy server for this request in protocol://host:port format.",
        ),
    ] = None,
    x_proxy_username: Annotated[str | None, Header(alias="X-Proxy-Username")] = None,
    x_proxy_password: Annotated[str | None, Header(alias="X-Proxy-Password")] = None,
) -> LinkResponse:
    """Handle POST requests.

    The Camoufox lifecycle (spawn, rotation/teardown, busy release) is fully
    contained in this handler, so the browser is already idle again by the time
    this function returns its response to the client.
    """
    phase = _Phase()
    async with camoufox_session(
        x_proxy_server, x_proxy_username, x_proxy_password
    ) as dep:
        try:
            return await wait_for(
                _solve(request, dep, phase),
                timeout=request.max_timeout + REQUEST_DEADLINE_MARGIN,
            )
        except TimeoutError as e:
            logger.error(
                "Request to %s exceeded hard deadline of %ss while %s",
                request.url,
                request.max_timeout,
                phase.name,
            )
            raise HTTPException(
                status_code=408,
                detail=(
                    f"Request exceeded maxTimeout of {request.max_timeout}s "
                    f"and was aborted while {phase.name}"
                ),
            ) from e


class _Phase:
    """Mutable holder for the operation currently in progress.

    Lets both the per-operation handler in ``_solve`` and the outer hard-deadline
    handler in ``read_item`` report which step was running when a timeout fired.
    """

    def __init__(self) -> None:
        self.name = "starting request"


def _remaining_ms(timer: TimeoutTimer) -> float:
    """Remaining budget in ms, raising once exhausted.

    Playwright treats ``timeout=0`` as "wait forever", so a depleted budget must
    never be passed straight through — otherwise an operation would hang past
    maxTimeout. Raising here yields a clean 408 instead.
    """
    remaining = timer.remaining()
    if remaining <= 0:
        raise TimeoutError("Request deadline exceeded")
    return remaining * 1000


async def _solve(
    request: LinkRequest, dep: CamoufoxDepClass, phase: "_Phase"
) -> LinkResponse:
    """Drive the page through the request and build the response."""
    start_time = int(time.time() * 1000)

    timer = TimeoutTimer(duration=request.max_timeout)

    request.url = request.url.replace('"', "").strip()

    if request.cookies:
        phase.name = "applying request cookies"
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
        phase.name = f"navigating to {request.url}"
        page_request = await dep.page.goto(
            request.url, timeout=_remaining_ms(timer)
        )
        status = page_request.status if page_request else HTTPStatus.OK
        phase.name = "waiting for page DOM to load (domcontentloaded)"
        await dep.page.wait_for_load_state(
            state="domcontentloaded", timeout=_remaining_ms(timer)
        )
        phase.name = "waiting for network to go idle (networkidle)"
        await dep.page.wait_for_load_state(
            "networkidle", timeout=_remaining_ms(timer)
        )

        phase.name = "reading page title"
        if await dep.page.title() in CHALLENGE_TITLES:
            logger.info("Challenge detected, attempting to solve...")
            # Solve the captcha
            phase.name = "solving the Cloudflare challenge"
            await wait_for(
                dep.solver.solve_captcha(  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
                    captcha_container=dep.page,
                    captcha_type=CaptchaType.CLOUDFLARE_INTERSTITIAL,
                    wait_checkbox_attempts=1,
                    wait_checkbox_delay=0.5,
                ),
                timeout=_remaining_ms(timer) / 1000,
            )
            status = HTTPStatus.OK
            logger.debug("Challenge solved successfully.")
    except TimeoutError as e:
        logger.error("Timed out while %s", phase.name)
        raise HTTPException(
            status_code=408,
            detail=f"Timed out while {phase.name}",
        ) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Browser request failed while %s: %s", phase.name, e)
        raise HTTPException(
            status_code=502,
            detail=f"Browser request failed while {phase.name}: {e}",
        ) from e

    phase.name = "collecting cookies"
    cookies = await dep.context.cookies()

    phase.name = "reading user agent"
    user_agent = await dep.page.evaluate("navigator.userAgent")
    phase.name = "reading page content"
    response_content = await dep.page.content()

    return LinkResponse(
        message="Success",
        solution=Solution(
            user_agent=user_agent,
            url=dep.page.url,
            status=status,
            cookies=cookies,
            headers=page_request.headers if page_request else {},
            response=response_content,
        ),
        start_timestamp=start_time,
    )
