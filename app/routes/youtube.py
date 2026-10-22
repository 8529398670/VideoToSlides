"""Queue YouTube videos: one URL in, one job per video out.

Expanding a playlist is a network call to YouTube, done here so the user
sees at once how many jobs a URL became (and gets an error for a bad URL
straight away rather than from a failed job later). The downloads
themselves happen in the workers.
"""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.db import Store
from app.core.security import actor_from, check_csrf
from app.models import jobs
from app.routes.helpers import bad_request, forbidden, get_config, get_dispatcher, get_store, not_found, read_json, run
from app.sources import youtube


def _queue(store: Store, user_id: int, batch: str | None, entries: list[youtube.Entry]) -> list[jobs.Job]:
    return [
        jobs.create_job(store, user_id, jobs.SOURCE_YOUTUBE, entry.title, source_url=entry.url, batch=batch)
        for entry in entries
    ]


async def add_youtube(request: Request) -> Response:
    cfg = get_config(request)
    if not cfg.youtube_enabled:
        return not_found("YouTube import is switched off")
    body = await read_json(request)
    if body is None:
        return bad_request("malformed request body")
    if not check_csrf(request, body.get("csrf_token")):
        return forbidden("invalid csrf token")
    url = youtube.normalize_url(body.get("url"))
    if url is None:
        return bad_request("that is not a YouTube URL")

    try:
        batch, entries = await run(youtube.expand, url, cfg.youtube_max_playlist_items)
    except youtube.YouTubeError as failure:
        return bad_request(str(failure))
    created = await run(_queue, get_store(request), actor_from(request).user.id, batch, entries)
    get_dispatcher(request).poke()
    return JSONResponse({"batch": batch, "jobs": [job.to_public() for job in created]}, status_code=201)
