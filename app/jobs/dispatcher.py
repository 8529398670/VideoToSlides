"""Starts worker processes for queued jobs, at most jobs.max_concurrent at a
time, and kills them on cancel.

YouTube videos are downloaded ahead by a separate download-only worker
(`worker <id> fetch`), one at a time, up to jobs.downloads_ahead fetched or
waiting, so a processing slot that frees up starts on a video that is
already on disk instead of waiting for the network. Processing slots then
take only videos whose file is there. downloads_ahead 0 turns this off: each
worker downloads its own video, as before.

Runs as one asyncio task inside the server (started in app/main.py's
lifespan). It wakes when poked -- a job was queued or cancelled -- and every
few seconds regardless, so a job queued by another process (a `manage`
command) is still picked up.

Each worker gets its own session (process group), so killing one takes its
ffmpeg and yt-dlp children with it.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys

from starlette.concurrency import run_in_threadpool

from app.core.config import Config
from app.core.db import Store
from app.models import combined, jobs

POLL_SECONDS = 3.0
KILL_GRACE_SECONDS = 5.0
# Downloads at once. YouTube downloads are bound by the connection, so a
# second one at the same time mostly halves the speed of both.
FETCHES_AT_ONCE = 1


class Dispatcher:
    def __init__(self, store: Store, cfg: Config) -> None:
        self.store = store
        self.cfg = cfg
        self.processes: dict[int, asyncio.subprocess.Process] = {}
        self.fetchers: dict[int, asyncio.subprocess.Process] = {}
        self._wake = asyncio.Event()
        self._watchers: set[asyncio.Task] = set()
        self._stopping = False

    def poke(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        requeued = await run_in_threadpool(jobs.requeue_interrupted, self.store)
        if requeued:
            print(f"[jobs] requeued {requeued} job(s) interrupted by the last shutdown", flush=True)
        await run_in_threadpool(combined.fail_interrupted, self.store)
        while True:
            try:
                await self._start_queued()
                await self._start_fetches()
            except Exception as error:  # noqa: BLE001 -- the dispatcher must outlive any one failure
                print(f"[jobs] dispatch failed: {error!r}", flush=True)
            try:
                await asyncio.wait_for(self._wake.wait(), POLL_SECONDS)
            except TimeoutError:
                pass
            self._wake.clear()

    async def _start_queued(self) -> None:
        free = self.cfg.jobs_max_concurrent - len(self.processes)
        if free <= 0:
            return
        ahead = self.cfg.jobs_downloads_ahead > 0
        for job in await run_in_threadpool(jobs.claim_queued, self.store, free, ahead):
            await self._spawn(job, fetching=False)

    async def _start_fetches(self) -> None:
        if self.cfg.jobs_downloads_ahead <= 0:
            return
        free = FETCHES_AT_ONCE - len(self.fetchers)
        if free <= 0:
            return
        room = self.cfg.jobs_downloads_ahead - await run_in_threadpool(jobs.download_backlog, self.store)
        if room <= 0:
            return
        for job in await run_in_threadpool(jobs.claim_for_download, self.store, min(free, room)):
            await self._spawn(job, fetching=True)

    async def _spawn(self, job: jobs.Job, fetching: bool) -> None:
        arguments = [sys.executable, "-m", "app", "worker", str(job.id)] + (["fetch"] if fetching else [])
        process = await asyncio.create_subprocess_exec(*arguments, stdin=subprocess.DEVNULL, start_new_session=True)
        (self.fetchers if fetching else self.processes)[job.id] = process
        what = "downloading ahead" if fetching else "started"
        print(f"[jobs] job {job.id} {what} (pid {process.pid}): {job.title}", flush=True)
        watcher = asyncio.create_task(self._watch(job.id, process, fetching))
        self._watchers.add(watcher)
        watcher.add_done_callback(self._watchers.discard)

    async def _watch(self, job_id: int, process: asyncio.subprocess.Process, fetching: bool) -> None:
        code = await process.wait()
        (self.fetchers if fetching else self.processes).pop(job_id, None)
        if self._stopping:
            # Killed by our own shutdown: leave the job running in the
            # record, so the next start requeues it instead of failing it.
            return
        # A worker that exits without finishing or failing its job died
        # (killed, out of memory, a crash below Python). fail_job only touches
        # a job that is still active, so a finished or cancelled one is left
        # as it is; fail_fetch only one still fetching, since a finished
        # download has put its job back in the queue.
        reason = "the worker stopped unexpectedly" + (f" (exit code {code})" if code else "")
        await run_in_threadpool(jobs.fail_fetch if fetching else jobs.fail_job, self.store, job_id, reason)
        self.poke()

    def _signal(self, process: asyncio.subprocess.Process, sig: int) -> None:
        try:
            os.killpg(process.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    async def cancel(self, job_id: int) -> None:
        """Kill a running job's worker (the record is already cancelled)."""
        process = self.processes.get(job_id) or self.fetchers.get(job_id)
        if process is None or process.returncode is not None:
            self.poke()
            return
        self._signal(process, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), KILL_GRACE_SECONDS)
        except TimeoutError:
            self._signal(process, signal.SIGKILL)

    async def shutdown(self) -> None:
        """Stop every worker. Their jobs are requeued on the next start."""
        self._stopping = True
        everything = list(self.processes.values()) + list(self.fetchers.values())
        for process in everything:
            self._signal(process, signal.SIGTERM)
        for process in everything:
            try:
                await asyncio.wait_for(process.wait(), KILL_GRACE_SECONDS)
            except TimeoutError:
                self._signal(process, signal.SIGKILL)
