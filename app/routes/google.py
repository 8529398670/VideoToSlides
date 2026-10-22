"""Connecting a user's Google account, for Slides export. Needs a Google
Cloud OAuth client, set by an admin in Settings (or in config.yaml).

    GET  /api/google              configured? connected? which account?
    POST /api/google/connect      -> {url}: where to send the browser
    GET  /api/google/callback     Google sends the browser back here
    POST /api/google/disconnect

connect and disconnect need a browser session, not an API key: linking an
outside account to this one is a person's decision, like minting a key.

The callback is public. The session cookie is SameSite=Strict, so it does
not arrive on a navigation coming from google.com, and the user is instead
identified by the single-use `state` that connect issued to them (see
app/models/oauth_states.py). An attacker cannot use their own state to
attach anything to someone else's account: a state only ever names the
account that started the flow.
"""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from app.core import encryption
from app.core.security import actor_from, check_csrf
from app.google import oauth
from app.models import google_accounts, oauth_states
from app.routes.helpers import bad_request, forbidden, get_config, get_store, not_found, read_json, run

CODE_MAX = 2048
STATE_MAX = 256


async def status(request: Request) -> Response:
    cfg = get_config(request)
    payload = {"enabled": cfg.google_enabled, "connected": False, "email": None}
    if cfg.google_enabled:
        account = await run(google_accounts.get_account, get_store(request), actor_from(request).user.id)
        payload["connected"] = account is not None
        payload["email"] = account.email if account else None
    return JSONResponse(payload)


async def connect(request: Request) -> Response:
    cfg = get_config(request)
    if not cfg.google_enabled:
        return not_found("Google Slides export is not configured on this server")
    body = await read_json(request)
    if body is None:
        return bad_request("malformed request body")
    if not check_csrf(request, body.get("csrf_token")):
        return forbidden("invalid csrf token")
    verifier, challenge = encryption.pkce_pair()
    state = await run(oauth_states.issue_state, get_store(request), actor_from(request).user.id, verifier)
    return JSONResponse({"url": oauth.consent_url(cfg, state, challenge)})


def _back(outcome: str) -> Response:
    # 303 to a fixed local path: nothing from the query string is reflected.
    return RedirectResponse(f"/?google={outcome}", status_code=303, headers={"referrer-policy": "no-referrer"})


async def callback(request: Request) -> Response:
    cfg = get_config(request)
    if not cfg.google_enabled:
        return _back("failed")
    state = request.query_params.get("state", "")
    code = request.query_params.get("code", "")
    if not state or len(state) > STATE_MAX:
        return _back("failed")
    store = get_store(request)
    # Consumed even when the user declined, so a state is never reusable.
    claimed = await run(oauth_states.consume_state, store, state)
    if claimed is None or not code or len(code) > CODE_MAX:
        return _back("failed")
    user_id, verifier = claimed
    try:
        refresh_token, email = await run(oauth.exchange_code, cfg, code, verifier)
    except oauth.GoogleScopeMissing:
        print(f"[google] user {user_id} signed in without granting Drive access", flush=True)
        return _back("noscope")
    except oauth.GoogleError as failure:
        print(f"[google] connecting user {user_id} failed: {failure}", flush=True)
        return _back("failed")
    await run(google_accounts.save_account, store, user_id, refresh_token, email)
    return _back("connected")


async def disconnect(request: Request) -> Response:
    body = await read_json(request)
    if body is None:
        return bad_request("malformed request body")
    if not check_csrf(request, body.get("csrf_token")):
        return forbidden("invalid csrf token")
    store = get_store(request)
    user_id = actor_from(request).user.id
    account = await run(google_accounts.get_account, store, user_id)
    if account is not None:
        await run(google_accounts.delete_account, store, user_id)
        await run(oauth.revoke, account.refresh_token)
    return JSONResponse({"ok": True})
