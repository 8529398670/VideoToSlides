"""The whole login flow. Redeeming a valid link is the login; there is no
form, no password, and nothing to phish out of a user beyond the link itself,
which dies on first use.

It takes two steps. GET /login/<credential> only serves the app's page
(StaticServer.serve_login_page), which asks the person to confirm; the page
then POSTs the credential here. Link previews in chat apps, mail scanners, and
browser prefetch all fetch a URL without anyone asking them to -- if the GET
were the login, they would spend the link before its owner ever clicked it.
And someone already signed in is told that continuing switches accounts,
rather than being silently signed in as whoever the link belongs to.
"""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.security import set_session_cookie
from app.models import login_tokens
from app.routes.account import destroy_cookie_session
from app.routes.helpers import bad_request, forbidden, get_config, get_store, read_json, run


def same_origin_json(request: Request) -> bool:
    """True for a request with a JSON content type that, if it came from a
    browser, came from one of this site's own pages."""
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        return False
    return request.headers.get("sec-fetch-site", "same-origin") == "same-origin"


async def redeem_login_link(request: Request) -> Response:
    """The one state-changing route with no CSRF token -- there is no session
    yet to hold one -- so it is guarded differently. A forged login signs a
    victim's browser in to the *attacker's* account, after which anything the
    victim does lands where the attacker can read it. Requiring a JSON body is
    what blocks that: an HTML form cannot send one, and a cross-origin fetch
    that tries needs a CORS preflight this server never approves.
    Sec-Fetch-Site, which every current browser sends, refuses the rest
    outright."""
    if not same_origin_json(request):
        return forbidden("login must be submitted from this site's own login page")
    body = await read_json(request)
    if body is None:
        return bad_request("malformed request body")
    credential = body.get("credential")
    credential = credential.strip().strip("/") if isinstance(credential, str) else ""

    store, cfg = get_store(request), get_config(request)
    # Expired, already spent, never existed, wrong secret, or a disabled
    # account all get the same answer. Telling them apart would let someone
    # probe which links are real.
    redeemed = await run(login_tokens.redeem_login_token, store, credential, cfg.session_ttl_seconds) if credential else None
    if redeemed is None:
        return bad_request("invalid login link")
    session_credential, _ = redeemed

    # Switching accounts: the session this browser had is ended server-side,
    # not merely replaced in the cookie jar.
    await destroy_cookie_session(request)
    response = JSONResponse({"ok": True})
    set_session_cookie(response, cfg, session_credential)
    return response
