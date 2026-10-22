"""Public endpoints that are about the app rather than about anyone in it."""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, Response


async def get_language(request: Request) -> Response:
    """The whole language.yaml tree as JSON. Deliberately public: the
    signed-out page has text too, and none of it is secret. Do not put
    anything sensitive in language.yaml -- it is, by design, readable by
    anyone who can reach the server."""
    return Response(
        request.app.state.language.json,
        media_type="application/json",
        headers={"cache-control": "no-store"},
    )


async def get_health(request: Request) -> Response:
    """What the Dockerfile HEALTHCHECK hits. Trivial on purpose -- if it
    touched the database, a slow query would get the container killed and
    restarted, turning a small problem into an outage."""
    return JSONResponse({"ok": True})


async def unknown_api(request: Request) -> Response:
    """An /api/ path nothing claims. Without this the static catch-all would
    answer with index.html and a 200, which is a confusing reply to a script
    that mistyped an endpoint."""
    return JSONResponse({"error": "not found"}, status_code=404)
