"""The cross-cutting protections that apply to every request no matter which
route answers it. Keeping them here means a route handler never has to
remember a header or a throttle -- if it is served by the app, it is covered.

These are plain ASGI middleware rather than Starlette's BaseHTTPMiddleware:
they only add headers or answer early, which needs no request body handling,
and plain ASGI avoids BaseHTTPMiddleware's known problems with streaming
responses and context variables.

The request body limit is Starlette's own (max_body_size on the app, wired in
app/main.py), so it is not repeated here.
"""
from __future__ import annotations

import ipaddress
import sys
import time
from collections import deque
from typing import Sequence, Union

from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Config, trusted_proxy_networks

IPNetwork = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]

# Everything the page needs is same-origin, which is true for this template on
# purpose: libraries are downloaded once and served from static/vendor/ rather
# than pulled from a CDN at runtime. That is what lets the CSP stay this
# tight, and it is why adding a <script src> pointing at some other origin
# will visibly break -- the fix is to vendor the file, not to loosen the
# policy.
CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)


class SecurityHeaders:
    def __init__(self, app: ASGIApp, cfg: Config) -> None:
        self.app = app
        self.headers = {
            "content-security-policy": CONTENT_SECURITY_POLICY,
            "x-frame-options": "DENY",
            "x-content-type-options": "nosniff",
            "referrer-policy": "same-origin",
            "permissions-policy": "geolocation=(), microphone=(), camera=(), payment=(), usb=()",
            "cross-origin-opener-policy": "same-origin",
            "cross-origin-resource-policy": "same-origin",
            "x-dns-prefetch-control": "off",
        }
        if cfg.secure_cookies:
            # Only claim HTTPS-only when the deployment actually is. Sending
            # HSTS from a local http:// run would pin the browser to https for
            # localhost and break every other project on that port.
            self.headers["strict-transport-security"] = "max-age=63072000; includeSubDomains"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                # setdefault, so a route that needs a stricter value (the
                # login page's Referrer-Policy: no-referrer) keeps it.
                for name, value in self.headers.items():
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)


def client_ip(scope: Scope, cfg: Config, trusted: Sequence[IPNetwork]) -> str:
    """The address a request is counted against.

    The socket's peer address, unless server.trust_proxy is on *and* that
    peer is one of server.trusted_proxies. Only then is server.proxy_header
    read -- right to left, skipping addresses that are themselves trusted
    proxies, and stopping at the first one that is not. That is the address
    your outermost trusted proxy saw; anything to its left came from the
    client and can say whatever the client likes. An entry that is not an IP
    address ends the walk at the last trusted hop rather than becoming a
    rate-limit key a client chose.

    Checking the peer is what makes the header safe to read at all: without
    it, any client that reaches the server directly could name its own IP and
    walk past the limiter."""
    client = scope.get("client")
    peer = client[0] if client else "unknown"
    if not cfg.trust_proxy or not _is_trusted(peer, trusted):
        return peer
    value = Headers(scope=scope).get(cfg.proxy_header, "")
    hop = peer
    for entry in reversed([entry.strip() for entry in value.split(",") if entry.strip()]):
        try:
            address = str(ipaddress.ip_address(entry))
        except ValueError:
            return hop
        if not _is_trusted(address, trusted):
            return address
        hop = address
    return hop


def _is_trusted(address: str, trusted: Sequence[IPNetwork]) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(parsed in network for network in trusted)


class RateLimit:
    """A per-process, in-memory sliding-window limiter, keyed by client IP.
    That is exactly right for the single uvicorn process this template runs.
    Run more than one worker or replica and the counters stop being shared, so
    each would allow the full budget -- move to a shared store at that point
    rather than assuming the limit still holds.

    It runs before the request is authenticated, which is what keeps an
    unauthenticated flood cheap to refuse."""

    def __init__(self, app: ASGIApp, cfg: Config) -> None:
        self.app = app
        self.cfg = cfg
        self.trusted = trusted_proxy_networks(cfg)
        self.max_requests = cfg.rate_limit_max
        self.window = float(cfg.rate_limit_window_seconds)
        self._hits: dict[str, deque[float]] = {}
        self._last_sweep = time.monotonic()

    def _sweep(self, now: float) -> None:
        # Forget clients with nothing inside the window, so the table does not
        # grow forever with every address that ever made one request.
        if now - self._last_sweep < self.window:
            return
        self._last_sweep = now
        for key in [key for key, hits in self._hits.items() if not hits or now - hits[-1] > self.window]:
            del self._hits[key]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        now = time.monotonic()
        self._sweep(now)
        hits = self._hits.setdefault(client_ip(scope, self.cfg, self.trusted), deque())
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.max_requests:
            response = JSONResponse(
                {"error": "too many requests"},
                status_code=429,
                headers={"retry-after": str(int(self.window))},
            )
            await response(scope, receive, send)
            return
        hits.append(now)
        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# error responses
#
# Never hand an internal error message to a client. Every error body has one
# shape, {"error": "..."}, and the detail of a crash goes to the log, where
# the operator can see it (uvicorn prints the traceback).
# ---------------------------------------------------------------------------


def status_text(code: int) -> str:
    if code == 404:
        return "not found"
    if code == 401:
        return "not authenticated"
    if code == 403:
        return "forbidden"
    if code == 405:
        return "method not allowed"
    if code == 413:
        return "request body too large"
    if code == 429:
        return "too many requests"
    if code >= 500:
        return "internal error"
    return "bad request"


async def http_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, HTTPException)
    return JSONResponse({"error": status_text(exc.status_code)}, status_code=exc.status_code, headers=exc.headers)


async def server_error(request: Request, exc: Exception) -> Response:
    print(f"[error] {request.method} {request.url.path}: {exc!r}", file=sys.stderr, flush=True)
    return JSONResponse({"error": "internal error"}, status_code=500)


EXCEPTION_HANDLERS = {HTTPException: http_error, 500: server_error}
