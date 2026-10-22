"""The only place that decides who a request belongs to and whether it is
allowed to change anything. Route handlers read the answer; they never
re-derive it. One implementation means one place to get right, and one place
to look when auditing.

There are two ways to prove who you are, and they meet here rather than
anywhere downstream: a session cookie (a browser) or an API key in an
Authorization header (a script, a cron job, another service). Everything past
this module sees one Actor and one effective role, so a route does not know or
care which door the request came through.
"""
from __future__ import annotations

from dataclasses import dataclass

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core import encryption
from app.core.config import Config
from app.core.db import Store
from app.models.api_keys import APIKey, load_api_key, narrow_role
from app.models.sessions import Session, load_session
from app.models.users import ROLE_ADMIN, User

# Auth method names, as reported by /api/me. Worth exposing so a client can
# tell a browser session from a key without guessing.
METHOD_SESSION = "session"
METHOD_API_KEY = "api_key"


@dataclass
class Actor:
    """Who a request belongs to, plus how they proved it.

    role is the *effective* role, not the account's: a user-scoped API key
    held by an admin authenticates as a user. Every permission check reads
    this field, so the narrowing cannot be forgotten at a call site."""

    user: User
    role: str
    session: Session | None = None
    api_key: APIKey | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    @property
    def via_api_key(self) -> bool:
        """Distinguishes the two doors. It decides the CSRF question below, and
        it is what an endpoint reads if it genuinely must be browser-only."""
        return self.api_key is not None

    @property
    def method(self) -> str:
        return METHOD_API_KEY if self.via_api_key else METHOD_SESSION


# ---------------------------------------------------------------------------
# the session cookie
# ---------------------------------------------------------------------------


def set_session_cookie(response: Response, cfg: Config, credential: str) -> None:
    """Store the session credential encrypted with the server key. The server
    already checks the credential against the database, so the encryption is
    not what makes the session unforgeable -- it means the cookie is opaque to
    anyone reading it out of a browser profile or a proxy log, and that a
    tampered cookie fails to decrypt instead of reaching the lookup."""
    response.set_cookie(
        cfg.cookie_name,
        encryption.xchacha_encrypt_string(cfg.secret_key, credential),
        max_age=cfg.session_ttl_seconds,
        path="/",
        secure=cfg.secure_cookies,
        httponly=True,
        samesite="strict",
    )


def clear_session_cookie(response: Response, cfg: Config) -> None:
    response.delete_cookie(cfg.cookie_name, path="/", secure=cfg.secure_cookies, httponly=True, samesite="strict")


def decrypt_cookie(cfg: Config, raw: str) -> str:
    """Turn the stored cookie back into a session credential. Exposed because
    logging out has to destroy the server-side record, not just drop the
    cookie -- otherwise a copy of the cookie taken earlier still works.
    Raises encryption.CipherTextError for anything that is not ours."""
    return encryption.xchacha_decrypt_string(cfg.secret_key, raw)


# ---------------------------------------------------------------------------
# resolving the caller
# ---------------------------------------------------------------------------


def _resolve_session(store: Store, cfg: Config, cookies: dict[str, str]) -> Actor | None:
    raw = cookies.get(cfg.cookie_name, "")
    if raw == "":
        return None
    try:
        credential = decrypt_cookie(cfg, raw)
    except (encryption.CipherTextError, encryption.BadKeyError):
        return None
    loaded = load_session(store, credential)
    if loaded is None:
        return None
    session, user = loaded
    return Actor(user=user, role=user.role, session=session)


def _resolve_api_key(store: Store, cfg: Config, authorization: str) -> Actor | None:
    """Reads "Authorization: Bearer <id>.<secret>".

    One header and one scheme, deliberately: a second accepted spelling (an
    X-API-Key, a query parameter) is a second code path to audit, and a
    credential in a query string ends up in access logs and browser history.

    cfg.api_keys_enabled is checked here rather than only on the routes that
    mint keys, so turning the feature off actually closes the door: the header
    stops being believed at all, and existing keys stop authenticating."""
    if not cfg.api_keys_enabled:
        return None
    header = authorization.strip()
    scheme, _, credential = header.partition(" ")
    if scheme.lower() != "bearer" or credential.strip() == "":
        return None
    loaded = load_api_key(store, credential.strip())
    if loaded is None:
        return None
    key, user = loaded
    # The ceiling, applied per request. See narrow_role: the stored role is
    # what was asked for, this is what is granted.
    return Actor(user=user, role=narrow_role(user.role, key.role), api_key=key)


def resolve_actor(store: Store, cfg: Config, cookies: dict[str, str], authorization: str) -> Actor | None:
    """The cookie is tried first and an API key only if there is no live
    session. Either order is safe, but this one keeps the CSRF rule below
    unambiguous: a request that arrives with ambient browser credentials is
    always held to the token check, and cannot opt out of it by also carrying
    a key."""
    return _resolve_session(store, cfg, cookies) or _resolve_api_key(store, cfg, authorization)


class LoadActor:
    """Runs on every request. It resolves the caller once and stashes the
    result on request.state, so a handler and a guard asking "who is this" do
    not cost two database reads. It never rejects anything -- that is the
    guards' job -- because plenty of routes are fine for anonymous visitors.

    The lookup touches SQLite, so it runs in a worker thread rather than on
    the event loop."""

    def __init__(self, app: ASGIApp, store: Store, cfg: Config) -> None:
        self.app = app
        self.store = store
        self.cfg = cfg

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            request = Request(scope)
            actor = await run_in_threadpool(
                resolve_actor, self.store, self.cfg, dict(request.cookies), request.headers.get("authorization", "")
            )
            request.state.actor = actor
        await self.app(scope, receive, send)


def actor_from(request: Request) -> Actor | None:
    return getattr(request.state, "actor", None)


# ---------------------------------------------------------------------------
# guards
#
# RequireLogin, RequireAdmin, and RequireSession are middleware attached to
# routes where they are registered (app/routes/__init__.py), rather than
# helpers called at the top of each handler, so a new protected route is
# protected by where it is registered -- forgetting the check is not something
# you can do silently.
# ---------------------------------------------------------------------------


def _refuse(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


class Guard:
    """Base for the guards: subclasses return a refusal, or None to let the
    request through."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    def refuse(self, request: Request) -> Response | None:  # pragma: no cover - overridden
        raise NotImplementedError

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        refusal = self.refuse(Request(scope))
        if refusal is not None:
            await refusal(scope, receive, send)
            return
        await self.app(scope, receive, send)


class RequireLogin(Guard):
    def refuse(self, request: Request) -> Response | None:
        if actor_from(request) is None:
            return _refuse(401, "not authenticated")
        return None


class RequireAdmin(Guard):
    def refuse(self, request: Request) -> Response | None:
        actor = actor_from(request)
        if actor is None:
            return _refuse(401, "not authenticated")
        if not actor.is_admin:
            return _refuse(403, "forbidden")
        return None


class RequireSession(Guard):
    """Refuses a request that authenticated with an API key, even a correctly
    scoped one. It guards every route that creates a credential: minting and
    revoking API keys, creating an account, and reissuing a login link (see
    app/routes/__init__.py).

    The reasoning is containment rather than privilege. A key is a bearer
    credential that lives in a config file or a CI secret; if a leaked key
    could create another credential -- a key directly, or a login link that
    becomes a session that mints a key -- revoking it would not end the
    compromise, because what it created while you were not looking survives.
    Requiring a browser session for all of these means every credential
    traces back to a person signing in, and revoking a key actually revokes
    it.

    What a leaked admin-scoped key can still do is everything else an admin
    can: list and disable accounts, revoke access. That is why admin keys have
    to be asked for, and why the admin key list exists.

    Drop this from a route if an app genuinely needs to provision accounts or
    keys programmatically -- but do it knowingly: such a key can then mint its
    own successors, and that app's docs should say so."""

    def refuse(self, request: Request) -> Response | None:
        actor = actor_from(request)
        if actor is None:
            return _refuse(401, "not authenticated")
        if actor.via_api_key:
            return _refuse(403, "this endpoint requires a signed-in session, not an API key")
        return None


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


def check_csrf(request: Request, submitted: object) -> bool:
    """Guards state-changing requests. SameSite=Strict already blocks the
    common cross-site POST, but it is a browser-side promise: older browsers,
    odd cross-scheme cases, and non-browser clients do not all honour it. A
    per-session token echoed back in the JSON body costs nothing and does not
    depend on the browser behaving.

    A request authenticated by an API key is exempt, and that is not a hole.
    CSRF exists because a cookie is *ambient* -- the browser attaches it to a
    request some other site caused. Nothing attaches an Authorization header
    on its own; a forged cross-site request from an attacker's page carries no
    key, so there is nothing to forge with. Requiring a token here would
    instead mean every script had to fetch one first, for no gain."""
    actor = actor_from(request)
    if actor is None:
        return False
    if actor.via_api_key:
        return True
    if actor.session is None or not isinstance(submitted, str) or submitted == "":
        return False
    return encryption.constant_time_equal(actor.session.csrf_token, submitted)
