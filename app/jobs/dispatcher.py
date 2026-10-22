"""Starts worker processes for queued jobs, at most jobs.max_concurrent at a
time, and kills them on cancel.

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
from app.models import jobs

POLL_SECONDS = 3.0
KILL_GRACE_SECONDS = 5.0


class Dispatcher:
    def __init__(self, store: Store, cfg: Config) -> None:
        self.store = store
        self.cfg = cfg
        self.processes: dict[int, asyncio.subprocess.Process] = {}
        self._wake = asyncio.Event()
        self._watchers: set[asyncio.Task] = set()
        self._stopping = False

    def poke(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        requeued = await run_in_threadpool(jobs.requeue_interrupted, self.store)
        if requeued:
            print(f"[jobs] requeued {requeued} job(s) interrupted by the last shutdown", flush=True)
        while True:
            try:
                await self._start_queued()
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
        for job in await run_in_threadpool(jobs.claim_queued, self.store, free):
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "app", "worker", str(job.id),
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.processes[job.id] = process
            print(f"[jobs] job {job.id} started (pid {process.pid}): {job.title}", flush=True)
            watcher = asyncio.create_task(self._watch(job.id, process))
            self._watchers.add(watcher)
            watcher.add_done_callback(self._watchers.discard)

    async def _watch(self, job_id: int, process: asyncio.subprocess.Process) -> None:
        code = await process.wait()
        self.processes.pop(job_id, None)
        if self._stopping:
            # Killed by our own shutdown: leave the job running in the
            # record, so the next start requeues it instead of failing it.
            return
        # A worker that exits without finishing or failing its job died
        # (killed, out of memory, a crash below Python). fail_job only touches
        # a job that is still active, so a finished or cancelled one is left
        # as it is.
        reason = "the worker stopped unexpectedly" + (f" (exit code {code})" if code else "")
        await run_in_threadpool(jobs.fail_job, self.store, job_id, reason)
        self.poke()

    def _signal(self, process: asyncio.subprocess.Process, sig: int) -> None:
        try:
            os.killpg(process.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    async def cancel(self, job_id: int) -> None:
        """Kill a running job's worker (the record is already cancelled)."""
        process = self.processes.get(job_id)
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
        for process in list(self.processes.values()):
            self._signal(process, signal.SIGTERM)
        for process in list(self.processes.values()):
            try:
                await asyncio.wait_for(process.wait(), KILL_GRACE_SECONDS)
            except TimeoutError:
                self._signal(process, signal.SIGKILL)
