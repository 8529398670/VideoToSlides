"""The job's Google Slides deck: remembered across re-runs, replaced in place
on a new Convert, and trashed on request when the job is deleted.

Run from the project root:  ./.venv/bin/python -m unittest discover tests
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.core import db
from app.google import oauth
from app.jobs import exports
from app.models import jobs


def ready_job(store: db.Store) -> jobs.Job:
    job = jobs.create_job(store, 1, jobs.SOURCE_YOUTUBE, "Lecture")
    jobs.claim_queued(store, 1)
    slides = [{"n": 1, "file": "1.jpg", "start_sec": 0.0, "captured_sec": 0.0}]
    jobs.finish_job(store, job.id, 1280, 720, 60.0, {}, slides)
    return jobs.get_job(store, job.id)


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = db.open_store(Path(self.tmp.name) / "app.db", "0" * 64, False)

    def tearDown(self) -> None:
        self.tmp.cleanup()


class DeckRecordTests(StoreTest):
    def test_deck_survives_new_exports_and_reruns(self) -> None:
        job = ready_job(self.store)
        jobs.start_export(self.store, job.id, [jobs.EXPORT_GSLIDES])
        jobs.finish_export(self.store, job.id, jobs.EXPORT_GSLIDES, url="https://deck/1", file_id="abc")
        self.assertEqual(jobs.get_job(self.store, job.id).gslides_file_id, "abc")

        jobs.start_export(self.store, job.id, [jobs.EXPORT_GSLIDES])
        self.assertEqual(jobs.get_job(self.store, job.id).gslides_url, "https://deck/1")

        jobs.finish_export(self.store, job.id, jobs.EXPORT_GSLIDES, error="boom")
        jobs.retry_job(self.store, job.id)
        jobs.claim_queued(self.store, 1)
        jobs.finish_job(self.store, job.id, 1280, 720, 60.0, {}, [])
        again = jobs.get_job(self.store, job.id)
        self.assertEqual(again.gslides_file_id, "abc")
        self.assertEqual(again.to_public()["gslides_url"], "https://deck/1")
        self.assertNotIn("abc", str(again.to_public()))

    def test_older_exports_fall_back_to_the_link(self) -> None:
        job = ready_job(self.store)
        jobs.finish_export(self.store, job.id, jobs.EXPORT_GSLIDES,
                           url="https://docs.google.com/presentation/d/1Xy_z-9/edit?usp=drivesdk")
        job = jobs.get_job(self.store, job.id)
        self.assertEqual(job.gslides_file_id, "1Xy_z-9")
        self.assertIsNone(ready_job(self.store).gslides_file_id)


class UploadTests(StoreTest):
    def setUp(self) -> None:
        super().setUp()
        self.cfg = SimpleNamespace(google_enabled=True)
        job = ready_job(self.store)
        jobs.finish_export(self.store, job.id, jobs.EXPORT_GSLIDES, url="https://deck/old", file_id="old")
        self.job = jobs.get_job(self.store, job.id)
        self.deck = Path(self.tmp.name) / "deck.pptx"
        self.deck.write_bytes(b"pptx")
        patches = [
            mock.patch.object(exports, "google_token", return_value="token"),
            mock.patch.object(exports.drive, "replace_slides", return_value=("old", "https://deck/old")),
            mock.patch.object(exports.drive, "upload_pptx_as_slides", return_value=("new", "https://deck/new")),
            mock.patch.object(exports.drive, "trash_file"),
        ]
        self.token, self.replace, self.upload, self.trash = [patch.start() for patch in patches]
        for patch in patches:
            self.addCleanup(patch.stop)

    def upload_to_google(self, **kwargs):
        return exports.upload_to_google(self.store, self.cfg, self.job, self.deck, **kwargs)

    def test_replaces_in_place(self) -> None:
        self.assertEqual(self.upload_to_google(), ("old", "https://deck/old"))
        self.upload.assert_not_called()
        self.trash.assert_not_called()

    def test_gone_deck_gets_a_new_one(self) -> None:
        self.replace.return_value = None
        self.assertEqual(self.upload_to_google(), ("new", "https://deck/new"))
        self.trash.assert_called_once_with("token", "old")

    def test_refused_replace_makes_a_new_one_and_trashes_the_old(self) -> None:
        self.replace.side_effect = oauth.GoogleError("no")
        self.assertEqual(self.upload_to_google(), ("new", "https://deck/new"))
        self.trash.assert_called_once_with("token", "old")

    def test_copy_leaves_the_old_deck_alone(self) -> None:
        self.assertEqual(self.upload_to_google(replace=False), ("new", "https://deck/new"))
        self.replace.assert_not_called()
        self.trash.assert_not_called()

    def test_delete_google_deck_trashes_it(self) -> None:
        exports.delete_google_deck(self.store, self.cfg, self.job)
        self.trash.assert_called_once_with("token", "old")


if __name__ == "__main__":
    unittest.main()
