"""The URL map -- the only place that knows every route this server answers.
Read build_routes top to bottom to see the whole surface.

Two halves. The template's: logging in, managing who can log in, issuing
the API keys that let non-browser callers in, and reading the UI's text. And
this app's: getting videos in (uploads, YouTube), following and steering the
jobs that turn them into slides, and exporting the result as a PowerPoint or
Google Slides deck. Every job route is "any signed-in user, own jobs only".

Protection comes from where a route is registered. group() attaches guards to
each route it builds, so a protected route cannot be written without its
guard, and the list below is a complete statement of who may call what.
Guards attach per route (Starlette's Route(middleware=...)), not per path
prefix, so a group never leaks onto routes outside it.
"""
from __future__ import annotations

from typing import Callable, Sequence

from starlette.middleware import Middleware
from starlette.routing import BaseRoute, Route

from app.core.config import Config
from app.core.security import RequireAdmin, RequireLogin, RequireSession
from app.core.static_files import StaticServer
from app.routes import account, admin, api_keys, auth, exports, google, jobs, meta, settings, slides, team, uploads, youtube
from app.routes.api_keys import RequireAPIKeys


def group(*guards: type) -> Callable[..., Route]:
    """A route builder whose routes all run behind the given guards, in
    order. Usage: signed_in = group(RequireLogin); signed_in("/api/me", me)."""
    middleware = [Middleware(guard) for guard in guards]

    def route(path: str, endpoint: Callable, methods: Sequence[str] = ("GET",),
              max_body_size: int | None = None) -> Route:
        # max_body_size replaces the app-wide limit for this one route.
        return Route(path, endpoint, methods=list(methods), middleware=middleware, max_body_size=max_body_size)

    return route


def build_routes(static_server: StaticServer, cfg: Config) -> list[BaseRoute]:
    public = group()
    signed_in = group(RequireLogin)
    # API keys: any signed-in user manages their own, from a browser session.
    my_keys = group(RequireAPIKeys, RequireLogin, RequireSession)
    # Admin only: deciding who gets in.
    admins = group(RequireAdmin)
    # The two admin routes that hand out a login link also require a browser
    # session. A login link becomes a session, and a session can mint API
    # keys -- so if an admin-scoped key could create accounts or reissue
    # links, a leaked key could turn itself into a fresh session and a fresh
    # key, and revoking the leaked one would not end the compromise. The rule
    # is: an API key may use what an account can do, but it never creates
    # credentials. See RequireSession.
    admin_sessions = group(RequireAdmin, RequireSession)
    # Admin view of every key in the app -- "who has standing access" -- and
    # the ability to revoke someone else's.
    admin_keys = group(RequireAPIKeys, RequireAdmin, RequireSession)
    # Linking an outside account is a person's decision, like minting a key.
    signed_in_browser = group(RequireLogin, RequireSession)
    # An upload chunk is the one body bigger than server.body_limit_bytes.
    chunk_limit = cfg.upload_chunk_bytes + 65536

    return [
        # Public: the login link's page and the call it makes, the UI's text
        # (needed to render the signed-out page at all), and the healthcheck.
        # Opening a login link only shows a page; the link is spent by the
        # POST that page sends when the person confirms. See auth.py.
        public("/login/{credential:path}", static_server.serve_login_page),
        public("/api/login", auth.redeem_login_link, ["POST"]),
        public("/api/language", meta.get_language),
        public("/api/health", meta.get_health),
        # Google sends the browser back here; the user is identified by the
        # single-use state, not the cookie. See app/routes/google.py.
        public("/api/google/callback", google.callback),
        # Any signed-in user.
        signed_in("/api/me", account.me),
        signed_in("/api/account/rename", account.rename, ["POST"]),
        signed_in("/api/logout", account.logout, ["POST"]),
        signed_in("/api/team", team.list_team),
        # Getting videos in.
        signed_in("/api/uploads", uploads.start_upload, ["POST"]),
        signed_in("/api/uploads/{upload_id}/chunk", uploads.upload_chunk, ["POST"], max_body_size=chunk_limit),
        signed_in("/api/uploads/{upload_id}/complete", uploads.complete_upload, ["POST"]),
        signed_in("/api/uploads/{upload_id}/abort", uploads.abort_upload, ["POST"]),
        signed_in("/api/jobs/youtube", youtube.add_youtube, ["POST"]),
        # The caller's own jobs.
        signed_in("/api/jobs", jobs.list_jobs),
        signed_in("/api/jobs/{job_id}", jobs.get_job),
        signed_in("/api/jobs/{job_id}/cancel", jobs.cancel_job, ["POST"]),
        signed_in("/api/jobs/{job_id}/retry", jobs.retry_job, ["POST"]),
        signed_in("/api/jobs/{job_id}/delete", jobs.delete_job, ["POST"]),
        signed_in("/api/jobs/{job_id}/slides/{n}", slides.slide_image),
        signed_in("/api/jobs/{job_id}/thumbs/{n}", slides.thumb_image),
        signed_in("/api/jobs/{job_id}/slides/{n}/exclude", jobs.set_slide_excluded, ["POST"]),
        signed_in("/api/jobs/{job_id}/export", exports.start_export, ["POST"]),
        signed_in("/api/jobs/{job_id}/download/pptx", exports.download_pptx),
        signed_in("/api/jobs/{job_id}/download/images", exports.download_images),
        # Google Slides export.
        signed_in("/api/google", google.status),
        signed_in_browser("/api/google/connect", google.connect, ["POST"]),
        signed_in_browser("/api/google/disconnect", google.disconnect, ["POST"]),
        my_keys("/api/keys", api_keys.list_my_keys),
        my_keys("/api/keys", api_keys.create_my_key, ["POST"]),
        my_keys("/api/keys/{key_id}/revoke", api_keys.revoke_my_key, ["POST"]),
        # Server-wide settings: admins, and saving only from a browser.
        admins("/api/settings", settings.get_settings),
        admin_sessions("/api/settings", settings.save_settings, ["POST"]),
        admins("/api/admin/users", admin.list_users),
        admin_sessions("/api/admin/users", admin.create_user, ["POST"]),
        admin_sessions("/api/admin/users/{user_id}/reissue-login", admin.reissue_login, ["POST"]),
        admins("/api/admin/users/{user_id}/disabled", admin.set_user_disabled, ["POST"]),
        admins("/api/admin/users/{user_id}/revoke-access", admin.revoke_access, ["POST"]),
        admin_keys("/api/admin/keys", api_keys.list_all_keys),
        admin_keys("/api/admin/keys/{key_id}/revoke", api_keys.revoke_any_key, ["POST"]),
        # Any other /api/ path is a JSON 404, not the index.html fallback.
        Route("/api/{rest:path}", meta.unknown_api),
        # Catch-all static serving. Must stay last -- it matches every
        # remaining path, so anything added after this line never runs.
        Route("/{path:path}", static_server.endpoint),
    ]
