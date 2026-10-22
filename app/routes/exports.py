"""Turning a finished job into a PowerPoint and/or a Google Slides deck, and
downloading the results.

POST /export answers at once and builds in the background; the job's
`exports` field reports progress, which the page polls. Downloads build the
zip on demand and stream the last-built deck.
"""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from app.core.security import check_csrf
from app.jobs import exports, files
from app.models import google_accounts, jobs
from app.routes.helpers import (
    bad_request,
    error,
    forbidden,
    get_config,
    get_store,
    not_found,
    owned_job,
    read_json,
    run,
    spawn,
)

PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def _google_ready(store, cfg, user_id: int) -> bool:
    return cfg.google_enabled and google_accounts.get_account(store, user_id) is not None


async def start_export(request: Request) -> Response:
    body = await read_json(request)
    if body is None:
        return bad_request("malformed request body")
    if not check_csrf(request, body.get("csrf_token")):
        return forbidden("invalid csrf token")
    kinds = [kind for kind in jobs.EXPORT_KINDS if body.get(kind) is True]
    if not kinds:
        return bad_request("choose PowerPoint, Google Slides, or both")
    # Converting again replaces the job's Google deck unless a copy is asked for.
    gslides_new = body.get("gslides_new") is True

    job = await owned_job(request)
    if job is None:
        return not_found("no such job")
    cfg, store = get_config(request), get_store(request)
    if jobs.EXPORT_GSLIDES in kinds and not await run(_google_ready, store, cfg, job.user_id):
        return error(409, "connect your Google account first (Profile -> Google Slides)")
    if not job.included_slides:
        return bad_request("every slide is excluded -- include at least one")

    started = await run(jobs.start_export, store, job.id, kinds)
    if started is None:
        return error(409, "the job is not ready, or that export is already running")
    spawn(request, run(exports.run_export, store, cfg, job.id, kinds, gslides_new))
    return JSONResponse(started.to_public())


async def download_pptx(request: Request) -> Response:
    job = await owned_job(request)
    if job is None:
        return not_found("no such job")
    path = files.job_dir(get_config(request), job.id) / files.DECK_FILE
    if not path.is_file():
        return not_found("no deck has been built yet")
    return FileResponse(path, media_type=PPTX_MIME, filename=files.download_name(job.title, ".pptx"),
                        headers={"cache-control": "no-store"})


async def download_images(request: Request) -> Response:
    job = await owned_job(request)
    if job is None or job.status != jobs.STATUS_READY:
        return not_found("no such job")
    try:
        path = await run(exports.build_images_zip, get_config(request), job)
    except exports.ExportError as failure:
        return bad_request(str(failure))
    return FileResponse(path, media_type="application/zip", filename=files.download_name(job.title, ".zip"),
                        headers={"cache-control": "no-store"})
