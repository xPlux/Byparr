import time
from http import HTTPStatus

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from src.models import LinkRequest
from src.utils import logger


class LogRequest(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint):
        """Log requests."""
        if request.url.path != "/v1" or request.method != "POST":
            return await call_next(request)

        start_time = time.perf_counter()
        try:
            request_body = LinkRequest.model_validate(await request.json())
            target_url = request_body.url
        except Exception:  # noqa: BLE001
            # Don't crash the middleware on a malformed body: the route's own
            # validation will return a proper JSON error. Just log generically.
            target_url = "<unparseable body>"
        logger.info(
            f"From: {request.client.host if request.client else 'unknown'} at {time.strftime('%Y-%m-%d %H:%M:%S')}: {target_url}"
        )
        response = await call_next(request)
        process_time = time.perf_counter() - start_time

        if response.status_code == HTTPStatus.OK:
            logger.info(f"Done {target_url} in {process_time:.2f}s")
        else:
            logger.warning(f"Failed {target_url} in {process_time:.2f}s")

        return response
