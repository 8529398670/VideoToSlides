"""Downloading YouTube videos ahead of processing (jobs model side).

Run from the project root:  ./.venv/bin/python -m unittest discover tests
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.core import db
from app.models import jobs


class DownloadAheadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = db.open_store(Path(self.tmp.name) / "app.db", "0" * 64, False)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def youtube(self, title: str) -> jobs.Job:
        return jobs.create_job(self.store, 1, jobs.SOURCE_YOUTUBE, title, source_url="https://youtu.be/x")

    def test_processing_waits_for_downloads_and_downloads_go_in_order(self) -> None:
        first, second = self.youtube("one"), self.youtube("two")
        upload = jobs.create_job(self.store, 1, jobs.SOURCE_UPLOAD, "file", source_file="source.mp4")
        # Only the upload can be processed straight away.
        self.assertEqual([job.id for job in jobs.claim_queued(self.store, 5, downloaded_only=True)], [upload.id])
        fetching = jobs.claim_for_download(self.store, 1)
        self.assertEqual([job.id for job in fetching], [first.id])
        self.assertEqual(jobs.download_backlog(self.store), 1)
        self.assertTrue(jobs.set_fetch_progress(self.store, first.id, 0.5))
        self.assertTrue(jobs.finish_fetch(self.store, first.id, "source.webm", "One (real title)"))
        done = jobs.get_job(self.store, first.id)
        self.assertEqual((done.status, done.downloaded, done.title), (jobs.STATUS_QUEUED, True, "One (real title)"))
        self.assertEqual([job.id for job in jobs.claim_for_download(self.store, 1)], [second.id])
        self.assertEqual(jobs.download_backlog(self.store), 2)
        self.assertEqual([job.id for job in jobs.claim_queued(self.store, 5, downloaded_only=True)], [first.id])
        self.assertEqual(jobs.download_backlog(self.store), 1)

    def test_without_downloading_ahead_workers_take_any_job(self) -> None:
        job = self.youtube("one")
        self.assertEqual([found.id for found in jobs.claim_queued(self.store, 1)], [job.id])

    def test_cancel_and_restart_and_dead_fetchers(self) -> None:
        job = self.youtube("one")
        jobs.claim_for_download(self.store, 1)
        self.assertIsNotNone(jobs.cancel_job(self.store, job.id))
        self.assertFalse(jobs.finish_fetch(self.store, job.id, "source.webm", None))
        self.assertIsNone(jobs.get_job(self.store, job.id).source_file)

        other = self.youtube("two")
        jobs.claim_for_download(self.store, 1)
        self.assertEqual(jobs.requeue_interrupted(self.store), 1)
        self.assertEqual(jobs.get_job(self.store, other.id).status, jobs.STATUS_QUEUED)

        jobs.claim_for_download(self.store, 1)
        jobs.finish_fetch(self.store, other.id, "source.webm", None)
        # The fetch worker then exits; that must not fail the downloaded job.
        self.assertFalse(jobs.fail_fetch(self.store, other.id, "worker stopped"))
        self.assertEqual(jobs.get_job(self.store, other.id).status, jobs.STATUS_QUEUED)


if __name__ == "__main__":
    unittest.main()
