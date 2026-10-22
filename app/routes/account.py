"""Self-service endpoints for any signed-in caller: who am I, rename myself,
log out. The account screen itself is static/js/app.js, calling these."""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.encryption import BadKeyError, CipherTextError
from app.core.security import actor_from, check_csrf, clear_session_cookie, decrypt_cookie
from app.models import sessions, users
from app.pipeline.tunables import VIDEO_EXTENSIONS
from app.routes.helpers import bad_request, forbidden, get_config, get_store, read_json, run, server_error


async def me(request: Request) -> Response:
    """The frontend's first call on every page load: it answers "who am I,
    what may I do, and what CSRF token do I echo back". Handing the token out
    here means no page needs a hidden form field or a template variable.

    "role" is the *effective* role, not the account's: a request made with a
    user-scoped API key belonging to an admin reports "user", because that is
    what it may actually do. account_role reports the account itself, for the
    rare caller that needs to tell the two apart. Reporting the account's role
    as "role" would have a client believe it has permissions the server will
    refuse to act on."""
    actor = actor_from(request)
    cfg = get_config(request)
    payload = {
        "authenticated": True,
        "id": actor.user.id,
        "display_name": actor.user.display_name,
        "role": actor.role,
        "account_role": actor.user.role,
        "auth": actor.method,
        "api_keys_enabled": cfg.api_keys_enabled,
        # What this server can do, so the page shows only what will work.
        "features": {
            "youtube": cfg.youtube_enabled,
            "google": cfg.google_enabled,
            "video_extensions": list(VIDEO_EXTENSIONS),
            "upload_max_bytes": cfg.upload_max_bytes,
        },
    }
    # Only a session has a CSRF token. A key-authenticated request has no
    # ambient credential to protect, so there is nothing to hand back -- see
    # check_csrf.
    if actor.session is not None:
        payload["csrf_token"] = actor.session.csrf_token
    if actor.api_key is not None:
        payload["api_key_id"] = actor.api_key.id
        payload["api_key_name"] = actor.api_key.name
    return JSONResponse(payload)


async def rename(request: Request) -> Response:
    body = await read_json(request)
    if body is None:
        return bad_request("malformed request body")
    if not check_csrf(request, body.get("csrf_token")):
        return forbidden("invalid csrf token")
    display_name = body.get("display_name")
    if not users.valid_display_name(display_name):
        return bad_request("display name must be 1-80 characters")

    if not await run(users.rename_user, get_store(request), actor_from(request).user.id, display_name):
        return server_error()
    return JSONResponse({"ok": True, "display_name": display_name.strip()})


async def logout(request: Request) -> Response:
    """Deletes the session server-side as well as clearing the cookie, so a
    copy of the cookie captured earlier is dead too.

    It is held to the CSRF token like every other state change. SameSite=
    Strict already stops another site from sending the cookie here, but not a
    page on a sibling subdomain, and being signed out by someone else's page
    is the first step of tricking a person into signing in to the wrong
    account.

    Called with an API key it does nothing and says so by succeeding: there is
    no session to destroy. A key is ended by revoking it, not by logging out."""
    body = await read_json(request)
    if body is None:
        return bad_request("malformed request body")
    if not check_csrf(request, body.get("csrf_token")):
        return forbidden("invalid csrf token")
    await destroy_cookie_session(request)
    response = JSONResponse({"ok": True})
    clear_session_cookie(response, get_config(request))
    return response


async def destroy_cookie_session(request: Request) -> None:
    """Delete the session named by this request's cookie, if any. Deleting
    the record rather than only the cookie is what matters: a copy of the
    cookie taken earlier is then dead too."""
    cfg = get_config(request)
    raw = request.cookies.get(cfg.cookie_name, "")
    if not raw:
        return
    try:
        credential = decrypt_cookie(cfg, raw)
    except (CipherTextError, BadKeyError):
        return
    await run(sessions.destroy_session, get_store(request), credential)
