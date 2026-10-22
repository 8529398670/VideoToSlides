"""Admin settings: the gear icon. Everything editable is declared in
app/core/settings.py; this module only reads, validates and saves.

    GET  /api/settings    every field: value, default, bounds
    POST /api/settings    {changes: {key: value | null}}; null = back to default

Admin-only because these apply to the whole server, for everyone. Saving
also needs a browser session: the Google client secret is a credential, and
the rule throughout this app is that an API key never sets one.
"""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core import settings
from app.core.security import check_csrf
from app.models import settings as settings_model
from app.routes.helpers import bad_request, forbidden, get_store, read_json, run


def base_config(request: Request):
    return request.app.state.base_config


async def get_settings(request: Request) -> Response:
    overrides = await run(settings_model.get_overrides, get_store(request))
    return JSONResponse(settings.describe(base_config(request), overrides))


async def save_settings(request: Request) -> Response:
    body = await read_json(request)
    if body is None:
        return bad_request("malformed request body")
    if not check_csrf(request, body.get("csrf_token")):
        return forbidden("invalid csrf token")
    base = base_config(request)

    def change(current: dict) -> dict:
        updated = settings.merged(current, body.get("changes"))
        settings.check(settings.apply_config(base, updated))
        return updated

    try:
        overrides = await run(settings_model.update_overrides, get_store(request), change)
    except settings.SettingsError as failure:
        return bad_request(str(failure))
    apply_live(request.app, overrides)
    return JSONResponse(settings.describe(base, overrides))


def apply_live(app, overrides: dict) -> None:
    """Make saved settings take effect in this process now. Workers pick
    them up when they start."""
    settings.apply_tunables(overrides)
    effective = settings.apply_config(app.state.base_config, overrides)
    app.state.config = effective
    dispatcher = getattr(app.state, "dispatcher", None)
    if dispatcher is not None:
        dispatcher.cfg = effective
