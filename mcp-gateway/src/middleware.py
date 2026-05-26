"""HTTP middleware: request logging, CSRF protection."""
import time
from urllib.parse import urlparse

from fastapi import Request, status
from fastapi.responses import JSONResponse

from logger import get_logger

logger = get_logger()

CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSRF_CHECK_PREFIXES = ("/admin/",)


async def request_logging_middleware(request: Request, call_next):
    """Log each HTTP request with duration."""
    start = time.time()
    client_host = request.client.host if request.client else "-"
    method = request.method
    path = request.url.path

    response = await call_next(request)

    duration = time.time() - start
    logger.info(
        f'{client_host} - "{method} {path} HTTP/1.1" {response.status_code} {duration:.3f}s'
    )
    return response


async def csrf_middleware(request: Request, call_next):
    """Lightweight CSRF check: require HX-Request or valid Origin for state-changing admin requests."""
    if request.method.upper() not in CSRF_SAFE_METHODS:
        if any(request.url.path.startswith(p) for p in CSRF_CHECK_PREFIXES):
            # HTMX requests are safe (browser-enforced custom header)
            if request.headers.get("HX-Request"):
                return await call_next(request)

            # Check Origin header if present
            origin = request.headers.get("Origin")
            if origin:
                origin_host = urlparse(origin).netloc
                host_header = request.headers.get("Host", "")
                if origin_host != host_header and not origin_host.endswith(f".{host_header}"):
                    logger.warning(f"CSRF: Origin mismatch - origin={origin}, host={host_header}")
                    return JSONResponse(
                        status_code=status.HTTP_403_FORBIDDEN,
                        content={"detail": "CSRF check failed: Origin mismatch"},
                    )

    return await call_next(request)
