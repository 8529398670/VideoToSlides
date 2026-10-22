"""A signed-in user's jobs: list them, look at one, cancel, retry, delete.

Every handler works only on the caller's own jobs (owned_job). Starting a
job is not here -- it happens when an upload completes (uploads.py) or a
YouTube URL is submitted (youtube.py).
"""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.security import actor_from, check_csrf
from app.google import oauth
from app.jobs import exports, files
from app.models import jobs
from app.routes.helpers import (
    bad_request,
    error,
    forbidden,
    get_config,
    get_dispatcher,
    get_store,
    not_found,
    owned_job,
    read_json,
    run,
)


async def list_jobs(request: Request) -> Response:
    found = await run(jobs.list_user_jobs, get_store(request), actor_from(request).user.id)
    return JSONResponse([job.to_public() for job in found])


async def get_job(request: Request) -> Response:
    job = await owned_job(request)
    if job is None:
        return not_found("no such job")
    return JSONResponse(job.to_public(with_slides=True))


async def _checked_body(request: Request) -> dict | None:
    """The JSON body, or None when it is malformed or its csrf token is wrong."""
    body = await read_json(request)
    if body is None or not check_csrf(request, body.get("csrf_token")):
        return None
    return body


async def _csrf_ok(request: Request) -> bool:
    return await _checked_body(request) is not None


async def cancel_job(request: Request) -> Response:
    if not await _csrf_ok(request):
        return forbidden("invalid csrf token")
    job = await owned_job(request)
    if job is None:
        return not_found("no such job")
    cancelled = await run(jobs.cancel_job, get_store(request), job.id)
    if cancelled is None:
        return error(409, "the job is not running")
    await get_dispatcher(request).cancel(job.id)
    return JSONResponse(cancelled.to_public())


async def retry_job(request: Request) -> Response:
    if not await _csrf_ok(request):
        return forbidden("invalid csrf token")
    job = await owned_job(request)
    if job is None:
        return not_found("no such job")
    if job.status == jobs.STATUS_READY and not job.can_rerun:
        return error(409, "the uploaded video was not kept after processing -- upload it again")
    retried = await run(jobs.retry_job, get_store(request), job.id)
    if retried is None:
        return error(409, "only a finished, failed or cancelled job can be run again")
    get_dispatcher(request).poke()
    return JSONResponse(retried.to_public())


async def delete_job(request: Request) -> Response:
    """Removes the record and every file. A running job is stopped first, so
    its worker is not writing into a directory that is being deleted.

    With delete_google, the job's Google Slides deck goes to the Drive trash
    first; if that fails nothing is deleted, so the person can try again or
    keep the deck."""
    body = await _checked_body(request)
    if body is None:
        return forbidden("invalid csrf token")
    job = await owned_job(request)
    if job is None:
        return not_found("no such job")
    store = get_store(request)
    if body.get("delete_google") is True and job.gslides_file_id:
        try:
            await run(exports.delete_google_deck, store, get_config(request), job)
        except (exports.ExportError, oauth.GoogleError) as failure:
            return error(502, f"the Google Slides deck could not be deleted: {failure}")
    if job.active:
        await run(jobs.cancel_job, store, job.id)
        await get_dispatcher(request).cancel(job.id)
    await run(jobs.delete_job, store, job.id)
    await run(files.delete_job_files, get_config(request), job.id)
    return JSONResponse({"ok": True})


async def set_slide_excluded(request: Request) -> Response:
    body = await read_json(request)
    if body is None:
        return bad_request("malformed request body")
    if not check_csrf(request, body.get("csrf_token")):
        return forbidden("invalid csrf token")
    excluded = body.get("excluded")
    if not isinstance(excluded, bool):
        return bad_request("excluded must be true or false")
    job = await owned_job(request)
    raw_n = request.path_params.get("n", "")
    if job is None or not raw_n.isdigit():
        return not_found("no such slide")
    updated = await run(jobs.set_slide_excluded, get_store(request), job.id, int(raw_n), excluded)
    if updated is None:
        return not_found("no such slide")
    return JSONResponse({"ok": True, "included_count": len(updated.included_slides)})
