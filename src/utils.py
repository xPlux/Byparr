import asyncio
import glob
import logging
import os
import shutil
import signal
import tempfile
import threading
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated, NamedTuple, cast

from camoufox import AsyncCamoufox
from fastapi import Header, HTTPException
from playwright.async_api import BrowserContext, Page
from playwright_captcha import (
    ClickSolver,
    FrameworkType,
)
from pydantic import BaseModel, Field

from src.consts import (
    ADDON_PATH,
    BROWSER_SHUTDOWN_TIMEOUT,
    FINGERPRINT_CLEAR_BETWEEN,
    FINGERPRINT_ROTATE_EVERY,
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


def _iter_descendant_pids(root_pid: int) -> list[int]:
    """Return all descendant PIDs of root_pid via /proc (Linux only, best-effort)."""
    try:
        proc_entries = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return []

    children: dict[int, list[int]] = {}
    for pid_str in proc_entries:
        try:
            with open(f"/proc/{pid_str}/stat", encoding="utf-8") as stat_file:
                data = stat_file.read()
        except OSError:
            continue
        # comm field is wrapped in parentheses and may contain spaces, so the
        # ppid is the 2nd field after the closing parenthesis.
        rparen = data.rfind(")")
        if rparen == -1:
            continue
        try:
            ppid = int(data[rparen + 2 :].split()[1])
        except (ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(int(pid_str))

    descendants: list[int] = []
    stack = [root_pid]
    while stack:
        for child in children.get(stack.pop(), []):
            descendants.append(child)
            stack.append(child)
    return descendants


def _proc_name(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/comm", encoding="utf-8") as comm_file:
            return comm_file.read().strip()
    except OSError:
        return ""


def kill_browser_processes() -> int:
    """SIGKILL any camoufox/firefox descendant of this process.

    Last-resort action when the asyncio deadline cannot be enforced (event-loop
    starvation under heavy CPU load, or a Playwright op ignoring cooperative
    cancellation). Killing the browser makes the stuck call fail immediately so
    the request unwinds and the busy flag is released. Linux/proc based and
    best-effort: returns the number of processes signalled.
    """
    if not hasattr(signal, "SIGKILL"):
        return 0
    killed = 0
    for pid in _iter_descendant_pids(os.getpid()):
        name = _proc_name(pid).lower()
        if "firefox" in name or "camoufox" in name:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                continue
            killed += 1
    return killed


class BrowserWatchdog:
    """Hard-deadline watchdog running on an OS thread, independent of the loop.

    If armed and not disarmed within ``timeout`` seconds, it force-kills the
    browser process. Because it runs on a separate thread (not the asyncio event
    loop), it still fires when the loop is starved by CPU-bound work — something
    ``asyncio.wait_for`` cannot guarantee.
    """

    def __init__(self, timeout: float) -> None:
        self._timer = threading.Timer(timeout, self._fire)
        self._timer.daemon = True
        self.fired = False

    def _fire(self) -> None:
        self.fired = True
        killed = kill_browser_processes()
        logger.error(
            "Watchdog deadline exceeded; force-killed %d browser process(es)",
            killed,
        )

    def __enter__(self) -> "BrowserWatchdog":
        self._timer.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._timer.cancel()



class CamoufoxDepClass(NamedTuple):
    page: Page
    solver: ClickSolver
    context: BrowserContext


class _SharedBrowser:
    def __init__(self) -> None:
        self.camoufox: AsyncCamoufox | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.profile_dir: str | None = None
        self.uses_left: int = 0
        self.busy: bool = False


_shared = _SharedBrowser()


async def _close_resource(resource, resource_name: str) -> None:
    if resource is None:
        return
    try:
        await resource.close()
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        raise
    except BaseException as cleanup_error:  # noqa: BLE001
        logger.warning("%s failed: %s", resource_name, cleanup_error)


async def _stop_camoufox(camoufox: AsyncCamoufox, exit_error: BaseException | None = None) -> None:
    # Skip Camoufox's own browser.close() — we already closed the context.
    try:
        camoufox.browser = None
    except Exception as cleanup_error:  # noqa: BLE001
        logger.warning("Clearing Camoufox browser handle failed: %s", cleanup_error)

    playwright_cm = getattr(camoufox, "_playwright_context_manager", None) or camoufox
    try:
        await playwright_cm.__aexit__(
            type(exit_error) if exit_error else None,
            exit_error,
            exit_error.__traceback__ if exit_error else None,
        )
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        raise
    except BaseException as cleanup_error:  # noqa: BLE001
        logger.warning("Playwright stop failed: %s", cleanup_error)


def _resolve_proxy(
    header_server: str | None,
    header_username: str | None,
    header_password: str | None,
) -> dict[str, str | None] | None:
    if header_server:
        return {
            "server": header_server,
            "username": header_username,
            "password": header_password,
        }
    if PROXY_SERVER:
        return {
            "server": PROXY_SERVER,
            "username": PROXY_USERNAME,
            "password": PROXY_PASSWORD,
        }
    return None


async def _spawn_shared(proxy_config: dict[str, str | None] | None) -> None:
    profile_dir = tempfile.mkdtemp(prefix=PROFILE_DIR_PREFIX)
    camoufox = AsyncCamoufox(
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
        persistent_context=True,
        user_data_dir=profile_dir,
    )
    try:
        context_raw = await camoufox.__aenter__()
    except BaseException as enter_exc:
        await _stop_camoufox(camoufox, enter_exc)
        shutil.rmtree(profile_dir, ignore_errors=True)
        raise

    _shared.camoufox = camoufox
    _shared.context = cast("BrowserContext", context_raw)
    _shared.page = None
    _shared.profile_dir = profile_dir
    _shared.uses_left = (
        FINGERPRINT_ROTATE_EVERY if FINGERPRINT_ROTATE_EVERY > 0 else -1
    )


def _reset_shared_state() -> None:
    """Drop all shared handles and delete the profile dir without awaiting."""
    profile_dir = _shared.profile_dir
    _shared.camoufox = None
    _shared.context = None
    _shared.page = None
    _shared.profile_dir = None
    _shared.uses_left = 0
    if profile_dir is not None:
        shutil.rmtree(profile_dir, ignore_errors=True)


async def shutdown_shared_browser() -> None:
    """Close the shared Camoufox instance and remove its profile, if any."""
    if _shared.camoufox is None and _shared.profile_dir is None:
        return
    camoufox = _shared.camoufox
    page = _shared.page
    context = _shared.context
    try:
        await _close_resource(page, "page.close()")
        await _close_resource(context, "context.close()")
        if camoufox is not None:
            await _stop_camoufox(camoufox)
    finally:
        _reset_shared_state()


async def _teardown_or_rotate(request_failed: bool) -> None:
    """Close the browser on failure, or count down to a fingerprint rotation."""
    if request_failed:
        await shutdown_shared_browser()
    elif FINGERPRINT_ROTATE_EVERY > 0:
        _shared.uses_left -= 1
        if _shared.uses_left <= 0:
            await shutdown_shared_browser()


async def _finalize_session(request_failed: bool) -> None:
    """Run teardown/rotation under a hard timeout, always freeing the lock."""
    try:
        await asyncio.wait_for(
            _teardown_or_rotate(request_failed),
            timeout=BROWSER_SHUTDOWN_TIMEOUT,
        )
    except TimeoutError:
        logger.error(
            "Browser teardown exceeded %ss; force-resetting shared state",
            BROWSER_SHUTDOWN_TIMEOUT,
        )
        _reset_shared_state()
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as teardown_error:  # noqa: BLE001
        logger.error("Browser teardown failed: %s; force-resetting", teardown_error)
        _reset_shared_state()
    finally:
        _shared.busy = False


@asynccontextmanager
async def camoufox_session(
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
    """Yield a Camoufox-backed page/context shared across requests.

    Unlike a FastAPI ``yield`` dependency, this context manager performs its
    teardown (or fingerprint rotation) and releases the busy flag *before* the
    request handler returns its response. That guarantees a caller that just
    received a ``200`` is talking to a container that is already idle again.

    The same Camoufox process is reused for ``FINGERPRINT_ROTATE_EVERY``
    requests before being restarted with a fresh fingerprint. Only one request
    at a time is served; concurrent callers get HTTP 429.
    """
    if _shared.busy:
        raise HTTPException(
            status_code=429,
            detail="Browser is busy processing another request",
        )
    _shared.busy = True
    request_failed = False
    try:
        proxy_config = _resolve_proxy(
            x_proxy_server, x_proxy_username, x_proxy_password
        )

        try:
            if _shared.camoufox is None:
                await _spawn_shared(proxy_config)
            elif FINGERPRINT_CLEAR_BETWEEN:
                try:
                    await _shared.context.clear_cookies()
                except Exception as clear_exc:  # noqa: BLE001
                    logger.warning(
                        "context.clear_cookies() failed: %s", clear_exc
                    )
                try:
                    await _shared.context.clear_permissions()
                except Exception as clear_exc:  # noqa: BLE001
                    logger.warning(
                        "context.clear_permissions() failed: %s", clear_exc
                    )
                if _shared.page is not None:
                    await _close_resource(_shared.page, "page.close()")
                    _shared.page = None
        except HTTPException:
            raise
        except Exception as e:
            request_failed = True
            logger.error("Failed to launch browser: %s", e)
            raise HTTPException(
                status_code=502,
                detail=f"Failed to launch browser: {e}",
            ) from e

        if _shared.page is None:
            try:
                _shared.page = await _shared.context.new_page()
            except Exception as e:
                request_failed = True
                logger.error("Failed to open new page: %s", e)
                raise HTTPException(
                    status_code=502,
                    detail=f"Failed to open new page: {e}",
                ) from e

        try:
            async with ClickSolver(
                framework=FrameworkType.CAMOUFOX,
                page=_shared.page,
                max_attempts=MAX_ATTEMPTS,
                attempt_delay=1,
            ) as solver:
                yield CamoufoxDepClass(_shared.page, solver, _shared.context)
        except BaseException:
            request_failed = True
            raise
    except BaseException:
        request_failed = True
        raise
    finally:
        await _finalize_session(request_failed)
