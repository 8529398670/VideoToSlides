"""`python -m app worker <job_id>` -- turn one job's video into slides.
`python -m app worker <job_id> fetch` -- only download its YouTube video,
ahead of its turn (the dispatcher runs these while others are processed).

Started by the dispatcher, one process per job, never by hand (though running
it by hand against a job in the "starting" state works, which is useful when
debugging the pipeline). It opens the same database the server has open --
SQLite's WAL mode allows that -- and reports progress through the job model,
so the rules there (a cancel always wins) hold here too.

A separate process rather than a thread in the server: the scan keeps every
core busy for minutes, a crash in a decoder takes down only this job, and
cancelling is killing a process group rather than asking a thread nicely.
"""
from __future__ import annotations

import sys
import threading
import time
import traceback
from typing import Callable

from app.core import config, db, machine, settings
from app.models import settings as settings_model
from app.jobs import files
from app.models import jobs
from app.pipeline import runner
from app.pipeline.probe import ProbeError
from app.sources import youtube

PROGRESS_WRITE_INTERVAL = 0.5


class WorkerError(Exception):
    """A failure whose message is meant for the person who started the job."""


def throttled_fetch_reporter(store: db.Store, job_id: int) -> Callable[[float], None]:
    last = {"at": 0.0}

    def report(fraction: float) -> None:
        now = time.monotonic()
        if now - last["at"] < PROGRESS_WRITE_INTERVAL and fraction < 1.0:
            return
        last["at"] = now
        jobs.set_fetch_progress(store, job_id, fraction)

    return report


def fetch(cfg: config.Config, store: db.Store, job: jobs.Job) -> None:
    """Download the video into the job's directory and put the job back in
    the queue, ready to process. A cancel meanwhile wins: the download is
    then thrown away (and the process is usually killed before this)."""
    if not job.source_url:
        raise WorkerError("the job has no YouTube link")
    directory = files.job_dir(cfg, job.id)
    directory.mkdir(parents=True, exist_ok=True)
    source, title = youtube.download(job.source_url, directory, cfg.tmp_dir, cfg.youtube_max_height,
                                     throttled_fetch_reporter(store, job.id))
    if jobs.finish_fetch(store, job.id, source.name, title):
        log(job.id, f"downloaded ahead {title!r}")
    else:
        source.unlink(missing_ok=True)


def throttled_reporter(store: db.Store, job_id: int) -> Callable[[str, float], None]:
    """Progress arrives from several threads, many times a second. Write a
    stage change at once and otherwise at most twice a second."""
    lock = threading.Lock()
    last = {"stage": "", "at": 0.0}

    def report(stage: str, fraction: float) -> None:
        now = time.monotonic()
        with lock:
            if stage == last["stage"] and now - last["at"] < PROGRESS_WRITE_INTERVAL and fraction < 1.0:
                return
            last["stage"], last["at"] = stage, now
        jobs.set_stage(store, job_id, stage, fraction)

    return report


def log(job_id: int, message: str) -> None:
    print(f"[job {job_id}] {message}", flush=True)


def process(cfg: config.Config, store: db.Store, job: jobs.Job) -> None:
    directory = files.job_dir(cfg, job.id)
    directory.mkdir(parents=True, exist_ok=True)
    report = throttled_reporter(store, job.id)

    source = files.source_path(cfg, job.id, job.source_file)
    if source is None:
        if job.source_kind != jobs.SOURCE_YOUTUBE or not job.source_url:
            raise WorkerError("the uploaded video is no longer on the server -- upload it again")
        report(jobs.STATUS_DOWNLOADING, 0.0)
        source, title = youtube.download(
            job.source_url, directory, cfg.tmp_dir, cfg.youtube_max_height,
            lambda fraction: report(jobs.STATUS_DOWNLOADING, fraction),
        )
        jobs.set_source(store, job.id, source.name, title)
        log(job.id, f"downloaded {title!r}")

    scan_workers, extract_workers = machine.worker_plan(cfg)
    result = runner.process_video(
        source, directory, scan_workers, extract_workers, report,
        log=lambda message: log(job.id, message),
    )
    finished = jobs.finish_job(
        store, job.id, result.width, result.height, result.duration_sec, result.counts,
        [
            {"n": s.n, "file": s.file, "start_sec": s.start_sec, "captured_sec": s.captured_sec,
             "energy": s.energy, "build_step": s.build_step}
            for s in result.slides
        ],
    )
    if not finished:
        log(job.id, "cancelled before it finished; result discarded")
        return
    log(job.id, f"done: {len(result.slides)} slides")
    if not cfg.jobs_keep_source:
        files.delete_source(cfg, job.id, source.name)
        jobs.set_source(store, job.id, None)


def run(arguments: list[str]) -> None:
    if not 1 <= len(arguments) <= 2 or not arguments[0].isdigit() or arguments[1:] not in ([], ["fetch"]):
        raise SystemExit("usage: python -m app worker <job_id> [fetch]")
    job_id = int(arguments[0])
    fetching = arguments[1:] == ["fetch"]
    cfg = config.load()
    store = db.open_store(cfg.database_path, cfg.secret_key, cfg.encrypt_at_rest)
    # What an admin changed in Settings, as of this job's start.
    overrides = settings_model.get_overrides(store)
    settings.apply_tunables(overrides)
    cfg = settings.apply_config(cfg, overrides)

    job = jobs.get_job(store, job_id)
    if fetching:
        if job is None or job.status != jobs.STATUS_FETCHING:
            return
        if cfg.jobs_low_priority:
            machine.lower_priority()
        try:
            fetch(cfg, store, job)
        except (WorkerError, youtube.YouTubeError) as error:
            log(job_id, f"download failed: {error}")
            jobs.fail_fetch(store, job_id, str(error))
        except Exception as error:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            jobs.fail_fetch(store, job_id, f"download failed ({type(error).__name__}) -- see the server log")
        return
    if job is None or job.status != jobs.STATUS_STARTING:
        # Claimed by the dispatcher, then cancelled before this process got
        # here -- nothing to do.
        return

    if cfg.jobs_low_priority:
        machine.lower_priority()
    try:
        process(cfg, store, job)
    except (WorkerError, ProbeError, youtube.YouTubeError) as error:
        log(job_id, f"failed: {error}")
        jobs.fail_job(store, job_id, str(error))
    except Exception as error:  # noqa: BLE001 -- anything else is a bug; record it and keep the detail in the log
        traceback.print_exc(file=sys.stderr)
        jobs.fail_job(store, job_id, f"processing failed ({type(error).__name__}) -- see the server log")
