"""The slide images themselves, full size and thumbnail. Served through
these routes rather than as static files because they are private: only the
job's owner can fetch them.

Image URLs carry ?r=<run>, which changes whenever a job is retried, so a
long private cache is safe -- a new run is a new URL."""
from __future__ import annotations

from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, Response

from app.jobs import files
from app.models.jobs import Job
from app.routes.helpers import get_config, not_found, owned_job

IMAGE_CACHE = "private, max-age=604800, immutable"


def _slide_file(job: Job, raw_n: str) -> str | None:
    if not raw_n.isdigit():
        return None
    n = int(raw_n)
    for slide in job.slides:
        if slide["n"] == n:
            return slide["file"]
    return None


async def _serve(request: Request, directory_of) -> Response:
    job = await owned_job(request)
    name = None if job is None else _slide_file(job, request.path_params.get("n", ""))
    if job is None or name is None:
        return not_found("no such slide")
    path: Path = directory_of(get_config(request), job.id) / name
    if not path.is_file():
        return not_found("no such slide")
    return FileResponse(path, media_type="image/jpeg", headers={"cache-control": IMAGE_CACHE})


async def slide_image(request: Request) -> Response:
    return await _serve(request, files.slides_dir)


async def thumb_image(request: Request) -> Response:
    return await _serve(request, files.thumbs_dir)
