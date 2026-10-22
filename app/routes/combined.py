"""Combined decks: a whole batch (playlist or dropped folder) in one
PowerPoint / Google Slides deck. See app/models/combined.py.

POST /api/combined saves the person's choice for a batch (title, order,
videos left out) and starts the export in the background, like a single
job's export; the record's `exports` reports progress. Every video named
must be the caller's own.
"""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from app.core.security import actor_from, check_csrf
from app.google import oauth
from app.jobs import exports, files
from app.models import combined, jobs
from app.routes.exports import PDF_MIME, PPTX_MIME, _google_ready
from app.routes.helpers import (
    bad_request,
    error,
    forbidden,
    get_config,
    get_store,
    not_found,
    parse_id,
    read_json,
    run,
    spawn,
)

MAX_VIDEOS = 1000


def _id_list(value: object) -> list[int] | None:
    if not isinstance(value, list) or len(value) > MAX_VIDEOS:
        return None
    if not all(isinstance(item, int) and not isinstance(item, bool) and item > 0 for item in value):
        return None
    return list(dict.fromkeys(value))


async def _checked_body(request: Request) -> dict | None:
    body = await read_json(request)
    if body is None or not check_csrf(request, body.get("csrf_token")):
        return None
    return body


async def _owned(request: Request) -> combined.Combined | None:
    combined_id = parse_id(request.path_params.get("combined_id", ""))
    if combined_id is None:
        return None
    combo = await run(combined.get, get_store(request), combined_id)
    actor = actor_from(request)
    if combo is None or actor is None or combo.user_id != actor.user.id:
        return None
    return combo


async def list_combined(request: Request) -> Response:
    found = await run(combined.list_for_user, get_store(request), actor_from(request).user.id)
    return JSONResponse([combo.to_public() for combo in found])


async def start_combined_export(request: Request) -> Response:
    body = await _checked_body(request)
    if body is None:
        return forbidden("invalid csrf token")
    batch = body.get("batch")
    title = body.get("title")
    job_ids = _id_list(body.get("job_ids"))
    left_out = _id_list(body.get("left_out", []))
    if not isinstance(batch, str) or not batch.strip() or job_ids is None or left_out is None:
        return bad_request("malformed request body")
    left_out = [job_id for job_id in left_out if job_id in job_ids]
    kinds = [kind for kind in jobs.EXPORT_KINDS if body.get(kind) is True]
    gslides_new = body.get("gslides_new") is True

    cfg, store = get_config(request), get_store(request)
    user_id = actor_from(request).user.id
    for job_id in job_ids + left_out:
        job = await run(jobs.get_job, store, job_id)
        if job is None or job.user_id != user_id:
            return not_found("no such job")
    if not [job_id for job_id in job_ids if job_id not in left_out]:
        return bad_request("tick at least one video")
    if jobs.EXPORT_GSLIDES in kinds and not await run(_google_ready, store, cfg, user_id):
        return error(409, "connect your Google account first (Profile -> Google Slides)")

    combo = await run(combined.save_choice, store, user_id, batch.strip(), str(title or batch),
                      job_ids, left_out)
    # No format chosen saves the choice only: the page does that before
    # sending the person to Google's consent screen.
    if not kinds:
        return JSONResponse(combo.to_public())
    started = await run(combined.start_export, store, combo.id, kinds)
    if started is None:
        return error(409, "that combined deck is already being built")
    spawn(request, run(exports.run_combined_export, store, cfg, combo.id, kinds, gslides_new))
    return JSONResponse(started.to_public())


async def download_combined_pptx(request: Request) -> Response:
    combo = await _owned(request)
    if combo is None:
        return not_found("no such combined deck")
    path = files.combined_dir(get_config(request), combo.id) / files.DECK_FILE
    if not path.is_file():
        return not_found("no combined deck has been built yet")
    return FileResponse(path, media_type=PPTX_MIME, filename=files.download_name(combo.title, ".pptx"),
                        headers={"cache-control": "no-store"})


async def download_combined_pdf(request: Request) -> Response:
    combo = await _owned(request)
    if combo is None:
        return not_found("no such combined deck")
    path = files.combined_dir(get_config(request), combo.id) / files.PDF_FILE
    if not path.is_file():
        return not_found("no combined PDF has been built yet")
    return FileResponse(path, media_type=PDF_MIME, filename=files.download_name(combo.title, ".pdf"),
                        headers={"cache-control": "no-store"})


async def delete_combined_gslides(request: Request) -> Response:
    """Trash the combined Google deck and forget it; the videos stay."""
    if await _checked_body(request) is None:
        return forbidden("invalid csrf token")
    combo = await _owned(request)
    if combo is None:
        return not_found("no such combined deck")
    if (combo.exports.get(jobs.EXPORT_GSLIDES) or {}).get("status") == jobs.EXPORT_WORKING:
        return error(409, "the Google Slides deck is being updated -- try again when it finishes")
    file_id = combo.gslides_file_id
    if not file_id:
        return not_found("there is no combined Google Slides deck")
    store = get_store(request)
    try:
        await run(exports.delete_google_deck, store, get_config(request), combo)
    except (exports.ExportError, oauth.GoogleError) as failure:
        return error(502, f"the Google Slides deck could not be deleted: {failure}")
    updated = await run(combined.forget_gslides, store, combo.id, file_id)
    if updated is None:
        return error(409, "the Google Slides deck is being updated -- try again when it finishes")
    return JSONResponse(updated.to_public())
