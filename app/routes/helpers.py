"""Small pieces every route module uses: the shared dependencies, the one
error shape, and the rule for calling synchronous code.

The rule: SQLite and bcrypt are synchronous, and an async handler that calls
them directly stalls every other request while it waits. So handlers reach the
models through `await run(...)`, which runs the call in a worker thread.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.config import Config
from app.core.db import Store
from app.core.security import actor_from
from app.models import jobs
from app.models.jobs import Job

if TYPE_CHECKING:
    from app.jobs.dispatcher import Dispatcher

run = run_in_threadpool


def get_store(request: Request) -> Store:
    return request.app.state.store


def get_config(request: Request) -> Config:
    return request.app.state.config


async def read_json(request: Request) -> dict[str, Any] | None:
    """The request body as a JSON object, or None if it is not one -- every
    endpoint answers that the same way (bad_request "malformed request
    body")."""
    try:
        body = await request.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


# One error shape for the whole API, so the frontend has one thing to handle
# and no handler accidentally leaks internal detail.


def error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def bad_request(message: str) -> JSONResponse:
    return error(400, message)


def forbidden(message: str) -> JSONResponse:
    return error(403, message)


def not_found(message: str) -> JSONResponse:
    return error(404, message)


def server_error() -> JSONResponse:
    return error(500, "internal error")


def parse_id(raw: str) -> int | None:
    """A positive integer path parameter, or None."""
    return int(raw) if raw.isascii() and raw.isdigit() and int(raw) > 0 else None


def get_dispatcher(request: Request) -> "Dispatcher":
    return request.app.state.dispatcher


def spawn(request: Request, coroutine: Any) -> None:
    """Run work after the response is sent, keeping a reference so the task
    is not garbage-collected mid-flight (asyncio holds tasks weakly)."""
    tasks: set = request.app.state.background_tasks
    task = asyncio.create_task(coroutine)
    tasks.add(task)
    task.add_done_callback(tasks.discard)


async def owned_job(request: Request) -> Job | None:
    """The job named by the {job_id} path parameter, if it belongs to the
    caller. Someone else's job answers exactly like a missing one, so ids
    cannot be probed. Ownership is per account, admins included: admin here
    means "decides who gets in", not "reads everyone's videos"."""
    job_id = parse_id(request.path_params.get("job_id", ""))
    if job_id is None:
        return None
    job = await run(jobs.get_job, get_store(request), job_id)
    actor = actor_from(request)
    if job is None or actor is None or job.user_id != actor.user.id:
        return None
    return job
