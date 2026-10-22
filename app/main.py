"""The web server. This module only wires things together -- load config,
resolve assets, open the database, build the app, start and stop cleanly.
Every decision of substance lives in a module under app/core/, app/models/,
or app/routes/. If you find yourself adding business logic here, it belongs
in a route module or a new core module instead.

Started by `python -m app` (see app/__main__.py), never by pointing uvicorn
at it from the command line: host, port, and every other setting come from
config.yaml, and a second place to pass them would be a second source of
truth.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

import uvicorn
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware

from app.core import assets, bootstrap, config, db, language
from app.core.middleware import EXCEPTION_HANDLERS, RateLimit, SecurityHeaders
from app.core.security import LoadActor
from app.core.static_files import StaticServer
from app.jobs import files
from app.jobs.dispatcher import Dispatcher
from app.core import settings
from app.models import api_keys, login_tokens, oauth_states, sessions, uploads
from app.models import settings as settings_model
from app.routes import build_routes

PURGE_INTERVAL_SECONDS = 60 * 60


def purge_once(store: db.Store, cfg: config.Config) -> None:
    """Drop expired sessions, expired login tokens, and API keys that have
    been dead long enough to stop being worth listing. None of it is a
    correctness issue -- every one of those is already refused on read -- so
    this runs quietly in the background purely to keep the file from growing
    forever. Abandoned uploads go too, and with them their partial files,
    which are the part that actually takes space."""
    removed_sessions = sessions.purge_expired_sessions(store)
    removed_tokens = login_tokens.purge_expired_login_tokens(store)
    removed_keys = api_keys.purge_dead_api_keys(store)
    oauth_states.purge_expired(store)
    stale_uploads = uploads.purge_stale(store)
    for upload_id in stale_uploads:
        files.upload_part_path(cfg, upload_id).unlink(missing_ok=True)
    if removed_sessions or removed_tokens or removed_keys or stale_uploads:
        print(
            f"[purge] removed {removed_sessions} expired sessions, {removed_tokens} expired login tokens, "
            f"{removed_keys} dead api keys, {len(stale_uploads)} abandoned uploads",
            flush=True,
        )


async def purge_loop(store: db.Store, cfg: config.Config) -> None:
    while True:
        await asyncio.sleep(PURGE_INTERVAL_SECONDS)
        try:
            await run_in_threadpool(purge_once, store, cfg)
        except Exception as error:  # noqa: BLE001 -- housekeeping must never take the server down
            print(f"[purge] failed: {error}", flush=True)


def build_app(cfg: config.Config, store: db.Store, lang: language.Language, static_server: StaticServer) -> Starlette:
    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        dispatcher = Dispatcher(store, app.state.config)
        app.state.dispatcher = dispatcher
        app.state.background_tasks = set()
        tasks = [asyncio.create_task(purge_loop(store, cfg)), asyncio.create_task(dispatcher.run())]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await dispatcher.shutdown()

    app = Starlette(
        routes=build_routes(static_server, cfg),
        # Outermost first. Headers go on everything; the rate limit runs
        # before anything touches the database, so a flood is cheap to refuse;
        # then the caller is resolved once for every route behind it.
        middleware=[
            Middleware(SecurityHeaders, cfg=cfg),
            Middleware(RateLimit, cfg=cfg),
            Middleware(LoadActor, store=store, cfg=cfg),
        ],
        exception_handlers=EXCEPTION_HANDLERS,
        lifespan=lifespan,
        max_body_size=cfg.body_limit_bytes,
    )
    # base_config is config.yaml as loaded; config is that plus whatever an
    # admin changed in Settings (app/core/settings.py), and is what every
    # route reads.
    app.state.base_config = cfg
    overrides = settings_model.get_overrides(store)
    settings.apply_tunables(overrides)
    app.state.config = settings.apply_config(cfg, overrides)
    app.state.store = store
    app.state.language = lang
    return app


def serve(version: str) -> None:
    cfg = config.load()
    bundle = assets.resolve(cfg.static_dir, cfg.language_file)
    store = db.open_store(cfg.database_path, cfg.secret_key, cfg.encrypt_at_rest)
    lang = language.parse(bundle.language_yaml)
    static_server = StaticServer(bundle.static_dir)

    bootstrap.ensure_first_admin(store, cfg, "Admin")

    app = build_app(cfg, store, lang, static_server)

    print(f"[start] {cfg.project_name} {version} listening on {cfg.listen_address} (secure cookies: {cfg.secure_cookies})")
    if cfg.config_created:
        print(f"[config] {cfg.config_file} (created, with a new secret key -- back it up)")
    else:
        print(f"[config] {cfg.config_file}")
    if cfg.config_warning:
        print(f"[config] warning: {cfg.config_warning}")
    print(f"[state]  {cfg.app_dir}")
    print(f"[assets] {bundle.source}", flush=True)

    uvicorn.run(
        app,
        host=cfg.host,
        port=cfg.port,
        # An empty Server header keeps the response from advertising which
        # server and version is running -- free reconnaissance otherwise.
        server_header=False,
        # Proxy trust is server.trust_proxy, applied by the rate limiter's
        # client_ip(); uvicorn's own X-Forwarded-For handling would be a
        # second, differently configured answer to the same question.
        proxy_headers=False,
        # Idle keep-alive connections are dropped rather than held until the
        # process runs out of sockets; in-flight requests get 10s to finish
        # on shutdown so the database is never left mid-write.
        timeout_keep_alive=75,
        timeout_graceful_shutdown=10,
        lifespan="on",
        log_level="info",
    )
