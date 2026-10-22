"""Jobs: one video on its way to becoming a folder of slide images.

A job is created by an upload finishing or a YouTube URL being submitted,
picked up by the dispatcher (app/jobs/dispatcher.py), and run by a worker
process (app/jobs/worker.py) that reports progress here. The files live in
<storage>/jobs/<id>/; this record is everything else.

Status moves one way: queued -> starting -> [downloading] -> scanning ->
extracting -> filtering -> ready, or to failed/cancelled from any active
state. Retry puts a failed or cancelled job back to queued.

Every change goes through store.update_value, and every mutate function
checks the status it expects first. That matters here more than anywhere
else in the app: the worker process and the web server both write these
records, and a progress update arriving just after a cancel must not
resurrect the job.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Callable

from app.core.db import BUCKET_JOBS, Record, Store
from app.models import from_iso, to_iso, utc_now

STATUS_QUEUED = "queued"
STATUS_STARTING = "starting"
STATUS_DOWNLOADING = "downloading"
STATUS_SCANNING = "scanning"
STATUS_EXTRACTING = "extracting"
STATUS_FILTERING = "filtering"
STATUS_READY = "ready"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

# A worker process owns the job while it is in one of these.
RUNNING_STATUSES = (STATUS_STARTING, STATUS_DOWNLOADING, STATUS_SCANNING, STATUS_EXTRACTING, STATUS_FILTERING)
ACTIVE_STATUSES = (STATUS_QUEUED,) + RUNNING_STATUSES
# A ready job can be run again too -- to pick up pipeline improvements. Its
# slides and exclusions are replaced.
RETRYABLE_STATUSES = (STATUS_FAILED, STATUS_CANCELLED, STATUS_READY)

SOURCE_UPLOAD = "upload"
SOURCE_YOUTUBE = "youtube"

EXPORT_PPTX = "pptx"
EXPORT_GSLIDES = "gslides"
EXPORT_KINDS = (EXPORT_PPTX, EXPORT_GSLIDES)

EXPORT_WORKING = "working"
EXPORT_DONE = "done"
EXPORT_FAILED = "failed"

# The file id inside a Google Slides link, for decks exported before the id
# was recorded on its own.
SLIDES_ID_PATTERN = re.compile(r"/presentation/d/([A-Za-z0-9_-]+)")

TITLE_MAX = 200
ERROR_MAX = 500


@dataclass
class Job:
    id: int
    user_id: int
    title: str
    source_kind: str
    status: str
    created_at: datetime
    updated_at: datetime
    source_url: str | None = None
    # Display label grouping the jobs from one playlist or one dropped folder.
    batch: str | None = None
    # The video's filename inside the job directory, while it exists.
    source_file: str | None = None
    progress: float = 0.0
    error: str | None = None
    # Bumped every time the job (re)runs. Image URLs carry it, so a retried
    # job's slides are never served from the browser's cache of the last run.
    run: int = 1
    width: int | None = None
    height: int | None = None
    duration_sec: float | None = None
    counts: dict[str, int] = field(default_factory=dict)
    # [{n, file, start_sec, captured_sec, energy, build_step, excluded}]
    slides: list[dict[str, Any]] = field(default_factory=list)
    # {"pptx": {"status", "error", "at"}, "gslides": {"status", "error", "url", "at"}}
    exports: dict[str, dict[str, Any]] = field(default_factory=dict)
    # The job's Google Slides deck: {"file_id", "url"}. Kept apart from
    # `exports`, which re-runs and new exports reset, so converting again
    # replaces this deck instead of making another, and deleting the job can
    # offer to trash it.
    gslides: dict[str, str] | None = None
    finished_at: datetime | None = None

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def can_rerun(self) -> bool:
        """Whether running again can find the video: YouTube is downloaded
        again; an upload only if it was kept (jobs.keep_source)."""
        if self.status not in RETRYABLE_STATUSES:
            return False
        return self.source_kind == SOURCE_YOUTUBE or self.source_file is not None

    @property
    def gslides_file_id(self) -> str | None:
        if self.gslides and self.gslides.get("file_id"):
            return self.gslides["file_id"]
        match = SLIDES_ID_PATTERN.search(self.gslides_url or "")
        return match.group(1) if match else None

    @property
    def gslides_url(self) -> str | None:
        if self.gslides and self.gslides.get("url"):
            return self.gslides["url"]
        state = self.exports.get(EXPORT_GSLIDES) or {}
        return state.get("url") if state.get("status") == EXPORT_DONE else None

    @property
    def included_slides(self) -> list[dict[str, Any]]:
        return [slide for slide in self.slides if not slide.get("excluded")]

    def to_record(self) -> Record:
        record = asdict(self)
        for key in ("created_at", "updated_at", "finished_at"):
            record[key] = to_iso(getattr(self, key))
        return record

    @classmethod
    def from_record(cls, record: Record) -> "Job":
        values = dict(record)
        for key in ("created_at", "updated_at", "finished_at"):
            values[key] = from_iso(values.get(key))
        return cls(**values)

    def to_public(self, with_slides: bool = False) -> dict[str, Any]:
        """What the browser sees. File names and paths stay server-side."""
        payload: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "source_kind": self.source_kind,
            "source_url": self.source_url,
            "batch": self.batch,
            "status": self.status,
            "progress": round(self.progress, 3),
            "error": self.error,
            "run": self.run,
            "width": self.width,
            "height": self.height,
            "duration_sec": self.duration_sec,
            "counts": self.counts,
            "slide_count": len(self.slides),
            "included_count": len(self.included_slides),
            "build_step_count": sum(1 for slide in self.slides if slide.get("build_step")),
            "hidden_build_steps": sum(1 for slide in self.slides if slide.get("build_step") and slide.get("excluded")),
            "can_rerun": self.can_rerun,
            "exports": self.exports,
            "gslides_url": self.gslides_url,
            "created_at": to_iso(self.created_at),
            "finished_at": to_iso(self.finished_at),
        }
        if with_slides:
            payload["slides"] = [
                {
                    "n": slide["n"],
                    "start_sec": slide["start_sec"],
                    "captured_sec": slide["captured_sec"],
                    "excluded": slide.get("excluded", False),
                    "build_step": slide.get("build_step", False),
                }
                for slide in self.slides
            ]
        return payload


def clean_title(value: str) -> str:
    title = " ".join(str(value).split())
    return title[:TITLE_MAX] or "Untitled video"


def reserve_id(store: Store) -> int:
    """A job id for files that must be in place before the job exists --
    an uploaded video is moved into the job's directory first, so the
    dispatcher can never claim a queued job whose video is not there yet."""
    return store.next_sequence(BUCKET_JOBS)


def create_job(store: Store, user_id: int, source_kind: str, title: str, *, source_url: str | None = None,
               batch: str | None = None, source_file: str | None = None, job_id: int | None = None) -> Job:
    now = utc_now()
    job = Job(
        id=job_id if job_id is not None else store.next_sequence(BUCKET_JOBS),
        user_id=user_id,
        title=clean_title(title),
        source_kind=source_kind,
        status=STATUS_QUEUED,
        created_at=now,
        updated_at=now,
        source_url=source_url,
        batch=clean_title(batch) if batch else None,
        source_file=source_file,
    )
    store.put(BUCKET_JOBS, job.id, job.to_record())
    return job


def get_job(store: Store, job_id: int) -> Job | None:
    record = store.get(BUCKET_JOBS, job_id)
    return None if record is None else Job.from_record(record)


def list_jobs(store: Store) -> list[Job]:
    return [Job.from_record(record) for _, record in store.all(BUCKET_JOBS)]


def list_user_jobs(store: Store, user_id: int) -> list[Job]:
    """Newest first."""
    return sorted((job for job in list_jobs(store) if job.user_id == user_id), key=lambda job: job.id, reverse=True)


def delete_job(store: Store, job_id: int) -> None:
    store.delete(BUCKET_JOBS, job_id)


def _update(store: Store, job_id: int, mutate: Callable[[Record], bool]) -> Job | None:
    """Apply mutate under the write lock. mutate returns False to leave the
    record untouched (the job was not in a state that allows the change);
    the result is then None, as for a missing job."""
    applied = [False]

    def wrapped(record: Record) -> None:
        if mutate(record):
            record["updated_at"] = to_iso(utc_now())
            applied[0] = True

    record = store.update_value(BUCKET_JOBS, job_id, wrapped)
    return Job.from_record(record) if record is not None and applied[0] else None


# -- the dispatcher and the worker ---------------------------------------------


def claim_queued(store: Store, limit: int) -> list[Job]:
    """Move up to `limit` of the oldest queued jobs to starting and return
    them. The status check inside the transaction is what makes a claim
    exclusive."""
    claimed: list[Job] = []
    for job in sorted((job for job in list_jobs(store) if job.status == STATUS_QUEUED), key=lambda job: job.id):
        if len(claimed) >= limit:
            break

        def mutate(record: Record) -> bool:
            if record["status"] != STATUS_QUEUED:
                return False
            record["status"] = STATUS_STARTING
            record["progress"] = 0.0
            record["error"] = None
            return True

        updated = _update(store, job.id, mutate)
        if updated is not None:
            claimed.append(updated)
    return claimed


def set_stage(store: Store, job_id: int, status: str, progress: float) -> bool:
    """Progress from the worker. Refused once the job has left the running
    states -- a cancel always wins over a late progress report."""

    def mutate(record: Record) -> bool:
        if record["status"] not in RUNNING_STATUSES:
            return False
        record["status"] = status
        record["progress"] = max(0.0, min(1.0, float(progress)))
        return True

    return _update(store, job_id, mutate) is not None


def set_source(store: Store, job_id: int, source_file: str | None, title: str | None = None) -> None:
    def mutate(record: Record) -> bool:
        record["source_file"] = source_file
        if title:
            record["title"] = clean_title(title)
        return True

    _update(store, job_id, mutate)


def finish_job(store: Store, job_id: int, width: int, height: int, duration_sec: float,
               counts: dict[str, int], slides: list[dict[str, Any]]) -> bool:
    def mutate(record: Record) -> bool:
        if record["status"] not in RUNNING_STATUSES:
            return False
        record.update(
            status=STATUS_READY, progress=1.0, error=None, width=width, height=height,
            duration_sec=round(duration_sec, 2), counts=counts,
            # Build steps start out excluded: the completed slide after them
            # holds everything they show. One click in review brings any back.
            slides=[dict(slide, excluded=bool(slide.get("build_step"))) for slide in slides],
            exports={}, finished_at=to_iso(utc_now()),
        )
        return True

    return _update(store, job_id, mutate) is not None


def fail_job(store: Store, job_id: int, error: str) -> bool:
    def mutate(record: Record) -> bool:
        if record["status"] not in ACTIVE_STATUSES:
            return False
        record["status"] = STATUS_FAILED
        record["error"] = str(error)[:ERROR_MAX] or "failed"
        record["finished_at"] = to_iso(utc_now())
        return True

    return _update(store, job_id, mutate) is not None


def requeue_interrupted(store: Store) -> int:
    """At server start: work that was running when the server stopped has no
    worker any more. Jobs go back to the queue; half-done exports are marked
    failed so the button comes back."""
    count = 0
    for job in list_jobs(store):
        stuck_export = any(state.get("status") == EXPORT_WORKING for state in job.exports.values())
        if job.status not in RUNNING_STATUSES and not stuck_export:
            continue

        def mutate(record: Record) -> bool:
            if record["status"] in RUNNING_STATUSES:
                record["status"] = STATUS_QUEUED
                record["progress"] = 0.0
                record["run"] = record.get("run", 1) + 1
            for state in record.get("exports", {}).values():
                if state.get("status") == EXPORT_WORKING:
                    state["status"] = EXPORT_FAILED
                    state["error"] = "interrupted by a server restart"
            return True

        if _update(store, job.id, mutate) is not None:
            count += 1
    return count


# -- the owner's actions ------------------------------------------------------


def cancel_job(store: Store, job_id: int) -> Job | None:
    def mutate(record: Record) -> bool:
        if record["status"] not in ACTIVE_STATUSES:
            return False
        record["status"] = STATUS_CANCELLED
        record["finished_at"] = to_iso(utc_now())
        return True

    return _update(store, job_id, mutate)


def retry_job(store: Store, job_id: int) -> Job | None:
    def mutate(record: Record) -> bool:
        if record["status"] not in RETRYABLE_STATUSES:
            return False
        record.update(status=STATUS_QUEUED, progress=0.0, error=None, slides=[], counts={}, exports={},
                      finished_at=None, run=record.get("run", 1) + 1)
        return True

    return _update(store, job_id, mutate)


def set_build_steps(store: Store, job_id: int, flags: list[bool], start_secs: list[float]) -> Job | None:
    """Re-mark build steps on a finished job (`manage detect-builds`).
    Exclusions the user made by hand on other slides are kept; slides that
    stop being build steps are included again."""

    def mutate(record: Record) -> bool:
        if record["status"] != STATUS_READY or len(record["slides"]) != len(flags):
            return False
        for slide, flag, start in zip(record["slides"], flags, start_secs):
            was = bool(slide.get("build_step"))
            slide["build_step"] = flag
            slide["start_sec"] = start
            if flag:
                slide["excluded"] = True
            elif was:
                slide["excluded"] = False
        record.setdefault("counts", {})["build_steps"] = sum(flags)
        return True

    return _update(store, job_id, mutate)


def set_slide_excluded(store: Store, job_id: int, n: int, excluded: bool) -> Job | None:
    def mutate(record: Record) -> bool:
        if record["status"] != STATUS_READY:
            return False
        for slide in record["slides"]:
            if slide["n"] == n:
                slide["excluded"] = bool(excluded)
                return True
        return False

    return _update(store, job_id, mutate)


def start_export(store: Store, job_id: int, kinds: list[str]) -> Job | None:
    """Mark exports as working, refusing if any requested one already is --
    a double click must not build the same deck twice at once."""

    def mutate(record: Record) -> bool:
        if record["status"] != STATUS_READY:
            return False
        exports = record.setdefault("exports", {})
        if any(exports.get(kind, {}).get("status") == EXPORT_WORKING for kind in kinds):
            return False
        for kind in kinds:
            exports[kind] = {"status": EXPORT_WORKING, "error": None, "url": None, "at": to_iso(utc_now())}
        return True

    return _update(store, job_id, mutate)


def finish_export(store: Store, job_id: int, kind: str, *, error: str | None = None, url: str | None = None,
                  file_id: str | None = None) -> None:
    def mutate(record: Record) -> bool:
        if kind == EXPORT_GSLIDES and file_id and not error:
            record["gslides"] = {"file_id": file_id, "url": url}
        record.setdefault("exports", {})[kind] = {
            "status": EXPORT_FAILED if error else EXPORT_DONE,
            "error": str(error)[:ERROR_MAX] if error else None,
            "url": url,
            "at": to_iso(utc_now()),
        }
        return True

    _update(store, job_id, mutate)

