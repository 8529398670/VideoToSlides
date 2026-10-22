"""Chunked video uploads from the browser.

    POST /api/uploads                      {filename, size, batch?} -> {id, chunk_bytes}
    POST /api/uploads/{id}/chunk?offset=N  raw bytes, X-CSRF-Token header
    POST /api/uploads/{id}/complete        -> the new job
    POST /api/uploads/{id}/abort

Chunks rather than one request: a lecture recording is gigabytes, and one
request that size is one network hiccup away from starting over. Each chunk
is its own request with its own body limit (uploads.chunk_bytes, applied to
the chunk route alone), and is accepted only at exactly the offset received
so far -- so a retried chunk is harmless and a gap is impossible.

The chunk body is raw bytes, so the CSRF token travels in a header instead
of the JSON body. It is checked by the same check_csrf.
"""
from __future__ import annotations

import os

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.config import Config
from app.core.security import actor_from, check_csrf
from app.jobs import files
from app.models import jobs, uploads
from app.pipeline.tunables import VIDEO_EXTENSIONS
from app.routes.helpers import (
    bad_request,
    error,
    forbidden,
    get_config,
    get_dispatcher,
    get_store,
    not_found,
    read_json,
    run,
)

CSRF_HEADER = "x-csrf-token"
BATCH_MAX = 200


async def _owned_upload(request: Request) -> uploads.Upload | None:
    upload_id = request.path_params.get("upload_id", "")
    try:
        files.upload_part_path(get_config(request), upload_id)
    except ValueError:
        return None
    upload = await run(uploads.get_upload, get_store(request), upload_id)
    if upload is None or upload.user_id != actor_from(request).user.id:
        return None
    return upload


def _create_part_file(cfg: Config, upload_id: str) -> None:
    path = files.upload_part_path(cfg, upload_id)
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))


async def start_upload(request: Request) -> Response:
    body = await read_json(request)
    if body is None:
        return bad_request("malformed request body")
    if not check_csrf(request, body.get("csrf_token")):
        return forbidden("invalid csrf token")
    cfg = get_config(request)
    filename, size, batch = body.get("filename"), body.get("size"), body.get("batch")
    if not isinstance(filename, str) or not filename.strip():
        return bad_request("filename is required")
    if files.safe_extension(filename, VIDEO_EXTENSIONS) is None:
        return bad_request("not a supported video type (" + ", ".join(VIDEO_EXTENSIONS) + ")")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        return bad_request("size must be a positive whole number of bytes")
    if size > cfg.upload_max_bytes:
        return error(413, "that video is larger than this server accepts")
    if batch is not None and (not isinstance(batch, str) or len(batch) > BATCH_MAX):
        return bad_request("batch must be a short name")

    upload = await run(uploads.create_upload, get_store(request), actor_from(request).user.id,
                       os.path.basename(filename.strip()), size, (batch or "").strip() or None)
    await run(_create_part_file, cfg, upload.id)
    return JSONResponse({"id": upload.id, "chunk_bytes": cfg.upload_chunk_bytes})


def _write_chunk(cfg: Config, upload_id: str, offset: int, data: bytes) -> None:
    with open(files.upload_part_path(cfg, upload_id), "r+b") as handle:
        handle.seek(offset)
        handle.write(data)


async def upload_chunk(request: Request) -> Response:
    if not check_csrf(request, request.headers.get(CSRF_HEADER)):
        return forbidden("invalid csrf token")
    upload = await _owned_upload(request)
    if upload is None:
        return not_found("no such upload")
    raw_offset = request.query_params.get("offset", "")
    if not raw_offset.isdigit():
        return bad_request("offset is required")
    offset = int(raw_offset)
    if offset != upload.received:
        # Tell the client where to resume rather than just refusing.
        return JSONResponse({"error": "unexpected offset", "received": upload.received}, status_code=409)

    data = await request.body()
    if not data:
        return bad_request("empty chunk")
    if offset + len(data) > upload.size:
        return bad_request("chunk runs past the declared size")
    cfg = get_config(request)
    await run(_write_chunk, cfg, upload.id, offset, data)
    received = await run(uploads.advance, get_store(request), upload.id, offset, len(data))
    if received is None:
        current = await run(uploads.get_upload, get_store(request), upload.id)
        return JSONResponse({"error": "unexpected offset", "received": current.received if current else 0},
                            status_code=409)
    return JSONResponse({"received": received})


def _move_into_job(cfg: Config, upload: uploads.Upload, job_id: int, extension: str) -> str:
    directory = files.job_dir(cfg, job_id)
    directory.mkdir(parents=True, exist_ok=True)
    name = files.SOURCE_STEM + extension
    files.upload_part_path(cfg, upload.id).replace(directory / name)
    return name


async def complete_upload(request: Request) -> Response:
    body = await read_json(request)
    if body is None:
        return bad_request("malformed request body")
    if not check_csrf(request, body.get("csrf_token")):
        return forbidden("invalid csrf token")
    upload = await _owned_upload(request)
    if upload is None:
        return not_found("no such upload")
    if not upload.complete:
        return JSONResponse({"error": "upload is incomplete", "received": upload.received}, status_code=409)

    cfg, store = get_config(request), get_store(request)
    extension = files.safe_extension(upload.filename, VIDEO_EXTENSIONS) or ".mp4"
    title = os.path.splitext(upload.filename)[0]
    # Video first, job second: once the job exists it is queued, and the
    # dispatcher may claim it at once.
    job_id = await run(jobs.reserve_id, store)
    source_file = await run(_move_into_job, cfg, upload, job_id, extension)
    await run(uploads.delete_upload, store, upload.id)
    job = await run(jobs.create_job, store, upload.user_id, jobs.SOURCE_UPLOAD, title,
                    batch=upload.batch, source_file=source_file, job_id=job_id)
    get_dispatcher(request).poke()
    return JSONResponse(job.to_public(), status_code=201)


async def abort_upload(request: Request) -> Response:
    body = await read_json(request)
    if body is None:
        return bad_request("malformed request body")
    if not check_csrf(request, body.get("csrf_token")):
        return forbidden("invalid csrf token")
    upload = await _owned_upload(request)
    if upload is None:
        return not_found("no such upload")
    cfg = get_config(request)
    await run(uploads.delete_upload, get_store(request), upload.id)
    await run(lambda: files.upload_part_path(cfg, upload.id).unlink(missing_ok=True))
    return JSONResponse({"ok": True})
